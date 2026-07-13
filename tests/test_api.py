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


def test_briefing_agents_thailand(client):
    b = client.get("/api/briefing").json()
    assert "opportunities worth your attention" in b["headline"]
    a = client.get("/api/agents").json()
    assert a["count"] >= 13 and all("reliability" in x for x in a["agents"])
    t = client.get("/api/thailand").json()
    assert t["venue_access"]["mercari_jp"]["sell"] is False
    assert client.get("/api/opportunities/nope").status_code == 404
