"""Phase 0 security proofs: auth gating, at-rest encryption, secret hygiene,
rate limiting, CSRF. Every claim in SECURITY.md is pinned by a test here."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from opportunity_os.api import create_app
from opportunity_os.config import Config
from opportunity_os.security import RateLimiter, SecretBox

SECRET = "sk-live-supersecret-value-XYZ-9876"
TOKEN = "correct-horse-battery"


def make_app(tmp_path, token=TOKEN):
    cfg = Config(db_path=tmp_path / "sec.db", auto_cycle_seconds=0)
    cfg.dashboard_token = token or ""
    return create_app(cfg, auto_cycle_seconds=0, seed_cycles=1), cfg


# ------------------------------------------------------------------ auth

def test_unauthenticated_cannot_view_or_modify_settings(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app)                              # no token header
    assert c.get("/api/settings").status_code == 401
    assert c.post("/api/settings", json={"SERPER_API_KEY": "x"}).status_code == 401
    assert c.post("/api/settings/test").status_code == 401
    assert c.post("/api/cycle").status_code == 401


def test_wrong_token_rejected_right_token_accepted(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    assert c.get("/api/settings", headers={"X-OOS-Token": "nope"}).status_code == 401
    assert c.get("/api/settings", headers={"X-OOS-Token": TOKEN}).status_code == 200
    # Authorization: Bearer works too
    assert c.get("/api/settings",
                 headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_no_token_configured_is_closed_to_non_local_clients(tmp_path):
    app, _ = make_app(tmp_path, token=None)
    c = TestClient(app)                              # client host is not loopback
    r = c.get("/api/settings")
    assert r.status_code == 403
    assert "OOS_DASHBOARD_TOKEN" in r.json()["detail"]


def test_no_token_reverse_proxy_fails_closed(tmp_path):
    """Behind a proxy every client looks like loopback — a forwarded header
    means the request is NOT local, so it must be refused without a token."""

    app, _ = make_app(tmp_path, token=None)
    c = TestClient(app, client=("127.0.0.1", 50000))
    assert c.get("/api/settings").status_code == 200          # genuinely local: allowed
    r = c.get("/api/settings", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403                               # proxied: refused


def test_cross_origin_request_refused(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app)
    r = c.post("/api/settings", json={"SERPER_API_KEY": "x"},
               headers={"X-OOS-Token": TOKEN, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_open_mode_disables_the_password(tmp_path, monkeypatch):
    """OOS_OPEN_MODE=1 → no token needed anywhere (opt-in convenience)."""

    monkeypatch.setenv("OOS_OPEN_MODE", "1")
    app, _ = make_app(tmp_path, token=None)
    c = TestClient(app)                              # no token header at all
    assert c.get("/api/settings").status_code == 200
    assert c.post("/api/cycle").status_code == 200
    assert c.get("/api/health").json()["auth"]["open_mode"] is True


def test_public_read_endpoints_stay_open(tmp_path):
    """The guard protects secrets + spending, not the feed itself."""

    app, _ = make_app(tmp_path)
    c = TestClient(app)                              # unauthenticated
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/opportunities").status_code == 200


# -------------------------------------------------------------- secrecy

def test_secret_never_appears_in_any_response(tmp_path):
    app, _ = make_app(tmp_path)
    c = TestClient(app, headers={"X-OOS-Token": TOKEN})
    r = c.post("/api/settings", json={"ANTHROPIC_API_KEY": SECRET})
    assert r.status_code == 200
    assert SECRET not in r.text
    for path in ("/api/settings", "/api/health", "/api/stats", "/api/briefing"):
        assert SECRET not in c.get(path).text, f"secret leaked via {path}"


def test_secret_encrypted_on_disk(tmp_path):
    app, cfg = make_app(tmp_path)
    c = TestClient(app, headers={"X-OOS-Token": TOKEN})
    assert c.post("/api/settings", json={"EBAY_CLIENT_SECRET": SECRET}).status_code == 200
    raw = sqlite3.connect(str(cfg.db_path)).execute(
        "SELECT value FROM meta WHERE key='api_keys'").fetchone()[0]
    assert SECRET not in raw                          # plaintext never touches disk
    assert "enc:v2:" in raw                           # encrypted representation present
    # and the running config still has the working plaintext value
    assert cfg.anthropic_api_key != SECRET
    assert cfg.ebay_client_secret == SECRET


def test_legacy_plaintext_is_migrated_to_encrypted(tmp_path):
    """Keys written by pre-encryption builds still load, and are re-written
    encrypted on first read."""

    from opportunity_os import settings as app_settings
    from opportunity_os.db import Store
    cfg = Config(db_path=tmp_path / "mig.db", auto_cycle_seconds=0)
    store = Store(cfg.db_path)
    store.meta_set("api_keys", {"SERPER_API_KEY": SECRET})       # old-style plaintext
    applied = app_settings.load_into(cfg, store)
    assert "SERPER_API_KEY" in applied and cfg.serper_api_key == SECRET
    raw = json.dumps(store.meta_get("api_keys"))
    assert SECRET not in raw and "enc:v2:" in raw                # migrated at rest
    store.close()


# ------------------------------------------------------------ encryption

def test_secretbox_roundtrip_tamper_and_wrong_key(tmp_path):
    cfg = Config(db_path=tmp_path / "box.db")
    box = SecretBox(cfg)
    assert box.active
    blob = box.encrypt(SECRET)
    assert SECRET not in blob and blob.startswith("enc:v2:")
    assert box.decrypt(blob) == SECRET
    assert box.decrypt(blob[:-6] + "AAAAAA") == ""               # tampered → nothing
    cfg2 = Config(db_path=tmp_path / "box.db")
    cfg2.secret_key = "a-different-master-key"
    assert SecretBox(cfg2).decrypt(blob) == ""                   # wrong key → nothing


# ---------------------------------------------------------- rate limiting

def test_rate_limiter_trips():
    rl = RateLimiter(max_hits=3, window_s=60)
    assert [rl.check("ip1") for _ in range(4)] == [True, True, True, False]
    assert rl.check("ip2")                                       # other ip unaffected


def test_settings_endpoint_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("OOS_ADMIN_RATE", "5")
    app, _ = make_app(tmp_path)
    c = TestClient(app, headers={"X-OOS-Token": TOKEN})
    codes = [c.get("/api/settings").status_code for _ in range(7)]
    assert 429 in codes and codes[0] == 200
