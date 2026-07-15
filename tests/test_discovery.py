"""Discovery engine: helpers, the three sources (mocked HTTP, no network),
the ranking/persistence engine, and a full live cycle that observes and
verifies an auto-discovered entity."""

import httpx
import pytest

from opportunity_os import discovery as disc
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.discovery import (Candidate, DiscoveryEngine, EbayBrowseDiscovery,
                                      GoogleTrendsDiscovery, HackerNewsDiscovery,
                                      RedditDiscovery)
from opportunity_os.market.live import LiveMarket
from opportunity_os.pipeline import Orchestrator
from tests.test_live import stub_adapters, write_watchlist


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------- helpers


def test_relevance_and_inference():
    assert disc.commerce_relevance("Pokemon booster box sold out, flip for profit") >= 0.9
    assert disc.commerce_relevance("paula badosa tennis result") == 0.0
    assert disc.infer_category("Pokemon 151 booster box") == "trading_cards"
    assert disc.infer_category("New Balance 2002R") == "sneakers"
    assert disc.infer_niche_kind("SaaS dashboard tool") == "digital"
    assert disc.infer_niche_kind("freelancer tax filing guide") == "info"
    assert disc.slug("Pokémon 151!!", "disc_p").startswith("disc_p_")
    assert disc.clean_query("How to buy the LEGO Bonsai Tree for you") == "buy lego bonsai tree"


# ---------------------------------------------------------- Google Trends


TRENDS_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
  <item>
    <title>Pokemon 151 booster box</title>
    <ht:approx_traffic>2,000+</ht:approx_traffic>
    <ht:news_item><ht:news_item_title>Sold out — restock news for collectors</ht:news_item_title></ht:news_item>
  </item>
  <item>
    <title>paula badosa</title>
    <ht:approx_traffic>100+</ht:approx_traffic>
    <ht:news_item><ht:news_item_title>tennis match live result</ht:news_item_title></ht:news_item>
  </item>
</channel></rss>"""


def test_google_trends_filters_noise():
    cfg = Config(mode="live")
    src = GoogleTrendsDiscovery(cfg, _client(lambda req: httpx.Response(200, text=TRENDS_RSS)))
    cands = src.discover()
    assert len(cands) == 1                     # the tennis trend is dropped as non-commerce
    c = cands[0]
    assert c.kind == "product" and c.category == "trading_cards"
    assert c.id.startswith("disc_p_") and "ebay_us" in c.queries
    ok, note = src.check()
    assert ok and "1" in note


# ------------------------------------------------------------- Hacker News


HN_HITS = {"hits": [
    {"title": "Ask HN: Is there a tool for tracking freelance invoices across clients?",
     "points": 45, "num_comments": 30},
    {"title": "Ask HN: Should I take the Cambridge masters or the Amazon offer?",
     "points": 120, "num_comments": 200},
    {"title": "Tell HN: I wish there was an app for splitting condo utility bills",
     "points": 18, "num_comments": 12},
]}


def test_hackernews_keeps_only_unmet_needs():
    src = HackerNewsDiscovery(Config(mode="live"),
                              _client(lambda req: httpx.Response(200, json=HN_HITS)))
    cands = src.discover()
    names = [c.name for c in cands]
    assert len(cands) == 2                       # the careers post is dropped
    assert all(c.kind == "niche" and c.id.startswith("disc_n_") for c in cands)
    assert "Is there a tool for tracking freelance invoices across clients?" in names
    invoice = next(c for c in cands if "invoice" in c.name)
    assert invoice.niche_kind == "digital" and invoice.score > 0.5
    assert "Hacker News" in invoice.reason
    ok, note = src.check()
    assert ok and "2 unmet-need" in note


def test_hackernews_failure_degrades():
    src = HackerNewsDiscovery(Config(mode="live"),
                              _client(lambda req: httpx.Response(500)))
    assert src.discover() == []
    assert "HN fetch failed" in src.last_error


def test_gap_relevance():
    assert disc.gap_relevance("Is there a tool for invoice tracking?") > 0.5
    assert disc.gap_relevance("Why is there no good alternative to X?") > 0.5
    assert disc.gap_relevance("My favourite programming language in 2026") == 0.0


# ------------------------------------------------------------------ Reddit


def _reddit_handler(req):
    if "access_token" in str(req.url):
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    return httpx.Response(200, json={"data": {"children": [
        {"data": {"title": "Sold out Pokemon booster box flip for big profit", "score": 300, "num_comments": 40}},
        {"data": {"title": "I built a SaaS analytics dashboard tool for shops", "score": 120, "num_comments": 15}},
    ]}})


def test_reddit_discovery_products_and_niches():
    cfg = Config(mode="live", reddit_client_id="rid", reddit_client_secret="rsec")
    src = RedditDiscovery(cfg, _client(_reddit_handler))
    cands = src.discover()
    assert cands, "expected candidates from mocked subreddits"
    kinds = {c.kind for c in cands}
    assert "product" in kinds and "niche" in kinds
    prod = next(c for c in cands if c.kind == "product")
    assert "ebay_us" in prod.queries and prod.reason.startswith("Hot in r/")
    ok, note = src.check()
    assert ok and "OAuth OK" in note


def test_reddit_discovery_needs_keys():
    src = RedditDiscovery(Config(mode="live"), _client(_reddit_handler))
    ok, note = src.check()
    assert not ok and "REDDIT_CLIENT_ID" in note


# -------------------------------------------------------------------- eBay


class _FakeEbayAd:
    name = "fake eBay"
    last_error = ""

    def configured(self):
        return True

    def product_snapshot(self, query):
        return {"price": 100.0, "min_price": 60.0, "stock": 30, "sellers": 9, "item_ids": ["v1|1|0"]}

    def check(self):
        return True, "OAuth OK"


def test_ebay_discovery_scores_spread():
    cfg = Config(mode="live", ebay_client_id="id", ebay_client_secret="sec")
    src = EbayBrowseDiscovery(cfg, ebay_adapter=_FakeEbayAd())
    cands = src.discover()
    assert len(cands) == len(src.seeds)
    assert all(c.kind == "product" and "ebay_us" in c.queries for c in cands)
    assert all(c.score > 0 for c in cands)


# ------------------------------------------------------------------ engine


class StubSource(disc.DiscoverySource):
    id = "stub"
    name = "Stub"

    def __init__(self, candidates):
        self._c = candidates
        self.last_error = ""

    def discover(self):
        return list(self._c)

    def check(self):
        return True, "ok"


def _cands(n, base_score=1.0):
    return [Candidate(kind="niche", id=f"disc_n_{i}", name=f"Niche {i}", source="stub",
                      score=base_score - i * 0.1, niche_kind="info") for i in range(n)]


def test_engine_promotes_dedupes_and_caps(tmp_path):
    store = Store(tmp_path / "d.db")
    cfg = Config(mode="live", discovery_scan_cap=2, discovery_max_active=2)
    eng = DiscoveryEngine(cfg, store, sources=[StubSource(_cands(3))])
    rep = eng.run()
    assert rep["found"] == 3 and rep["promoted"] == 2      # scan cap keeps the top 2 by score
    # second run: same ids already known → nothing newly promoted
    rep2 = eng.run()
    assert rep2["promoted"] == 0
    active = store.list_discovered(active_only=True)
    assert len(active) == 2 and active[0]["score"] >= active[1]["score"]


def test_engine_expiry_respects_max_active(tmp_path):
    store = Store(tmp_path / "d.db")
    cfg = Config(mode="live", discovery_scan_cap=10, discovery_max_active=1)
    DiscoveryEngine(cfg, store, sources=[StubSource(_cands(4))]).run()
    assert store.discovered_counts()["active"] == 1        # capped down to 1 active


def test_upsert_discovered_reports_new_vs_seen(tmp_path):
    store = Store(tmp_path / "d.db")
    c = Candidate(kind="niche", id="disc_n_x", name="X", source="stub", score=1.0)
    from dataclasses import asdict
    assert store.upsert_discovered(asdict(c)) is True       # first time
    assert store.upsert_discovered(asdict(c)) is False      # already known


# -------------------------------------------------------- full live cycle


def test_discovered_entity_flows_through_pipeline(tmp_path):
    path = write_watchlist(tmp_path, products=[], niches=[])
    cfg = Config(db_path=tmp_path / "live.db", mode="live", watchlist_path=path,
                 discovery_enabled=False)          # inject our own engine below
    store = Store(cfg.db_path)
    product = Candidate(kind="product", id="disc_p_gba", name="Game Boy Advance SP",
                        source="stub", score=1.5, category="gaming",
                        queries={"ebay_us": "gba sp ags-101"}, reddit_query="gba sp")
    niche = Candidate(kind="niche", id="disc_n_tax", name="Freelancer tax guide",
                      source="stub", score=1.2, niche_kind="info", reddit_query="freelance tax")
    engine = DiscoveryEngine(cfg, store, sources=[StubSource([product, niche])])
    world = LiveMarket(cfg, store, adapters=stub_adapters(), discovery=engine)
    orch = Orchestrator(cfg, store, world=world)

    for _ in range(3):
        orch.run_cycle()

    # the discovery sweep promoted both, and the fleet now observes them
    assert store.discovered_counts()["active"] == 2
    assert "disc_p_gba" in world.product_ids()
    assert "disc_n_tax" in [n["id"] for n in world.niches()]
    # the discovered product got real (stubbed) eBay observations
    assert world.listing("disc_p_gba", "ebay_us") is not None
    # and the cycle report carries the discovery summary
    rep = store.recent_cycles(3)[-1]["report"]
    assert rep["discovered"]["promoted"] == 2
