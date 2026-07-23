"""The economics engine — where "looks profitable" becomes "is profitable".

Every candidate opportunity is priced with a full cost waterfall: acquisition,
proxy fees, every shipping leg, Thai import VAT + duty, marketplace commissions,
payment and FX costs — then stress-tested with a pessimistic scenario (price
slippage, fee drift, destination duties). Nothing is published unless the
pessimistic case still clears.

NOTE ON DATA: fee percentages, shipping rates, duty rates and FX here are
approximate reference values baked in for demo mode, current as of early 2026.
A production deployment replaces them with live feeds (marketplace fee APIs,
carrier rate cards, official tariff schedules, an FX provider). The structure —
not the constants — is the product.
"""

from __future__ import annotations

from .models import CostLine, Economics, Scenario
from . import thailand

# ---------------------------------------------------------------------------
# Venues
# ---------------------------------------------------------------------------

VENUES: dict[str, dict] = {
    "yahoo_auctions_jp": {"name": "Yahoo! Auctions JP", "country": "JP", "currency": "JPY", "buy": True, "sell": False},
    "mercari_jp":        {"name": "Mercari JP",         "country": "JP", "currency": "JPY", "buy": True, "sell": False},
    "amazon_us":         {"name": "Amazon US",          "country": "US", "currency": "USD", "buy": True, "sell": True},
    "ebay_us":           {"name": "eBay US",            "country": "US", "currency": "USD", "buy": True, "sell": True},
    "etsy":              {"name": "Etsy",               "country": "US", "currency": "USD", "buy": True, "sell": True},
    "shopee_th":         {"name": "Shopee TH",          "country": "TH", "currency": "THB", "buy": True, "sell": True},
    "lazada_th":         {"name": "Lazada TH",          "country": "TH", "currency": "THB", "buy": True, "sell": True},
    "facebook_mp_th":    {"name": "Facebook Marketplace TH", "country": "TH", "currency": "THB", "buy": True, "sell": True},
    "kaidee_th":         {"name": "Kaidee TH",          "country": "TH", "currency": "THB", "buy": True, "sell": True},
    "tiktok_shop_th":    {"name": "TikTok Shop TH",     "country": "TH", "currency": "THB", "buy": True, "sell": True},
    "aliexpress":        {"name": "AliExpress",         "country": "CN", "currency": "USD", "buy": True, "sell": False},
}

# Sell-side fee model per venue (fractions of sale price unless noted).
SELL_FEES: dict[str, dict] = {
    "ebay_us":        {"pct": 0.1325, "fixed": 0.30, "payment_pct": 0.0, "cross_border_pct": 0.0165, "payout_fx_pct": 0.02,
                       "note": "Managed payments; intl fee applies to TH-based sellers; payout via Payoneer/Wise."},
    "amazon_us":      {"pct": 0.15, "fixed": 0.0, "payment_pct": 0.0, "fba_fixed": 4.50, "payout_fx_pct": 0.02,
                       "note": "Referral fee + FBA fulfillment (standard size)."},
    "etsy":           {"pct": 0.065, "fixed": 0.20, "payment_pct": 0.03, "payment_fixed": 0.25, "payout_fx_pct": 0.02,
                       "note": "Transaction + listing + Etsy Payments."},
    "shopee_th":      {"pct": 0.08, "fixed": 0.0, "payment_pct": 0.0, "payout_fx_pct": 0.0,
                       "note": "Marketplace + transaction programme fees (TH)."},
    "lazada_th":      {"pct": 0.06, "fixed": 0.0, "payment_pct": 0.02, "payout_fx_pct": 0.0,
                       "note": "Commission + payment fee (TH)."},
    "facebook_mp_th": {"pct": 0.0, "fixed": 0.0, "payment_pct": 0.0, "payout_fx_pct": 0.0,
                       "note": "No platform fee; PromptPay/cash on meetup."},
    "kaidee_th":      {"pct": 0.0, "fixed": 0.0, "payment_pct": 0.0, "payout_fx_pct": 0.0,
                       "note": "Classifieds; no sale commission."},
    "mercari_jp":     {"pct": 0.10, "fixed": 0.0, "payment_pct": 0.0, "payout_fx_pct": 0.02, "note": "JP residents only."},
    "yahoo_auctions_jp": {"pct": 0.10, "fixed": 0.0, "payment_pct": 0.0, "payout_fx_pct": 0.02, "note": "JP residents only."},
    "tiktok_shop_th": {"pct": 0.08, "fixed": 0.0, "payment_pct": 0.03, "payout_fx_pct": 0.0,
                       "note": "Commission + transaction fee (TH); COD handled by platform."},
    "aliexpress":     {"pct": 0.0, "fixed": 0.0, "payment_pct": 0.0, "payout_fx_pct": 0.0,
                       "note": "Buy-side only from Thailand."},
}

# Buy-side extras per venue (per unit, USD).
BUY_COSTS: dict[str, dict] = {
    "yahoo_auctions_jp": {"proxy_fixed": 2.20, "proxy_pct": 0.02, "origin_ship": 5.50,
                          "note": "Via proxy (Buyee/ZenMarket): service fee + JP domestic shipping."},
    "mercari_jp":        {"proxy_fixed": 2.20, "proxy_pct": 0.02, "origin_ship": 4.80,
                          "note": "Via proxy buyer; JP domestic shipping to warehouse."},
    "amazon_us":         {"origin_ship": 0.0, "note": "Free domestic shipping (Prime assumed for US prep)."},
    "ebay_us":           {"origin_ship": 4.20, "note": "US domestic shipping to prep/forwarder."},
    "etsy":              {"origin_ship": 4.50, "note": "US domestic shipping."},
    "shopee_th":         {"origin_ship": 1.20, "note": "TH domestic courier (Flash/Kerry)."},
    "lazada_th":         {"origin_ship": 1.20, "note": "TH domestic courier."},
    "facebook_mp_th":    {"origin_ship": 0.0, "note": "Local pickup (BTS meetup) or COD."},
    "kaidee_th":         {"origin_ship": 1.50, "note": "TH domestic shipping."},
    "tiktok_shop_th":    {"origin_ship": 1.20, "note": "TH domestic courier."},
    "aliexpress":        {"origin_ship": 0.0, "note": "Subsidised/free CN shipping is common; "
                                                      "allow 10–20 days lead time to Thailand."},
}

# International/domestic shipping rate card: (base USD, USD per kg).
SHIP_RATES: dict[tuple[str, str], tuple[float, float]] = {
    ("JP", "TH"): (8.5, 9.0),
    ("JP", "US"): (13.5, 14.0),
    ("TH", "US"): (10.5, 11.0),   # Thailand Post registered air / ePacket class
    ("TH", "JP"): (10.0, 10.0),
    ("US", "TH"): (14.0, 12.0),
    ("US", "US"): (4.5, 1.2),
    ("TH", "TH"): (1.3, 0.5),
    ("JP", "JP"): (5.0, 1.0),
    ("CN", "TH"): (3.5, 4.5),     # AliExpress standard / consolidated freight class
    ("CN", "US"): (6.0, 7.5),
}

# Direct-forward (proxy warehouse ships internationally on your behalf):
FORWARD_FEE_FIXED = 3.00
FORWARD_SHIP_MULT = 1.15

PACKAGING_USD = 0.90

# US import duties (destination-side). The US suspended the $800 de-minimis
# exemption for commercial shipments in Aug 2025, so cross-border sales into
# the US now carry buyer-side duties — we price them into the pessimistic
# scenario as a demand/price concession. Approximate ad-valorem by origin.
US_DEST_DUTY_BY_ORIGIN = {"TH": 0.19, "JP": 0.15, "CN": 0.30}
US_DEST_DUTY_DEFAULT = 0.10

# FX (USD pivot). Demo mode uses these reference values; live mode overwrites
# them every cycle from a real FX feed via set_fx().
FX_TO_USD = {"USD": 1.0, "THB": 1 / 36.4, "JPY": 1 / 147.9}
USD_THB = 36.4


def set_fx(usd_thb: float, usd_jpy: float | None = None) -> None:
    """Install live FX rates (called by the live data source each cycle)."""

    global USD_THB
    if usd_thb and usd_thb > 0:
        USD_THB = round(usd_thb, 4)
        FX_TO_USD["THB"] = 1 / USD_THB
    if usd_jpy and usd_jpy > 0:
        FX_TO_USD["JPY"] = 1 / usd_jpy


def convert(amount: float, frm: str, to: str) -> float:
    return amount * FX_TO_USD[frm] / FX_TO_USD[to]


def usd_to_thb(amount: float) -> float:
    return round(amount * USD_THB, 2)


def ship_cost(frm: str, to: str, weight_kg: float) -> float:
    base, per_kg = SHIP_RATES[(frm, to)]
    return round(base + per_kg * weight_kg, 2)


# ---------------------------------------------------------------------------
# Product flips (arbitrage)
# ---------------------------------------------------------------------------

def candidate_paths(buy_venue: str, sell_venue: str, home: str = "TH") -> list[list[str]]:
    """Country paths the goods can take. From Thailand there are usually two
    options for third-country flips: route through home (inspect everything,
    pay Thai import VAT/duty) or direct-forward via the proxy warehouse."""

    b = VENUES[buy_venue]["country"]
    s = VENUES[sell_venue]["country"]
    if b == s:
        return [[b]] if b == home else [[b]]          # domestic flow (may be remote-operated)
    if b == home:
        return [[b, s]]
    if s == home:
        return [[b, s]]
    paths = [[b, home, s]]
    if BUY_COSTS.get(buy_venue, {}).get("proxy_fixed") is not None:
        paths.append([b, s])                           # proxy direct-forward
    return paths


def _flip_scenario(name: str, *, item: dict, buy_venue: str, sell_venue: str,
                   buy_usd: float, sell_usd: float, path: list[str], qty: int,
                   pessimistic: bool, extra_cost_usd: float = 0.0, extra_note: str = "") -> Scenario:
    """Per-unit waterfall at a given lot size. Fixed logistics costs (parcel
    base rates, origin shipping, forwarding fees) are amortised across the
    consolidated lot — the difference between a hobbyist mailing one card and
    an operator shipping twenty in one box.

    `extra_cost_usd` is an optional per-unit cost added to the waterfall — used
    by the refurbishment thesis for parts + labour + a scrap-rate reserve. In
    the pessimistic case it is inflated (repairs run over, some units are
    unfixable), so a thin refurb edge dies in the gate like any other."""

    weight = item.get("weight_kg", 0.5)
    category = item.get("category", "collectibles")
    qty = max(1, qty)
    fees = SELL_FEES[sell_venue]
    buyc = BUY_COSTS.get(buy_venue, {})
    ship_mult = 1.15 if pessimistic else 1.0
    sale = round(sell_usd * (0.95 if pessimistic else 1.0), 2)
    commission_pct = fees["pct"] + (0.015 if pessimistic else 0.0)
    lot = f" (lot of {qty})" if qty > 1 else ""

    lines: list[CostLine] = [CostLine(f"Acquisition — {VENUES[buy_venue]['name']}", round(buy_usd, 2))]
    if buyc.get("proxy_fixed"):
        lines.append(CostLine("Proxy service fee", round(buyc["proxy_fixed"] + buyc.get("proxy_pct", 0) * buy_usd, 2),
                              buyc.get("note", "")))
    if buyc.get("origin_ship"):
        lines.append(CostLine("Origin domestic shipping", round(buyc["origin_ship"] / qty, 2),
                              f"one consolidated pickup{lot}" if qty > 1 else ""))

    landed = buy_usd + sum(l.amount_usd for l in lines[1:])
    direct_forward = len(path) == 2 and path[0] != "TH" and path[1] != "TH" if len(path) >= 2 else False

    for leg_from, leg_to in zip(path, path[1:]):
        base_rate, per_kg = SHIP_RATES[(leg_from, leg_to)]
        cost = (base_rate / qty + per_kg * weight) * ship_mult
        label = f"Shipping {leg_from} → {leg_to}"
        if direct_forward:
            cost = cost * FORWARD_SHIP_MULT + FORWARD_FEE_FIXED / qty
            label += " (proxy forward)"
        if qty > 1:
            label += lot
        lines.append(CostLine(label, round(cost, 2)))
        landed += cost
        if leg_to == "TH" and leg_from != "TH":
            duty, vat, notes = thailand.import_charges(cif_usd=landed, category=category,
                                                       usd_thb=USD_THB, parcel_cif_usd=landed * qty)
            if duty:
                lines.append(CostLine("Thai import duty", duty, notes["duty"]))
            lines.append(CostLine("Thai import VAT 7%", vat, notes["vat"]))
            landed += duty + vat

    # Last-mile delivery to the buyer. Cross-border final legs (e.g. TH → US)
    # already ARE the delivery; but domestic flips (Facebook → Shopee) and
    # import-then-sell-locally routes (AliExpress → CN → TH → Thai buyer) still
    # need the local courier hop.
    sell_country = VENUES[sell_venue]["country"]
    if path[-1] == sell_country and (len(path) == 1 or sell_country == "TH"):
        lines.append(CostLine("Domestic delivery to buyer",
                              round(ship_cost(sell_country, sell_country, weight) * ship_mult, 2),
                              "Kerry/Flash/J&T class" if sell_country == "TH" else ""))
        landed += lines[-1].amount_usd

    if "TH" in path[1:] or path == ["TH"] or path[0] == "TH":
        lines.append(CostLine("Packaging & handling", PACKAGING_USD))

    commission = round(sale * commission_pct + fees.get("fixed", 0.0), 2)
    lines.append(CostLine(f"{VENUES[sell_venue]['name']} fees ({commission_pct * 100:.1f}%)", commission, fees.get("note", "")))
    if fees.get("payment_pct") or fees.get("payment_fixed"):
        lines.append(CostLine("Payment processing", round(sale * fees.get("payment_pct", 0.0) + fees.get("payment_fixed", 0.0), 2)))
    if fees.get("cross_border_pct"):
        lines.append(CostLine("Cross-border seller fee", round(sale * fees["cross_border_pct"], 2)))
    if fees.get("fba_fixed"):
        lines.append(CostLine("FBA fulfillment", fees["fba_fixed"]))
    if fees.get("payout_fx_pct"):
        lines.append(CostLine("FX / payout spread", round(sale * fees["payout_fx_pct"], 2), "Payoneer/Wise USD→THB payout"))

    if extra_cost_usd:
        cost = extra_cost_usd * (1.35 if pessimistic else 1.0)
        lines.append(CostLine("Refurbishment — parts + labour" + (" + scrap reserve" if pessimistic else ""),
                              round(cost, 2), extra_note or "estimated repair cost per unit"))

    if pessimistic:
        dest = VENUES[sell_venue]["country"]
        origin = path[-2] if len(path) >= 2 else path[0]
        if dest == "US" and origin != "US":
            duty_pct = US_DEST_DUTY_BY_ORIGIN.get(origin, US_DEST_DUTY_DEFAULT)
            lines.append(CostLine(f"US duty concession (½ of {duty_pct * 100:.0f}%)",
                                  round(sale * duty_pct * 0.5, 2),
                                  "US de-minimis suspended Aug 2025; buyer pays duty at the door — "
                                  "assume half is conceded back through pricing"))
        lines.append(CostLine("Returns / mishap reserve (3%)", round(sale * 0.03, 2)))

    return Scenario(name=name, revenue_usd=sale, lines=lines).finalize()


def compute_flip(item: dict, buy_venue: str, sell_venue: str,
                 buy_usd: float, sell_usd: float, qty: int = 1,
                 extra_cost_usd: float = 0.0, extra_note: str = "") -> Economics:
    """Price a product flip along the best available route. `extra_cost_usd`
    adds a per-unit cost (e.g. refurbishment) to the waterfall."""

    best: tuple[Scenario, Scenario, list[str]] | None = None
    notes = []
    for path in candidate_paths(buy_venue, sell_venue):
        base = _flip_scenario("base", item=item, buy_venue=buy_venue, sell_venue=sell_venue,
                              buy_usd=buy_usd, sell_usd=sell_usd, path=path, qty=qty, pessimistic=False,
                              extra_cost_usd=extra_cost_usd, extra_note=extra_note)
        pess = _flip_scenario("pessimistic", item=item, buy_venue=buy_venue, sell_venue=sell_venue,
                              buy_usd=buy_usd, sell_usd=sell_usd, path=path, qty=qty, pessimistic=True,
                              extra_cost_usd=extra_cost_usd, extra_note=extra_note)
        notes.append(f"{' → '.join(path)}: net ${base.net_usd}/unit")
        if best is None or base.net_usd > best[0].net_usd:
            best = (base, pess, path)

    assert best is not None
    base, pess, path = best
    unit_cost = base.total_cost_usd
    qty = max(1, qty)
    econ = Economics(
        kind="flip",
        base=base,
        pessimistic=pess,
        qty=qty,
        capital_usd=round(unit_cost * qty, 2),
        total_net_usd=round(base.net_usd * qty, 2),
        breakeven_revenue_usd=round(unit_cost / (1 - (SELL_FEES[sell_venue]["pct"] + SELL_FEES[sell_venue].get("payment_pct", 0)
                                                      + SELL_FEES[sell_venue].get("cross_border_pct", 0)
                                                      + SELL_FEES[sell_venue].get("payout_fx_pct", 0))), 2),
        fx={"USD_THB": USD_THB},
        route_note=f"Best route: {' → '.join(path)}.  Alternatives — " + "; ".join(notes),
    )
    econ.thb = {
        "net_per_unit_thb": usd_to_thb(base.net_usd),
        "total_net_thb": usd_to_thb(econ.total_net_usd),
        "capital_thb": usd_to_thb(econ.capital_usd),
    }
    # Flip inputs: prices come from real listings (observed, ASKING not sold-comp),
    # fees/shipping are calculated, the projected net is a calculation on top.
    econ.input_provenance = {
        "buy_price": "observed", "sell_price": "observed",
        "fees_shipping_tax": "calculated", "projected_net": "calculated",
    }
    return econ


# ---------------------------------------------------------------------------
# Ventures (digital products, local services, B2B, info products)
# ---------------------------------------------------------------------------

VENTURE_PARAMS = {
    "digital":  {"capture": 0.06, "conv": 0.035, "monthly_cost": 35.0, "startup": 120.0,
                 "fee_pct": 0.034, "fee_fixed": 0.30, "cost_note": "hosting, domain, LLM API"},
    "info":     {"capture": 0.05, "conv": 0.02,  "monthly_cost": 15.0, "startup": 60.0,
                 "fee_pct": 0.10,  "fee_fixed": 0.30, "cost_note": "Gumroad/marketplace + tools"},
    "local":    {"capture": 0.30, "conv": 0.25,  "monthly_cost": 90.0, "startup": 450.0,
                 "fee_pct": 0.0,   "fee_fixed": 0.0,  "cost_note": "supplies, fuel, LINE ads"},
    "b2b":      {"capture": 0.20, "conv": 0.15,  "monthly_cost": 120.0, "startup": 600.0,
                 "fee_pct": 0.0,   "fee_fixed": 0.0,  "cost_note": "tooling, certification, outreach"},
    # Lead generation: a share of local searchers become a lead you can sell to
    # a provider. `price_point_usd` on the niche is the PER-LEAD price (derived
    # in the generator from the underlying service value). Running cost is ad
    # spend to capture the search intent; there's no fulfilment cost.
    "leadgen":  {"capture": 0.05, "conv": 1.0,   "monthly_cost": 60.0, "startup": 200.0,
                 "fee_pct": 0.03,  "fee_fixed": 0.0,  "cost_note": "Google/Facebook ad spend + landing page"},
}


def compute_venture(niche: dict) -> Economics:
    """Monthly unit economics for build-it opportunities (per month, USD)."""

    kind = niche["kind"]
    p = VENTURE_PARAMS[kind]
    volume = niche["metrics"]["volume"]
    price = niche["price_point_usd"]

    def scenario(name: str, mult: float, price_mult: float) -> Scenario:
        customers = max(1.0, volume * p["capture"] * p["conv"] * mult)
        eff_price = price * price_mult
        revenue = round(customers * eff_price, 2)
        lines = [
            CostLine(f"Platform/payment fees", round(revenue * p["fee_pct"] + customers * p["fee_fixed"], 2)),
            CostLine("Running costs / month", p["monthly_cost"], p["cost_note"]),
            CostLine("Customer acquisition tests", round(revenue * 0.08, 2), "small paid tests + content"),
        ]
        return Scenario(name=name, revenue_usd=revenue, lines=lines).finalize()

    base = scenario("base", 1.0, 1.0)
    pess = scenario("pessimistic", 0.45, 0.9)
    econ = Economics(
        kind="venture",
        base=base,
        pessimistic=pess,
        qty=1,
        capital_usd=p["startup"],
        total_net_usd=base.net_usd,
        breakeven_revenue_usd=round(p["monthly_cost"] / max(0.01, 1 - p["fee_pct"] - 0.08), 2),
        fx={"USD_THB": USD_THB},
        route_note=f"Model estimate from {volume:,} monthly demand events × capture × conversion. "
                   f"Payback ≈ {max(0.1, p['startup'] / max(1.0, base.net_usd)):.1f} months.",
    )
    econ.thb = {
        "net_per_month_thb": usd_to_thb(base.net_usd),
        "capital_thb": usd_to_thb(econ.capital_usd),
    }
    # Venture inputs: demand volume + price carry the niche's measured provenance
    # (user_supplied / estimated / unknown); conversion rates are model estimates;
    # cost lines and the projected net are calculations. Nothing here is a proven
    # earning — the projected_net is only as strong as its weakest input.
    prov = (niche.get("metrics") or {}).get("observed") or {}
    econ.input_provenance = {
        "demand_volume": prov.get("volume", "unknown"),
        "price_point": prov.get("price", "estimated"),
        "conversion_rate": "estimated",
        "costs": "calculated",
        "projected_net": "calculated",
    }
    return econ
