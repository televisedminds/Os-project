"""Phase 1/2: source-health probing + funnel tracing. Uses fake adapters so
every status path is exercised deterministically, plus one live-optional test."""

import os

import pytest

from opportunity_os import diagnostics as dx
from opportunity_os.config import Config
from opportunity_os.db import Store


# ------------------------------------------------------- fake adapters

class FakeEbayOK:
    last_error = ""
    def product_snapshot(self, q):
        return {"price": 300.0, "stock": 143,
                "sample": [{"item_id": f"i{i}", "title": "x", "price": 300 + i,
                            "url": "u", "seller": f"s{i}", "condition": ""} for i in range(12)]}

class FakeEbayAuthFail:
    last_error = "eBay OAuth failed: 401 Unauthorized"
    def product_snapshot(self, q):
        return None

class FakeEbayNoData:
    last_error = "no priced results for query"
    def product_snapshot(self, q):
        return None

class FakeShopeeParserBroken:
    last_error = "no parseable Shopee results (site layout may have changed)"
    def product_snapshot(self, q):
        return None

class FakeSerperQuota:
    last_error = "Serper search failed: 429 quota exceeded"
    def reddit_posts_7d(self, q):
        return None


def _cfg(tmp_path, **kw):
    cfg = Config(db_path=tmp_path / "d.db", mode="live")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


# ------------------------------------------------------------- probes

def test_ebay_healthy(tmp_path):
    cfg = _cfg(tmp_path, ebay_client_id="a", ebay_client_secret="b")
    h = dx._probe_ebay(cfg, FakeEbayOK())
    assert h.status == dx.HEALTHY and h.records_parsed == 12 and h.auth_ok


def test_ebay_disabled_without_keys(tmp_path):
    h = dx._probe_ebay(_cfg(tmp_path), FakeEbayOK())
    assert h.status == dx.DISABLED and h.credential_present is False


def test_ebay_auth_failure_classified(tmp_path):
    cfg = _cfg(tmp_path, ebay_client_id="a", ebay_client_secret="b")
    h = dx._probe_ebay(cfg, FakeEbayAuthFail())
    assert h.status == dx.AUTH_FAILED and h.request_ok is False


def test_ebay_no_data_classified(tmp_path):
    cfg = _cfg(tmp_path, ebay_client_id="a", ebay_client_secret="b")
    h = dx._probe_ebay(cfg, FakeEbayNoData())
    assert h.status == dx.NO_DATA


def test_serper_quota_exhausted(tmp_path):
    cfg = _cfg(tmp_path, serper_api_key="k")
    h = dx._probe_serper(cfg, FakeSerperQuota())
    assert h.status == dx.QUOTA_EXHAUSTED


def test_shopee_parser_broken(tmp_path):
    cfg = _cfg(tmp_path, scrapingdog_api_key="k")
    h = dx._probe_shopee(cfg, FakeShopeeParserBroken())
    assert h.status == dx.PARSER_BROKEN


def test_disabled_sources_report_why(tmp_path):
    cfg = _cfg(tmp_path)                          # nothing configured
    for h in (dx._probe_reddit(cfg, None), dx._probe_serper(cfg, None),
              dx._probe_shopee(cfg, None), dx._probe_anthropic(cfg), dx._probe_telegram(cfg)):
        assert h.status == dx.DISABLED
        assert h.note, f"{h.id} disabled but gives no reason"   # no silent zero-results


def test_probe_sources_full_set(tmp_path):
    cfg = _cfg(tmp_path, ebay_client_id="a", ebay_client_secret="b")
    store = Store(cfg.db_path)
    adapters = {"ebay_us": FakeEbayOK(), "reddit": None, "serper": None,
                "news": None, "fx": None, "shopee_th": None}
    rows = dx.probe_sources(cfg, store, adapters)
    ids = {r["id"] for r in rows}
    assert {"ebay_us", "reddit", "serper", "news", "fx", "shopee_th",
            "anthropic", "telegram"} <= ids
    ebay = next(r for r in rows if r["id"] == "ebay_us")
    assert ebay["status"] == dx.HEALTHY
    store.close()


# --------------------------------------------------------------- funnels

def test_source_and_type_funnel_from_stored_opps(tmp_path):
    """Records the very bottleneck the audit is about: eBay dominates
    publications; the type funnel exposes whether counts are inflated."""

    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    # Two eBay flips (same model, different listing → same thesis family) and
    # one venture, to show dedup counting.
    store.upsert_opportunity({
        "id": "o1", "type": "product_arbitrage", "status": "active", "category": "gaming",
        "title": "GBA #1", "entity_id": "gba", "sources": ["ebay_us"],
        "route": {"buy_venue": "ebay_us", "sell_venue": "ebay_us", "kind": "dislocation"},
        "economics": {"total_net_usd": 30, "capital_usd": 40, "kind": "flip", "qty": 1,
                      "base": {"net_usd": 30, "margin_pct": 30}, "thb": {}},
        "score": {"overall": 60}, "confidence": 0.8, "window_days": 7,
        "tick_created": 1, "tick_updated": 1})
    store.upsert_opportunity({
        "id": "o2", "type": "product_arbitrage", "status": "active", "category": "gaming",
        "title": "GBA #2", "entity_id": "gba", "sources": ["ebay_us"],
        "route": {"buy_venue": "ebay_us", "sell_venue": "ebay_us", "kind": "dislocation"},
        "economics": {"total_net_usd": 25, "capital_usd": 35, "kind": "flip", "qty": 1,
                      "base": {"net_usd": 25, "margin_pct": 28}, "thb": {}},
        "score": {"overall": 55}, "confidence": 0.8, "window_days": 7,
        "tick_created": 1, "tick_updated": 1})
    store.upsert_opportunity({
        "id": "o3", "type": "digital_product", "status": "active", "category": "digital_tools",
        "title": "SaaS gap", "entity_id": "niche1", "sources": ["serper", "reddit"],
        "route": {"geo": "global", "kind": "digital_product"},
        "economics": {"total_net_usd": 500, "capital_usd": 120, "kind": "venture", "qty": 1,
                      "base": {"net_usd": 500, "margin_pct": 70}, "thb": {}},
        "score": {"overall": 70}, "confidence": 0.8, "window_days": 30,
        "tick_created": 1, "tick_updated": 1})

    sf = dx.source_funnel(store)
    ebay_row = next(r for r in sf if r["source"] == "ebay_us")
    assert ebay_row["published_opportunities"] == 2       # eBay dominates, quantified

    tf = dx.type_funnel(store)
    assert tf["verified_opportunities"] == 3
    assert tf["unique_products"] == 2                     # gba + niche1
    # o1 and o2 share the same thesis (same type/route/entity) → 2 unique theses
    assert tf["unique_theses"] == 2
    assert tf["by_type"]["dislocation"] == 2
    store.close()


@pytest.mark.skipif(not os.environ.get("OOS_LIVE_TESTS"),
                    reason="set OOS_LIVE_TESTS=1 and real keys to probe live services")
def test_live_probe_smoke(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path)
    rows = dx.probe_sources(cfg, store)
    assert any(r["id"] == "fx" for r in rows)             # keyless FX should be reachable
    store.close()
