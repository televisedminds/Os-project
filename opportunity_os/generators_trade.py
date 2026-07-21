"""Trade generators — import/export and wholesale (Phase 9, generators 8 & 10).

Both are strictly ADDITIVE and emit their own opportunity types, so they never
relabel or duplicate a product-arbitrage flip:

* **ImportExportGenerator** surfaces the cross-border route the profit-max flip
  *hides*. The greedy flip picks the single global cheapest-buy → dearest-sell
  pair; for a Thai operator the most executable route is often a DIFFERENT one:
  import the item and sell it domestically (PromptPay, no export paperwork), or
  source it locally in Thailand and export it. This generator emits an
  import/export thesis ONLY when the headline flip goes the other way (ships
  abroad → we add the domestic-import route; sources abroad → we add the
  export-from-TH route), so there is never a second card for the same action.
  It gates on OBSERVED destination demand and runs the Thailand import legal
  screen (a prohibited-import product is dropped, a licensed one is surfaced
  with its permit cost, never hidden).

* **WholesaleGenerator** finds bulk LOTS inside a venue's own listing sample —
  "lot of 20", "x12", "case of 24" — whose per-unit price sits below the
  single-unit market on that same venue. Buy the lot, break it up, resell as
  singles. A quantity thesis, distinct from a single-listing dislocation, built
  from the exact same zero-cost evidence (the page of asks already fetched).

Both face the same council + pessimistic economics gate as every other
candidate — a generator proposes, the pipeline disposes.
"""

from __future__ import annotations

import math

from .generators import OpportunityGenerator, GenContext, generator
from .models import AnomalyKind, InvestigationStep, OppType
from . import economics as eco, research, thailand as th

# ---- import / export -------------------------------------------------------

IE_MIN_UNIT_NET = 8.0          # a cross-border thesis worth the customs hassle
IE_MIN_DEST_VELOCITY = 0.3     # units/day the destination must actually move
IE_MIN_MARKUP = 1.15           # sell must clear buy by ≥15% before costs


def _best_flip_route(listings: dict) -> tuple[str, str] | None:
    """Replicate the profit-max flip's route pick (global cheapest buyable →
    dearest sellable), so import/export can emit only the OTHER direction."""

    buyable = {v: l for v, l in listings.items()
               if l and l["stock"] > 0 and eco.VENUES.get(v, {}).get("buy")
               and th.VENUE_ACCESS.get(v, {}).get("buy")}
    sellable = {v: l for v, l in listings.items()
                if l and eco.VENUES.get(v, {}).get("sell")
                and th.VENUE_ACCESS.get(v, {}).get("sell")}
    if not buyable or not sellable:
        return None
    bv = min(buyable, key=lambda v: buyable[v]["price"])
    sv = max(sellable, key=lambda v: sellable[v]["price"])
    if bv == sv or sellable[sv]["price"] <= buyable[bv]["price"] * IE_MIN_MARKUP:
        return None
    return bv, sv


@generator
class ImportExportGenerator(OpportunityGenerator):
    id, name = "import_export", "Cross-border import/export"
    types = ("import_export",)
    uses_marketplace = True

    def generate(self, ctx: GenContext) -> list[dict]:
        ds = ctx.ds
        out = []
        for pid, anomalies in ctx.by_entity.items():
            if pid in ctx.niche_ids:
                continue
            product = ds.product_public(pid)
            listings = {v: ds.listing(pid, v) for v in product.get("venues", [])}
            flip = _best_flip_route(listings)
            if not flip:
                continue
            buyable = {v: l for v, l in listings.items()
                       if l and l["stock"] > 0 and eco.VENUES.get(v, {}).get("buy")
                       and th.VENUE_ACCESS.get(v, {}).get("buy")}
            sellable = {v: l for v, l in listings.items()
                        if l and eco.VENUES.get(v, {}).get("sell")
                        and th.VENUE_ACCESS.get(v, {}).get("sell")}
            flip_sells_abroad = eco.VENUES[flip[1]]["country"] != "TH"
            flip_buys_abroad = eco.VENUES[flip[0]]["country"] != "TH"

            # IMPORT: buy foreign → sell in Thailand, only when the headline
            # flip ships abroad (otherwise it's the same route).
            if flip_sells_abroad:
                th_sell = {v: l for v, l in sellable.items() if eco.VENUES[v]["country"] == "TH"}
                foreign_buy = {v: l for v, l in buyable.items() if eco.VENUES[v]["country"] != "TH"}
                if th_sell and foreign_buy:
                    bv = min(foreign_buy, key=lambda v: foreign_buy[v]["price"])
                    sv = max(th_sell, key=lambda v: th_sell[v]["price"])
                    c = self._build(ctx, pid, product, bv, sv, foreign_buy[bv],
                                    th_sell[sv], anomalies, "import")
                    if c:
                        out.append(c)

            # EXPORT: source in Thailand → sell abroad, only when the headline
            # flip sources abroad.
            if flip_buys_abroad:
                th_buy = {v: l for v, l in buyable.items() if eco.VENUES[v]["country"] == "TH"}
                foreign_sell = {v: l for v, l in sellable.items() if eco.VENUES[v]["country"] != "TH"}
                if th_buy and foreign_sell:
                    bv = min(th_buy, key=lambda v: th_buy[v]["price"])
                    sv = max(foreign_sell, key=lambda v: foreign_sell[v]["price"])
                    c = self._build(ctx, pid, product, bv, sv, th_buy[bv],
                                    foreign_sell[sv], anomalies, "export")
                    if c:
                        out.append(c)
        return out

    def _build(self, ctx, pid, product, bv, sv, buy, sell, anomalies, direction):
        if sell["price"] <= buy["price"] * IE_MIN_MARKUP:
            return None
        dest_velocity = sell.get("sold_7d", 0) / 7.0
        if dest_velocity < IE_MIN_DEST_VELOCITY:
            return None                          # a price gap with no demand isn't a trade

        category, name = product.get("category", "collectibles"), product.get("name", "")
        if direction == "import":
            legal = th.import_restriction(category, name)
            if not legal["allowed"]:
                return None                      # never suggest an illegal import
        else:
            legal = {"level": "clear",
                     "note": th.GENERAL_EXPORT_NOTE
                     + (" " + th.EXPORT_NOTES_BY_CATEGORY[category]
                        if category in th.EXPORT_NOTES_BY_CATEGORY else "")}

        probe = eco.compute_flip(product, bv, sv, buy["price"], sell["price"], qty=1)
        unit_cost = probe.base.total_cost_usd
        qty = max(1, min(int(buy["stock"] * 0.25),
                         int(ctx.cfg.capital_cap_usd / max(1.0, unit_cost)),
                         max(1, math.ceil(dest_velocity * 10))))
        econ = eco.compute_flip(product, bv, sv, buy["price"], sell["price"], qty=qty)
        if econ.base.net_usd < IE_MIN_UNIT_NET or econ.pessimistic.net_usd <= 0:
            return None

        feas = th.feasibility("import_export", category, bv, sv)
        bname, sname = eco.VENUES[bv]["name"], eco.VENUES[sv]["name"]
        bcountry, scountry = eco.VENUES[bv]["country"], eco.VENUES[sv]["country"]
        velocity = dest_velocity
        window = round(min(18.0, max(3.0, (sell.get("stock", qty) + qty) / max(0.5, velocity))), 1)
        if direction == "import":
            headline = (f"Import {name}: buy {bname} ${buy['price']:.2f} → "
                        f"sell {sname} ${sell['price']:.2f} to Thai buyers")
            title = f"{name} — import to Thailand"
        else:
            headline = (f"Export {name}: source {bname} ${buy['price']:.2f} in TH → "
                        f"sell {sname} ${sell['price']:.2f} abroad")
            title = f"{name} — export from Thailand"

        why = [
            InvestigationStep(
                "What is the cross-border gap?",
                f"{name} clears ${buy['price']:.2f} on {bname} ({bcountry}) but "
                f"${sell['price']:.2f} on {sname} ({scountry}) — a {(sell['price'] / buy['price'] - 1) * 100:.0f}% "
                f"raw gap across the {bcountry}→{scountry} border.",
                {"buy_usd": buy["price"], "sell_usd": sell["price"]}),
            InvestigationStep(
                "Is there real demand at the destination?",
                f"{sname} shows ≈{velocity:.1f} sales/day ({sell.get('sold_7d', 0)} in 7 days) — "
                f"a price gap alone isn't a trade; this one moves units.",
                {"dest_velocity_per_day": round(velocity, 2)}),
            InvestigationStep(
                "Is it legal to move across the border?",
                legal["note"],
                {"legal_level": legal["level"]}),
            InvestigationStep(
                "What is the landed profit after customs + fees?",
                f"${econ.base.net_usd:.2f}/unit base ({econ.base.margin_pct:.0f}% margin), "
                f"${econ.pessimistic.net_usd:.2f}/unit pessimistic. {econ.route_note}",
                {"net_base": econ.base.net_usd, "net_pessimistic": econ.pessimistic.net_usd}),
            InvestigationStep(
                "Can a Thailand operator run it?",
                (f"Import route: goods clear Thai customs (VAT/duty priced in), then you sell "
                 f"domestically — PromptPay, no export paperwork. " if direction == "import"
                 else f"Export route: source locally in Thailand, ship abroad with CN22/CN23. ")
                + f"Buy: {'yes' if feas.can_buy else 'no'}; Sell: {'yes' if feas.can_sell else 'no'}.",
                {"requires_proxy": feas.requires_proxy, "direction": direction}),
        ]

        return {
            "kind": "flip", "opp_type": OppType.IMPORT_EXPORT, "entity_id": pid,
            "title": title, "subtitle": headline,
            "category": category, "item": product,
            "buy_venue": bv, "sell_venue": sv, "buy_usd": buy["price"], "sell_usd": sell["price"],
            "qty": qty, "buy_stock": buy["stock"], "velocity": velocity,
            "sellers": sell.get("sellers", 1), "window_days": window,
            "economics": econ, "feasibility": feas, "why": why,
            "anomalies": anomalies, "sources": sorted({bv, sv}),
            "route": {"buy_venue": bv, "sell_venue": sv,
                      "buy_country": bcountry, "sell_country": scountry,
                      "kind": direction, "legal_level": legal["level"]},
        }


# ---- wholesale -------------------------------------------------------------

WHOLESALE_MIN_UNIT_NET = 6.0       # per-unit net after breaking the lot up


@generator
class WholesaleGenerator(OpportunityGenerator):
    id, name = "wholesale", "Wholesale lot break-up"
    types = ("wholesale",)
    uses_marketplace = True

    def generate(self, ctx: GenContext) -> list[dict]:
        ds = ctx.ds
        if not hasattr(ds, "listing_sample"):
            return []
        out = []
        for pid, anomalies in ctx.by_entity.items():
            if pid in ctx.niche_ids:
                continue
            product = ds.product_public(pid)
            for venue in product.get("venues", []):
                v = eco.VENUES.get(venue, {})
                acc = th.VENUE_ACCESS.get(venue, {})
                # Buy the lot AND relist singles on the same venue → it must
                # support both, and be reachable both ways from Thailand.
                if not (v.get("buy") and v.get("sell") and acc.get("buy") and acc.get("sell")):
                    continue
                sample = ds.listing_sample(pid, venue)
                if not sample:
                    continue
                out += self._from_sample(ctx, pid, product, venue, sample, anomalies)
        return out

    def _from_sample(self, ctx, pid, product, venue, sample, anomalies):
        lots = research.find_wholesale_lots(sample, product.get("name", ""))
        if not lots:
            return []
        lot = lots[0]                            # one wholesale thesis per (product, venue)
        n, per_unit, fair = lot["lot_size"], lot["per_unit_usd"], lot["single_fair_usd"]
        econ = eco.compute_flip(product, venue, venue, per_unit, fair, qty=n)
        if econ.base.net_usd < WHOLESALE_MIN_UNIT_NET or econ.pessimistic.net_usd <= 0:
            return []
        vname = eco.VENUES[venue]["name"]
        feas = th.feasibility("wholesale", product.get("category", "collectibles"), venue, venue)
        agg = ctx.ds.listing(pid, venue) or {}
        velocity = max(agg.get("sold_7d", 0) / 7.0, 0.2)
        # Breaking up N units takes longer to clear than one flip.
        window = round(min(30.0, max(5.0, n / max(0.5, velocity))), 1)

        why = [
            InvestigationStep(
                "What is the wholesale edge?",
                f"A lot of {n} {product['name']} lists at ${lot['lot_price_usd']:.2f} on {vname} "
                f"— ${per_unit:.2f}/unit, {lot['edge_pct']:.0f}% under the ${fair:.2f} single-unit "
                f"price ({lot['market_n']} singles, {lot['market_sellers']} sellers).",
                {"lot_size": n, "per_unit_usd": per_unit, "single_fair_usd": fair}),
            InvestigationStep(
                "What is the capital and the per-unit profit?",
                f"Buy the whole lot for ${econ.capital_usd:.2f}, resell {n} singles at ${fair:.2f}: "
                f"${econ.base.net_usd:.2f}/unit base ({econ.base.margin_pct:.0f}% margin), "
                f"${econ.pessimistic.net_usd:.2f}/unit pessimistic after {vname} fees on every resale.",
                {"capital_usd": econ.capital_usd, "net_per_unit": econ.base.net_usd}),
            InvestigationStep(
                "How long to clear the inventory?",
                f"≈{velocity:.1f} sales/day on {vname} → ~{window:.0f} days to sell {n} units. "
                f"Inventory risk scales with the lot: size the buy to what you can hold.",
                {"window_days": window, "velocity_per_day": round(velocity, 2)}),
            InvestigationStep(
                "Can a Thailand operator run it?",
                f"Buy the lot to a prep address, relist singles on {vname} "
                f"(TH sellers accepted; payout via Payoneer). Buy: {'yes' if feas.can_buy else 'no'}; "
                f"Sell: {'yes' if feas.can_sell else 'no'}.",
                {"requires_proxy": feas.requires_proxy}),
            InvestigationStep(
                "What's the downside if the singles don't sell?",
                f"Unlike a single flip, you commit ${econ.capital_usd:.2f} up front for {n} units and "
                f"carry inventory until they clear. If demand softens you can dump the remainder at the "
                f"lot per-unit (${per_unit:.2f}) and roughly break even — size the buy to what you can hold.",
                {"capital_at_risk_usd": econ.capital_usd, "floor_per_unit_usd": per_unit}),
        ]

        return [{
            "kind": "flip", "opp_type": OppType.WHOLESALE, "entity_id": pid,
            "title": f"{product['name']} — buy the lot, sell singles",
            "subtitle": f"Lot of {n} @ ${per_unit:.2f}/unit on {vname} → resell singles ${fair:.2f}",
            "category": product.get("category", "collectibles"), "item": product,
            "buy_venue": venue, "sell_venue": venue, "buy_usd": per_unit, "sell_usd": fair,
            "qty": n, "buy_stock": n, "velocity": velocity, "sellers": lot["market_sellers"],
            "window_days": window, "economics": econ, "feasibility": feas, "why": why,
            "anomalies": anomalies, "sources": sorted({venue}),
            "wholesale": {"item_id": lot["item_id"], "url": lot["url"], "lot_size": n,
                          "per_unit_usd": per_unit, "single_fair_usd": fair,
                          "min_edge": 0.25},
            "route": {"buy_venue": venue, "sell_venue": venue,
                      "buy_country": eco.VENUES[venue]["country"],
                      "sell_country": eco.VENUES[venue]["country"],
                      "kind": "wholesale", "item_id": lot["item_id"], "buy_url": lot["url"],
                      "lot_size": n},
        }]
