"""Thesis clustering / de-duplication (Phase 6).

Twenty underpriced listings of the same New Balance model are ONE market
inefficiency, not twenty opportunities. Showing them as twenty inflates the
count and buries the signal. This module collapses opportunities that express
the same edge into a single **thesis**, and reports the depth behind it —
how many qualifying listings, the price range, the inventory available and the
recommended quantity — instead of repeating the thesis N times.

Clustering keys, from most specific to the market thesis:

* exact listing        — the opportunity id itself
* same model + route   — (type, entity, buy_venue, sell_venue): the same product
                         flipped the same way, regardless of which individual
                         listing triggered it (this is what collapses N
                         dislocations of one product into one thesis)
* same cross-market    — the same buy→sell spread on the same entity

The representative is the strongest member; the rest become depth on it.
"""

from __future__ import annotations


def thesis_key(o: dict) -> str:
    """The market-thesis identity: same product, same buy→sell route, same type.
    Distinct individual listings of the same underpriced model collapse here."""

    route = o.get("route", {}) or {}
    # entity_id is the product/niche family; fall back to the opp id so
    # malformed rows without one never collapse into a single false thesis.
    entity = o.get("entity_id") or o.get("id")
    return "|".join([
        str(o.get("type")),
        str(route.get("kind") or o.get("type")),
        str(entity),
        str(route.get("buy_venue")),
        str(route.get("sell_venue")),
    ])


def _buy_price(o: dict) -> float:
    try:
        return float(o["economics"]["base"]["lines"][0]["amount_usd"])
    except Exception:  # noqa: BLE001
        return 0.0


def _score(o: dict) -> float:
    try:
        return float(o["score"]["overall"])
    except Exception:  # noqa: BLE001
        return 0.0


_LEVEL_RANK = {"execution_ready": 4, "multi_source_verified": 3,
               "partially_verified": 2, "discovered": 1, "invalidated": 0}


def cluster(opps: list[dict]) -> list[dict]:
    """Group opportunities into theses. Returns one clustered entry per thesis,
    strongest first, each carrying its representative + the depth behind it."""

    groups: dict[str, list[dict]] = {}
    for o in opps:
        groups.setdefault(thesis_key(o), []).append(o)

    clusters = []
    for key, members in groups.items():
        members.sort(key=_score, reverse=True)
        rep = members[0]
        buys = [_buy_price(m) for m in members if _buy_price(m) > 0]
        qtys = [int(m.get("economics", {}).get("qty", 1)) for m in members]
        depths = []
        for m in members:
            d = (m.get("route", {}) or {}).get("inventory_depth")
            depths.append(int(d) if d else int(m.get("economics", {}).get("qty", 1)))
        best_level = max((m.get("verification_level", "discovered") for m in members),
                         key=lambda lv: _LEVEL_RANK.get(lv, 0))
        clusters.append({
            "thesis_key": key,
            "representative_id": rep["id"],
            "qualifying_listings": len(members),
            "price_range_usd": [round(min(buys), 2), round(max(buys), 2)] if buys else None,
            "inventory_depth": sum(depths),
            "recommended_qty": min(sum(qtys), max(qtys) * 3) if qtys else 1,
            "best_confidence": round(max((m.get("confidence", 0) for m in members), default=0), 3),
            "best_score": round(_score(rep), 1),
            "verification_level": best_level,
            "member_ids": [m["id"] for m in members],
        })
    clusters.sort(key=lambda c: (c["best_score"], c["qualifying_listings"]), reverse=True)
    return clusters


def cluster_metrics(raw_candidates: int, opps: list[dict]) -> dict:
    """The honesty scoreboard: how many raw candidates became how many unique
    listings, products, and market theses — so "N verified" can be read for
    what it is."""

    theses = {thesis_key(o) for o in opps}
    products = {o.get("entity_id") for o in opps}
    return {
        "raw_candidates": raw_candidates,
        "verified_opportunities": len(opps),
        "unique_listings": len(opps),          # each stored opp is a distinct listing/thesis instance
        "unique_products": len(products),
        "unique_theses": len(theses),
        "inflation_ratio": round(len(opps) / max(1, len(theses)), 2),
    }
