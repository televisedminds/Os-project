"""Selection — turn a wall of verified opportunities into today's best moves.

The pipeline publishes many verified opportunities; the operator can only act on
a few. This module ranks them by a conservative, risk-adjusted **expected
realized value**, separates what is execution-ready from what is
validation-required, and surfaces only the best 1–3 actions for today — each with
capital, time, the conservative (pessimistic) result, its risks, its evidence
quality, and the exact next step.

Pure and deterministic: it reads opportunity dicts and returns ranked structures.
The API layer attaches links and the do-this-deal card.
"""

from __future__ import annotations

# Evidence quality by verification level — a discount, never a boost. Execution-
# ready (sold comps / ≥3 independent corroborations) is trusted most; a
# single-source, asking-price-only edge is trusted least.
_VERIF_FACTOR = {
    "execution_ready": 1.0,
    "multi_source_verified": 0.9,
    "partially_verified": 0.6,
    "discovered": 0.4,
    "invalidated": 0.0,
}


def conservative_result_usd(o: dict) -> float:
    """The PESSIMISTIC net — a one-time total for a flip, a monthly figure for a
    venture. Never the optimistic base; today's decision uses the worst case."""
    e = o["economics"]
    if e["kind"] == "flip":
        return round(e["pessimistic"]["net_usd"] * e.get("qty", 1), 2)
    return round(e["pessimistic"]["net_usd"], 2)


def evidence_quality(o: dict) -> float:
    """0..1 — how much the numbers can be trusted. Verification level, discounted
    again for single-source evidence and any estimated economic input."""
    f = _VERIF_FACTOR.get(o.get("verification_level"), 0.5)
    if o.get("single_source"):
        f *= 0.7
    ip = (o.get("economics", {}) or {}).get("input_provenance") or {}
    if any(v == "estimated" for v in ip.values()):
        f *= 0.8
    return round(f, 3)


def expected_value_usd(o: dict) -> float:
    """Conservative expected realized value = worst-case net × probability it is
    real (calibrated confidence) × evidence quality. Clamped at 0 — a negative
    pessimistic case contributes nothing, it doesn't subtract."""
    cons = conservative_result_usd(o)
    conf = float(o.get("confidence", 0.0))
    return round(max(0.0, cons) * conf * evidence_quality(o), 2)


def risks(o: dict) -> list[str]:
    """The honest caveats an operator must weigh before committing capital."""
    out: list[str] = []
    e = o["economics"]
    if o.get("single_source"):
        out.append("Single-source evidence — asking prices, not confirmed sold comps.")
    ip = (e.get("input_provenance") or {})
    est = [k.replace("_", " ") for k, v in ip.items() if v == "estimated"]
    if est:
        out.append("Estimated (unproven) input" + ("s" if len(est) > 1 else "") + ": " + ", ".join(est) + ".")
    if e["pessimistic"]["net_usd"] <= 0:
        out.append("Loses money in the pessimistic case — thin margin of safety.")
    lvl = o.get("verification_level")
    if lvl not in ("execution_ready", "multi_source_verified"):
        out.append(f"Only {(lvl or 'partially verified').replace('_', ' ')} — corroborate a second source.")
    if not out:
        out.append("No blocking risk flagged — still re-check the live listing before you buy.")
    return out


def _thesis_key(o: dict) -> tuple:
    return (o.get("type"), o.get("category"), (o.get("route") or {}).get("kind"))


def rank_execution_ready(opps: list[dict], limit: int = 3) -> list[dict]:
    """Best distinct actions by expected realized value. Deduplicated by thesis
    so twenty listings of one edge collapse to a single action."""
    ranked = sorted(opps, key=expected_value_usd, reverse=True)
    seen: set = set()
    picks: list[dict] = []
    for o in ranked:
        if expected_value_usd(o) <= 0:
            continue
        k = _thesis_key(o)
        if k in seen:
            continue
        seen.add(k)
        picks.append(o)
        if len(picks) >= limit:
            break
    return picks


def action_summary(o: dict) -> dict:
    """The decision-grade summary for one execution-ready opportunity (the API
    attaches the do-this-deal card + links on top of this)."""
    e = o["economics"]
    is_flip = e["kind"] == "flip"
    return {
        "id": o["id"],
        "title": o["title"],
        "type": o.get("type"),
        "capital_usd": round(e.get("capital_usd", 0.0), 2),
        "time": (f"{o.get('window_days', 0):.0f}-day window" if is_flip
                 else f"~{_payback_months(e):.1f}-month payback"),
        "conservative_result_usd": conservative_result_usd(o),
        "conservative_result_note": ("worst-case total, this batch" if is_flip
                                     else "worst-case monthly net"),
        "expected_value_usd": expected_value_usd(o),
        "evidence_quality": evidence_quality(o),
        "verification_level": o.get("verification_level"),
        "confidence": o.get("confidence"),
        "risks": risks(o),
    }


def _payback_months(e: dict) -> float:
    base = e.get("base", {}).get("net_usd", 0.0)
    return e.get("capital_usd", 0.0) / max(1.0, base)


def validation_required(evals: list[dict], limit: int = 5) -> list[dict]:
    """Promising discovered niches that reached the council but need the operator
    to validate real demand before any capital goes in — newest first, one row
    per niche."""
    out: list[dict] = []
    seen: set = set()
    for r in evals:
        if r.get("category") != "validation_required" or r.get("entity_id") in seen:
            continue
        seen.add(r["entity_id"])
        out.append({
            "entity_id": r["entity_id"],
            "family": r.get("family"),
            "demand_points": r.get("demand_points"),
            "demand_supply_ratio": r.get("demand_supply_ratio"),
            "why": "Real unmet-need signal and a supply gap, but demand VOLUME is only "
                   "estimated from search-result counts.",
            "next_step": "Validate real monthly demand — a keyword-volume tool or a small "
                         "paid smoke test (a landing page + ads) — before building.",
        })
        if len(out) >= limit:
            break
    return out
