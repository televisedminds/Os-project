"""Milestone 2 — steady-state venture entry path.

A venture niche with steady, non-spiking demand never triggers an anomaly, so it
was never placed in `by_entity` (built from anomalies in the investigator) and
never evaluated — a *silent zero*. This module adds a SECOND, honest entry path:
a well-observed niche (enough real demand observations, an OBSERVED supply side,
fresh evidence, real provenance) is surfaced to the SAME verification council.

Eligibility to be *evaluated* is never approval to *publish*. A steady niche
still faces the full council and can be rejected honestly (insufficient_demand,
high_competition, …). The goal is to eliminate silent non-evaluation, not to
manufacture opportunities. Every threshold is read from Config — no magic
numbers — and demo/simulated niches (no observed provenance) are never eligible,
so this path is live-evidence only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import diversity as dv

# niche kind -> the venture opp type it produces (for family reporting)
KIND_TO_OPP_TYPE = {
    "digital": "digital_product",
    "info": "info_product",
    "local": "local_service",
    "b2b": "b2b_service",
}
VENTURE_KINDS = frozenset(KIND_TO_OPP_TYPE)

# eligibility reason codes (stable strings for the funnel/tests)
R_ELIGIBLE = "steady_state_eligible"
R_NOT_VENTURE = "not_a_venture_kind"
R_SIMULATED = "no_observed_provenance"          # demo/simulated — steady path is live-only
R_GATHERING = "still_gathering_evidence"        # < min demand observations
R_NO_SUPPLY = "supply_never_observed"           # no observed supply scan
R_STALE = "observations_stale"
R_COOLDOWN = "cooldown_no_new_observations"
R_DEFERRED = "deferred_per_cycle_cap"


@dataclass
class SteadyAssessment:
    """One venture niche's steady-state evaluation-readiness verdict."""

    nid: str
    kind: str
    family: str
    eligible: bool
    reason: str
    evidence: dict


def _demand_points(ds, nid: str) -> int:
    """Longest single measured mention series — the SAME meter verify_venture
    and the niche_state panel use as 'N/6'."""

    pts = 0
    for src in ds.social_sources():
        h = ds.mentions(nid, src)
        if h:
            pts = max(pts, len(h))
    return pts


def _source_count(ds, nid: str, supply_observed: bool) -> int:
    n = sum(1 for s in ds.social_sources() if ds.mentions(nid, s))
    return n + (1 if supply_observed else 0)


def _freshness_days(store, nid: str) -> float | None:
    """Days since the most recent stored observation for this niche (supply /
    demand / competitor). None when nothing timestamped exists."""

    if store is None:
        return None
    latest = 0.0
    for kind in ("supply", "demand", "competitor"):
        o = store.latest_search_obs(nid, kind)
        if o and o.get("ts"):
            latest = max(latest, float(o["ts"]))
    if latest <= 0:
        return None
    return (time.time() - latest) / 86400.0


def assess_ventures(ds, store, cfg, tick: int) -> list[SteadyAssessment]:
    """Assess EVERY venture niche for steady-state evaluation readiness. Returns
    one assessment per venture niche (eligible or not) so the caller can record
    an explicit reason for each — no silent zeros. The eligible ones, capped at
    `steady_max_per_cycle`, are the steady entrants."""

    assessments: list[SteadyAssessment] = []
    for n in ds.niches():
        kind = n.get("kind")
        if kind not in VENTURE_KINDS:
            continue
        family = dv.opp_type_family(KIND_TO_OPP_TYPE[kind])
        m = n.get("metrics", {}) or {}
        prov = m.get("observed") or {}
        supply_src = prov.get("supply")
        demand_src = prov.get("demand")
        supply_observed = supply_src == "observed"
        providers = int(m.get("providers", m.get("solution_count", 0)) or 0)
        demand_posts = float(m.get("demand_posts", 0) or 0)
        ratio = demand_posts / max(1, providers)
        pts = _demand_points(ds, n["id"])
        fresh = _freshness_days(store, n["id"])
        src_count = _source_count(ds, n["id"], supply_observed)
        ev = {
            "demand_points": pts,
            "supply_count": providers,
            "supply_provenance": supply_src,
            "demand_provenance": demand_src,
            "source_count": src_count,
            "freshness_days": round(fresh, 2) if fresh is not None else None,
            "demand_supply_ratio": round(ratio, 1),
            "family": family,
        }
        reason, eligible = _eligibility(cfg, store, n["id"], pts, supply_observed,
                                        demand_src, fresh, tick, ev)
        assessments.append(SteadyAssessment(n["id"], kind, family, eligible, reason, ev))

    # Bounded: keep only the strongest N eligible this cycle (best-observed
    # first). The diversity budget may cap how much we EVALUATE; it never
    # manufactures a passing result. Deferred niches keep an explicit reason.
    eligible = [a for a in assessments if a.eligible]
    eligible.sort(key=lambda a: (a.evidence["demand_points"],
                                 a.evidence["demand_supply_ratio"]), reverse=True)
    for a in eligible[cfg.steady_max_per_cycle:]:
        a.eligible = False
        a.reason = R_DEFERRED
    return assessments


def _eligibility(cfg, store, nid, pts, supply_observed, demand_src, fresh, tick, ev):
    """Returns (reason_code, eligible). All thresholds come from Config."""

    if not cfg.steady_venture_enabled:
        return "steady_disabled", False
    # Real provenance only — a demo/simulated niche has no observed provenance.
    if demand_src not in ("observed", "user_supplied"):
        return R_SIMULATED, False
    if pts < cfg.steady_min_demand_points:
        return R_GATHERING, False
    if cfg.steady_require_observed_supply and not supply_observed:
        return R_NO_SUPPLY, False
    if fresh is None:
        return R_STALE, False
    if fresh > cfg.steady_freshness_days:
        return R_STALE, False
    # Cooldown: don't re-evaluate an unchanged niche every cycle. The reference
    # is the last time the niche ACTUALLY entered evaluation (eligible=1), NOT
    # the newest ledger row — a cooled-down cycle writes a skip row every tick,
    # and keying off that would keep resetting the clock and starve the niche
    # forever. New observations (a longer demand series) lift the cooldown
    # immediately.
    last = None
    if store is not None:
        last = (store.last_entered_venture_eval(nid)
                if hasattr(store, "last_entered_venture_eval")
                else store.last_venture_eval(nid))
    if last and (tick - int(last.get("tick", 0))) < cfg.steady_cooldown_ticks \
            and pts <= int(last.get("demand_points", 0)):
        return R_COOLDOWN, False
    return R_ELIGIBLE, True


def build_venture_entries(ds, store, cfg, tick, by_entity: dict) -> tuple[dict, list]:
    """Merge the anomaly path and the steady path into one deduplicated set of
    venture entries. An entity qualifying through both is evaluated ONCE, tagged
    entry_path='both'. Returns (entries, assessments) where entries maps
    nid -> {anomalies, entry_path, steady_evidence} and assessments is the full
    per-niche steady readiness list (for recording explicit reasons)."""

    niche_ids = {n["id"] for n in ds.niches()}
    entries: dict[str, dict] = {}
    # 1) anomaly path (unchanged): venture niches that had an anomaly this cycle
    for nid, group in by_entity.items():
        if nid in niche_ids:
            entries[nid] = {"anomalies": group, "entry_path": "anomaly",
                            "steady_evidence": None}
    # 2) steady path
    assessments = assess_ventures(ds, store, cfg, tick)
    for a in assessments:
        if not a.eligible:
            continue
        if a.nid in entries:
            entries[a.nid]["entry_path"] = "both"
            entries[a.nid]["steady_evidence"] = a.evidence
        else:
            entries[a.nid] = {"anomalies": [], "entry_path": "steady_state",
                              "steady_evidence": a.evidence}
    return entries, assessments
