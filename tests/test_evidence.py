"""Phases 7-9: evidence ledger, verification levels, rejection taxonomy, and
the AI investigator's grounding constraint."""

import pytest

from opportunity_os import evidence as EV
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market import SimulatedMarket
from opportunity_os.pipeline import Orchestrator


@pytest.fixture
def built(tmp_path):
    cfg = Config(db_path=tmp_path / "ev.db", auto_cycle_seconds=0)
    store = Store(cfg.db_path)
    world = SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks)
    orch = Orchestrator(cfg, store, world)
    for _ in range(16):
        orch.run_cycle()
    return orch, store


# ------------------------------------------------ Phase 9: evidence ledger

def test_every_opportunity_has_a_traceable_ledger(built):
    _, store = built
    for o in store.list_opportunities(status="active", limit=500):
        led = o.get("evidence")
        assert led, f"{o['id']} has no evidence ledger"
        for item in led:
            assert item["field"] and item["kind"] in (
                EV.OBSERVED, EV.ESTIMATED, EV.CALCULATED, EV.ASSUMPTION,
                EV.USER_SUPPLIED, EV.AI_INTERPRETATION, EV.UNKNOWN)
            assert "source" in item


def test_economics_are_calculated_not_observed(built):
    _, store = built
    for o in store.list_opportunities(status="active", limit=500):
        econ = [e for e in o["evidence"] if e["field"] in ("net_profit", "fees_and_tax")]
        assert econ and all(e["kind"] == EV.CALCULATED for e in econ)


def test_sell_price_is_labelled_asking_not_sold_comp(built):
    _, store = built
    flips = [o for o in store.list_opportunities(status="active", limit=500)
             if o["type"] in ("product_arbitrage", "refurbishment")]
    for o in flips:
        sell = next((e for e in o["evidence"] if e["field"] == "sell_price"), None)
        if sell:
            assert sell["is_sold_comp"] is False        # eBay Browse = asking, honestly
            assert "asking" in sell["value"].lower()


def test_counterfeit_risk_is_unknown_not_faked(built):
    _, store = built
    for o in store.list_opportunities(status="active", limit=500):
        cf = next((e for e in o["evidence"] if e["field"] == "counterfeit_condition_risk"), None)
        assert cf and cf["kind"] == EV.UNKNOWN           # named as unknown, not invented


# -------------------------------------------- Phase 7: verification levels

def test_single_source_never_reaches_execution_ready():
    # A dislocation ledger: buy + sell are the SAME venue → one independent source.
    cand = {"kind": "flip", "buy_venue": "ebay_us", "sell_venue": "ebay_us",
            "buy_usd": 40, "sell_usd": 90, "velocity": 1.0, "sellers": 8,
            "dislocation": {"item_id": "x"}, "economics": None, "feasibility": None,
            "sources": ["ebay_us"]}
    led = EV.build_ledger(cand, None, "demo")
    assert EV.is_single_source(led)
    from opportunity_os.thailand import feasibility
    feas = feasibility("product_arbitrage", "gaming", "ebay_us", "ebay_us")
    level = EV.compute_level(led, None, feas, passed_gates=True)
    assert level == EV.VerificationLevel.PARTIALLY_VERIFIED


def test_cross_market_reaches_multi_source(built):
    _, store = built
    cross = [o for o in store.list_opportunities(status="active", limit=500)
             if o["type"] == "product_arbitrage"
             and o.get("route", {}).get("buy_venue") != o.get("route", {}).get("sell_venue")]
    assert cross, "expected at least one cross-market flip"
    assert any(o["verification_level"] in ("multi_source_verified", "execution_ready")
               for o in cross)


def test_verification_levels_are_valid_values(built):
    _, store = built
    valid = {v.value for v in EV.VerificationLevel}
    for o in store.list_opportunities(status="active", limit=500):
        assert o["verification_level"] in valid


# --------------------------------------------- Phase 8: rejection taxonomy

def test_rejection_classifier_maps_reasons_to_categories():
    assert EV.classify_rejection("base margin 4% below the 10% floor") == EV.MARGIN_BELOW_THRESHOLD
    assert EV.classify_rejection("Blocked from TH. customs paperwork") == EV.UNSUPPORTED_THAILAND
    assert EV.classify_rejection("consensus confidence 0.60 below the bar") == EV.LOW_CONFIDENCE
    assert EV.classify_rejection("source inventory exhausted") == EV.INSUFFICIENT_SUPPLY
    assert EV.classify_rejection("excluded by your operator profile") == EV.EXCLUDED_BY_OPERATOR
    assert EV.classify_rejection("Listing disappeared on re-check") == EV.STALE_DATA


def test_rejection_funnel_aggregates_categories(built):
    _, store = built
    from opportunity_os import diagnostics as dx
    rf = dx.rejection_funnel(store)
    assert rf["total_rejected"] > 0
    # every category present maps to the fixed taxonomy, with an example reason
    for cat in rf["by_category"]:
        assert cat in rf["example_reason"]


def test_type_funnel_breaks_down_by_verification_level(built):
    _, store = built
    from opportunity_os import diagnostics as dx
    tf = dx.type_funnel(store)
    assert "by_verification_level" in tf and tf["by_verification_level"]
    assert "single_source" in tf


# ---------------------------------- Phase 9: AI grounded on the ledger only

def test_ai_payload_carries_ledger_and_no_free_numbers(monkeypatch):
    """The risk-review payload must hand Claude the evidence ledger (its only
    ground truth), and the prompt must forbid inventing numbers."""

    from opportunity_os.ai import AIClassifier, RISK_SYSTEM
    assert "MUST NOT invent" in RISK_SYSTEM and "missing_evidence" in RISK_SYSTEM

    cfg = Config(anthropic_api_key="k")
    ai = AIClassifier(cfg)
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            class R:
                stop_reason = "end_turn"
                content = [type("B", (), {"type": "text",
                    "text": '{"reviews":[{"id":"o1","verdict":"proceed","edge":"e",'
                            '"risks":["r"],"missing_evidence":["sold comps"]}]}'})()]
            return R()

    class FakeClient:
        messages = FakeMessages()

    ai._client = FakeClient()
    opp = {"id": "o1", "title": "t", "type": "product_arbitrage",
           "evidence": [{"field": "buy_price", "value": "$40 on ebay_us",
                         "kind": EV.OBSERVED, "source": "ebay_us", "freshness": "live"}]}
    out = ai.risk_review([opp])
    assert out["o1"]["missing_evidence"] == ["sold comps"]
    body = captured["messages"][0]["content"]
    assert "evidence_ledger" in body and "buy_price" in body
