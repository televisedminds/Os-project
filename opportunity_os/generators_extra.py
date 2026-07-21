"""The genuinely-new, non-flip opportunity generators (Phase 4).

These are not marketplace flips. They read the same evidence every other
generator does and construct different theses:

* **MicroSaasGenerator** — a recurring-revenue software play. Fires only on a
  digital niche with a strong, corroborated evidence bundle (real demand
  growth + a genuinely weak supply side), which is a higher bar than a generic
  digital product. Distinct opportunity type so the funnel shows it.

* **BundleRepairGenerator** — the "a profitable Game Boy should spawn related
  hypotheses, not more Game Boy listings" generator. From a product's own live
  ask distribution it finds *broken / for-parts* units priced far under working
  comps and builds a REFURBISHMENT thesis: buy the parts unit, repair it, sell
  at the working price. Economics carry a real parts+labour cost and a scrap
  reserve, so a thin refurb edge dies in the pessimistic gate like anything
  else. It also seeds the knowledge graph with the product so the pipeline can
  traverse to accessories/variants next.

Both register with the same `@generator` decorator and face the same council.
"""

from __future__ import annotations

from .generators import OpportunityGenerator, GenContext, generator
from .models import AnomalyKind, OppType
from . import economics, research, thailand

# --------------------------------------------------------------- micro-SaaS

MICRO_SAAS_MIN_VOLUME = 2000        # needs enough demand to sustain subscriptions
MICRO_SAAS_MAX_SOLUTIONS = 2        # genuinely weak supply, not "a bit competitive"
MICRO_SAAS_MIN_GROWTH = 20.0


def qualifies_micro_saas(niche: dict) -> bool:
    """A digital niche strong enough to justify a recurring-revenue build."""

    if niche.get("kind") != "digital":
        return False
    m = niche.get("metrics", {})
    return (m.get("volume", 0) >= MICRO_SAAS_MIN_VOLUME
            and m.get("solution_count", 99) <= MICRO_SAAS_MAX_SOLUTIONS
            and m.get("growth_pct", 0) >= MICRO_SAAS_MIN_GROWTH)


@generator
class MicroSaasGenerator(OpportunityGenerator):
    id, name = "micro_saas", "Micro-SaaS (recurring revenue)"
    types = ("micro_saas",)
    uses_marketplace = False

    def generate(self, ctx: GenContext) -> list[dict]:
        out = []
        niches = {n["id"]: n for n in ctx.ds.niches()}
        for nid, group in ctx.by_entity.items():
            niche = niches.get(nid)
            if not niche or not qualifies_micro_saas(niche):
                continue
            cand = ctx.investigator._venture_candidate(
                ctx.ds, nid, group, opp_type_override=OppType.MICRO_SAAS,
                route_kind="micro_saas", title_prefix="Micro-SaaS: ")
            if cand:
                m = niche["metrics"]
                cand["subtitle"] = (f"{m['volume']:,.0f} searches/mo growing {m['growth_pct']:.0f}%/mo, "
                                    f"only {m['solution_count']} weak tools — recurring-revenue gap")
                out.append(cand)
        return out


# --------------------------------------------------------- bundle / repair

# Titles that mark a unit as broken / incomplete — the raw material of a
# refurbishment thesis (the opposite of what the dislocation detector wants).
_PARTS_TERMS = ("for parts", "parts only", "not working", "broken", "as-is", "as is",
                "untested", "faulty", "repair", "spares", "damaged", "cracked", "no power")

# Refurbishment only makes sense where a fault can actually be fixed: consumer
# electronics and mechanical goods. A "for parts" trading card or sealed
# collectible is mislabeled or worthless — never a refurb thesis.
_REPAIR_COST = {
    "gaming": 12.0, "electronics": 14.0, "cameras": 25.0, "watches": 30.0,
}
REPAIRABLE_CATEGORIES = set(_REPAIR_COST)
_REPAIR_DEFAULT = 15.0

MIN_REFURB_NET = 12.0               # don't bother below this working headroom


def _is_parts(title: str) -> bool:
    low = f" {title.lower()} "
    return any(t in low for t in _PARTS_TERMS)


@generator
class BundleRepairGenerator(OpportunityGenerator):
    id, name = "bundle_repair", "Refurbishment (parts → working)"
    types = ("refurbishment",)
    uses_marketplace = True

    def generate(self, ctx: GenContext) -> list[dict]:
        ds = ctx.ds
        if not hasattr(ds, "listing_sample"):
            return []
        out = []
        for pid, group in ctx.by_entity.items():
            if pid in ctx.niche_ids:
                continue
            product = ds.product_public(pid)
            if product.get("category") not in REPAIRABLE_CATEGORIES:
                continue                            # only fixable goods can be refurbished
            for venue in product.get("venues", []):
                v = economics.VENUES.get(venue, {})
                acc = thailand.VENUE_ACCESS.get(venue, {})
                if not (v.get("buy") and v.get("sell") and acc.get("buy") and acc.get("sell")):
                    continue
                sample = ds.listing_sample(pid, venue)
                if not sample:
                    continue
                out += self._from_sample(ctx, pid, product, venue, sample, group)
        return out

    def _from_sample(self, ctx, pid, product, venue, sample, anomalies):
        working = [s for s in sample if not _is_parts(s.get("title", ""))
                   and float(s.get("price", 0)) > 0]
        parts = [s for s in sample if _is_parts(s.get("title", "")) and float(s.get("price", 0)) > 0]
        if len(working) < research.MIN_MARKET_DEPTH or not parts:
            return []
        wstats = research.market_stats(working)
        fair = wstats["fair_usd"]
        if fair < research.MIN_PRICE_USD:
            return []
        repair = _REPAIR_COST.get(product.get("category"), _REPAIR_DEFAULT)
        vname = economics.VENUES[venue]["name"]
        out = []
        parts.sort(key=lambda s: float(s["price"]))
        for p in parts[:2]:                        # at most two refurb theses per market
            cost = float(p["price"])
            # Need real headroom: working comp must clear parts + repair by a margin.
            econ = economics.compute_flip(product, venue, venue, cost, fair, qty=1,
                                          extra_cost_usd=repair,
                                          extra_note=f"typical {product.get('category','item')} refurb: "
                                                     f"parts + ~1h labour")
            if econ.base.net_usd < MIN_REFURB_NET:
                continue
            agg = ctx.ds.listing(pid, venue) or {}
            velocity = max(agg.get("sold_7d", 0) / 7.0, 0.2)
            if ctx.graph is not None:               # seed the graph for later traversal
                ctx.graph.add_node(pid, "product", product["name"])
            out.append({
                "kind": "flip", "opp_type": OppType.REFURBISHMENT, "entity_id": pid,
                "title": f"{product['name']} — refurbish & resell",
                "subtitle": f"Buy a for-parts unit ${cost:.2f} on {vname}, repair (~${repair:.0f}), "
                            f"resell ${fair:.2f} working",
                "category": product["category"], "item": product,
                "buy_venue": venue, "sell_venue": venue, "buy_usd": cost, "sell_usd": fair,
                "qty": 1, "buy_stock": 1, "velocity": velocity,
                "sellers": wstats["sellers"],
                "window_days": round(min(21.0, max(5.0, wstats["n"] / max(0.5, velocity))), 1),
                "economics": econ,
                "feasibility": thailand.feasibility(OppType.PRODUCT_ARBITRAGE.value,
                                                    product["category"], venue, venue),
                "why": ctx.investigator._refurb_why(product, cost, fair, repair, econ, wstats, vname)
                       if hasattr(ctx.investigator, "_refurb_why") else [],
                "anomalies": anomalies, "sources": sorted({venue}),
                "dislocation": {"item_id": p.get("item_id", ""), "url": p.get("url", ""),
                                "ask_usd": cost, "fair_usd": fair,
                                "min_edge": 0.0, "refurbish": True, "repair_cost": repair},
                "route": {"buy_venue": venue, "sell_venue": venue,
                          "buy_country": economics.VENUES[venue]["country"],
                          "sell_country": economics.VENUES[venue]["country"],
                          "kind": "refurbish", "item_id": p.get("item_id", ""),
                          "buy_url": p.get("url", ""), "repair_cost": repair},
            })
        return out
