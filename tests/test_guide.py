"""Execution guide — the driven, dated, single-next-step plan for a selected
action. Reconciles playbook step check-offs with the chat lifecycle state,
surfaces the ONE next move, gates on Thailand feasibility, and anchors the
PROJECTED money/time schedule to the day the deal started.

Honesty boundary under test: the guide reports PLAN progress and PROJECTED
money only. It never claims realised cash — that is the Outcome-learning
milestone.
"""

import time

import pytest
from fastapi.testclient import TestClient

from opportunity_os import guide, execution
from opportunity_os.api import create_app
from opportunity_os.config import Config


def _flip_opp(exec_ready=True):
    ex = ({"checks": [{"question": "Payout available?", "status": "yes", "detail": "Payoneer"}],
           "unknowns": [], "blockers": [], "execution_ready": True} if exec_ready else
          {"checks": [{"question": "Buy on source?", "status": "unknown", "detail": "unknown venue"},
                      {"question": "Company required?", "status": "yes", "detail": "needs a company"}],
           "unknowns": ["Buy on source?"], "blockers": ["Company required?"],
           "execution_ready": False})
    return {
        "id": "opp_flip", "title": "Test flip", "type": "product_arbitrage",
        "economics": {"kind": "flip", "capital_usd": 887,
                      "base": {"net_usd": 120}, "pessimistic": {"net_usd": 100}},
        "executability": ex,
        "playbook": {"timeline_days": 20, "steps": [
            {"order": 1, "title": "Secure 1 unit on eBay", "eta": "day 0", "automatable": True, "tool": "x"},
            {"order": 2, "title": "Route the goods", "eta": "day 0–1", "automatable": False},
            {"order": 3, "title": "Customs paperwork", "eta": "dispatch day", "automatable": True},
            {"order": 4, "title": "List on eBay", "eta": "day 1", "automatable": True}]},
        "action": {"type": "flip",
                   "buy": {"venue": "eBay US", "max_price_usd": 486, "price_usd": 450},
                   "sell": {"venue": "eBay US", "price_usd": 2451}, "invest_usd": 887,
                   "money_timeline": [{"day": 0, "label": "Buy inventory", "amount_usd": -887},
                                      {"day": 14, "label": "Break even", "amount_usd": 0},
                                      {"day": 20, "label": "Profit banked", "amount_usd": 1639}]},
    }


def _venture_opp():
    return {
        "id": "opp_v", "title": "Test venture", "type": "digital_product",
        "economics": {"kind": "venture", "capital_usd": 300, "base": {"net_usd": 500, "price_point_usd": 39},
                      "pessimistic": {"net_usd": 200}},
        "executability": {"checks": [], "unknowns": [], "blockers": [], "execution_ready": True},
        "playbook": {"timeline_days": 30, "steps": [
            {"order": 1, "title": "Validate with 10 conversations", "eta": "day 0–2", "automatable": False},
            {"order": 2, "title": "Build the minimum version", "eta": "day 2–7", "automatable": True}]},
        "action": {"type": "venture"},
    }


# ---------------------------------------------------------- stage reconciliation

def test_stage_from_steps_maps_flip_and_venture():
    assert guide.stage_from_steps(_flip_opp(), [1]) == execution.PURCHASED
    assert guide.stage_from_steps(_flip_opp(), [1, 4]) == execution.LISTED
    assert guide.stage_from_steps(_venture_opp(), [1]) == execution.RESEARCHING
    assert guide.stage_from_steps(_flip_opp(), []) == execution.NOT_STARTED


def test_effective_stage_is_furthest_of_both_signals():
    o = _flip_opp()
    # steps say purchased, chat says not_started -> purchased
    assert guide.effective_stage(o, [1], execution.NOT_STARTED) == execution.PURCHASED
    # chat says listed, no steps ticked -> listed (chat drove it, reconciled)
    assert guide.effective_stage(o, [], execution.LISTED) == execution.LISTED
    # both present -> the further one wins
    assert guide.effective_stage(o, [1], execution.LISTED) == execution.LISTED
    assert guide.effective_stage(o, [1, 4], execution.PURCHASED) == execution.LISTED


# ------------------------------------------------------------ single next step

def test_next_step_is_state_aware_and_concrete():
    o = _flip_opp()
    ns0 = guide.next_step(o, execution.NOT_STARTED, o["action"])
    assert "486" in ns0["concrete"] and "eBay US" in ns0["concrete"]   # the buy cap + venue
    ns_listed = guide.next_step(o, execution.LISTED, o["action"])
    assert "2,451" in ns_listed["concrete"] or "2451" in ns_listed["concrete"]  # the list price
    # the coarse title still comes from the lifecycle machine
    assert ns0["title"] and ns0["stage"] == execution.NOT_STARTED


def test_venture_next_step_confirms_willingness_to_pay():
    o = _venture_opp()
    ns = guide.next_step(o, execution.NOT_STARTED, o["action"])
    assert "39" in ns["concrete"] and "pay" in ns["concrete"].lower()


# --------------------------------------------------------- dated money/time clock

def test_schedule_anchors_to_start_and_marks_done_today_upcoming():
    o = _flip_opp()
    now = time.time()
    sc = guide.schedule(o, o["action"], started_ts=now - 15 * 86400, now=now)
    assert sc["anchored"] and sc["day"] == 15
    statuses = [m["status"] for m in sc["milestones"]]
    assert statuses == ["done", "done", "upcoming"]     # day 0 & 14 passed, day 20 ahead
    # a deal at exactly its milestone day is "today"
    sc0 = guide.schedule(o, o["action"], started_ts=now - 14 * 86400, now=now)
    assert sc0["milestones"][1]["status"] == "today"


def test_schedule_unanchored_before_start_is_all_planned():
    o = _flip_opp()
    sc = guide.schedule(o, o["action"], started_ts=None)
    assert not sc["anchored"] and sc["day"] is None
    assert all(m["status"] == "planned" for m in sc["milestones"])


def test_schedule_is_projected_never_realised():
    o = _flip_opp()
    sc = guide.schedule(o, o["action"], started_ts=time.time() - 30 * 86400)
    assert sc["basis"] == "projected"
    assert "not realised" in sc["note"].lower()
    # even a fully-elapsed schedule only means milestones' projected days passed
    assert all(m["status"] in ("done", "today", "upcoming", "planned") for m in sc["milestones"])


def test_schedule_falls_back_to_playbook_etas_without_money_timeline():
    o = _venture_opp()   # action has no money_timeline
    sc = guide.schedule(o, o["action"], started_ts=None)
    assert len(sc["milestones"]) == 2 and sc["milestones"][0]["day"] == 0  # "day 0–2" -> 0


# ------------------------------------------------------------- Thailand gate

def test_readiness_surfaces_unknowns_and_blockers():
    ok = guide.readiness(_flip_opp(exec_ready=True))
    assert ok["ready"] and not ok["unknowns"] and not ok["blockers"]
    bad = guide.readiness(_flip_opp(exec_ready=False))
    assert not bad["ready"] and bad["unknowns"] and bad["blockers"]


def test_readiness_falls_back_to_live_assess_when_absent():
    o = _flip_opp()
    o.pop("executability")            # force the engine to run
    o["route"] = {"buy_venue": "shopee_th", "sell_venue": "ebay_us"}
    r = guide.readiness(o)
    assert "ready" in r and isinstance(r["checks"], list) and r["checks"]


# --------------------------------------------------------------- composition

def test_build_guide_composition_and_next_flag():
    o = _flip_opp()
    g = guide.build_guide(o, done_steps=[1], chat_state=execution.NOT_STARTED,
                          started_ts=time.time() - 3 * 86400)
    assert g["stage"] == execution.PURCHASED
    assert g["progress"] == {"done": 1, "total": 4, "pct": 25}
    # first UNticked step is flagged is_next (step 1 done -> step 2)
    nxt = [s["order"] for s in g["steps"] if s["is_next"]]
    assert nxt == [2]
    assert g["steps"][0]["done"] and not g["steps"][1]["done"]
    assert "projected" in g["basis"].lower() and "realised" in g["basis"].lower()


def test_compact_for_today_view():
    o = _flip_opp()
    c = guide.compact(guide.build_guide(o, done_steps=[], started_ts=None))
    assert c["stage"] == execution.NOT_STARTED
    assert c["ready_in_thailand"] is True
    assert "486" in c["next_do"]                # concrete instruction, not a label
    assert c["open_questions"] == 0 and c["progress_pct"] == 0


# ----------------------------------------------------------------- API surface

def test_execution_endpoint_and_404(tmp_path):
    cfg = Config(db_path=tmp_path / "g.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=3)
    with TestClient(app) as c:
        oid = c.get("/api/today").json()["execution_ready"][0]["id"]
        g = c.get(f"/api/opportunities/{oid}/execution").json()
        assert set(g) >= {"stage", "next_step", "readiness", "schedule", "steps", "progress"}
        assert g["next_step"]["title"]
        assert c.get("/api/opportunities/nope/execution").status_code == 404


def test_today_embeds_compact_guide(tmp_path):
    cfg = Config(db_path=tmp_path / "g2.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=3)
    with TestClient(app) as c:
        d = c.get("/api/today").json()
        for a in d["execution_ready"]:
            assert "guide" in a
            g = a["guide"]
            assert set(g) >= {"stage", "next_do", "ready_in_thailand", "on_day"}
            assert g["next_do"]


def test_detail_carries_execution_guide(tmp_path):
    cfg = Config(db_path=tmp_path / "g3.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=3)
    with TestClient(app) as c:
        oid = c.get("/api/today").json()["execution_ready"][0]["id"]
        det = c.get(f"/api/opportunities/{oid}?plan=pro").json()
        assert "execution_guide" in det and det["execution_guide"]["next_step"]["title"]


def test_deal_started_ts_reflects_first_action(tmp_path):
    from opportunity_os.db import Store
    st = Store(tmp_path / "d.db")
    assert st.deal_started_ts("opp_z") is None          # nothing started
    st.set_progress("opp_z", 1, True)                    # first step checked
    t1 = st.deal_started_ts("opp_z")
    assert t1 is not None
    # a later lifecycle change does not move the START earlier
    st.set_chat_state("opp_z", execution.PURCHASED, None)
    assert st.deal_started_ts("opp_z") == pytest.approx(t1, abs=2.0)
