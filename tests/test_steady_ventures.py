"""Milestone 2 — steady-state venture entry path.

A venture niche with steady demand never fires an anomaly, so it used to be
silently non-evaluated. These tests prove the second entry path: a well-observed
niche enters the SAME council without an anomaly, entry paths are deduplicated
and recorded, and eligibility is never approval — flat demand is still rejected
honestly.
"""

import time

import pytest

from opportunity_os import steady as st
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.models import Anomaly, AnomalyKind, OppType
from opportunity_os.generators import GenContext, LocalServiceGenerator, InfoProductGenerator
from opportunity_os.agents.investigator import Investigator


# --------------------------------------------------------- fake data source

class _NicheDS:
    """A live-like data source over one niche, with a controllable mention
    series (the demand-points meter) and observed provenance."""

    tick_no = 20

    def __init__(self, niche, mentions):
        self._niche = niche
        self._mentions = mentions

    def niches(self):
        return [self._niche]

    def social_sources(self):
        return ["serper"]

    def mentions(self, nid, src):
        return self._mentions if src == "serper" else []

    def niche_history(self, nid):
        return [dict(self._niche["metrics"]) for _ in range(12)]

    def event_log(self, nid):
        return []


def _niche(nid="bkk_clean", kind="local", providers=2, demand_posts=150.0,
           volume=300.0, growth=0.0, supply="observed", demand="user_supplied"):
    m = {"volume": volume, "growth_pct": growth, "solution_count": providers,
         "demand_posts": demand_posts, "providers": providers,
         "observed": {"demand": demand, "supply": supply,
                      "demand_series": "serper", "price": "estimated"}}
    return {"id": nid, "name": "Airbnb turnover cleaning", "kind": kind, "geo": "TH",
            "price_point_usd": 32.0, "metrics": m}


def _store(tmp_path, fresh_niche_id=None):
    s = Store(tmp_path / "m2.db")
    if fresh_niche_id:                      # a fresh supply observation → freshness OK
        s.add_search_obs(fresh_niche_id, "serper", "supply", 20, "bangkok airbnb cleaning",
                         {"provider_count": 2, "providers": [{"domain": "a.com"}]})
    return s


def _cfg():
    return Config(steady_venture_enabled=True, steady_min_demand_points=6,
                  steady_require_observed_supply=True, steady_freshness_days=14,
                  steady_cooldown_ticks=6, steady_max_per_cycle=12)


# ------------------------------------------------------------- eligibility

def test_case2_below_min_demand_is_still_gathering(tmp_path):
    ds = _NicheDS(_niche(), mentions=[9, 10, 11])       # 3 < 6 points
    a = st.assess_ventures(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)[0]
    assert not a.eligible and a.reason == st.R_GATHERING
    assert a.evidence["demand_points"] == 3


def test_case3_no_observed_supply_is_ineligible(tmp_path):
    ds = _NicheDS(_niche(supply="unknown"), mentions=[10] * 12)
    a = st.assess_ventures(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)[0]
    assert not a.eligible and a.reason == st.R_NO_SUPPLY


def test_case4_sufficient_demand_and_supply_is_eligible(tmp_path):
    ds = _NicheDS(_niche(), mentions=[10] * 12)         # 12 >= 6, supply observed, fresh
    a = st.assess_ventures(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)[0]
    assert a.eligible and a.reason == st.R_ELIGIBLE
    assert a.evidence["demand_points"] == 12 and a.evidence["supply_provenance"] == "observed"


def test_simulated_niche_never_eligible(tmp_path):
    # No observed provenance (demo/simulated) → steady path is live-only.
    ds = _NicheDS(_niche(demand="simulated"), mentions=[10] * 12)
    a = st.assess_ventures(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)[0]
    assert not a.eligible and a.reason == st.R_SIMULATED


def test_stale_observations_ineligible(tmp_path):
    # store with NO fresh obs → freshness unknown → stale
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    a = st.assess_ventures(ds, _store(tmp_path), _cfg(), 20)[0]
    assert not a.eligible and a.reason == st.R_STALE


# ---------------------------------------------------------------- entry paths

def test_case1_anomaly_niche_still_enters(tmp_path):
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    by_entity = {"bkk_clean": [Anomaly(AnomalyKind.DEMAND_ACCELERATION, "bkk_clean",
                                       2.5, "demand accelerating", {})]}
    entries, _ = st.build_venture_entries(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20, by_entity)
    assert entries["bkk_clean"]["entry_path"] == "both"     # anomaly AND steady-eligible


def test_case5_both_paths_dedupe_to_one_entry(tmp_path):
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    by_entity = {"bkk_clean": [Anomaly(AnomalyKind.DEMAND_ACCELERATION, "bkk_clean",
                                       2.5, "spike", {})]}
    entries, _ = st.build_venture_entries(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20, by_entity)
    assert list(entries) == ["bkk_clean"]                  # evaluated once, not twice
    assert entries["bkk_clean"]["entry_path"] == "both"


def test_steady_only_entry_path(tmp_path):
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    entries, _ = st.build_venture_entries(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20, {})
    assert entries["bkk_clean"]["entry_path"] == "steady_state"
    assert entries["bkk_clean"]["anomalies"] == []


def _simulate_cycle(s, ds, cfg, tick):
    """Replicate investigator.build_candidates' ledger writes for one cycle: it
    records EVERY assessment (entered AND skipped)."""
    a = st.assess_ventures(ds, s, cfg, tick)[0]
    s.record_venture_eval(a.nid, tick, "steady_state", a.family, eligible=a.eligible,
                          evidence=a.evidence, reason=(None if a.eligible else a.reason))
    return a


def test_cooldown_naturally_expires_despite_skip_rows(tmp_path):
    """Regression: cooldown-skip rows are written to the ledger every cycle. If
    the cooldown reference is the NEWEST row, each skip resets the clock and an
    unchanged niche starves in cooldown forever. It must instead reference the
    last ACTUAL evaluation, so a 6-tick cooldown from tick 18 expires at 24."""

    s = _store(tmp_path, "bkk_clean")
    ds = _NicheDS(_niche(), mentions=[10] * 12)          # unchanged observations
    cfg = _cfg()                                         # cooldown = 6 ticks
    a18 = _simulate_cycle(s, ds, cfg, 18)
    assert a18.eligible                                  # first eval enters
    for tick in range(19, 24):                          # 19..23: within cooldown
        a = _simulate_cycle(s, ds, cfg, tick)
        assert not a.eligible and a.reason == st.R_COOLDOWN, f"tick {tick}"
    a24 = _simulate_cycle(s, ds, cfg, 24)               # 24-18 == 6 → expired
    assert a24.eligible, "cooldown must expire at tick 24, not starve forever"


def test_cooldown_skip_rows_do_not_reset_the_evaluation_clock(tmp_path):
    s = _store(tmp_path, "bkk_clean")
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    cfg = _cfg()
    _simulate_cycle(s, ds, cfg, 18)                     # real eval at 18
    for tick in range(19, 23):
        _simulate_cycle(s, ds, cfg, tick)              # skips at 19..22
    # The clock must still point at tick 18, so at tick 25 (>6 later) it's open.
    a25 = st.assess_ventures(ds, s, cfg, 25)[0]
    assert a25.eligible


def test_cooldown_blocks_reevaluation_without_new_observations(tmp_path):
    s = _store(tmp_path, "bkk_clean")
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    # record a recent eval at tick 18 with the same demand points
    s.record_venture_eval("bkk_clean", 18, "steady_state", "local_service", True,
                          {"demand_points": 12, "supply_count": 2, "source_count": 2,
                           "freshness_days": 1.0, "demand_supply_ratio": 75.0})
    a = st.assess_ventures(ds, s, _cfg(), 20)[0]            # tick 20, cooldown 6 → 20-18<6
    assert not a.eligible and a.reason == st.R_COOLDOWN
    # a longer demand series (new observations) lifts the cooldown immediately
    ds2 = _NicheDS(_niche(), mentions=[10] * 14)
    a2 = st.assess_ventures(ds2, s, _cfg(), 20)[0]
    assert a2.eligible


# ------------------------------------------------------ council integration

def _entries_ctx(ds, store, cfg, tick, by_entity=None):
    inv = Investigator(cfg)
    entries, _ = st.build_venture_entries(ds, store, cfg, tick, by_entity or {})
    return GenContext(ds=ds, anomalies=[], by_entity=by_entity or {}, niche_ids={ds.niches()[0]["id"]},
                      store=store, cfg=cfg, investigator=inv, graph=None, venture_entries=entries)


def test_case6_steady_flat_demand_reaches_council_and_is_rejected(tmp_path):
    """The point of M2: a flat steady niche is EVALUATED (not silently skipped)
    and honestly rejected as insufficient_demand — a real verdict, not silence."""
    from opportunity_os.agents.verifiers import VerificationCouncil
    from opportunity_os.evidence import classify_rejection, INSUFFICIENT_DEMAND
    ds = _NicheDS(_niche(growth=0.0), mentions=[10] * 13)   # flat, plentiful gap
    ctx = _entries_ctx(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)
    cands = LocalServiceGenerator().generate(ctx)
    assert len(cands) == 1 and cands[0]["entry_path"] == "steady_state"
    v = VerificationCouncil(_cfg()).verify_venture(ds, cands[0])
    dc = next(c for c in v.checks if c.verifier == "demand_corroboration")
    assert not dc.passed and not v.passed
    assert classify_rejection(f"{dc.name} failed — {dc.evidence}") == INSUFFICIENT_DEMAND


def test_case7_high_competition_steady_niche_classifies_high_competition():
    from opportunity_os.evidence import classify_rejection, HIGH_COMPETITION
    served = ("Supply-side gap confirmed failed — 40 providers vs 60 demand posts/mo. "
              "Observed via Google supply scan: a.com, b.com.")
    assert classify_rejection(served) == HIGH_COMPETITION


def test_case8_qualifying_steady_niche_can_create_candidate(tmp_path):
    # growing demand + real gap → the candidate is created AND could verify
    ds = _NicheDS(_niche(growth=15.0, providers=1, demand_posts=200.0), mentions=[4, 6, 9, 13, 20, 30])
    ctx = _entries_ctx(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)
    cands = LocalServiceGenerator().generate(ctx)
    assert len(cands) == 1
    assert cands[0]["kind"] == "venture" and cands[0]["entry_path"] == "steady_state"


# ------------------------------------------------------ metadata persistence

def test_case9_entry_path_metadata_persists(tmp_path):
    s = _store(tmp_path, "bkk_clean")
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    inv = Investigator(_cfg())
    inv.build_candidates(ds, [], store=s, graph=None)
    row = s.last_venture_eval("bkk_clean")
    assert row["entry_path"] == "steady_state" and row["eligible"] == 1
    assert row["demand_points"] == 12 and row["family"] == "local_service"
    summary = s.venture_eval_summary(10)
    assert summary["entrants"]["steady_state"] == 1 and summary["deduped_total"] == 1


def test_no_silent_zero_skips_are_recorded(tmp_path):
    """A niche that is NOT eligible still leaves an explicit reason, never a
    silent zero."""
    s = _store(tmp_path)                                  # no fresh obs → stale
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    Investigator(_cfg()).build_candidates(ds, [], store=s, graph=None)
    row = s.last_venture_eval("bkk_clean")
    assert row["eligible"] == 0 and row["reason"] == st.R_STALE


# ----------------------------------------------------- classification routing

def test_thai_local_service_routes_to_local_service_not_physical(tmp_path):
    from opportunity_os import discovery as disc
    # The exact production Thai phrase must classify local (upstream of steady).
    assert disc.infer_niche_kind("ช่วยแนะนำร้าน/โรงงานทำเฟอร์นิเจอร์ไม้แท้") == "local"
    ds = _NicheDS(_niche(kind="local"), mentions=[10] * 12)
    a = st.assess_ventures(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)[0]
    assert a.family == "local_service"                    # family, not physical
    ctx = _entries_ctx(ds, _store(tmp_path, "bkk_clean"), _cfg(), 20)
    assert len(LocalServiceGenerator().generate(ctx)) == 1     # local generator owns it
    assert InfoProductGenerator().generate(ctx) == []          # info does not


def test_info_product_routes_to_info_family(tmp_path):
    ds = _NicheDS(_niche(nid="dtv_guide", kind="info"), mentions=[10] * 12)
    a = st.assess_ventures(ds, _store(tmp_path, "dtv_guide"), _cfg(), 20)[0]
    assert a.family == "info"
    ctx = _entries_ctx(ds, _store(tmp_path, "dtv_guide"), _cfg(), 20)
    assert len(InfoProductGenerator().generate(ctx)) == 1
    assert LocalServiceGenerator().generate(ctx) == []


def test_b2b_workflow_does_not_become_physical(tmp_path):
    from opportunity_os import diversity as dv
    ds = _NicheDS(_niche(nid="ev_wallbox", kind="b2b"), mentions=[10] * 12)
    a = st.assess_ventures(ds, _store(tmp_path, "ev_wallbox"), _cfg(), 20)[0]
    assert a.family == "b2b"
    assert a.family != dv.opp_type_family("product_arbitrage")   # never physical


# ------------------------------------------------- ledger semantics (fix)

def test_anomaly_only_entry_unaffected_when_not_steady_eligible(tmp_path):
    from opportunity_os.models import Anomaly, AnomalyKind
    # Only 3 demand points → NOT steady-eligible, but an anomaly fired.
    ds = _NicheDS(_niche(), mentions=[9, 10, 11])
    by_entity = {"bkk_clean": [Anomaly(AnomalyKind.DEMAND_ACCELERATION, "bkk_clean",
                                       2.5, "spike", {})]}
    entries, assessments = st.build_venture_entries(
        ds, _store(tmp_path, "bkk_clean"), _cfg(), 20, by_entity)
    assert entries["bkk_clean"]["entry_path"] == "anomaly"       # still enters via anomaly
    assert entries["bkk_clean"]["steady_evidence"] is None
    assert not assessments[0].eligible                          # steady path declined it


def test_ledger_distinguishes_entered_from_skipped(tmp_path):
    s = _store(tmp_path, "bkk_clean")
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    cfg = _cfg()
    _simulate_cycle(s, ds, cfg, 18)                    # entered (eligible=1)
    _simulate_cycle(s, ds, cfg, 19)                    # cooldown skip (eligible=0)
    _simulate_cycle(s, ds, cfg, 20)                    # cooldown skip (eligible=0)
    newest = s.last_venture_eval("bkk_clean")
    entered = s.last_entered_venture_eval("bkk_clean")
    assert newest["tick"] == 20 and newest["eligible"] == 0     # newest is a skip
    assert entered["tick"] == 18 and entered["eligible"] == 1   # last real eval


def test_summary_counts_with_entered_and_skipped(tmp_path):
    s = _store(tmp_path, "bkk_clean")
    ds = _NicheDS(_niche(), mentions=[10] * 12)
    cfg = _cfg()
    for tick in range(18, 21):                          # 18 entered, 19-20 skipped
        _simulate_cycle(s, ds, cfg, tick)
    summ = s.venture_eval_summary(10)
    assert summ["eligible"] == 1 and summ["skipped"] == 2
    assert summ["entrants"]["steady_state"] == 1 and summ["deduped_total"] == 1
    assert summ["verdicts"].get(st.R_COOLDOWN) == 2    # skips carry their reason
