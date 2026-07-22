"""v1.5.1 — production observability: per-family funnel, source query logs,
and the full source→frontend trace endpoint. Read-only instruments."""

import pytest
from fastapi.testclient import TestClient

from opportunity_os.api import create_app
from opportunity_os.config import Config
from opportunity_os.evidence import classify_rejection, RESEARCH_REQUIRED


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("obs")
    cfg = Config(db_path=d / "obs.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=3)
    with TestClient(app) as c:
        yield c


def test_research_required_classification():
    assert classify_rejection("Supply-side gap confirmed failed — Supply side never "
                              "observed ... Research required") == RESEARCH_REQUIRED
    assert classify_rejection("consensus confidence 0.5 below bar") != RESEARCH_REQUIRED


def test_observability_reports_all_families(client):
    d = client.get("/api/observability").json()
    assert d["cycles_scanned"] >= 3
    fams = d["families"]
    assert set(fams) == {"physical", "local_service", "b2b", "digital", "info"}
    assert fams["physical"]["candidates"] > 0            # demo produces flips
    for f in fams.values():
        assert set(f) >= {"candidates", "published_new", "rejected",
                          "research_required", "rejections",
                          "watched_discovered", "verified_active"}
    assert isinstance(d["serper_log"], list) and isinstance(d["scrapingdog_log"], list)


def test_trace_assembles_full_chain(client):
    opps = client.get("/api/opportunities?plan=pro").json()["opportunities"]
    flip = next(o for o in opps if o["type"] == "product_arbitrage")
    t = client.get(f"/api/trace/{flip['id']}").json()
    for stage in ("1_source_request", "2_raw_record", "3_parsed_observation",
                  "4_generator", "5_candidate_reasoning", "6_verification",
                  "7_frontend_record"):
        assert stage in t
    assert t["7_frontend_record"]["id"] == flip["id"]
    assert t["5_candidate_reasoning"], "why-chain missing from trace"
    assert t["6_verification"]["checks"], "council checks missing from trace"


def test_trace_404_on_unknown(client):
    assert client.get("/api/trace/opp_nope").status_code == 404


def test_cycle_report_carries_candidates_by_type(client):
    s = client.get("/api/stats").json()
    rep = s["last_report"]
    cbt = rep.get("candidates_by_type")
    assert cbt and sum(cbt.values()) == rep["candidates"]


def test_discovery_kind_filter_surfaces_buried_niches(tmp_path):
    """A flood of high-scoring products must not make the diversity-kept
    niches uninspectable: kind=niche returns them regardless of rank."""

    from dataclasses import asdict
    from opportunity_os.db import Store
    from opportunity_os.discovery import Candidate

    cfg = Config(db_path=tmp_path / "kf.db", auto_cycle_seconds=0)
    app = create_app(cfg, auto_cycle_seconds=0, seed_cycles=0)
    with TestClient(app) as c:
        store2 = app.state.orch.db if hasattr(app.state, "orch") else None
        # write directly through a fresh Store handle on the same db
        s = Store(cfg.db_path)
        for i in range(30):
            s.upsert_discovered(asdict(Candidate(kind="product", id=f"disc_p_{i}",
                                                 name=f"P{i}", source="s", score=1.5)))
        s.upsert_discovered(asdict(Candidate(kind="niche", id="disc_n_th", name="ซ่อมกระเป๋า",
                                             source="serper_gaps", score=0.6,
                                             niche_kind="local", geo="TH")))
        top = c.get("/api/discovery?limit=10").json()["found"]
        assert all(r["kind"] == "product" for r in top)      # niche buried by score
        niches = c.get("/api/discovery?limit=10&kind=niche").json()["found"]
        assert len(niches) == 1 and niches[0]["id"] == "disc_n_th"
        assert niches[0]["niche_kind"] == "local"
