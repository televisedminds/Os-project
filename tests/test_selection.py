"""Selection — today's best 1–3 moves, ranked by conservative expected realized
value, execution-ready separated from validation-required."""

import pytest
from fastapi.testclient import TestClient

from opportunity_os import selection as sel
from opportunity_os.api import create_app
from opportunity_os.config import Config


def _opp(id, kind="flip", pess_net=100.0, base_net=120.0, qty=1, conf=0.9,
         level="multi_source_verified", single=False, otype="product_arbitrage",
         category="collectibles", route_kind="dislocation", ip=None, capital=50.0):
    return {
        "id": id, "title": id, "type": otype, "category": category,
        "confidence": conf, "verification_level": level, "single_source": single,
        "window_days": 20, "route": {"kind": route_kind},
        "economics": {"kind": kind, "qty": qty, "capital_usd": capital,
                      "base": {"net_usd": base_net}, "pessimistic": {"net_usd": pess_net},
                      "input_provenance": ip or {"buy_price": "observed", "projected_net": "calculated"}},
    }


# ------------------------------------------------------------ ranking maths

def test_expected_value_is_conservative_and_risk_adjusted():
    strong = _opp("strong", pess_net=100, conf=0.9, level="multi_source_verified")
    weak = _opp("weak", pess_net=100, conf=0.9, level="partially_verified", single=True)
    # same worst-case net + confidence, but weaker evidence → lower expected value
    assert sel.expected_value_usd(strong) > sel.expected_value_usd(weak)
    # a negative pessimistic case contributes nothing (clamped at 0)
    assert sel.expected_value_usd(_opp("neg", pess_net=-20)) == 0.0


def test_estimated_input_discounts_evidence_quality():
    clean = _opp("c", ip={"buy_price": "observed", "projected_net": "calculated"})
    est = _opp("e", ip={"demand_volume": "estimated", "projected_net": "calculated"})
    assert sel.evidence_quality(est) < sel.evidence_quality(clean)


def test_rank_dedupes_by_thesis_and_takes_top_n():
    opps = [
        _opp("a1", pess_net=200, category="cameras", route_kind="dislocation"),
        _opp("a2", pess_net=180, category="cameras", route_kind="dislocation"),  # same thesis as a1
        _opp("b", pess_net=150, category="watches", route_kind="dislocation"),
        _opp("c", pess_net=90, category="sneakers", route_kind="dislocation"),
    ]
    picks = sel.rank_execution_ready(opps, limit=3)
    ids = [p["id"] for p in picks]
    assert ids == ["a1", "b", "c"]          # a2 collapsed into a1's thesis; sorted by EV


def test_action_summary_carries_decision_fields():
    s = sel.action_summary(_opp("x", kind="flip", pess_net=80, qty=2, capital=120))
    assert s["capital_usd"] == 120 and "window" in s["time"]
    assert s["conservative_result_usd"] == 160.0        # 80 × 2 units, worst case
    assert isinstance(s["risks"], list) and s["evidence_quality"] <= 1.0


def test_risks_flag_single_source_estimated_and_negative():
    o = _opp("r", pess_net=-5, single=True, level="partially_verified",
             ip={"demand_volume": "estimated"})
    r = sel.risks(o)
    joined = " ".join(r).lower()
    assert "single-source" in joined and "estimated" in joined and "pessimistic" in joined


def test_validation_required_bucket_from_ledger():
    evals = [
        {"entity_id": "disc_n_a", "category": "validation_required", "family": "info",
         "demand_points": 6, "demand_supply_ratio": 100.0},
        {"entity_id": "disc_n_a", "category": "validation_required", "family": "info"},  # dup
        {"entity_id": "disc_n_b", "category": "insufficient_demand", "family": "local"},  # not vr
    ]
    vr = sel.validation_required(evals, limit=5)
    assert len(vr) == 1 and vr[0]["entity_id"] == "disc_n_a"
    assert "validate" in vr[0]["next_step"].lower()


# ------------------------------------------------------------- API surface

def test_today_endpoint_shape(tmp_path):
    cfg = Config(db_path=tmp_path / "today.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=3)
    with TestClient(app) as c:
        d = c.get("/api/today").json()
        assert set(d) >= {"execution_ready", "validation_required", "counts", "as_of_tick"}
        assert len(d["execution_ready"]) <= 3
        for a in d["execution_ready"]:
            assert set(a) >= {"capital_usd", "time", "conservative_result_usd",
                              "expected_value_usd", "evidence_quality", "risks", "next_step"}
            assert a["next_step"]
