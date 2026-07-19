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


def test_kit_endpoint_generates_caches_and_gates(tmp_path):
    import json as _json

    import httpx
    anthropic = pytest.importorskip("anthropic")
    from tests.test_ai import FLIP_KIT, _message_body

    cfg = Config(db_path=tmp_path / "kit.db", auto_cycle_seconds=0, anthropic_api_key="test-key")
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=2)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json=_message_body(FLIP_KIT))

    app.state.brain._client = anthropic.Anthropic(
        api_key="test-key", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with TestClient(app) as c:
        opp = next(o for o in c.get("/api/opportunities?plan=pro").json()["opportunities"]
                   if o["type"] == "product_arbitrage" and o["status"] == "active")
        r = c.post(f"/api/opportunities/{opp['id']}/kit?plan=pro")
        assert r.status_code == 200 and r.json()["cached"] is False
        assert r.json()["kit"]["_kind"] == "flip"
        # second call is served from the cache — no new API spend
        r2 = c.post(f"/api/opportunities/{opp['id']}/kit?plan=pro")
        assert r2.json()["cached"] is True and calls["n"] == 1
        # force regenerates
        r3 = c.post(f"/api/opportunities/{opp['id']}/kit?plan=pro&force=true")
        assert r3.json()["cached"] is False and calls["n"] == 2
        # detail carries the cached kit + availability flag
        d = c.get(f"/api/opportunities/{opp['id']}?plan=pro").json()
        assert d["kit"]["kit"]["_kind"] == "flip" and d["kit_available"] is True
        # free plan: kits are gated like playbooks
        assert c.post(f"/api/opportunities/{opp['id']}/kit?plan=free").status_code == 403


def test_kit_endpoint_without_key_says_why(client):
    opp_id = client.get("/api/opportunities").json()["opportunities"][0]["id"]
    r = client.post(f"/api/opportunities/{opp_id}/kit")
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_funnel_counts_new_verifications_not_refreshes(client):
    """'103 verified' when it's 2 deals re-checked 50 times reads as a lie.
    The funnel must count first-time verifications; refreshes are re-checks."""

    client.post("/api/cycle")                    # extra cycle: mostly refreshes
    fu = client.get("/api/funnel").json()
    active = client.get("/api/stats").json()["opportunities_active"]
    assert fu["rechecked"] > 0
    assert fu["verified"] <= active + fu["killed"] + 5      # same order as reality
    assert fu["verified"] < fu["investigations"]            # no per-cycle inflation
    s = client.get("/api/stats").json()
    assert s["watching"]["total"] > 0                       # breadth is now visible


def test_item_url_builds_exact_links():
    from opportunity_os import links
    # eBay Browse returns "v1|<legacy id>|0"; link resolves to the numeric middle.
    assert links.item_url("ebay_us", "v1|123456789012|0") == "https://www.ebay.com/itm/123456789012"
    assert links.item_url("ebay_us", "123456789012") == "https://www.ebay.com/itm/123456789012"
    assert links.item_url("aliexpress", "1005006357290000") == \
        "https://www.aliexpress.com/item/1005006357290000.html"
    assert links.item_url("facebook_mp_th", "x") is None    # no template for this venue
    assert links.item_url("ebay_us", None) is None


def test_action_card_prefers_exact_over_search():
    from opportunity_os.api import _action_card
    o = {
        "type": "product_arbitrage",
        "route": {"buy_venue": "aliexpress", "sell_venue": "shopee_th"},
        "window_days": 7,
        "title": "Foldable phone gimbal",
        "economics": {"base": {"lines": [{"amount_usd": 23.0}], "revenue_usd": 53.0,
                               "total_cost_usd": 30.0, "net_usd": 23.0, "margin_pct": 43.0},
                      "pessimistic": {"net_usd": 10.0}, "qty": 10, "capital_usd": 300.0,
                      "total_net_usd": 230.0, "fx": {"USD_THB": 34.0}},
        "playbook": {"steps": [], "listing": {"price_usd": 53.0}},
    }
    resolved = {"aliexpress": ("https://www.aliexpress.com/item/1.html", "https://search/ali"),
                "shopee_th": ("https://search/shopee", "https://search/shopee")}
    card = _action_card(o, {}, lambda v: resolved[v])
    assert card["buy"]["url"] == "https://www.aliexpress.com/item/1.html"
    assert card["buy"]["exact"] is True and card["buy"]["search_url"] == "https://search/ali"
    assert card["sell"]["exact"] is False    # url == search_url → not exact


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


def test_goal_engine_wallet_mission_and_recommendation(client):
    g = client.get("/api/goal").json()
    assert g["wallet_usd"] > 0 and g["cash_usd"] <= g["wallet_usd"]
    assert isinstance(g["recommendation"], str) and g["recommendation"]
    if g["mission"]:
        assert g["mission"]["capital_usd"] <= g["wallet_usd"]
        assert g["mission"]["roi_pct"] > 0
    # recording a success moves realized wealth
    oid = client.get("/api/opportunities").json()["opportunities"][0]["id"]
    client.post(f"/api/opportunities/{oid}/outcome",
                json={"result": "success", "realized_profit_usd": 50.0})
    g2 = client.get("/api/goal").json()
    assert g2["realized_usd"] == 50.0
    assert g2["wallet_usd"] == g["wallet_usd"] + 50.0


def test_funnel_narrows_and_radar_parses(client):
    f = client.get("/api/funnel").json()
    assert f["observations"] >= f["anomalies"] >= f["investigations"] >= f["verified"]
    assert f["recommended_now"] >= 0
    r = client.get("/api/radar").json()
    assert isinstance(r["items"], list)
    for item in r["items"]:
        assert item["days_until"] >= -7 and "status" in item


def test_category_affinity_prioritizes_feed(client):
    rows = client.get("/api/opportunities").json()["opportunities"]
    target = next(o for o in rows if o["status"] == "active")
    client.post(f"/api/opportunities/{target['id']}/outcome",
                json={"result": "success", "realized_profit_usd": 10.0})
    rows2 = client.get("/api/opportunities").json()["opportunities"]
    boosted = [o for o in rows2 if o.get("personal") and o["personal"]["boost"] > 0]
    assert any(o["category"] == target["category"] for o in boosted)
    assert all("before" in o["personal"]["note"] for o in boosted)


def test_briefing_agents_thailand(client):
    b = client.get("/api/briefing").json()
    assert "opportunities worth your attention" in b["headline"]
    a = client.get("/api/agents").json()
    assert a["count"] >= 13 and all("reliability" in x for x in a["agents"])
    t = client.get("/api/thailand").json()
    assert t["venue_access"]["mercari_jp"]["sell"] is False
    assert client.get("/api/opportunities/nope").status_code == 404
