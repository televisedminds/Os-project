"""Type-diversity budget (Phase 10).

The problem this fixes: eBay-backed physical products carry the richest evidence
and score highest at discovery, so a pure top-by-score watch set expires every
gap-mined Thai niche out of existence — and non-flip opportunity types die
before they can ever verify. Coverage gets monopolized by one family.

The fix, exactly as the mandate framed it: allocate the WATCH budget across
opportunity FAMILIES with a reserved floor each, and adapt those shares toward
the families that actually verify — *without ever lowering the bar to publish*.

Two rules are load-bearing and non-negotiable:

1. **A family's budget governs how much we WATCH it, never whether a candidate
   PUBLISHES.** Publishing still requires passing the full council + pessimistic
   economics gate. We never invent or wave through a weak opportunity to fill a
   quota — a family with a floor but no qualifying candidates simply under-fills,
   and we REPORT the under-fill (found vs target, so the gap is visible).

2. **Adaptation never zeroes a family.** Weights move toward what verifies, but
   every family keeps an exploration floor so the system keeps buying
   information on the quiet ones instead of collapsing onto today's winner.

Families are defined at the granularity discovery can actually distinguish — a
discovered candidate is a physical product or a niche of a given kind — which
maps cleanly onto the published opportunity types downstream.
"""

from __future__ import annotations

# ---- family taxonomy ------------------------------------------------------
#
# Each family: the default exploration weight (share of the watch budget it is
# guaranteed before adaptation), and the published opportunity types it feeds.
# Physical products feed several generators (flip, cross-market, import/export,
# wholesale, refurbishment) — they share one discovery family because at
# discovery time they are indistinguishable; the diversity that matters at this
# layer is physical-vs-the-various-venture kinds, which is exactly where the
# monopoly bites.

FAMILIES: dict[str, dict] = {
    "physical":      {"weight": 0.40, "label": "Physical trade (flip/import/wholesale)",
                      "opp_types": ("product_arbitrage", "import_export", "wholesale",
                                    "refurbishment")},
    "local_service": {"weight": 0.15, "label": "Thailand local services",
                      "opp_types": ("local_service",)},
    "b2b":           {"weight": 0.15, "label": "B2B automation / services",
                      "opp_types": ("b2b_service",)},
    "digital":       {"weight": 0.15, "label": "Digital products / micro-SaaS",
                      "opp_types": ("digital_product", "micro_saas")},
    "info":          {"weight": 0.15, "label": "Info products",
                      "opp_types": ("info_product",)},
}

_OPP_TYPE_TO_FAMILY = {t: fam for fam, spec in FAMILIES.items() for t in spec["opp_types"]}


def candidate_family(row: dict) -> str:
    """Family of a discovered candidate (a product, or a niche of some kind)."""

    if row.get("kind") == "product":
        return "physical"
    return {"local": "local_service", "b2b": "b2b",
            "digital": "digital", "info": "info"}.get(row.get("niche_kind", "info"), "info")


def opp_type_family(opp_type: str) -> str:
    """Family a published opportunity type belongs to (for yield attribution)."""

    return _OPP_TYPE_TO_FAMILY.get(opp_type, "physical")


# ---- adaptive weighting ---------------------------------------------------

def adapt_weights(yield_by_family: dict[str, int], adapt: float = 0.5) -> dict[str, float]:
    """Blend the base exploration weights toward realized family yield.

    `yield_by_family` is each family's count of verified/active opportunities —
    the honest "did watching this family actually produce anything" signal.
    `adapt` in [0,1] is how far to move from the base weights toward the yield
    distribution (0 = fixed exploration, 1 = fully yield-driven). Every family
    keeps at least half its base weight, so a quiet family is never zeroed —
    exploration continues.
    """

    base = {f: FAMILIES[f]["weight"] for f in FAMILIES}
    total_yield = sum(max(0, yield_by_family.get(f, 0)) for f in FAMILIES)
    if total_yield <= 0 or adapt <= 0:
        return base
    a = min(1.0, max(0.0, adapt))
    blended = {}
    for f in FAMILIES:
        yshare = max(0, yield_by_family.get(f, 0)) / total_yield
        w = (1 - a) * base[f] + a * yshare
        blended[f] = max(w, base[f] * 0.5)       # never below half the exploration floor
    s = sum(blended.values()) or 1.0
    return {f: w / s for f, w in blended.items()}


# ---- the allocator --------------------------------------------------------

class DiversityAllocator:
    """Decides which discovered candidates occupy the finite watch set, so every
    family gets its budgeted floor before the surplus flows to the strongest."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.min_floor = int(getattr(cfg, "diversity_min_floor", 8))
        self.adapt = float(getattr(cfg, "diversity_adapt", 0.5))

    def targets(self, total: int, yield_by_family: dict[str, int]) -> dict[str, int]:
        """Per-family target slot counts for a watch set of `total`. Each family
        gets max(min_floor, weight×total); if that over-subscribes the budget,
        floors are honoured first and the remainder is split by weight."""

        weights = adapt_weights(yield_by_family, self.adapt)
        floors = {f: min(self.min_floor, total) for f in FAMILIES}
        if sum(floors.values()) >= total:         # tiny budget: split by weight, floors can't all fit
            return _largest_remainder({f: weights[f] for f in FAMILIES}, total)
        remainder = total - sum(floors.values())
        extra = _largest_remainder(weights, remainder)
        return {f: floors[f] + extra.get(f, 0) for f in FAMILIES}

    def select(self, candidates: list[dict], total: int,
               yield_by_family: dict[str, int]) -> tuple[list[str], dict]:
        """Choose up to `total` candidate ids for the watch set, family-aware.

        Returns (kept_ids, report). The report carries, per family: how many
        candidates that family offered, its target, how many were kept, and
        whether it UNDER-FILLED (offered fewer than its target — a
        buying-information state, not a failure). Surplus from under-filled
        families is redistributed to families with overflow, by score, so no
        watch slot is wasted.
        """

        by_family: dict[str, list[dict]] = {f: [] for f in FAMILIES}
        for c in candidates:
            by_family.setdefault(candidate_family(c), []).append(c)
        for f in by_family:
            by_family[f].sort(key=lambda r: float(r.get("score", 0)), reverse=True)

        targets = self.targets(total, yield_by_family)
        kept: list[dict] = []
        leftovers: list[dict] = []
        report_families: dict[str, dict] = {}
        for f in FAMILIES:
            offered = by_family.get(f, [])
            tgt = targets.get(f, 0)
            take = offered[:tgt]
            kept += take
            leftovers += offered[tgt:]
            report_families[f] = {
                "label": FAMILIES[f]["label"], "weight": round(FAMILIES[f]["weight"], 3),
                "target": tgt, "offered": len(offered), "kept": len(take),
                "under_filled": len(offered) < tgt,
                "missing": max(0, tgt - len(offered)),
            }

        # Redistribute the slots unfilled by under-supplied families to the best
        # remaining candidates of any family (never waste watch capacity).
        free = total - len(kept)
        if free > 0 and leftovers:
            leftovers.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
            for c in leftovers[:free]:
                kept.append(c)
                fam = candidate_family(c)
                report_families[fam]["kept"] += 1

        kept_ids = [c["id"] for c in kept]
        report = {
            "watch_budget": total,
            "kept": len(kept_ids),
            "adapted_weights": {f: round(w, 3) for f, w in
                                adapt_weights(yield_by_family, self.adapt).items()},
            "families": report_families,
        }
        return kept_ids, report


def _largest_remainder(weights: dict[str, float], total: int) -> dict[str, int]:
    """Apportion `total` integer slots across weighted keys (largest-remainder
    method), so rounding never loses or invents a slot."""

    s = sum(weights.values()) or 1.0
    raw = {k: (w / s) * total for k, w in weights.items()}
    floor = {k: int(v) for k, v in raw.items()}
    used = sum(floor.values())
    remainder = total - used
    order = sorted(weights, key=lambda k: raw[k] - floor[k], reverse=True)
    for k in order[:max(0, remainder)]:
        floor[k] += 1
    return floor
