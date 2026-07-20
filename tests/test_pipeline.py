"""Full research cycles: publish, gate, re-verify, invalidate, learn, replay."""

from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market import SimulatedMarket
from opportunity_os.pipeline import Orchestrator, briefing


def test_cycle_publishes_verified_opportunities(orch):
    r1, r2 = orch.run_cycle(), orch.run_cycle()
    assert r2["signals"] > 100 and r2["agents"] == len(orch.fleet)
    actives = orch.db.active_opportunities()
    assert len(actives) >= 6
    for o in actives:
        assert o["confidence"] >= orch.cfg.min_consensus_confidence
        assert o["economics"]["pessimistic"]["net_usd"] > 0
        assert len(o["why_chain"]) >= 5
        assert o["playbook"]["steps"]
        assert o["verification"]["checks"]
    # the flagship example is found and priced
    card = next(o for o in actives if o["entity_id"] == "pokemon_card_214")
    assert card["route"]["buy_venue"] == "yahoo_auctions_jp"
    assert card["feasibility"]["requires_proxy"] is True


def test_published_entries_flag_new_vs_refreshed(orch):
    r1, r2 = orch.run_cycle(), orch.run_cycle()
    assert r1["published"] and all(p["new"] for p in r1["published"])
    refreshed = [p for p in r2["published"] if not p["new"]]
    assert refreshed, "second cycle should refresh existing opportunities, not re-flag them as new"
    from opportunity_os.notify import alert_text
    text = alert_text([p for p in r1["published"]][:2])
    assert "new opportunit" in text and r1["published"][0]["title"] in text


def test_rejections_carry_reasons(orch):
    r = orch.run_cycle()
    assert r["rejected"], "expected some candidates to fail the gates"
    for rej in r["rejected"]:
        assert rej["reason"]


def test_reverification_invalidates_when_market_turns(orch):
    # The 151 booster box publishes after its tick-14 supply shock and must be
    # invalidated when the distributor restock lands at tick 21.
    seen_active = False
    invalidated = None
    for _ in range(10):                              # ticks 13..22
        r = orch.run_cycle()
        ids = {o["entity_id"] for o in orch.db.active_opportunities()}
        if "pokemon_151_bb" in ids:
            seen_active = True
        hit = [iv for iv in r["invalidated"] if "151" in iv["title"]]
        if hit:
            invalidated = hit[0]
            break
    assert seen_active, "booster box never published"
    assert invalidated, "booster box was never invalidated after the restock"
    stored = orch.db.get_opportunity(invalidated["id"])
    assert stored["status"] == "invalidated"
    assert stored["invalidation_reason"]


def test_window_elapsed_expires_instead_of_invalidating(orch):
    # An opportunity aged far past its window must retire as EXPIRED (natural
    # end of life), not INVALIDATED (market turned) — the UI filters differ.
    from opportunity_os.agents import ScoringEngine, VerificationCouncil
    from opportunity_os.models import OppStatus

    orch.run_cycle()
    opp = orch.db.active_opportunities()[0]
    opp["tick_created"] -= int(opp["window_days"] * 2 + 10)
    council = VerificationCouncil(orch.cfg, orch.learning.verifier_reliability)
    scorer = ScoringEngine(orch.learning.weights, orch.cfg.capital_cap_usd)
    ok, reason, status = orch._reverify(opp, council, scorer, orch.world.tick_no)
    assert not ok and "window elapsed" in reason
    assert status is OppStatus.EXPIRED


def test_learning_updates_from_outcomes(orch):
    orch.run_cycle()
    opp = orch.db.active_opportunities()[0]
    before = dict(orch.learning.weights)
    upd = orch.learning.record_outcome(opp, "failure", None, "competition")
    assert orch.learning.calibration < 1.0
    assert orch.learning.weights["competition"] > before["competition"]
    assert "competition" in upd["note"]
    upd2 = orch.learning.record_outcome(opp, "success", 25.0, None)
    assert upd2["calibration"] > upd["calibration"]


def test_briefing_reads_like_a_briefing(orch, cfg):
    orch.run_cycle()
    b = briefing(orch.db, cfg)
    assert "opportunities worth your attention" in b["headline"]
    assert b["counts"]["active"] == len(b["top"])
    free = briefing(orch.db, cfg, "free")
    assert len(free["top"]) <= 3


def test_deterministic_replay_across_restart(tmp_path):
    cfg = Config(db_path=tmp_path / "replay.db")
    store = Store(cfg.db_path)
    o1 = Orchestrator(cfg, store, SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks))
    o1.run_cycle(); o1.run_cycle()
    card_before = o1.world.listing("pokemon_card_214", "ebay_us")

    # "restart": new orchestrator restores the world from seed + stored tick
    o2 = Orchestrator(cfg, store)
    assert o2.world.tick_no == o1.world.tick_no
    assert o2.world.listing("pokemon_card_214", "ebay_us") == card_before
    store.close()
