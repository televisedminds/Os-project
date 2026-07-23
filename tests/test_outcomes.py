"""Outcome learning — closing the loop from predicted opportunity to realized
truth. Realized metrics from actual cash only, prediction graded (never trained)
against reality, honest taxonomy, and recalibration that ONLY a real recorded
outcome can trigger, with a structured before→after audit.
"""

import pytest
from fastapi.testclient import TestClient

from opportunity_os import outcomes as oc
from opportunity_os.api import create_app
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.agents.learning import LearningEngine


def _opp(id="opp_t", category="cameras", type="product_arbitrage",
         sources=("ebay_us", "serper"), predicted_net=500.0, capital=540.0, conf=0.88):
    return {
        "id": id, "title": "Test", "type": type, "category": category,
        "confidence": conf, "verification_level": "multi_source_verified",
        "sources": list(sources),
        "verification": {"checks": [{"verifier": "PriceVerifier"}, {"verifier": "DemandVerifier"}]},
        "evidence": [{"label": "sold_comp_1"}, {"label": "sold_comp_2"}],
        "economics": {"kind": "flip", "capital_usd": capital, "total_net_usd": predicted_net,
                      "base": {"net_usd": predicted_net}, "pessimistic": {"net_usd": predicted_net * 0.8}},
    }


# ------------------------------------------------------------- realized metrics

def test_realized_metrics_from_actuals_only():
    m = oc.realized_metrics(actual_spend=450, actual_revenue=900, actual_fees=90,
                            actual_hours=5, days_taken=20, capital_usd=540)
    assert m["realized_profit_usd"] == 360.0          # 900 - 450 - 90
    assert m["actual_outlay_usd"] == 540.0            # 450 + 90
    assert m["roi"] == pytest.approx(0.6667, abs=1e-3)   # 360 / 540
    assert m["profit_per_hour_usd"] == 72.0           # 360 / 5
    assert m["capital_turnover"] == pytest.approx(1.667, abs=1e-2)  # 900 / 540
    assert m["basis"] == "realized_actuals"


def test_metrics_missing_inputs_stay_none_not_zero():
    m = oc.realized_metrics(actual_spend=100, actual_revenue=150)   # no hours/days/capital
    assert m["profit_per_hour_usd"] is None            # no hours -> not a fake 0
    assert m["capital_turnover_per_year"] is None       # no days
    assert m["realized_profit_usd"] == 50.0


def test_prediction_error_grades_prediction_against_reality():
    m = oc.realized_metrics(actual_spend=450, actual_revenue=900, actual_fees=90,
                            predicted_profit_usd=500)
    pe = m["prediction_error"]
    assert pe["error_usd"] == -140.0                   # realized 360 - predicted 500
    assert pe["direction"] == "over_predicted"
    assert pe["error_pct"] == -28.0
    # the prediction is only ever the graded value, never mixed into realized
    assert pe["realized_profit_usd"] == 360.0 and pe["predicted_profit_usd"] == 500.0


def test_no_profit_claimed_when_revenue_below_outlay():
    m = oc.realized_metrics(actual_spend=500, actual_revenue=300, actual_fees=40)
    assert m["realized_profit_usd"] == -240.0          # a loss, stated as a loss
    assert m["roi"] < 0


# ------------------------------------------------------------------- taxonomy

def test_status_taxonomy_and_completion_rules():
    assert set(oc.STATUSES) == {"bought", "sold", "delivered", "abandoned", "refunded", "failed"}
    assert not oc.is_terminal("bought") and oc.is_terminal("sold")
    for s in ("abandoned", "refunded", "failed"):
        assert oc.needs_reason(s)
        assert not oc.is_complete(s, None)             # terminal but missing reason
        assert oc.is_complete(s, "market moved")
    assert oc.is_complete("sold", None)                # sale needs no walk-away reason
    assert not oc.is_complete("bought", None)          # interim is never 'complete'


# -------------------------------------------- the only signal allowed to teach

def test_recalibration_signal_only_from_terminal_realized_cash():
    m_win = oc.realized_metrics(actual_spend=100, actual_revenue=300)   # +200
    m_loss = oc.realized_metrics(actual_spend=300, actual_revenue=100)  # -200
    assert oc.recalibration_signal("bought", m_win) is None             # interim -> no learning
    assert oc.recalibration_signal("sold", m_win)["result"] == "success"
    assert oc.recalibration_signal("sold", m_loss)["result"] == "failure"
    assert oc.recalibration_signal("delivered", m_win)["result"] == "success"
    assert oc.recalibration_signal("refunded", m_loss)["result"] == "failure"
    assert oc.recalibration_signal("abandoned", m_win, reason="no demand")["result"] == "abandoned"
    # every emitted signal is grounded in realized cash
    assert oc.recalibration_signal("sold", m_win)["basis"] == "realized_cash"


def test_predictions_cannot_train_predictions_guard():
    # a signal that isn't grounded in realized cash is refused at the boundary
    with pytest.raises(ValueError):
        oc.assert_realized_basis({"result": "success", "basis": "predicted"})
    # and a real signal passes through untouched
    ok = {"result": "success", "basis": "realized_cash"}
    assert oc.assert_realized_basis(ok) is ok


# ---------------------------------------------------- frozen provenance snapshot

def test_provenance_snapshot_captures_and_freezes_attribution():
    o = _opp()
    snap = oc.provenance_snapshot(o)
    assert snap["generator"] == "flip_generator"
    assert snap["sources"] == ["ebay_us", "serper"]
    assert "PriceVerifier" in snap["verifiers"]
    assert snap["predicted"]["confidence"] == 0.88
    assert snap["predicted"]["total_net_usd"] == 500.0
    # frozen: mutating the live opp afterwards does not rewrite the snapshot
    o["sources"].append("late_source")
    o["confidence"] = 0.1
    assert snap["sources"] == ["ebay_us", "serper"] and snap["predicted"]["confidence"] == 0.88


# --------------------------------------------- learning recalibration + audit

def test_success_recalibrates_and_records_before_after_audit(tmp_path):
    st = Store(tmp_path / "l.db")
    le = LearningEngine(st)
    prov = oc.provenance_snapshot(_opp())
    sig = oc.recalibration_signal("sold", oc.realized_metrics(actual_spend=100, actual_revenue=500))
    cal0 = le.calibration
    res = le.record_realized_outcome(prov, sig, opp_id="opp_t")
    assert res["result"] == "success"
    assert res["calibration"] == round(cal0 + 0.02, 4)
    assert res["changes"]["calibration"] == [cal0, res["calibration"]]
    assert res["changes"]["source_reliability"]["ebay_us"][1] > res["changes"]["source_reliability"]["ebay_us"][0]
    assert res["changes"]["category_affinity"]["cameras"] == [0, 1]
    trail = st.weight_audit_trail()
    assert len(trail) == 1 and trail[0]["result"] == "success" and trail[0]["trigger"] == "realized_outcome"


def test_abandoned_is_honest_no_cash_test_no_calibration_move(tmp_path):
    st = Store(tmp_path / "l.db")
    le = LearningEngine(st)
    prov = oc.provenance_snapshot(_opp(category="watches"))
    m = oc.realized_metrics(actual_spend=0, actual_revenue=0)     # walked away, nothing deployed
    sig = oc.recalibration_signal("abandoned", m, reason="demand")
    cal0 = le.calibration
    rel0 = le.source_reliability("ebay_us")
    res = le.record_realized_outcome(prov, sig, opp_id="opp_t")
    assert res["result"] == "abandoned"
    assert res["calibration"] == cal0                            # NOT moved — no cash test happened
    assert le.source_reliability("ebay_us") == rel0             # sources not penalised for a walk-away
    assert le.category_affinity("watches") == -1                # only the track record dips
    assert "calibration" not in res["changes"]


def test_failure_lowers_calibration_and_weights_reason_factor(tmp_path):
    st = Store(tmp_path / "l.db")
    le = LearningEngine(st)
    prov = oc.provenance_snapshot(_opp(category="audio"))
    sig = oc.recalibration_signal("failed", oc.realized_metrics(actual_spend=300, actual_revenue=50),
                                  reason="competition")
    cal0 = le.calibration
    w0 = dict(le.weights)
    res = le.record_realized_outcome(prov, sig, opp_id="opp_t")
    assert res["result"] == "failure"
    assert res["calibration"] == round(cal0 - 0.04, 4)
    assert le.weights["competition"] > w0["competition"]        # the failure reason weighs more now
    assert le.category_affinity("audio") == -1


def test_learning_refuses_non_realized_signal(tmp_path):
    st = Store(tmp_path / "l.db")
    le = LearningEngine(st)
    prov = oc.provenance_snapshot(_opp())
    with pytest.raises(ValueError):
        le.record_realized_outcome(prov, {"result": "success", "basis": "predicted"}, opp_id="x")


# ----------------------------------------------------------------- API surface

def _client(tmp_path, name):
    cfg = Config(db_path=tmp_path / name, auto_cycle_seconds=0)
    return TestClient(create_app(cfg, auto_cycle_seconds=0, seed_cycles=3))


def test_api_enforces_explicit_reason(tmp_path):
    with _client(tmp_path, "a.db") as c:
        oid = c.get("/api/opportunities").json()["opportunities"][0]["id"]
        assert c.post(f"/api/opportunities/{oid}/outcome/record",
                      json={"status": "abandoned"}).status_code == 422
        assert c.post(f"/api/opportunities/{oid}/outcome/record",
                      json={"status": "failed", "reason": ""}).status_code == 422
        assert c.post(f"/api/opportunities/{oid}/outcome/record",
                      json={"status": "not_a_status"}).status_code == 422
        # with a reason it's accepted
        r = c.post(f"/api/opportunities/{oid}/outcome/record",
                   json={"status": "abandoned", "reason": "demand faded"})
        assert r.status_code == 200 and r.json()["complete"]


def test_api_interim_bought_records_but_teaches_nothing(tmp_path):
    with _client(tmp_path, "b.db") as c:
        oid = c.get("/api/opportunities").json()["opportunities"][0]["id"]
        r = c.post(f"/api/opportunities/{oid}/outcome/record",
                   json={"status": "bought", "actual_spend_usd": 500}).json()
        assert r["recorded"] and not r["complete"] and r["scoring_change"] is None
        assert not c.get("/api/learning").json()["weight_audit"]      # no learning from interim


def test_api_terminal_sold_computes_compares_and_recalibrates(tmp_path):
    with _client(tmp_path, "c.db") as c:
        oid = c.get("/api/opportunities").json()["opportunities"][0]["id"]
        cal0 = c.get("/api/learning").json()["calibration"]
        r = c.post(f"/api/opportunities/{oid}/outcome/record",
                   json={"status": "sold", "actual_spend_usd": 500, "actual_revenue_usd": 900,
                         "actual_fees_usd": 80, "actual_hours": 6, "days_taken": 18}).json()
        assert r["metrics"]["realized_profit_usd"] == 320.0
        # projected and realized are kept explicitly separate
        assert "projected" in r["comparison"] and "realized" in r["comparison"]
        assert r["comparison"]["realized"]["profit_usd"] == 320.0
        assert r["scoring_change"]["result"] == "success"
        assert r["provenance"]["generator"]                          # provenance frozen with the outcome
        # a REAL outcome measurably moved future scoring
        assert c.get("/api/learning").json()["calibration"] != cal0
        assert c.get(f"/api/opportunities/{oid}").json()["status"] == "executed"


def test_api_get_outcome_and_learning_trail(tmp_path):
    with _client(tmp_path, "d.db") as c:
        oid = c.get("/api/opportunities").json()["opportunities"][0]["id"]
        assert c.get(f"/api/opportunities/{oid}/outcome").json() == {"recorded": False}
        c.post(f"/api/opportunities/{oid}/outcome/record",
               json={"status": "sold", "actual_spend_usd": 100, "actual_revenue_usd": 400,
                     "actual_hours": 3, "days_taken": 10})
        got = c.get(f"/api/opportunities/{oid}/outcome").json()
        assert got["recorded"] and got["outcome"]["status"] == "sold"
        assert "prediction_error" in got["comparison"]
        learn = c.get("/api/learning").json()
        assert learn["weight_audit"] and learn["realized_outcomes"]
        assert "category_affinity" in learn


def test_db_realized_outcome_roundtrip(tmp_path):
    st = Store(tmp_path / "e.db")
    m = oc.realized_metrics(actual_spend=10, actual_revenue=40)
    prov = oc.provenance_snapshot(_opp())
    rid = st.add_realized_outcome("opp_z", "sold", None, 10, 40, 0, 2, 5, m, prov, "note")
    assert rid == 1
    got = st.get_realized_outcome("opp_z")
    assert got["status"] == "sold" and got["metrics"]["realized_profit_usd"] == 30.0
    assert got["provenance"]["generator"] == "flip_generator"
    assert len(st.recent_realized_outcomes()) == 1
