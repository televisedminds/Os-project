"""AI brain: classification verdicts, graceful degradation on every failure
mode, and auto-wiring into the discovery engine. No network — the Anthropic
SDK client is built over a mocked HTTP transport."""

import json

import httpx
import pytest

anthropic = pytest.importorskip("anthropic")

from opportunity_os.ai import AIClassifier
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.discovery import Candidate, DiscoveryEngine


def _cands():
    return [
        Candidate(kind="product", id="disc_p_pokemon", name="Pokemon 151 Booster Box",
                  source="google_trends", score=1.0, category="collectibles",
                  reason="Trending search."),
        Candidate(kind="niche", id="disc_n_tennis", name="Paula Badosa Tennis Result",
                  source="google_trends", score=0.9, niche_kind="info",
                  reason="Trending topic."),
    ]


def _message_body(text: str, stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [{"type": "text", "text": text}] if text else [],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50}}


def _client(handler):
    return anthropic.Anthropic(api_key="test-key", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _cfg(**kw):
    kw.setdefault("mode", "live")
    kw.setdefault("anthropic_api_key", "test-key")
    return Config(**kw)


VERDICTS = json.dumps({"verdicts": [
    {"id": "disc_p_pokemon", "keep": True, "score_adj": 0.5,
     "category": "trading_cards", "niche_kind": "", "reason": "Sealed product with strong resale."},
    {"id": "disc_n_tennis", "keep": False, "score_adj": 0.0,
     "category": "", "niche_kind": "", "reason": "Sports news, nothing to sell."},
]})


def test_unconfigured_is_passthrough():
    ai = AIClassifier(Config(mode="live"))          # no key
    cands = _cands()
    assert ai.classify(cands) is cands
    ok, note = ai.check()
    assert not ok and "ANTHROPIC_API_KEY" in note


def test_classify_applies_verdicts():
    def handler(req):
        assert "api.anthropic.com" in str(req.url)
        payload = json.loads(req.content)
        assert payload["model"] == "claude-opus-4-8"
        assert payload["output_config"]["format"]["type"] == "json_schema"
        return httpx.Response(200, json=_message_body(VERDICTS))

    ai = AIClassifier(_cfg(), client=_client(handler))
    out = ai.classify(_cands())
    assert [c.id for c in out] == ["disc_p_pokemon"]        # tennis dropped
    kept = out[0]
    assert kept.score == 1.5 and kept.category == "trading_cards"
    assert "AI:" in kept.reason
    assert ai.last_error == "" and "kept 1, dropped 1" in ai.last_summary


def test_malformed_output_is_passthrough():
    ai = AIClassifier(_cfg(), client=_client(
        lambda req: httpx.Response(200, json=_message_body("not json at all"))))
    cands = _cands()
    assert ai.classify(cands) == cands
    assert "AI classify failed" in ai.last_error


def test_api_error_is_passthrough():
    ai = AIClassifier(_cfg(), client=_client(
        lambda req: httpx.Response(500, json={"type": "error",
                                              "error": {"type": "api_error", "message": "boom"}})))
    cands = _cands()
    assert ai.classify(cands) == cands
    assert "500" in ai.last_error


def test_refusal_is_passthrough():
    ai = AIClassifier(_cfg(), client=_client(
        lambda req: httpx.Response(200, json=_message_body("", stop_reason="refusal"))))
    cands = _cands()
    assert ai.classify(cands) == cands
    assert "declined" in ai.last_error


def test_missing_verdict_fails_open():
    only_first = json.dumps({"verdicts": [
        {"id": "disc_p_pokemon", "keep": True, "score_adj": 0.0,
         "category": "", "niche_kind": "", "reason": "ok"}]})
    ai = AIClassifier(_cfg(), client=_client(
        lambda req: httpx.Response(200, json=_message_body(only_first))))
    out = ai.classify(_cands())
    assert len(out) == 2                       # the unjudged candidate survives


def test_engine_autowires_ai_and_reports_it(tmp_path):
    store = Store(tmp_path / "ai.db")
    cfg = _cfg(db_path=tmp_path / "ai.db", discovery_scan_cap=10, discovery_max_active=10)

    class StubSource:
        id, name, last_error = "stub", "Stub", ""

        def discover(self):
            return _cands()

        def check(self):
            return True, "ok"

    engine = DiscoveryEngine(cfg, store, sources=[StubSource()])
    assert engine.ai is not None and engine.classify_hook is not None
    engine.ai._client = _client(lambda req: httpx.Response(200, json=_message_body(VERDICTS)))
    rep = engine.run()
    assert rep["found"] == 1 and rep["promoted"] == 1       # AI dropped the tennis noise
    assert "kept 1" in rep["ai"]
    assert any(s["id"] == "ai_brain" and s["ok"] for s in engine.status())


def test_engine_without_key_has_no_hook(tmp_path):
    store = Store(tmp_path / "nokey.db")
    cfg = Config(mode="live", db_path=tmp_path / "nokey.db")
    engine = DiscoveryEngine(cfg, store, sources=[])
    assert engine.classify_hook is None
    ai_row = next(s for s in engine.status() if s["id"] == "ai_brain")
    assert not ai_row["ok"] and "keyword" in ai_row["note"]
