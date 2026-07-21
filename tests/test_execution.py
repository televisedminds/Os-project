"""Phases 6/10/11: thesis clustering, Thailand executability, capital optimizer."""

import pytest

from opportunity_os import clustering, executability, research
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market import SimulatedMarket
from opportunity_os.pipeline import Orchestrator


@pytest.fixture
def built(tmp_path):
    cfg = Config(db_path=tmp_path / "x.db", auto_cycle_seconds=0)
    store = Store(cfg.db_path)
    world = SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks)
    orch = Orchestrator(cfg, store, world)
    for _ in range(16):
        orch.run_cycle()
    return orch, store


# --------------------------------------------- Phase 6: clustering / dedup

def _disloc(pid, item_id, buy, score, net):
    return {"id": f"{pid}-{item_id}", "type": "product_arbitrage", "status": "active",
            "category": "gaming", "entity_id": pid, "confidence": 0.85,
            "route": {"buy_venue": "ebay_us", "sell_venue": "ebay_us", "kind": "dislocation"},
            "economics": {"capital_usd": buy, "total_net_usd": net, "qty": 1,
                          "base": {"lines": [{"amount_usd": buy}], "net_usd": net},
                          "pessimistic": {"net_usd": net * 0.7}},
            "score": {"overall": score}, "window_days": 7, "verification_level": "partially_verified"}


def test_twenty_listings_of_one_model_collapse_to_one_thesis():
    # Same product, twenty different underpriced listings → ONE market thesis.
    opps = [_disloc("new_balance_2002r", f"L{i}", 60 + i, 50 + i, 40 + i) for i in range(20)]
    clusters = clustering.cluster(opps)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["qualifying_listings"] == 20                      # depth reported, not 20 rows
    assert c["price_range_usd"][0] < c["price_range_usd"][1]   # a real price range
    assert c["inventory_depth"] >= 20
    assert len(c["member_ids"]) == 20


def test_distinct_products_do_not_collapse():
    opps = [_disloc("model_a", "L1", 60, 50, 40), _disloc("model_b", "L1", 60, 50, 40)]
    assert len(clustering.cluster(opps)) == 2


def test_cluster_metrics_expose_inflation():
    opps = [_disloc("one_model", f"L{i}", 60, 50, 40) for i in range(10)]
    m = clustering.cluster_metrics(raw_candidates=200, opps=opps)
    assert m["verified_opportunities"] == 10 and m["unique_theses"] == 1
    assert m["inflation_ratio"] == 10.0                        # 10 rows for 1 real edge


def test_feed_clusters_by_default(built):
    orch, store = built
    from fastapi.testclient import TestClient
    from opportunity_os.api import create_app
    # reuse the same DB via a fresh app pointed at it
    app = create_app(orch.cfg, auto_cycle_seconds=0, seed_cycles=0)
    with TestClient(app) as c:
        clustered = c.get("/api/opportunities?cluster=true").json()
        flat = c.get("/api/opportunities?cluster=false").json()
    assert "metrics" in clustered
    assert clustered["count"] <= flat["count"]                 # clustering never inflates


# ------------------------------------------ Phase 10: executability engine

def test_executability_answers_every_question_or_says_unknown(built):
    _, store = built
    for o in store.list_opportunities(status="active", limit=500):
        rep = o.get("executability")
        assert rep and rep["checks"]
        for c in rep["checks"]:
            assert c["status"] in ("yes", "no", "estimated", "unknown")
            assert c["question"] and c["detail"]


def test_execution_ready_requires_no_unknown_critical():
    # A flip missing tax lines → taxes are UNKNOWN → not execution ready.
    cand = {"type": "product_arbitrage", "kind": "flip", "category": "gaming",
            "route": {"buy_venue": "ebay_us", "sell_venue": "ebay_us",
                      "buy_country": "US", "sell_country": "US"},
            "economics": {"base": {"lines": [{"label": "Acquisition", "amount_usd": 40}]},
                          "pessimistic": {"net_usd": 10}},
            "feasibility": {"customs_notes": []}}
    rep = executability.assess(cand)
    assert rep["execution_ready"] is False
    assert any("tax" in q.lower() for q in rep["unknowns"])


def test_execution_ready_true_when_all_answered():
    cand = {"type": "product_arbitrage", "kind": "flip", "category": "gaming",
            "route": {"buy_venue": "shopee_th", "sell_venue": "ebay_us",
                      "buy_country": "TH", "sell_country": "US"},
            "economics": {"base": {"lines": [{"label": "eBay US fees", "amount_usd": 5},
                                             {"label": "Thai import VAT 7%", "amount_usd": 2}]},
                          "pessimistic": {"net_usd": 12}},
            "feasibility": {"customs_notes": ["CN22 declaration"]}}
    rep = executability.assess(cand)
    assert rep["execution_ready"] is True and not rep["unknowns"]


def test_verification_level_gated_on_executability(built):
    _, store = built
    # No opportunity can be execution_ready while its executability report isn't.
    for o in store.list_opportunities(status="active", limit=500):
        if o["verification_level"] == "execution_ready":
            assert o["executability"]["execution_ready"] is True


# ---------------------------------------- Phase 11: capital optimizer v2

def _opp(oid, cap, net, days, cat, level="multi_source_verified", pess=None):
    return {"id": oid, "title": oid, "category": cat, "entity_id": oid,
            "verification_level": level, "window_days": days,
            "route": {"buy_venue": "x", "sell_venue": "y", "kind": "flip"},
            "economics": {"capital_usd": cap, "total_net_usd": net, "qty": 1,
                          "pessimistic": {"net_usd": pess if pess is not None else net * 0.6}}}


def test_optimizer_reports_worst_case_and_completion():
    plan = research.allocate_capital([_opp("a", 100, 50, 7, "gaming")], 500)
    p = plan["plan"][0]
    assert p["worst_case_loss_usd"] > 0 and p["worst_case_loss_usd"] < p["capital_usd"]
    assert p["completion_date"] and p["opportunity_cost_usd"] >= 0
    assert plan["worst_case_loss_usd"] > 0 and plan["expected_completion"]


def test_conservative_excludes_partially_verified():
    opps = [_opp("multi", 100, 50, 7, "gaming", level="multi_source_verified"),
            _opp("partial", 100, 50, 7, "cameras", level="partially_verified")]
    ids = [p["id"] for p in research.allocate_capital(opps, 500, risk_tolerance="conservative")["plan"]]
    assert "multi" in ids and "partial" not in ids


def test_liquidity_reserve_is_kept_uninvested():
    opps = [_opp(f"o{i}", 100, 50, 7, f"cat{i}") for i in range(10)]
    plan = research.allocate_capital(opps, 1000, liquidity_reserve_pct=0.3)
    assert plan["deployed_usd"] <= 700 + 1e-6                  # 30% held back
    assert plan["reserve_usd"] >= 300 - 1e-6


def test_optimizer_dedupes_same_thesis():
    # Ten listings of one model must draw capital ONCE, not ten times.
    opps = [{"id": f"L{i}", "title": "NB 2002R", "category": "gaming",
             "entity_id": "nb2002r", "verification_level": "multi_source_verified",
             "window_days": 7, "route": {"buy_venue": "ebay_us", "sell_venue": "ebay_us",
                                         "kind": "dislocation"},
             "economics": {"capital_usd": 100, "total_net_usd": 40, "qty": 1,
                           "pessimistic": {"net_usd": 25}}} for i in range(10)]
    plan = research.allocate_capital(opps, 1000)
    nb = [p for p in plan["plan"] if p["category"] == "gaming"]
    assert len(nb) == 1                                       # one thesis, one allocation
