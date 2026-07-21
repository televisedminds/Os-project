"""Thailand operator profile.

The operator runs from Thailand, so every opportunity is screened against a
Thailand lens: which platforms can you actually buy/sell on from TH, which
need a proxy, what import VAT/duty applies when goods route through Thailand,
what paperwork export needs, and how the money comes home.

Rates are reference values for demo mode (early 2026); production syncs the
official Thai Customs tariff schedule and platform policy pages.
"""

from __future__ import annotations

from .models import Feasibility

VAT_RATE = 0.07
DE_MINIMIS_THB = 1500.0   # duty-free threshold; VAT is collected even below it (rule change July 2024)

IMPORT_DUTY_BY_CATEGORY = {
    "trading_cards": 0.10,
    "toys": 0.10,
    "lego": 0.10,
    "sneakers": 0.30,
    "apparel": 0.30,
    "luxury_bags": 0.20,
    "electronics": 0.05,
    "watches": 0.05,
    "cameras": 0.05,
    "audio": 0.10,
    "books": 0.0,
    "stationery": 0.10,
    "collectibles": 0.10,
    "gaming": 0.10,
    "food": 0.30,
    "handmade": 0.10,
}
DEFAULT_DUTY = 0.10

EXPORT_NOTES_BY_CATEGORY = {
    "food": "US-bound food shipments need FDA Prior Notice; use commercial invoice + ingredient list.",
    "electronics": "Devices with lithium batteries: ship via carriers accepting PI966/PI967.",
    "audio": "Lithium battery rules apply (PI966/PI967); declare accurately.",
    "luxury_bags": "High-value: insure the shipment and photograph serial/date codes before dispatch.",
    "watches": "High-value: registered/EMS with insurance; keep authentication photos.",
}
GENERAL_EXPORT_NOTE = "Attach CN22/CN23 customs declaration; Thailand Post ePacket/EMS or DHL for speed."

PAYMENT_RAILS = [
    "PromptPay (instant THB, local sales)",
    "Payoneer (receive USD from eBay/Amazon/Etsy, withdraw to THB)",
    "Wise multi-currency (hold USD/JPY, good FX rates)",
    "Thai bank transfer (Kasikorn/SCB) for local B2B invoicing",
]

# Which platforms a Thailand-based operator can realistically use.
VENUE_ACCESS: dict[str, dict] = {
    "yahoo_auctions_jp": {"buy": True, "sell": False, "proxy": True,
                          "buy_note": "Buy via proxy (Buyee/ZenMarket): they bid, receive, consolidate, forward.",
                          "sell_note": "Selling requires JP residency/bank — not available from TH."},
    "mercari_jp":        {"buy": True, "sell": False, "proxy": True,
                          "buy_note": "Buy via proxy buyer; consolidate at proxy warehouse.",
                          "sell_note": "JP phone + bank required — not available from TH."},
    "amazon_us":         {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Source via US prep centre/forwarder; some listings ship to TH directly.",
                          "sell_note": "Amazon Global Selling accepts TH-based sellers; FBA inbound from TH works."},
    "ebay_us":           {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Ships to TH or to a US forwarder.",
                          "sell_note": "TH sellers accepted; payouts via Payoneer. Ship TH→buyer directly."},
    "etsy":              {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Most shops ship internationally.",
                          "sell_note": "TH shops supported; payouts via Payoneer."},
    "shopee_th":         {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Domestic COD/PromptPay.", "sell_note": "Local seller onboarding with Thai ID."},
    "lazada_th":         {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Domestic.", "sell_note": "Local seller centre with Thai ID/bank."},
    "facebook_mp_th":    {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Meetup (BTS/MRT) or COD via Kerry/Flash.", "sell_note": "Zero fees; PromptPay."},
    "kaidee_th":         {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Classifieds; inspect before paying.", "sell_note": "Free listings."},
    "tiktok_shop_th":    {"buy": True, "sell": True, "proxy": False,
                          "buy_note": "Buy in-app; COD or card.",
                          "sell_note": "Seller signup needs Thai ID card + Thai bank account; approval "
                                       "typically 1–3 days. Live/video content strongly boosts reach."},
    "aliexpress":        {"buy": True, "sell": False, "proxy": False,
                          "buy_note": "Register with email; pays with Thai card/TrueMoney; ships to TH in "
                                      "10–20 days (Standard). Order samples before committing to a lot.",
                          "sell_note": "Selling is for CN-registered businesses — buy-side only from TH."},
}


# Import/export legal screen. Categories needing a licence/registration before
# they can be lawfully imported for resale (reference-level, early 2026), and
# product-name keywords that are outright PROHIBITED to import into Thailand —
# the latter is the gotcha that turns a "great margin" into a seized parcel.
IMPORT_LICENCE_CATEGORIES = {
    "food": "Thai FDA (อย.) import licence + product registration required before resale; "
            "customs holds unregistered food/supplements.",
    "electronics": "Wireless/radio devices (Bluetooth, Wi-Fi, RF remotes) need NBTC type-approval; "
                   "non-wireless electronics are fine.",
    "audio": "Bluetooth/wireless audio needs NBTC type-approval; wired audio is unrestricted.",
    "watches": "Smartwatches with cellular/Wi-Fi fall under NBTC; mechanical/quartz watches are fine.",
}
IMPORT_PROHIBITED_KEYWORDS = (
    "vape", "e-cigarette", "e cigarette", "e-cig", "vaporizer", "vaporiser", "pod system",
    "baraku", "kratom", "cbd", "cannabis oil", "e-liquid", "vape juice", "nicotine pouch",
)


def import_restriction(category: str, product_name: str = "") -> dict:
    """Legal screen for importing a product into Thailand for resale.

    Returns {allowed, level, note}. `level` is 'prohibited' (illegal — never
    publish), 'licensed' (legal but needs a permit/registration — surface it as
    a real cost/risk, don't hide it) or 'clear'."""

    low = product_name.lower()
    hit = next((k for k in IMPORT_PROHIBITED_KEYWORDS if k in low), None)
    if hit:
        return {"allowed": False, "level": "prohibited",
                "note": f"Prohibited import to Thailand ('{hit}'): e-cigarettes/vapes and "
                        f"related products are illegal to import — do not attempt."}
    if category in IMPORT_LICENCE_CATEGORIES:
        return {"allowed": True, "level": "licensed", "note": IMPORT_LICENCE_CATEGORIES[category]}
    return {"allowed": True, "level": "clear",
            "note": "No special import licence for this category (general goods)."}


def import_charges(cif_usd: float, category: str, usd_thb: float,
                   parcel_cif_usd: float | None = None) -> tuple[float, float, dict]:
    """Thai import duty + VAT for goods entering Thailand, per unit (USD).

    `cif_usd` is the per-unit CIF; `parcel_cif_usd` is the whole consolidated
    parcel's CIF, which is what customs compares against the de-minimis line.
    """

    parcel_thb = (parcel_cif_usd or cif_usd) * usd_thb
    duty_pct = IMPORT_DUTY_BY_CATEGORY.get(category, DEFAULT_DUTY)
    duty = round(cif_usd * duty_pct, 2) if parcel_thb > DE_MINIMIS_THB else 0.0
    vat = round((cif_usd + duty) * VAT_RATE, 2)
    notes = {
        "duty": (f"{duty_pct * 100:.0f}% on CIF (parcel ฿{parcel_thb:,.0f} > ฿{DE_MINIMIS_THB:,.0f} de-minimis)"
                 if duty else f"Waived: parcel ฿{parcel_thb:,.0f} ≤ ฿{DE_MINIMIS_THB:,.0f} de-minimis"),
        "vat": "7% VAT on CIF+duty (collected on low-value imports since Jul 2024)",
    }
    return duty, vat, notes


def feasibility(opp_type: str, category: str, buy_venue: str | None, sell_venue: str | None) -> Feasibility:
    """Screen an opportunity for executability from Thailand."""

    if opp_type not in ("product_arbitrage", "import_export", "wholesale"):
        geo_note = {
            "local_service":  "Runs on the ground in Thailand (Bangkok metro assumed) — fully local.",
            "digital_product": "Built and operated remotely from Thailand; global distribution.",
            "b2b_service":    "Serves Thai businesses; Thai-language advantage applies.",
            "info_product":   "Created and sold online from Thailand; no customs involved.",
        }.get(opp_type, "Operable from Thailand.")
        return Feasibility(can_buy=True, can_sell=True,
                           buy_notes=[geo_note],
                           sell_notes=["Collect revenue via Stripe/Gumroad/PromptPay depending on audience."],
                           requires_proxy=False,
                           payment_rails=PAYMENT_RAILS[:2],
                           customs_notes=[])

    buy = VENUE_ACCESS.get(buy_venue or "", {"buy": False, "buy_note": "Unknown venue."})
    sell = VENUE_ACCESS.get(sell_venue or "", {"sell": False, "sell_note": "Unknown venue."})
    customs = [GENERAL_EXPORT_NOTE]
    if category in EXPORT_NOTES_BY_CATEGORY:
        customs.append(EXPORT_NOTES_BY_CATEGORY[category])
    return Feasibility(
        can_buy=bool(buy.get("buy")),
        can_sell=bool(sell.get("sell")),
        buy_notes=[buy.get("buy_note", "")],
        sell_notes=[sell.get("sell_note", "")],
        requires_proxy=bool(buy.get("proxy")),
        payment_rails=PAYMENT_RAILS,
        customs_notes=customs,
    )


def profile() -> dict:
    """The whole Thailand lens, for the dashboard panel."""

    return {
        "home_country": "TH",
        "vat_rate": VAT_RATE,
        "de_minimis_thb": DE_MINIMIS_THB,
        "duty_by_category": IMPORT_DUTY_BY_CATEGORY,
        "venue_access": VENUE_ACCESS,
        "payment_rails": PAYMENT_RAILS,
        "notes": [
            "Duty-free under ฿1,500 CIF, but 7% VAT is collected on all imports since Jul 2024.",
            "US suspended its $800 de-minimis (Aug 2025): US-bound buyers may face duties — priced into pessimistic scenarios.",
            "JP marketplaces are buy-only from TH (via proxy); eBay/Amazon/Etsy support TH-based sellers.",
        ],
    }
