"""Milestone 4.1 — fair niche-measurement scheduler.

The niche-measurement budget (Serper demand/supply scans per pass) is scarce.
The old loop walked `_all_niches()` in fixed order and drained a shared budget,
so the same top-of-list niches were measured every pass while low-score
*discovered* niches starved forever — never accumulating the demand+supply
evidence they need to be evaluated by the council.

This module allocates that budget FAIRLY and by RESEARCH STAGE:

* every niche is scored for the ONE measurement that best unlocks its next stage
  (first demand → more demand → supply → refresh);
* a configurable share of the budget is RESERVED for auto-discovered niches, so
  a hot watchlist niche can't consume every slot;
* an AGING term lifts long-waiting niches so nothing starves — every eligible
  niche has a bounded wait;
* niches inside a measurement cooldown, or whose evidence is already sufficient,
  are skipped with an explicit reason (never silently dropped);
* the plan is fully inspectable — each niche ends a pass in exactly one state
  with a reason, priority and (if deferred) queue position.

It decides WHAT to measure; the live world executes the selected scans. Pure and
deterministic for tests — no I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

# ---- research stages: what evidence a niche needs next ---------------------
STAGE_NO_DEMAND = "no_observed_provenance"     # no demand observation yet
STAGE_GATHERING = "still_gathering_evidence"   # has demand, < min points
STAGE_NO_SUPPLY = "demand_ready_no_supply"     # enough demand, supply unobserved
STAGE_SUPPLY_STALE = "supply_stale"            # supply observed but old
STAGE_SUFFICIENT = "evidence_sufficient"       # nothing to measure now

NEED_DEMAND = "demand"
NEED_SUPPLY = "supply"
NEED_NONE = "none"

# outcome states (every niche ends a pass in exactly one)
SEL_DEMAND = "selected_for_demand_scan"
SEL_SUPPLY = "selected_for_supply_scan"
WAIT_COOLDOWN = "waiting_for_cooldown"
DEFERRED_BUDGET = "deferred_by_budget"
EVIDENCE_SUFFICIENT = "evidence_sufficient"

# Base urgency per (need, stage). Supply for a demand-ready niche unlocks
# eligibility in a single scan, so it ranks highest; bootstrapping a niche's
# first demand point is next; then filling out the demand series; refresh last.
_BASE = {
    (NEED_SUPPLY, STAGE_NO_SUPPLY): 100.0,
    (NEED_DEMAND, STAGE_NO_DEMAND): 80.0,
    (NEED_DEMAND, STAGE_GATHERING): 60.0,
    (NEED_SUPPLY, STAGE_SUPPLY_STALE): 40.0,
}


@dataclass
class NicheSchedule:
    niche_id: str
    origin: str                 # "manual" | "auto"
    stage: str
    need: str                   # the need that was scored (demand/supply/none)
    demand_points: int
    has_supply: bool
    last_demand_tick: int | None
    last_supply_tick: int | None
    starvation_age: int         # ticks since this niche was last scanned (either kind)
    selection_count: int
    priority: float
    priority_reason: str
    outcome: str                # one of SEL_*/WAIT_COOLDOWN/DEFERRED_BUDGET/EVIDENCE_SUFFICIENT
    queue_position: int | None  # rank among deferred, 1 = next in line
    next_required_measurement: str

    def to_dict(self) -> dict:
        return asdict(self)


def stage_and_need(demand_points: int, has_supply: bool, supply_stale: bool, cfg) -> tuple[str, str]:
    """The niche's stage and the single measurement that advances it."""
    if not has_supply and demand_points >= 1:
        # A one-shot supply scan unlocks eligibility fastest once any demand exists.
        return STAGE_NO_SUPPLY, NEED_SUPPLY
    if demand_points <= 0:
        return STAGE_NO_DEMAND, NEED_DEMAND
    if demand_points < cfg.steady_min_demand_points:
        return STAGE_GATHERING, NEED_DEMAND
    if not has_supply:
        return STAGE_NO_SUPPLY, NEED_SUPPLY
    if supply_stale:
        return STAGE_SUPPLY_STALE, NEED_SUPPLY
    return STAGE_SUFFICIENT, NEED_NONE


def schedule(niche_states: list[dict], budget: int, cfg, tick: int) -> list[NicheSchedule]:
    """Plan one measurement pass. `niche_states` items carry:
        id, origin ('manual'/'auto'), demand_points, has_supply, supply_stale,
        last_demand_tick, last_supply_tick, selection_count.
    Returns one NicheSchedule per niche (selected, cooling, deferred or
    sufficient), deterministic and budget-bounded."""

    aging = float(getattr(cfg, "measure_aging_coef", 1.0))
    origin_bonus = float(getattr(cfg, "measure_manual_bonus", 5.0))
    cooldown = int(getattr(cfg, "measure_cooldown_ticks", 3))
    auto_frac = float(getattr(cfg, "measure_auto_reserve_frac", 0.5))

    rows: dict[str, NicheSchedule] = {}
    candidates: list[NicheSchedule] = []          # scannable this pass
    for s in niche_states:
        stage, need = stage_and_need(s["demand_points"], s["has_supply"],
                                     s.get("supply_stale", False), cfg)
        last_scan = max([t for t in (s.get("last_demand_tick"), s.get("last_supply_tick"))
                         if t is not None], default=None)
        age = (tick - last_scan) if last_scan is not None else tick + 1  # never-scanned = maximally starved
        sched = NicheSchedule(
            niche_id=s["id"], origin=s.get("origin", "auto"), stage=stage, need=need,
            demand_points=s["demand_points"], has_supply=s["has_supply"],
            last_demand_tick=s.get("last_demand_tick"), last_supply_tick=s.get("last_supply_tick"),
            starvation_age=age, selection_count=s.get("selection_count", 0),
            priority=0.0, priority_reason="", outcome=EVIDENCE_SUFFICIENT,
            queue_position=None, next_required_measurement=need)
        rows[s["id"]] = sched
        if need == NEED_NONE:
            sched.outcome = EVIDENCE_SUFFICIENT
            sched.priority_reason = "evidence sufficient — no scan needed"
            continue
        # cooldown applies to the NEEDED kind: don't re-demand-scan too soon
        # (supply already has its own staleness gate upstream).
        last_of_need = s.get("last_demand_tick") if need == NEED_DEMAND else s.get("last_supply_tick")
        if need == NEED_DEMAND and last_of_need is not None and (tick - last_of_need) < cooldown:
            sched.outcome = WAIT_COOLDOWN
            sched.priority_reason = f"demand scanned {tick - last_of_need} ticks ago (<{cooldown})"
            continue
        base = _BASE.get((need, stage), 30.0)
        sched.priority = round(base + aging * age + (origin_bonus if sched.origin == "manual" else 0.0), 3)
        sched.priority_reason = (f"{stage}: base {base:.0f} + age {age}×{aging:g}"
                                 + (f" + manual {origin_bonus:g}" if sched.origin == "manual" else ""))
        candidates.append(sched)

    # Rank by priority, then age, then fewest prior selections (fairness tiebreak).
    candidates.sort(key=lambda c: (c.priority, c.starvation_age, -c.selection_count), reverse=True)

    # Reserve a share of the budget for auto-discovered niches so a hot watchlist
    # niche cannot consume every slot; fill the remainder from all candidates.
    auto_reserved = min(int(round(budget * auto_frac)), budget)
    selected: list[NicheSchedule] = []
    autos = [c for c in candidates if c.origin == "auto"]
    for c in autos[:auto_reserved]:
        selected.append(c)
    picked = {c.niche_id for c in selected}
    for c in candidates:
        if len(selected) >= budget:
            break
        if c.niche_id in picked:
            continue
        selected.append(c); picked.add(c.niche_id)

    for c in selected:
        c.outcome = SEL_DEMAND if c.need == NEED_DEMAND else SEL_SUPPLY
    # Everything scannable but not selected is explicitly deferred, with its
    # queue position so the wait is legible (and bounded by aging next pass).
    deferred = [c for c in candidates if c.niche_id not in picked]
    deferred.sort(key=lambda c: (c.priority, c.starvation_age), reverse=True)
    for i, c in enumerate(deferred, start=1):
        c.outcome = DEFERRED_BUDGET
        c.queue_position = i
    return list(rows.values())


def allocation_report(schedules: list[NicheSchedule], budget: int) -> dict:
    """Aggregate a pass into the measurement-allocation report."""
    def cnt(pred):
        return sum(1 for s in schedules if pred(s))
    autos = [s for s in schedules if s.origin == "auto"]
    sel = [s for s in schedules if s.outcome in (SEL_DEMAND, SEL_SUPPLY)]
    return {
        "budget": budget,
        "selected": len(sel),
        "selected_demand": cnt(lambda s: s.outcome == SEL_DEMAND),
        "selected_supply": cnt(lambda s: s.outcome == SEL_SUPPLY),
        "manual_selected": cnt(lambda s: s.outcome in (SEL_DEMAND, SEL_SUPPLY) and s.origin == "manual"),
        "auto_selected": cnt(lambda s: s.outcome in (SEL_DEMAND, SEL_SUPPLY) and s.origin == "auto"),
        "deferred": cnt(lambda s: s.outcome == DEFERRED_BUDGET),
        "cooldown": cnt(lambda s: s.outcome == WAIT_COOLDOWN),
        "evidence_sufficient": cnt(lambda s: s.outcome == EVIDENCE_SUFFICIENT),
        # aggregate niche-health metrics (auto niches only — the ones at risk)
        "auto_total": len(autos),
        "auto_zero_measurements": sum(1 for s in autos if s.demand_points == 0 and not s.has_supply),
        "auto_demand_only": sum(1 for s in autos if s.demand_points > 0 and not s.has_supply),
        "auto_supply_only": sum(1 for s in autos if s.demand_points == 0 and s.has_supply),
        "auto_demand_and_supply": sum(1 for s in autos if s.demand_points > 0 and s.has_supply),
        "auto_eligible": sum(1 for s in autos if s.stage == STAGE_SUFFICIENT),
        "max_starvation_age": max((s.starvation_age for s in schedules), default=0),
    }
