"""Live layer: watchlist, adapters (mocked HTTP), and the full pipeline
running over LiveMarket with stub connectors — no network in tests."""

import json

import httpx
import pytest

from opportunity_os import economics
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market import watchlist as wl
from opportunity_os.market.adapters import EbayAdapter, FxAdapter, NewsAdapter, RedditAdapter
from opportunity_os.market.live import LiveMarket
from opportunity_os.notify import briefing_text
from opportunity_os.pipeline import Orchestrator, briefing

# ------------------------------------------------------------------ fixtures


def write_watchlist(tmp_path, products=None, niches=None):
    p = tmp_path / "watchlist.json"
    p.write_text(json.dumps({"products": products or [], "niches": niches or []}))
    return p


PRODUCT = {
    "id": "gba", "name": "GBA SP AGS-101", "category": "gaming", "weight_kg": 0.4,
    "queries": {"ebay_us": "gba sp ags-101"},
    "manual_listings": {"shopee_th": {"price_usd": 45.0, "stock": 9, "sellers": 3, "sold_7d": 2,
                                      "note": "test quote"}},
    "bootstrap_sold_7d": {"ebay_us": 8},
    "news_query": "gameboy prices",
}
NICHE = {
    "id": "th_tax", "name": "Thai freelancer tax guide", "kind": "info", "geo": "TH",
    "price_point_usd": 15, "reddit_query": "thailand tax",
    "base_volume": 8000, "solution_count": 2, "providers": 2, "demand_posts": 120,
}


@pytest.fixture
def live_cfg(tmp_path):
    path = write_watchlist(tmp_path, [PRODUCT], [NICHE])
    return Config(db_path=tmp_path / "live.db", mode="live", watchlist_path=path,
                  discovery_enabled=False)   # discovery is exercised in test_discovery.py


class FakeEbay:
    """Scripted eBay: stable price, one listing disappears per tick."""

    name = "fake eBay"
    last_error = ""

    def __init__(self):
        self.calls = 0

    def configured(self):
        return True

    def product_snapshot(self, query):
        self.calls += 1
        ids = [f"item{i}" for i in range(self.calls, self.calls + 30)]   # one id rotates out per call
        return {"price": 129.0, "min_price": 110.0, "stock": 30, "sellers": 12, "item_ids": ids}

    def check(self):
        return True, "ok"


class FakeReddit:
    name = "fake Reddit"
    last_error = ""

    def __init__(self, series):
        self.series = list(series)
        self.i = 0

    def mentions_24h(self, query):
        v = self.series[min(self.i, len(self.series) - 1)]
        self.i += 1
        return v

    def check(self):
        return True, "ok"


class FakeNews:
    name = "fake News"
    last_error = ""

    def headlines(self, query, n=4):
        return ["Retro handheld demand climbs as collectors chase AGS-101 units"]

    def check(self):
        return True, "ok"


class FakeFx:
    name = "fake FX"
    last_error = ""

    def rates(self):
        return {"THB": 33.5, "JPY": 160.0}

    def check(self):
        return True, "ok"


def stub_adapters(reddit_series=(5, 5, 5, 5, 6, 18, 24, 30, 34, 36)):
    return {"ebay_us": FakeEbay(), "reddit": FakeReddit(reddit_series),
            "news": FakeNews(), "fx": FakeFx()}


# ----------------------------------------------------------------- watchlist


def test_serper_adapter_counts_and_degrades():
    from opportunity_os.market.adapters import SerperAdapter
    cfg = Config(mode="live", serper_api_key="sk-serper")

    def handler(req):
        assert "google.serper.dev" in str(req.url)
        assert req.headers["X-API-KEY"] == "sk-serper"
        body = json.loads(req.content)
        assert body["q"].startswith("site:reddit.com ") and body["tbs"] == "qdr:w"
        return httpx.Response(200, json={"organic": [{"title": "a"}, {"title": "b"}]})

    ad = SerperAdapter(cfg, _client(handler))
    assert ad.reddit_posts_7d("thai freelance tax") == 2

    dead = SerperAdapter(cfg, _client(lambda req: httpx.Response(500)))
    assert dead.reddit_posts_7d("x") is None and "Serper" in dead.last_error
    assert not SerperAdapter(Config(mode="live")).configured()


def test_serper_feeds_niche_demand_when_reddit_dark(live_cfg):
    """No Reddit key → the Serper series drives niche momentum instead. The fair
    scheduler (M4.1) alternates demand/supply and stops once evidence is
    sufficient, so the series accrues toward the demand-point floor with real
    momentum rather than one point per tick forever."""

    from opportunity_os.market.adapters import SerperAdapter
    store = Store(live_cfg.db_path)
    live_cfg.serper_api_key = "sk-serper"
    live_cfg.measure_cooldown_ticks = 0                      # no throttle in this every-tick test
    counts = iter([2, 2, 2, 3, 6, 9, 10, 10, 10, 10, 10, 10, 10])

    def handler(req):
        return httpx.Response(200, json={"organic": [{}] * next(counts)})

    adapters = stub_adapters()
    del adapters["reddit"]                                   # reddit fully dark
    serper = SerperAdapter(live_cfg, _client(handler))
    serper.every_n_ticks = 1                                 # measure every pass
    adapters["serper"] = serper
    lm = LiveMarket(live_cfg, store, adapters=adapters)
    for _ in range(12):
        lm.tick()
    series = store.live_mention_series("th_tax", "serper", 30)
    assert len(series) >= 4 and series == sorted(series)     # real, non-decreasing demand series
    assert series[-1] >= 6
    metrics = lm.niches()[0]["metrics"]
    assert metrics["growth_pct"] > 0                         # momentum from the serper series
    assert "serper" in lm.social_sources()                   # council sees it as corroboration
    # The scheduler also ran a one-off supply scan and stored the observation;
    # its 0-provider result must NOT override the operator's typed estimate.
    assert store.latest_search_obs("th_tax", "supply") is not None
    assert metrics["observed"]["supply"] == "user_supplied"


def test_tiered_scheduler_budgets_scans_by_priority(tmp_path):
    """Watchlist entities rescan every cycle; warm discoveries every 4th;
    the cold tail every 12th — same API budget, several times the coverage."""

    from opportunity_os.discovery import Candidate
    from dataclasses import asdict
    path = write_watchlist(tmp_path, [PRODUCT], [])
    cfg = Config(db_path=tmp_path / "tier.db", mode="live", watchlist_path=path,
                 discovery_enabled=False, scan_warm_slots=1,
                 scan_warm_interval=4, scan_cold_interval=12)
    store = Store(cfg.db_path)
    for i, score in enumerate([2.0, 1.0]):
        store.upsert_discovered(asdict(Candidate(
            kind="product", id=f"disc_p_t{i}", name=f"T{i}", source="stub",
            score=score, queries={"ebay_us": f"tier query {i}"})))

    calls: dict[str, int] = {}

    class CountingEbay:
        name, last_error = "fake eBay", ""

        def configured(self):
            return True

        def product_snapshot(self, query):
            calls[query] = calls.get(query, 0) + 1
            return {"price": 100.0, "min_price": 90.0, "stock": 30, "sellers": 9,
                    "item_ids": ["a"]}

        def check(self):
            return True, "ok"

    adapters = stub_adapters()
    adapters["ebay_us"] = CountingEbay()
    lm = LiveMarket(cfg, store, adapters=adapters)
    # discovered entities must be observed even with the engine off in tests
    lm.discovery = type("D", (), {"extra_products": lambda s, ids: [
        wl.WatchProduct(id=f"disc_p_t{i}", name=f"T{i}", category="collectibles",
                        weight_kg=0.5, queries={"ebay_us": f"tier query {i}"})
        for i in range(2)], "extra_niches": lambda s, ids: [],
        "run": lambda s: {}, "status": lambda s: [], "ai": None})()
    for _ in range(24):
        lm.tick()

    assert calls["gba sp ags-101"] == 24                    # watchlist = hot, every cycle
    assert calls["tier query 0"] == 6                       # warm: every 4th
    assert calls["tier query 1"] == 2                       # cold: every 12th


def test_briefing_names_the_missing_keys(live_cfg):
    """A blocked pipeline must say WHICH key it is waiting for — silence about
    a missing key reads as 'the app is broken'."""

    store = Store(live_cfg.db_path)
    orch = Orchestrator(live_cfg, store, world=LiveMarket(live_cfg, store, adapters=stub_adapters()))
    orch.run_cycle()
    notes = " ".join(briefing(store, live_cfg)["notes"])
    assert "eBay key" in notes and "Reddit key" in notes       # no keys set in live_cfg

    live_cfg.ebay_client_id = "id"
    live_cfg.reddit_client_id = "rid"
    notes2 = " ".join(briefing(store, live_cfg)["notes"])
    assert "eBay key" not in notes2 and "Reddit key" not in notes2


def test_watchlist_validation(tmp_path):
    bad = write_watchlist(tmp_path, [{"id": "x", "name": "X", "category": "toys", "weight_kg": 1,
                                      "queries": {"nope_venue": "q"}}])
    with pytest.raises(ValueError, match="unknown venue"):
        wl.load(bad)
    with pytest.raises(FileNotFoundError):
        wl.load(tmp_path / "missing.json")
    ok = write_watchlist(tmp_path / "sub" if False else tmp_path, [PRODUCT], [NICHE])
    w = wl.load(ok)
    assert w.product("gba").venues() == ["ebay_us", "shopee_th"]


def test_watchlist_falls_back_to_example_when_missing(tmp_path):
    """Live mode must BOOT on the bundled starter seed instead of crashing when
    the operator hasn't created watchlist.json yet — the #1 blocker to going live."""

    missing = tmp_path / "nope_watchlist.json"
    w = wl.load_or_example(missing)
    assert w.products                                   # real starter products loaded
    assert w.path == missing                            # edits still expected at the operator's file
    # the bundled example is itself valid (fallback can't raise on a broken seed)
    assert wl.load(wl.EXAMPLE_PATH).products
    # a real file still wins over the fallback
    real = write_watchlist(tmp_path, [PRODUCT], [NICHE])
    assert wl.load_or_example(real).product("gba") is not None


def test_manual_listing_requires_price(tmp_path):
    bad = write_watchlist(tmp_path, [{"id": "x", "name": "X", "category": "toys", "weight_kg": 1,
                                      "manual_listings": {"shopee_th": {"stock": 3}}}])
    with pytest.raises(ValueError, match="price_thb or price_usd"):
        wl.load(bad)


# ------------------------------------------------------------------ adapters


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fx_adapter_primary_and_fallback():
    cfg = Config(mode="live")

    def ok_handler(req):
        assert "open.er-api.com" in str(req.url)
        return httpx.Response(200, json={"result": "success", "rates": {"THB": 33.1, "JPY": 159.0}})

    fx = FxAdapter(cfg, _client(ok_handler))
    assert fx.rates() == {"THB": 33.1, "JPY": 159.0}

    def fallback_handler(req):
        if "er-api" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"rates": {"THB": 34.0, "JPY": 161.0}})

    fx2 = FxAdapter(cfg, _client(fallback_handler))
    assert fx2.rates()["THB"] == 34.0


def test_set_fx_updates_economics():
    old = economics.USD_THB
    try:
        economics.set_fx(usd_thb=30.0, usd_jpy=150.0)
        assert economics.usd_to_thb(10) == 300.0
        assert economics.convert(150, "JPY", "USD") == pytest.approx(1.0)
    finally:
        economics.set_fx(usd_thb=old, usd_jpy=147.9)


def test_ebay_adapter_oauth_search_and_token_cache():
    cfg = Config(mode="live", ebay_client_id="id", ebay_client_secret="sec")
    calls = {"token": 0, "search": 0}

    def handler(req):
        if "identity/v1/oauth2/token" in str(req.url):
            calls["token"] += 1
            assert req.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        calls["search"] += 1
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
        return httpx.Response(200, json={
            "total": 143,
            "itemSummaries": [
                {"itemId": "a", "title": "GBA SP AGS-101", "itemWebUrl": "http://x/a",
                 "price": {"value": "120.00", "currency": "USD"},
                 "seller": {"username": "s1"}, "condition": "USED_GOOD"},
                {"itemId": "b", "title": "GBA SP AGS-101 boxed", "itemWebUrl": "http://x/b",
                 "price": {"value": "129.00", "currency": "USD"},
                 "seller": {"username": "s2"}},
                {"itemId": "c", "title": "GBA SP AGS-001", "itemWebUrl": "http://x/c",
                 "price": {"value": "300.00", "currency": "USD"},
                 "seller": {"username": "s1"}},
            ]})

    ad = EbayAdapter(cfg, _client(handler))
    snap = ad.product_snapshot("gba sp")
    # Aggregate stats unchanged, plus the full per-listing sample for research.
    assert snap["price"] == 129.0 and snap["min_price"] == 120.0
    assert snap["stock"] == 143 and snap["sellers"] == 2
    assert snap["item_ids"] == ["a", "b", "c"]
    assert len(snap["sample"]) == 3
    assert snap["sample"][0] == {"item_id": "a", "title": "GBA SP AGS-101",
                                 "price": 120.0, "url": "http://x/a", "seller": "s1",
                                 "condition": "USED_GOOD"}
    ad.product_snapshot("gba sp")
    assert calls["token"] == 1 and calls["search"] == 2       # token reused

    unconfigured = EbayAdapter(Config(mode="live"), _client(handler))
    assert unconfigured.product_snapshot("x") is None
    assert "EBAY_CLIENT_ID" in unconfigured.last_error


def test_reddit_adapter_oauth_flow():
    cfg = Config(mode="live", reddit_client_id="rid", reddit_client_secret="rsec")

    def handler(req):
        if "access_token" in str(req.url):
            return httpx.Response(200, json={"access_token": "rtok", "expires_in": 3600})
        assert "oauth.reddit.com" in str(req.url)
        return httpx.Response(200, json={"data": {"children": [{}, {}, {}]}})

    ad = RedditAdapter(cfg, _client(handler))
    assert ad.mentions_24h("thailand tax") == 3


def test_scrapingdog_shopee_adapter_parses_and_degrades():
    from opportunity_os.market.adapters import ScrapingDogShopeeAdapter

    no_key = ScrapingDogShopeeAdapter(Config(mode="live"))
    assert no_key.product_snapshot("gimbal") is None
    assert "SCRAPINGDOG_API_KEY" in no_key.last_error

    cfg = Config(mode="live", scrapingdog_api_key="sd_test")

    def handler(req):
        assert "api.scrapingdog.com" in str(req.url)
        assert req.url.params["api_key"] == "sd_test"
        assert "shopee.co.th" in req.url.params["url"]
        return httpx.Response(200, json={"items": [
            {"item_basic": {"price": 179000000, "sold": 40, "stock": 25, "shopid": 1, "itemid": 111}},
            {"item_basic": {"price": 185000000, "sold": 12, "stock": 10, "shopid": 2, "itemid": 222}},
            {"item_basic": {"price": 209000000, "sold": 4, "stock": 5, "shopid": 1, "itemid": 333}},
        ]})

    ad = ScrapingDogShopeeAdapter(cfg, _client(handler))
    snap = ad.product_snapshot("กันสั่นมือถือ gimbal")
    assert snap["sellers"] == 2 and snap["stock"] == 40
    assert snap["sold_7d_hint"] == 14                        # (40+12+4)/4
    assert snap["item_ids"] == ["111", "222", "333"]
    # ฿1,850 median at the current USD_THB rate
    assert snap["price"] == pytest.approx(1850 / economics.USD_THB, rel=0.01)
    assert ad.every_n_ticks == 4                             # credit-saving cadence


def test_news_adapter_parses_rss():
    rss = ('<?xml version="1.0"?><rss><channel>'
           "<item><title>Headline one</title></item>"
           "<item><title>Headline two</title></item></channel></rss>")
    ad = NewsAdapter(Config(mode="live"),
                     _client(lambda req: httpx.Response(200, text=rss)))
    assert ad.headlines("q") == ["Headline one", "Headline two"]


# ----------------------------------------------------- live market + pipeline


def test_live_market_persists_and_estimates(live_cfg):
    store = Store(live_cfg.db_path)
    lm = LiveMarket(live_cfg, store, adapters=stub_adapters())
    for _ in range(7):
        lm.tick()

    assert lm.venue_ids() == ["ebay_us", "shopee_th"]
    ebay = lm.listing("gba", "ebay_us")
    assert ebay["price"] == 129.0 and ebay["stock"] == 30
    assert ebay["sold_7d"] >= 7                    # bootstrap + disappearance estimate
    manual = lm.listing("gba", "shopee_th")
    assert manual == {"price": 45.0, "stock": 9, "sellers": 3, "sold_7d": 2}
    assert len(lm.product_history("gba", "ebay_us")) == 7
    assert lm.mentions("th_tax", "reddit")         # recorded series
    assert lm.event_log("gba")[0]["etype"] == "news"
    assert economics.USD_THB == 33.5               # live FX installed
    n = lm.niches()[0]
    assert n["metrics"]["volume"] > 8000           # momentum > 1 after the ramp
    store.close()


def test_full_live_pipeline_publishes_verified_flip_and_niche(live_cfg):
    store = Store(live_cfg.db_path)
    lm = LiveMarket(live_cfg, store, adapters=stub_adapters())
    orch = Orchestrator(live_cfg, store, world=lm)
    last = None
    for _ in range(8):
        last = orch.run_cycle()

    actives = store.active_opportunities()
    by_entity = {o["entity_id"]: o for o in actives}
    assert "gba" in by_entity, f"flip not published; last report: {last}"
    flip = by_entity["gba"]
    assert flip["route"]["buy_venue"] == "shopee_th"
    assert flip["route"]["sell_venue"] == "ebay_us"
    assert flip["economics"]["pessimistic"]["net_usd"] > 0
    assert flip["playbook"]["steps"]

    assert "th_tax" in by_entity, "info niche not published after demand ramp"
    b = briefing(store, live_cfg)
    assert "opportunities worth your attention" in b["headline"]
    assert briefing_text(b).startswith("◆ OPPORTUNITY OS")
    store.close()


class FakeEbayDislocation:
    """A deep eBay market for one product with a single, persistent, genuinely
    underpriced listing — the raw material of a dislocation flip."""

    name = "fake eBay dislocation"
    last_error = ""

    def configured(self):
        return True

    def product_snapshot(self, query):
        sample = [{"item_id": f"fair{i}", "title": "GBA SP AGS-101 boxed tested",
                   "price": 128.0 + i, "url": f"https://ebay.com/itm/fair{i}",
                   "seller": f"seller{i}", "condition": "USED_GOOD"} for i in range(10)]
        sample.append({"item_id": "BARGAIN", "title": "GBA SP AGS-101 works great",
                       "price": 62.0, "url": "https://ebay.com/itm/BARGAIN",
                       "seller": "motivated_seller", "condition": "USED_GOOD"})
        prices = [s["price"] for s in sample]
        return {"price": 130.0, "min_price": min(prices), "stock": 40, "sellers": 11,
                "item_ids": [s["item_id"] for s in sample], "sample": sample}

    def check(self):
        return True, "ok"


def test_live_pipeline_publishes_dislocation_with_exact_url(tmp_path):
    path = write_watchlist(tmp_path, products=[{
        "id": "gba", "name": "GBA SP AGS-101", "category": "gaming", "weight_kg": 0.4,
        "queries": {"ebay_us": "gba sp ags-101"}, "manual_listings": {}}])
    cfg = Config(db_path=tmp_path / "live.db", mode="live", watchlist_path=path,
                 discovery_enabled=False)
    store = Store(cfg.db_path)
    adapters = {"ebay_us": FakeEbayDislocation(), "reddit": FakeReddit((5,) * 10),
                "news": FakeNews(), "fx": FakeFx()}
    lm = LiveMarket(cfg, store, adapters=adapters)
    orch = Orchestrator(cfg, store, world=lm)
    last = None
    for _ in range(6):
        last = orch.run_cycle()

    disl = [o for o in store.active_opportunities()
            if o.get("route", {}).get("kind") == "dislocation"]
    assert disl, f"no dislocation published; last report: {last}"
    o = disl[0]
    assert o["route"]["item_id"] == "BARGAIN"
    assert o["route"]["buy_url"] == "https://ebay.com/itm/BARGAIN"   # exact listing
    assert o["route"]["buy_venue"] == "ebay_us" and o["route"]["sell_venue"] == "ebay_us"
    assert o["economics"]["pessimistic"]["net_usd"] > 0

    # The action card must hand over the exact buy URL, not a search.
    from opportunity_os.api import _action_card
    card = _action_card(o, {}, resolve=lambda v: (None, None))
    assert card["dislocation"] is True
    assert card["buy"]["url"] == "https://ebay.com/itm/BARGAIN"

    # The research yield ledger recorded the win against the entity.
    totals = store.yield_totals()
    assert totals["scans"] > 0 and totals["published"] > 0
    store.close()


def test_mode_guard_blocks_cross_mode_db(live_cfg):
    store = Store(live_cfg.db_path)
    LiveMarket(live_cfg, store, adapters=stub_adapters())     # no guard yet — orchestrator guards
    Orchestrator(live_cfg, store, world=LiveMarket(live_cfg, store, adapters=stub_adapters()))
    demo_cfg = Config(db_path=live_cfg.db_path, mode="demo")
    with pytest.raises(SystemExit, match="created in 'live' mode"):
        Orchestrator(demo_cfg, store, world=object())
    store.close()
