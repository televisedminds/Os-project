"""v1.1.0 — the honest multi-type evidence layer.

Covers the root-cause fixes for the eBay-US-only feed:

1. Serper is an evidence COLLECTOR (demand/supply observations, TH + EN),
   not just a result counter, and its records land in `live_search_obs`.
2. Discovered niches carry NO fabricated baselines — demand/supply are
   observed, user-supplied, or explicitly unknown.
3. The verification council refuses to "confirm" a supply gap that was never
   observed (research-required instead of fiction), and the evidence ledger
   records true provenance for venture claims.
4. The SerperGapDiscovery source turns Thai/EN unmet-need searches into niche
   candidates with the right kind/geo, so Thailand candidates enter the funnel.

All offline — Serper is served by an httpx.MockTransport.
"""

import json

import httpx
import pytest

from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.discovery import Candidate, SerperGapDiscovery, gap_relevance, infer_niche_kind
from opportunity_os.market.adapters import SerperAdapter


# ---------------------------------------------------------------- fixtures

SERPER_BODY = {
    "organic": [
        {"title": "ร้านซ่อมรองเท้าแตะ", "link": "https://shoefix.co.th/repair", "snippet": "รับซ่อม"},
        {"title": "Cobbler Bangkok", "link": "https://www.cobblerbkk.com/", "snippet": "shoe repair"},
        {"title": "Pantip thread", "link": "https://pantip.com/topic/123", "snippet": "หาไม่เจอ"},
        {"title": "Reddit thread", "link": "https://reddit.com/r/Thailand/abc", "snippet": "..."},
        {"title": "Second page of shoefix", "link": "https://shoefix.co.th/about", "snippet": "..."},
    ],
    "peopleAlsoAsk": [
        {"question": "มีใครรับ ซ่อมรองเท้าหนัง ไหม"},
        {"question": "What is shoe repair called?"},
    ],
    "relatedSearches": [
        {"query": "ร้านซ่อมรองเท้า ใกล้ฉัน แนะนำร้าน"},
        {"query": "shoe glue"},
    ],
}


def _serper(cfg=None, body=None, status=200):
    cfg = cfg or Config(mode="live", serper_api_key="test-key")
    handler = lambda req: httpx.Response(status, json=body if body is not None else SERPER_BODY)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SerperAdapter(cfg, client)


# ------------------------------------------------- adapter: evidence collectors

def test_supply_observation_counts_commercial_domains_once():
    obs = _serper().supply_observation("ซ่อมรองเท้า", gl="th", hl="th")
    # shoefix.co.th (deduped) + cobblerbkk.com — pantip/reddit are demand, not supply
    assert obs["provider_count"] == 2
    doms = {p["domain"] for p in obs["providers"]}
    assert doms == {"shoefix.co.th", "cobblerbkk.com"}
    assert obs["gl"] == "th" and obs["query"] == "ซ่อมรองเท้า"


def test_demand_observation_captures_questions_and_related():
    obs = _serper().demand_observation("ซ่อมรองเท้า", gl="th", hl="th")
    assert obs["results"] == 5
    assert any("ซ่อมรองเท้าหนัง" in q for q in obs["questions"])
    assert any("ใกล้ฉัน" in r for r in obs["related"])
    assert obs["top"][0]["url"].startswith("https://shoefix.co.th")


def test_serper_failure_degrades_not_raises():
    ad = _serper(body={"error": "quota"}, status=429)
    assert ad.demand_observation("x") is None
    assert ad.supply_observation("x") is None
    assert "Serper search failed" in ad.last_error


# ------------------------------------------------- discovered niches: no fiction

def test_discovered_niche_has_no_fabricated_baselines():
    n = Candidate(kind="niche", id="disc_n_x", name="X", source="serper_gaps",
                  niche_kind="local", geo="TH").to_watch_niche()
    assert n.base_volume == 0.0 and n.solution_count == 0
    assert n.providers == 0 and n.demand_posts == 0.0


def test_niche_metrics_provenance(tmp_path):
    from opportunity_os.market.live import LiveMarket
    from opportunity_os.market import watchlist as wl

    cfg = Config(mode="live", db_path=tmp_path / "m.db", discovery_enabled=False,
                 watchlist_path=tmp_path / "none.json")
    store = Store(cfg.db_path)
    lm = LiveMarket.__new__(LiveMarket)          # metrics only — no adapters needed
    lm.cfg, lm.db = cfg, store

    disc = Candidate(kind="niche", id="disc_n_y", name="Y", source="serper_gaps",
                     niche_kind="local", geo="TH").to_watch_niche()
    m = lm._niche_metrics(disc, None)
    assert m["observed"] == {"demand": "unknown", "supply": "unknown",
                             "demand_series": "serper", "price": "estimated"}
    assert m["volume"] == 0.0 and m["solution_count"] == 0

    # A measured mention series + a stored supply scan flip provenance to observed.
    for i, c in enumerate([2, 2, 2, 3, 3, 4, 5]):
        store.add_live_mention("disc_n_y", "serper", i + 1, c)
    store.add_search_obs("disc_n_y", "serper", "supply", 8, "ซ่อม Y",
                         {"provider_count": 2,
                          "providers": [{"domain": "a.co.th"}, {"domain": "b.com"}]},
                         geo="th", lang="th")
    m2 = lm._niche_metrics(disc, 5)
    assert m2["observed"]["demand"] == "observed" and m2["observed"]["supply"] == "observed"
    assert m2["demand_posts"] == 150.0            # 5/day × 30 — measured, not invented
    assert m2["solution_count"] == 2 and m2["supply_domains"] == ["a.co.th", "b.com"]

    # Hand-typed watchlist niches stay the operator's own numbers.
    user = wl.WatchNiche(id="n_user", name="U", kind="local", geo="TH",
                         price_point_usd=40, base_volume=900, solution_count=4,
                         providers=4, demand_posts=80)
    mu = lm._niche_metrics(user, None)
    assert mu["observed"]["demand"] == "user_supplied"
    assert mu["observed"]["supply"] == "user_supplied"
    assert mu["providers"] == 4


# ------------------------------------------------- council: research-required

def _venture_cand(metrics, kind="local", opp_type=None):
    from opportunity_os.models import OppType
    from opportunity_os import economics, thailand
    niche = {"id": "n1", "name": "N", "kind": kind, "geo": "TH",
             "price_point_usd": 40.0, "metrics": metrics}
    econ = economics.compute_venture(niche)
    return {
        "kind": "venture", "opp_type": opp_type or OppType.LOCAL_SERVICE,
        "entity_id": "n1", "niche": niche, "economics": econ,
        "feasibility": thailand.feasibility("local_service", "local_services", None, None),
        "sources": ["serper"],
    }


class _NicheDS:
    tick_no = 30

    def __init__(self, niche, mentions):
        self._niche, self._mentions = niche, mentions

    def niches(self):
        return [self._niche]

    def social_sources(self):
        return ["serper"]

    def mentions(self, entity_id, source):
        return self._mentions

    def niche_history(self, nid):
        return [dict(self._niche["metrics"]) for _ in range(10)]


def test_unobserved_supply_fails_the_gap_check_as_research_required():
    from opportunity_os.agents.verifiers import VerificationCouncil
    metrics = {"volume": 150.0, "growth_pct": 30.0, "solution_count": 0,
               "demand_posts": 150.0, "providers": 0,
               "observed": {"demand": "observed", "supply": "unknown",
                            "demand_series": "serper"}}
    cand = _venture_cand(metrics)
    v = VerificationCouncil(Config(mode="live")).verify_venture(
        _NicheDS(cand["niche"], [2, 2, 2, 3, 3, 4, 6, 8]), cand)
    gap = next(c for c in v.checks if c.verifier == "competition_gap")
    assert not gap.passed and "Research required" in gap.evidence
    assert not v.passed                          # critical check → cannot publish


def test_observed_supply_gap_passes_and_names_domains():
    from opportunity_os.agents.verifiers import VerificationCouncil
    metrics = {"volume": 3000.0, "growth_pct": 30.0, "solution_count": 2,
               "demand_posts": 3000.0, "providers": 2,
               "observed": {"demand": "observed", "supply": "observed",
                            "demand_series": "serper"},
               "supply_domains": ["a.co.th", "b.com"]}
    cand = _venture_cand(metrics)
    v = VerificationCouncil(Config(mode="live")).verify_venture(
        _NicheDS(cand["niche"], [40, 44, 43, 47, 52, 60, 80, 100]), cand)
    gap = next(c for c in v.checks if c.verifier == "competition_gap")
    assert gap.passed and "a.co.th" in gap.evidence


def test_demo_metrics_without_provenance_keep_original_behaviour():
    from opportunity_os.agents.verifiers import VerificationCouncil
    from opportunity_os.models import OppType
    metrics = {"volume": 2000.0, "growth_pct": 20.0, "solution_count": 2,
               "demand_posts": 90.0, "providers": 2}
    cand = _venture_cand(metrics, kind="digital", opp_type=OppType.DIGITAL_PRODUCT)
    v = VerificationCouncil(Config()).verify_venture(
        _NicheDS(cand["niche"], [30, 33, 31, 36, 40, 44, 60, 70]), cand)
    gap = next(c for c in v.checks if c.verifier == "competition_gap")
    assert gap.passed                            # simulator numbers are demo ground truth


# ------------------------------------------------- ledger: true provenance

def test_ledger_marks_unknown_supply_and_observed_demand():
    from opportunity_os import evidence
    metrics = {"volume": 150.0, "growth_pct": 30.0, "solution_count": 0,
               "demand_posts": 150.0, "providers": 0,
               "observed": {"demand": "observed", "supply": "unknown",
                            "demand_series": "serper"}}
    cand = _venture_cand(metrics)
    ledger = evidence.build_ledger(cand, None, mode="live", latest_ts=0.0)
    supply = next(i for i in ledger if i["field"] == "supply")
    assert supply["kind"] == evidence.UNKNOWN and "research required" in supply["value"]
    demand = next(i for i in ledger if i["field"] == "demand")
    assert demand["kind"] == evidence.OBSERVED and demand["source"] == "serper"


def test_ledger_marks_user_supplied_baselines():
    from opportunity_os import evidence
    metrics = {"volume": 900.0, "growth_pct": 5.0, "solution_count": 4,
               "demand_posts": 80.0, "providers": 4,
               "observed": {"demand": "user_supplied", "supply": "user_supplied",
                            "demand_series": "reddit"}}
    cand = _venture_cand(metrics)
    ledger = evidence.build_ledger(cand, None, mode="live", latest_ts=0.0)
    supply = next(i for i in ledger if i["field"] == "supply")
    assert supply["kind"] == evidence.USER_SUPPLIED and supply["source"] == "watchlist"


# ------------------------------------------------- gap discovery (TH + EN)

def test_gap_relevance_reads_thai():
    assert gap_relevance("มีใครรับ ซ่อมกระเป๋าหนัง ไหมครับ หาไม่เจอ") >= 0.5
    assert gap_relevance("ราคาทองวันนี้") == 0.0


def test_infer_niche_kind_reads_thai():
    assert infer_niche_kind("รับซ่อมรองเท้า ใกล้ฉัน") == "local"
    assert infer_niche_kind("หาซัพพลายเออร์ ขายส่ง") == "b2b"


def test_serper_gap_discovery_emits_thai_local_candidates(tmp_path):
    cfg = Config(mode="live", serper_api_key="test-key")
    src = SerperGapDiscovery(cfg, serper_adapter=_serper(cfg))
    cands = src.discover()
    assert cands, "harvest produced nothing"
    thai_local = [c for c in cands if c.geo == "TH" and c.kind == "niche"]
    assert thai_local, f"no Thai candidates in {[(c.name, c.geo) for c in cands]}"
    c = thai_local[0]
    assert c.serper_query_th and c.niche_kind in ("local", "b2b", "digital")
    assert c.source == "serper_gaps"


def test_serper_gap_discovery_idle_without_key():
    src = SerperGapDiscovery(Config(mode="live"))
    assert src.discover() == []
    ok, note = src.check()
    assert not ok and "SERPER_API_KEY" in note


# ------------------------------------------------- storage roundtrip

def test_search_obs_roundtrip_and_counts(tmp_path):
    store = Store(tmp_path / "obs.db")
    store.add_search_obs("disc_n_z", "serper", "demand", 3, "หา Z",
                         {"results": 7, "questions": ["q1"]}, geo="th", lang="th")
    latest = store.latest_search_obs("disc_n_z", "demand")
    assert latest["payload"]["results"] == 7 and latest["lang"] == "th"
    assert store.latest_search_obs("disc_n_z", "supply") is None
    assert store.search_obs_counts() == {"serper": 1}
    series = store.search_obs_series("disc_n_z", "demand")
    assert len(series) == 1 and series[0]["query"] == "หา Z"


def test_candidate_serper_query_survives_db_roundtrip(tmp_path):
    from dataclasses import asdict
    from opportunity_os.discovery import _candidate_fields
    store = Store(tmp_path / "rt.db")
    c = Candidate(kind="niche", id="disc_n_rt", name="RT", source="serper_gaps",
                  niche_kind="local", geo="TH", serper_query_th="ซ่อม RT")
    store.upsert_discovered(asdict(c))
    row = store.list_discovered(active_only=True, limit=5)[0]
    back = Candidate(**_candidate_fields(row))
    assert back.serper_query_th == "ซ่อม RT"
    assert back.to_watch_niche().serper_query_th == "ซ่อม RT"


def test_discovery_every_1_runs_every_tick(tmp_path):
    """Regression: `t % 1 == 1` is never true — n<=1 must mean every cycle."""

    from opportunity_os.market.live import LiveMarket

    cfg = Config(mode="live", db_path=tmp_path / "e1.db", watchlist_path=tmp_path / "wl.json",
                 discovery_enabled=True, discover_every_n_ticks=1)
    (tmp_path / "wl.json").write_text('{"products": [], "niches": [], "operator": {}}')
    store = Store(cfg.db_path)

    class CountingDisco:
        runs = 0

        def run(self):
            CountingDisco.runs += 1
            return {"sources": {}, "found": 0, "promoted": 0, "errors": []}

        def extra_products(self, ids):
            return []

        def extra_niches(self, ids):
            return []

    lm = LiveMarket(cfg, store, adapters={}, discovery=CountingDisco())
    for _ in range(3):
        lm.tick()
    assert CountingDisco.runs == 3
