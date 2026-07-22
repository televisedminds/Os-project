"""v1.5.0 — ScrapingDog as a general evidence collector (Phase 7).

ScrapingDog is generalized from a Shopee-only price scraper into a page-evidence
collector: it reads the provider pages Serper already surfaces and extracts
competitor pricing + a review/complaint signal, grounding a venture's price
point in what real providers charge instead of an estimate.

All offline — ScrapingDog + Serper are served by httpx.MockTransport. Every
extraction is labelled scraped/experimental and degrades to no-evidence on
failure, never a crash.
"""

import json

import httpx
import pytest

from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market.adapters import ScrapingDogPageAdapter, SerperAdapter
from opportunity_os.market.live import LiveMarket


PROVIDER_PAGE = ("<html><body><h1>รับติดตั้ง EV charger</h1>"
                 "<div>ราคาเริ่มต้น ฿15,000 ต่อจุด — โปรโมชั่น ฿12,000</div>"
                 "<div>Installation from $350 per unit</div>"
                 "<div>รีวิว: บริการดีมาก professional recommend แต่รอนาน ช้า</div>"
                 "<div>โทร 021234567</div></body></html>")


def _page_adapter(cfg=None, text=PROVIDER_PAGE, status=200):
    cfg = cfg or Config(mode="live", scrapingdog_api_key="sd_test")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(status, text=text)))
    return ScrapingDogPageAdapter(cfg, client)


# ------------------------------------------------ adapter extraction

def test_extracts_thb_and_usd_prices_skips_non_prices():
    ev = _page_adapter().page_evidence("https://evfix.co.th/pricing")
    p = ev["prices"]
    # 12000 THB (~$330), 15000 THB (~$412), $350 — the phone number is skipped.
    assert p["n"] == 3
    assert 320 <= p["low_usd"] <= 340
    assert p["high_usd"] >= 400
    assert ev["experimental"] is True


def test_review_signal_counts_both_languages():
    ev = _page_adapter().page_evidence("https://x")
    r = ev["review"]
    assert r["positive_hits"] >= 2 and r["complaint_hits"] >= 1
    assert 0.0 <= r["complaint_ratio"] <= 1.0


def test_fetch_failure_degrades_to_none():
    ad = _page_adapter(status=500)
    assert ad.page_evidence("https://x") is None
    assert "fetch failed" in ad.last_error


def test_unparseable_page_returns_none_with_reason():
    ad = _page_adapter(text="<html>nothing priced, no reviews</html>")
    assert ad.page_evidence("https://x") is None
    assert "not extractable" in ad.last_error


def test_unconfigured_adapter_is_inert():
    ad = ScrapingDogPageAdapter(Config(mode="live"))
    assert ad.fetch("https://x") is None and "SCRAPINGDOG_API_KEY" in ad.last_error
    ok, note = ad.check()
    assert not ok and "SCRAPINGDOG_API_KEY" in note


def test_price_bands_reject_ids_and_years():
    # A page with a year (2026), a big id, and a tiny fee — only the fee is a
    # plausible price; year/id fall outside the bands.
    ad = _page_adapter(text="<html>order #2026 ref 999999999 — service $45</html>")
    ev = ad.page_evidence("https://x")
    assert ev["prices"]["n"] == 1 and ev["prices"]["median_usd"] == 45.0


# ------------------------------------------------ live wiring + price grounding

def _live(tmp_path, niche):
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps({"products": [], "operator": {}, "niches": [niche]}))
    cfg = Config(mode="live", db_path=tmp_path / "c.db", watchlist_path=wlp,
                 serper_api_key="sk", scrapingdog_api_key="sd", discovery_enabled=False,
                 serper_every_n_ticks=1, scrape_every_n_ticks=1)
    store = Store(cfg.db_path)
    organic = [{"title": "EV Fix", "link": "https://evfix.co.th/pricing"},
               {"title": "Charge BKK", "link": "https://chargebkk.com"}]
    serper = SerperAdapter(cfg, httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"organic": organic,
                                              "peopleAlsoAsk": [], "relatedSearches": []}))))
    serper.every_n_ticks = 1
    page = _page_adapter(cfg)
    page.every_n_ticks = 1
    lm = LiveMarket(cfg, store, adapters={"serper": serper, "scrapingdog_page": page})
    return lm, store


NICHE = {"id": "ev_install", "name": "EV charger install TH", "kind": "b2b", "geo": "TH",
         "price_point_usd": 300, "serper_query_th": "ติดตั้ง ev charger ราคา",
         "base_volume": 400, "providers": 4, "solution_count": 4, "demand_posts": 90}


def test_live_scrapes_provider_and_grounds_price(tmp_path):
    lm, store = _live(tmp_path, dict(NICHE))
    for _ in range(3):
        lm.tick()
    comp = store.latest_search_obs("ev_install", "competitor")
    assert comp is not None
    assert comp["payload"]["url"] == "https://evfix.co.th/pricing"
    assert comp["payload"]["prices"]["n"] >= 2

    niche = lm.niches()[0]
    # The estimate (300) is replaced by the observed competitor median.
    assert niche["price_point_usd"] != 300
    assert niche["metrics"]["observed"]["price"] == "observed"
    assert niche["metrics"]["competitor_review"]["positive_hits"] >= 1


def test_live_no_key_leaves_price_estimated(tmp_path):
    lm, store = _live(tmp_path, dict(NICHE))
    lm.adapters.pop("scrapingdog_page")          # no scraper
    for _ in range(3):
        lm.tick()
    assert store.latest_search_obs("ev_install", "competitor") is None
    niche = lm.niches()[0]
    assert niche["metrics"]["observed"]["price"] == "estimated"


def test_competitor_scan_is_stale_gated(tmp_path):
    """Once a competitor obs is fresh, the scraper must not re-spend on it."""

    lm, store = _live(tmp_path, dict(NICHE))
    calls = {"n": 0}
    real = lm.adapters["scrapingdog_page"].page_evidence

    def counting(url, dynamic=False):
        calls["n"] += 1
        return real(url, dynamic=dynamic)

    lm.adapters["scrapingdog_page"].page_evidence = counting
    for _ in range(4):
        lm.tick()
    assert calls["n"] == 1                        # scraped once, then stale-gated for 7 days


# ------------------------------------------------ evidence ledger

def test_ledger_records_competitor_pricing_as_observed(tmp_path):
    from opportunity_os import evidence as EV
    lm, store = _live(tmp_path, dict(NICHE))
    for _ in range(3):
        lm.tick()
    niche = lm.niches()[0]
    cand = {"kind": "venture", "niche": niche, "sources": ["serper"],
            "economics": None, "feasibility": None}
    ledger = EV.build_ledger(cand, None, mode="live", latest_ts=1.0)
    cp = next((i for i in ledger if i["field"] == "competitor_pricing"), None)
    assert cp is not None
    assert cp["kind"] == EV.OBSERVED and cp["source"] == "scrapingdog" and cp["independent"]
    assert "scraped" in cp["value"]


# ------------------------------------------------ diagnostics

def test_diagnostics_probe_verifies_extractor_without_spending(tmp_path):
    from opportunity_os import diagnostics as dx
    store = Store(tmp_path / "d.db")
    rows = dx.probe_sources(Config(mode="live"), store)
    sd = next(r for r in rows if r["id"] == "scrapingdog_page")
    assert sd["status"] == dx.DISABLED            # no key
    assert "verified OK" in sd["note"]            # but the parser was proven on a synthetic page
    assert sd["records_parsed"] >= 1
