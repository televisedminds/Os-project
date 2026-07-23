"""Milestone 4.1 — fair measurement scheduler.

Deterministic tests that the scarce Serper budget is allocated fairly and by
research stage: manual niches keep priority, auto-discovered niches get reserved
capacity and cannot be starved by deterministic ordering, demand/supply are
balanced by what unlocks the next stage, cooldowns and sufficiency are honoured,
and budget exhaustion produces explicit deferrals. Regression of M2/M3/watchlist/
physical behaviour is covered by the full suite.
"""

import pytest

from opportunity_os import measurement as ms
from opportunity_os.config import Config


def _cfg(**kw):
    base = dict(steady_min_demand_points=6, measure_auto_reserve_frac=0.5,
                measure_aging_coef=1.0, measure_manual_bonus=5.0, measure_cooldown_ticks=3)
    base.update(kw)
    return Config(**base)


def _n(id, origin="auto", dp=0, supply=False, stale=True, ld=None, ls=None, sc=0):
    return {"id": id, "origin": origin, "demand_points": dp, "has_supply": supply,
            "supply_stale": stale, "last_demand_tick": ld, "last_supply_tick": ls,
            "selection_count": sc}


def _by_id(scheds):
    return {s.niche_id: s for s in scheds}


# ---------------------------------------------------------- stage / need

def test_stage_and_need_transitions():
    cfg = _cfg()
    assert ms.stage_and_need(0, False, True, cfg) == (ms.STAGE_NO_DEMAND, ms.NEED_DEMAND)
    assert ms.stage_and_need(2, False, True, cfg) == (ms.STAGE_NO_SUPPLY, ms.NEED_SUPPLY)   # supply unlocks fastest
    assert ms.stage_and_need(3, True, False, cfg) == (ms.STAGE_GATHERING, ms.NEED_DEMAND)
    assert ms.stage_and_need(6, True, False, cfg) == (ms.STAGE_SUFFICIENT, ms.NEED_NONE)
    assert ms.stage_and_need(6, True, True, cfg) == (ms.STAGE_SUPPLY_STALE, ms.NEED_SUPPLY)
    assert ms.stage_and_need(0, True, False, cfg) == (ms.STAGE_NO_DEMAND, ms.NEED_DEMAND)   # no demand yet


# 12 & 13 — a niche progresses across stages as its evidence grows
def test_case12_13_progression_no_provenance_to_sufficient():
    cfg = _cfg()
    assert ms.stage_and_need(0, False, True, cfg)[0] == ms.STAGE_NO_DEMAND
    assert ms.stage_and_need(1, False, True, cfg)[0] == ms.STAGE_NO_SUPPLY        # got demand → needs supply
    assert ms.stage_and_need(1, True, False, cfg)[0] == ms.STAGE_GATHERING        # has supply → gather demand
    assert ms.stage_and_need(6, True, False, cfg)[0] == ms.STAGE_SUFFICIENT       # eligible


# ---------------------------------------------------------- fairness

def test_case2_auto_niches_get_reserved_capacity():
    # 4 saturated-ish manual niches all wanting a scan vs 2 auto niches, budget 4,
    # 50% reserved → at least 2 slots must go to auto even though manual has the
    # +bonus.
    cfg = _cfg()
    states = ([_n(f"watch_{i}", origin="manual", dp=2, supply=False, ld=48, sc=5) for i in range(4)]
              + [_n("disc_1", dp=2, supply=False, ld=48), _n("disc_2", dp=2, supply=False, ld=48)])
    sch = _by_id(ms.schedule(states, 4, cfg, 50))
    auto_sel = [s for s in sch.values() if s.origin == "auto" and s.outcome.startswith("selected")]
    assert len(auto_sel) >= 2


def test_case1_and_18_manual_niches_retain_priority():
    cfg = _cfg()
    # equal footing: one manual, one auto, both need demand, same age. Manual's
    # baseline bonus must give it a selectable priority (not zeroed out).
    states = [_n("watch", origin="manual", dp=1, supply=True, ld=40),
              _n("disc", origin="auto", dp=1, supply=True, ld=40)]
    sch = _by_id(ms.schedule(states, 2, cfg, 50))
    assert sch["watch"].priority > 0 and sch["watch"].outcome == ms.SEL_DEMAND


def test_case3_8_long_waiting_auto_outranks_repeatedly_scanned_hot_niche():
    cfg = _cfg()
    # hot manual niche scanned recently but past cooldown (age 5); auto niche
    # never scanned (age huge). Aging must lift the starved auto niche above it.
    states = [_n("hot", origin="manual", dp=3, supply=True, ld=45, sc=30),
              _n("starved", origin="auto", dp=3, supply=True, ld=None, sc=0)]
    sch = _by_id(ms.schedule(states, 1, cfg, 50))
    assert sch["starved"].outcome == ms.SEL_DEMAND          # aging beats hot niche
    assert sch["hot"].outcome == ms.DEFERRED_BUDGET


def test_case7_one_niche_cannot_consume_budget_repeatedly():
    cfg = _cfg()
    # After a niche is selected, its selection_count rises; a peer with fewer
    # selections and equal priority should win the next comparable slot.
    states = [_n("greedy", dp=3, supply=True, ld=40, sc=20),
              _n("fresh", dp=3, supply=True, ld=40, sc=0)]
    sch = _by_id(ms.schedule(states, 1, cfg, 50))
    assert sch["fresh"].outcome == ms.SEL_DEMAND           # fewer prior selections wins the tie


# ---------------------------------------------------------- cooldown / dedup

def test_case4_niche_in_cooldown_not_selected():
    cfg = _cfg(measure_cooldown_ticks=5)
    states = [_n("cool", dp=2, supply=True, ld=48)]        # demand scanned 2 ticks ago (<5)
    sch = _by_id(ms.schedule(states, 4, cfg, 50))
    assert sch["cool"].outcome == ms.WAIT_COOLDOWN


def test_case10_11_sufficient_or_fresh_evidence_is_not_rescanned():
    cfg = _cfg()
    # sufficient niche: no scan spent. fresh-demand niche: cooldown, no scan.
    states = [_n("done", dp=6, supply=True, stale=False, ld=40, ls=40),
              _n("fresh", dp=2, supply=True, ld=49)]
    sch = _by_id(ms.schedule(states, 4, cfg, 50))
    assert sch["done"].outcome == ms.EVIDENCE_SUFFICIENT
    assert sch["fresh"].outcome == ms.WAIT_COOLDOWN
    rep = ms.allocation_report(list(sch.values()), 4)
    assert rep["selected"] == 0                            # no requests wasted


# ---------------------------------------------------------- demand/supply balance

def test_case5_demand_without_supply_prioritised_for_supply():
    cfg = _cfg()
    sch = _by_id(ms.schedule([_n("d", dp=3, supply=False, ld=40)], 1, cfg, 50))
    assert sch["d"].outcome == ms.SEL_SUPPLY


def test_case6_supply_without_demand_prioritised_for_demand():
    cfg = _cfg()
    sch = _by_id(ms.schedule([_n("s", dp=0, supply=True, ld=None)], 1, cfg, 50))
    assert sch["s"].outcome == ms.SEL_DEMAND


# ---------------------------------------------------------- deferral / report

def test_case9_budget_exhaustion_produces_explicit_deferrals():
    cfg = _cfg()
    states = [_n(f"disc_{i}", dp=2, supply=False, ld=40) for i in range(6)]
    sch = ms.schedule(states, 2, cfg, 50)
    by = _by_id(sch)
    deferred = [s for s in sch if s.outcome == ms.DEFERRED_BUDGET]
    assert len(deferred) == 4
    assert all(s.queue_position and s.priority_reason for s in deferred)   # explicit reason + queue slot
    assert sorted(s.queue_position for s in deferred) == [1, 2, 3, 4]


def test_allocation_report_metrics():
    cfg = _cfg()
    states = [_n("a", dp=0, supply=False), _n("b", dp=3, supply=False, ld=40),
              _n("c", dp=6, supply=True, stale=False, ld=40, ls=40),
              _n("m", origin="manual", dp=6, supply=True, stale=False, ld=40, ls=40)]
    rep = ms.allocation_report(ms.schedule(states, 2, cfg, 50), 2)
    assert rep["auto_total"] == 3 and rep["auto_eligible"] == 1
    assert rep["auto_zero_measurements"] == 1 and rep["auto_demand_only"] == 1
    assert rep["evidence_sufficient"] == 2                # c (auto) + m (manual)


# 14 & 15 — an evidence-sufficient discovered niche reaches the council
def test_case14_15_sufficient_discovered_niche_reaches_council_with_verdict(tmp_path):
    """The end of the chain: a niche the scheduler marks evidence_sufficient is
    steady-eligible, routes to the correct generator, and the SAME council
    returns an explicit verdict (a rejection is acceptable)."""

    from opportunity_os.db import Store
    from opportunity_os.agents.investigator import Investigator
    from opportunity_os.agents.verifiers import VerificationCouncil
    from opportunity_os.generators import GenContext, LocalServiceGenerator
    from opportunity_os import steady as st

    class _DS:
        tick_no = 50
        def __init__(self, niche, mentions):
            self._n, self._m = niche, mentions
        def niches(self): return [self._n]
        def social_sources(self): return ["serper"]
        def mentions(self, nid, src): return self._m if src == "serper" else []
        def niche_history(self, nid): return [dict(self._n["metrics"]) for _ in range(10)]
        def event_log(self, nid): return []

    m = {"volume": 3000.0, "growth_pct": 0.0, "solution_count": 2, "demand_posts": 300.0,
         "providers": 2, "observed": {"demand": "observed", "supply": "observed",
                                      "demand_series": "serper", "price": "estimated"}}
    niche = {"id": "disc_n_th_gap", "name": "TH auto-discovered gap", "kind": "local",
             "geo": "TH", "price_point_usd": 32.0, "metrics": m}
    ds = _DS(niche, [40] * 8)                              # 8 demand points, observed supply
    s = Store(tmp_path / "m41.db")
    s.add_search_obs("disc_n_th_gap", "serper", "supply", 50, "th gap",
                     {"provider_count": 2, "providers": [{"domain": "a.co.th"}]})

    cfg = _cfg()
    entries, assessments = st.build_venture_entries(ds, s, cfg, 50, {})
    assert "disc_n_th_gap" in entries and entries["disc_n_th_gap"]["entry_path"] == "steady_state"
    ctx = GenContext(ds=ds, anomalies=[], by_entity={}, niche_ids={"disc_n_th_gap"},
                     store=s, cfg=cfg, investigator=Investigator(cfg), graph=None,
                     venture_entries=entries)
    cands = LocalServiceGenerator().generate(ctx)
    assert len(cands) == 1                                 # correct generator produced it
    v = VerificationCouncil(cfg).verify_venture(ds, cands[0])
    assert isinstance(v.passed, bool) and v.checks         # an explicit council verdict exists
