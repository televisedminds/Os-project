"""In-app key management: storage, precedence, masking, live application,
and the API surface. The security property under test everywhere: raw key
values must never come back out of the API."""

import pytest
from fastapi.testclient import TestClient

from opportunity_os import settings as app_settings
from opportunity_os.api import create_app
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.pipeline import Orchestrator


def test_save_applies_to_live_config_and_masks(tmp_path):
    cfg = Config(db_path=tmp_path / "s.db")
    store = Store(cfg.db_path)
    changed = app_settings.save(cfg, store, {"ANTHROPIC_API_KEY": " sk-ant-test-123456789 "})
    assert changed == ["ANTHROPIC_API_KEY"]
    assert cfg.anthropic_api_key == "sk-ant-test-123456789"      # stripped + applied live

    rows = {r["name"]: r for r in app_settings.status(cfg, store)}
    row = rows["ANTHROPIC_API_KEY"]
    assert row["set"] and row["source"] == "app"
    assert "sk-ant-test-123456789" not in str(row)               # never the raw value
    assert row["masked"].startswith("sk-") and row["masked"].endswith("(21 chars)")


def test_clear_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "env-value")
    cfg = Config(db_path=tmp_path / "s.db")
    store = Store(cfg.db_path)
    assert cfg.ebay_client_id == "env-value"                     # from env at construct

    app_settings.save(cfg, store, {"EBAY_CLIENT_ID": "app-value"})
    assert cfg.ebay_client_id == "app-value"                     # app wins
    app_settings.save(cfg, store, {"EBAY_CLIENT_ID": ""})
    assert cfg.ebay_client_id == "env-value"                     # cleared → env again
    assert app_settings.stored(store) == {}


def test_unknown_key_rejected(tmp_path):
    cfg = Config(db_path=tmp_path / "s.db")
    store = Store(cfg.db_path)
    with pytest.raises(ValueError, match="unknown key"):
        app_settings.save(cfg, store, {"OPENAI_API_KEY": "x"})


def test_orchestrator_loads_saved_keys(tmp_path):
    cfg = Config(db_path=tmp_path / "o.db")
    store = Store(cfg.db_path)
    app_settings.save(cfg, store, {"TELEGRAM_BOT_TOKEN": "tok-abc"})
    fresh_cfg = Config(db_path=tmp_path / "o.db")                # new process simulation
    assert fresh_cfg.telegram_bot_token == ""
    Orchestrator(fresh_cfg, store)
    assert fresh_cfg.telegram_bot_token == "tok-abc"             # applied at startup


def test_run_checks_skips_unconfigured(tmp_path):
    cfg = Config(db_path=tmp_path / "s.db")                      # nothing set
    assert app_settings.run_checks(cfg) == []                    # zero network calls


def test_run_checks_runs_configured(monkeypatch, tmp_path):
    from opportunity_os.market.adapters import EbayAdapter
    monkeypatch.setattr(EbayAdapter, "check", lambda self: (True, "OAuth OK"))
    monkeypatch.setattr("opportunity_os.notify.send_telegram",
                        lambda cfg, text: (True, "sent"))
    cfg = Config(db_path=tmp_path / "s.db", ebay_client_id="id", ebay_client_secret="sec",
                 telegram_bot_token="t", telegram_chat_id="c")
    results = {r["id"]: r for r in app_settings.run_checks(cfg)}
    assert results["ebay"]["ok"] and "OAuth OK" in results["ebay"]["note"]
    assert results["telegram"]["ok"] and "check your phone" in results["telegram"]["note"]


# ------------------------------------------------------------------- API

@pytest.fixture
def client(tmp_path):
    cfg = Config(db_path=tmp_path / "api.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=1)
    with TestClient(app) as c:
        yield c, app


def test_settings_api_roundtrip_never_leaks(client):
    c, app = client
    r = c.post("/api/settings", json={"ANTHROPIC_API_KEY": "sk-ant-secret-value-9999"})
    assert r.status_code == 200 and "ANTHROPIC_API_KEY" in r.json()["changed"]
    assert "sk-ant-secret-value-9999" not in r.text              # masked in the response

    g = c.get("/api/settings")
    assert "sk-ant-secret-value-9999" not in g.text              # and in every later read
    row = next(k for k in g.json()["keys"] if k["name"] == "ANTHROPIC_API_KEY")
    assert row["set"] and row["source"] == "app"

    # applied to the live app immediately: kits become available
    assert app.state.config.anthropic_api_key == "sk-ant-secret-value-9999"
    opp = c.get("/api/opportunities").json()["opportunities"][0]
    assert c.get(f"/api/opportunities/{opp['id']}").json()["kit_available"] is True

    assert c.post("/api/settings", json={"NOT_A_KEY": "x"}).status_code == 422
