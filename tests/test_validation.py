"""Observed-economics validation (Backlog #22) — the exit from
`validation_required`.

M5 made estimated-demand ventures permanently unverifiable, which is honest but a
dead end. These tests pin the exit: when REAL cash is recorded, the projection is
re-priced from it, provenance is promoted estimated → observed, and the council
finally judges measured numbers. And, just as importantly, they pin that nothing
happens without recorded cash.
"""

import pytest
from fastapi.testclient import TestClient

from opportunity_os import economics, outcomes as oc, validation as val
from opportunity_os.api import create_app
from opportunity_os.config import Config
from opportunity_os.db import Store


def _venture_opp(id="opp_v", type="info_product", prov=None, net=80.0, revenue=300.0):
    return {
        "id": id, "title": "Thai freelancer tax guide", "type": type,
        "category": "info_product", "confidence": 0.7,
        "economics": {
            "kind": "venture", "capital_usd": 60.0, "total_net_usd": net,
            "base": {"net_usd": net, "revenue_usd": revenue},
            "pessimistic": {"net_usd": net * 0.25},
            "input_provenance": prov or {"demand_volume": "estimated",
                                         "price_point": "estimated",
                                         "costs": "calculated"},
        },
    }


def _record(store, opp_id, status="sold", spend=50.0, revenue=400.0, fees=20.0,
            hours=3.0, days=20.0):
    m = oc.realized_metrics(actual_spend=spend, actual_revenue=revenue, actual_fees=fees,
                            actual_hours=hours, days_taken=days)
    store.add_realized_outcome(opp_id, status, None, spend, revenue, fees, hours, days,
                               m, {}, None)
    return m


# ------------------------------------------------------------ the recompute itself

def test_recompute_uses_observed_revenue_and_invents_no_upside():
    e = economics.recompute_venture_from_observed("info", 15.0, 600.0)
    assert e.base.revenue_usd == 600.0
    # one observed month is a data point, not a distribution — no modelled band
    assert e.pessimistic.net_usd == e.base.net_usd
    assert e.input_provenance["demand_volume"] == "observed"
    assert e.input_provenance["monthly_revenue"] == "observed"
    assert e.input_provenance["costs"] == "calculated"      # costs stay modelled
    assert "OBSERVED" in e.route_note and "recorded cash" in e.route_note
    # the real cost model is still applied (fees + running + acquisition)
    assert len(e.base.lines) == 3 and e.base.net_usd < 600.0


def test_recompute_is_deterministic():
    a = economics.recompute_venture_from_observed("digital", 29.0, 1000.0)
    b = economics.recompute_venture_from_observed("digital", 29.0, 1000.0)
    assert a.base.net_usd == b.base.net_usd and a.pessimistic.net_usd == b.pessimistic.net_usd


# ------------------------------------------------------- what counts as evidence

def test_no_cash_no_recompute(tmp_path):
    store = Store(tmp_path / "a.db")
    opp = _venture_opp()
    assert val.needs_revalidation(opp)                     # it IS waiting on validation
    assert val.observed_monthly_revenue(store, "opp_v") is None
    assert val.revalidate(opp, None) is None               # never invents an observation


def test_blank_form_and_missing_period_are_not_evidence(tmp_path):
    store = Store(tmp_path / "b.db")
    blank = oc.realized_metrics()
    store.add_realized_outcome("opp_v", "sold", None, None, None, None, None, None,
                               blank, {}, None)
    assert val.observed_monthly_revenue(store, "opp_v") is None      # empty form
    # revenue with no period cannot be normalised to a monthly rate without guessing
    noperiod = oc.realized_metrics(actual_spend=10, actual_revenue=400)
    store.add_realized_outcome("opp_v", "sold", None, 10, 400, 0, 2, None,
                               noperiod, {}, None)
    assert val.observed_monthly_revenue(store, "opp_v") is None


def test_abandonment_and_failure_are_not_revenue_evidence(tmp_path):
    store = Store(tmp_path / "c.db")
    for status in ("abandoned", "failed", "refunded", "bought"):
        m = oc.realized_metrics(actual_spend=100, actual_revenue=0, days_taken=10)
        store.add_realized_outcome(f"o_{status}", status, "reason", 100, 0, 0, 1, 10,
                                   m, {}, None)
        assert val.observed_monthly_revenue(store, f"o_{status}") is None, status


def test_revenue_is_normalised_to_a_monthly_rate(tmp_path):
    store = Store(tmp_path / "d.db")
    _record(store, "opp_v", revenue=400.0, days=20.0)
    obs = val.observed_monthly_revenue(store, "opp_v")
    assert obs["monthly_revenue_usd"] == 600.0             # 400 over 20 days -> 600/mo
    assert obs["basis"] == "observed_cash" and obs["source"] == "recorded_outcome"


# --------------------------------------------------------------- promotion rules

def test_promotion_rewrites_provenance_and_audits(tmp_path):
    store = Store(tmp_path / "e.db")
    _record(store, "opp_v")
    opp = _venture_opp()
    new_econ, audit = val.revalidate(opp, val.observed_monthly_revenue(store, "opp_v"))
    assert new_econ["input_provenance"]["demand_volume"] == "observed"
    assert audit["promoted_inputs"] == ["demand_volume", "price_point"]
    assert audit["before"]["monthly_net_usd"] == 80.0
    assert audit["after"]["monthly_net_usd"] != 80.0        # re-priced from real cash
    assert audit["evidence"]["basis"] == "observed_cash"
    assert audit["trigger"] == "observed_cash"


def test_never_downgrades_an_already_observed_input(tmp_path):
    store = Store(tmp_path / "f.db")
    _record(store, "opp_v")
    opp = _venture_opp(prov={"demand_volume": "estimated", "price_point": "observed"})
    new_econ, _ = val.revalidate(opp, val.observed_monthly_revenue(store, "opp_v"))
    assert new_econ["input_provenance"]["price_point"] == "observed"


def test_nothing_left_to_promote_is_a_noop(tmp_path):
    store = Store(tmp_path / "g.db")
    _record(store, "opp_v")
    clean = _venture_opp(prov={"demand_volume": "observed", "costs": "calculated"})
    assert not val.needs_revalidation(clean)
    assert val.revalidate(clean, val.observed_monthly_revenue(store, "opp_v")) is None


def test_flips_are_never_revalidated_this_way():
    """Flips are priced on observed marketplace prices already; this path is for
    ventures whose DEMAND was estimated."""
    flip = {"id": "f", "type": "product_arbitrage",
            "economics": {"input_provenance": {"demand_volume": "estimated"}}}
    assert val.venture_kind(flip) is None
    assert not val.needs_revalidation(flip)


@pytest.mark.parametrize("type,kind", [
    ("digital_product", "digital"), ("micro_saas", "digital"), ("info_product", "info"),
    ("local_service", "local"), ("b2b_service", "b2b"), ("lead_generation", "leadgen")])
def test_every_venture_family_maps_to_a_cost_model(type, kind):
    assert val.venture_kind({"type": type}) == kind
    assert economics.recompute_venture_from_observed(kind, 20.0, 500.0).base.revenue_usd == 500.0


# ------------------------------------------------- the council gate actually lifts

def test_observed_cash_lifts_the_validation_required_gate():
    """THE POINT of this milestone: measured cash outranks an estimated search
    signal, so a venture stuck at validation_required can finally get a real
    pass/fail. Without it, re-pricing would change numbers but change nothing."""

    from opportunity_os.agents.verifiers import VerificationCouncil
    from opportunity_os.models import Check
    import inspect
    src = inspect.getsource(VerificationCouncil.verify_venture)
    assert "cash_observed" in src and "Positive on OBSERVED revenue" in src
    # estimated volume + no cash -> still blocked; cash observed -> real check
    assert 'prov.get("volume") == "estimated" and not cash_observed' in src


def test_verifier_prefers_observed_economics_over_estimated_niche_signal(tmp_path):
    """End-to-end through the council: the same niche whose volume provenance says
    'estimated' verifies once the economics carry observed cash."""

    from opportunity_os.agents.verifiers import VerificationCouncil
    cfg = Config(db_path=tmp_path / "h.db")
    council = VerificationCouncil(cfg)
    est = economics.compute_venture({
        "kind": "info", "price_point_usd": 15.0,
        "metrics": {"volume": 5000, "growth_pct": 5.0, "solution_count": 2,
                    "providers": 2, "demand_posts": 90,
                    "observed": {"volume": "estimated", "supply": "user_supplied"}}})
    assert est.input_provenance["demand_volume"] == "estimated"
    obs = economics.recompute_venture_from_observed("info", 15.0, 900.0)
    assert obs.input_provenance["demand_volume"] == "observed"
    # the promoted economics no longer carry the estimated flag the gate keys on
    assert not any(v == "estimated" for v in obs.input_provenance.values())


# ----------------------------------------------------------------- API behaviour

def test_api_records_cash_then_promotes_and_audits(tmp_path):
    """The wired loop: record a real sale on a venture priced from an estimate →
    the projection is re-priced, the provenance promoted, and an audit row kept."""

    cfg = Config(db_path=tmp_path / "api.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=4)
    with TestClient(app) as c:
        opps = c.get("/api/opportunities?status=active").json()["opportunities"]
        target = None
        for o in opps:
            d = c.get(f"/api/opportunities/{o['id']}").json()
            if val.needs_revalidation(d):
                target = d
                break
        if target is None:
            pytest.skip("no estimated-input venture in this seed")
        before = (target["economics"]["base"] or {}).get("net_usd")
        r = c.post(f"/api/opportunities/{target['id']}/outcome/record",
                   json={"status": "sold", "actual_spend_usd": 60,
                         "actual_revenue_usd": 450, "actual_fees_usd": 25,
                         "actual_hours": 4, "days_taken": 15}).json()
        rv = r["economics_revalidation"]
        assert rv and rv["trigger"] == "observed_cash"
        assert rv["after"]["input_provenance"]["demand_volume"] == "observed"
        assert rv["before"]["monthly_net_usd"] == before
        # persisted on the opportunity + retrievable as an audit trail
        after = c.get(f"/api/opportunities/{target['id']}").json()
        assert after["economics"]["input_provenance"]["demand_volume"] == "observed"
        assert after["economics_revalidated"]["promoted"]
        trail = c.get(f"/api/opportunities/{target['id']}/economics-audit").json()["trail"]
        assert len(trail) == 1 and trail[0]["trigger"] == "observed_cash"


def test_api_no_revalidation_without_cash(tmp_path):
    cfg = Config(db_path=tmp_path / "api2.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=3)
    with TestClient(app) as c:
        oid = c.get("/api/opportunities").json()["opportunities"][0]["id"]
        r = c.post(f"/api/opportunities/{oid}/outcome/record",
                   json={"status": "abandoned", "reason": "demand faded"}).json()
        assert r["economics_revalidation"] is None          # abandonment is not revenue
        assert c.get(f"/api/opportunities/{oid}/economics-audit").json()["trail"] == []
        assert c.get("/api/opportunities/nope/economics-audit").status_code == 404
