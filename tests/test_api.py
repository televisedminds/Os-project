"""API surface: feed, plan gating, detail, outcomes, cycle trigger."""

import pytest
from fastapi.testclient import TestClient

from opportunity_os.api import create_app
from opportunity_os.config import Config


@pytest.fixture
def client(tmp_path):
    cfg = Config(db_path=tmp_path / "api.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=2)
    with TestClient(app) as c:
        yield c


def test_health_declares_demo_mode(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["demo_mode"] and h["tick"] >= 14


def test_feed_and_filters(client):
    r = client.get("/api/opportunities").json()
    assert r["count"] > 0
    row = r["opportunities"][0]
    for key in ("id", "title", "score", "confidence", "net_usd", "net_thb", "window_days", "route"):
        assert key in row
    flips = client.get("/api/opportunities?category=trading_cards").json()
    assert all(o["category"] == "trading_cards" for o in flips["opportunities"])
    strict = client.get("/api/opportunities?min_score=99").json()
    assert strict["count"] == 0


def test_free_plan_gates_feed_and_playbooks(client):
    r = client.get("/api/opportunities?plan=free").json()
    actives = [o for o in r["opportunities"] if o["status"] == "active"]
    assert len(actives) <= 3 and r["locked"] >= 1
    d = client.get(f"/api/opportunities/{actives[0]['id']}?plan=free").json()
    assert d["playbook"] is None and d["automation"] is None
    assert "locked" in d

    d_pro = client.get(f"/api/opportunities/{actives[0]['id']}?plan=pro").json()
    assert d_pro["playbook"] and d_pro["playbook"]["steps"]


def test_detail_carries_full_evidence(client):
    r = client.get("/api/opportunities").json()
    d = client.get(f"/api/opportunities/{r['opportunities'][0]['id']}").json()
    assert d["why_chain"] and d["verification"]["checks"] and d["score"]["factors"]
    assert d["economics"]["base"]["lines"]
    assert d["feasibility"]["payment_rails"]
    assert d["history"] is None or len(d["history"]["points"]) > 2


def test_detail_action_card_says_where_to_buy_and_sell(client):
    r = client.get("/api/opportunities").json()
    flips = [o for o in r["opportunities"] if o["type"] == "product_arbitrage"]
    d = client.get(f"/api/opportunities/{flips[0]['id']}").json()
    a = d["action"]
    assert a["type"] == "flip"
    assert a["buy"]["venue"] and a["buy"]["price_thb"] > 0 and a["buy"]["qty"] >= 1
    assert a["buy"]["max_price_usd"] > a["buy"]["price_usd"]
    assert a["sell"]["venue"] and a["sell"]["price_usd"] > a["buy"]["price_usd"]
    assert a["invest_thb"] > 0 and a["profit_usd"] > 0
    assert len(a["first_steps"]) >= 3

    ventures = [o for o in r["opportunities"] if o["type"] != "product_arbitrage"]
    if ventures:
        dv = client.get(f"/api/opportunities/{ventures[0]['id']}").json()
        assert dv["action"]["type"] == "venture" and dv["action"]["payback_months"] > 0


def test_outcome_feeds_learning_and_marks_executed(client):
    r = client.get("/api/opportunities").json()
    oid = r["opportunities"][0]["id"]
    res = client.post(f"/api/opportunities/{oid}/outcome",
                      json={"result": "failure", "failure_reason": "shipping"}).json()
    assert res["recorded"] and "calibration" in res["learning"]
    assert client.get(f"/api/opportunities/{oid}").json()["status"] == "executed"
    learn = client.get("/api/learning").json()
    assert learn["outcomes"]["failure"] == 1


def test_cycle_endpoint_runs_pipeline(client):
    before = client.get("/api/health").json()["tick"]
    rep = client.post("/api/cycle").json()
    assert rep["tick"] == before + 1 and rep["signals"] > 0
    assert client.get("/api/stats").json()["last_report"]["tick"] == rep["tick"]


def test_action_card_has_real_links_timeline_and_forecast(client):
    r = client.get("/api/opportunities").json()
    flips = [o for o in r["opportunities"] if o["type"] == "product_arbitrage"]
    d = client.get(f"/api/opportunities/{flips[0]['id']}").json()
    a = d["action"]
    assert a["buy"]["url"] and a["buy"]["url"].startswith("https://")
    assert a["sell"]["url"] and a["sell"]["url"].startswith("https://")
    assert a["sell"]["signup_url"] is None or a["sell"]["signup_url"].startswith("https://")
    tl = a["money_timeline"]
    assert tl[0]["day"] == 0 and tl[0]["amount_usd"] < 0            # spend first
    assert tl[-1]["amount_usd"] > 0                                 # end with money back
    probs = a["forecast"]["probabilities"]
    assert probs["1"] >= probs["3"] >= probs["7"] >= probs["14"]    # decay is monotonic
    assert d["discovery"] is None or len(d["discovery"]["rows"]) >= 2


def test_mission_progress_roundtrip(client):
    r = client.get("/api/opportunities").json()
    oid = r["opportunities"][0]["id"]
    res = client.post(f"/api/opportunities/{oid}/progress", json={"step": 1, "done": True}).json()
    assert res["done_steps"] == [1]
    client.post(f"/api/opportunities/{oid}/progress", json={"step": 2, "done": True})
    d = client.get(f"/api/opportunities/{oid}").json()
    assert d["progress"]["done_steps"] == [1, 2] and d["progress"]["pct"] > 0
    client.post(f"/api/opportunities/{oid}/progress", json={"step": 1, "done": False})
    assert client.get(f"/api/opportunities/{oid}").json()["progress"]["done_steps"] == [2]
    assert client.post("/api/opportunities/nope/progress", json={"step": 1}).status_code == 404


def test_activity_feed_shows_fleet_working(client):
    items = client.get("/api/activity").json()["items"]
    kinds = {i["kind"] for i in items}
    assert "scan" in kinds and ("publish" in kinds or "reject" in kinds)
    assert all(i["ts"] and i["actor"] and i["text"] for i in items)
    assert items == sorted(items, key=lambda i: i["ts"], reverse=True)


def test_briefing_agents_thailand(client):
    b = client.get("/api/briefing").json()
    assert "opportunities worth your attention" in b["headline"]
    a = client.get("/api/agents").json()
    assert a["count"] >= 13 and all("reliability" in x for x in a["agents"])
    t = client.get("/api/thailand").json()
    assert t["venue_access"]["mercari_jp"]["sell"] is False
    assert client.get("/api/opportunities/nope").status_code == 404
