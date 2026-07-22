"""v1.3.0 — the type-diversity budget (Phase 10).

The load-bearing guarantees:

* a flood of high-scored physical products can neither out-PROMOTE nor
  out-LIVE the gap-mined niches — every family keeps a floor of the watch set;
* the budget governs how much a family is WATCHED, never whether a candidate
  publishes (no weak opportunity is waved through to fill a quota);
* under-filled families are reported honestly (found < target), and their
  unused slots are redistributed so no watch capacity is wasted;
* adaptation moves shares toward families that verify, but never zeroes one.
"""

import pytest

from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.diversity import (DiversityAllocator, FAMILIES, adapt_weights,
                                      candidate_family, opp_type_family, _largest_remainder)


# ------------------------------------------------ family mapping

def test_candidate_family_mapping():
    assert candidate_family({"kind": "product"}) == "physical"
    assert candidate_family({"kind": "niche", "niche_kind": "local"}) == "local_service"
    assert candidate_family({"kind": "niche", "niche_kind": "b2b"}) == "b2b"
    assert candidate_family({"kind": "niche", "niche_kind": "digital"}) == "digital"
    assert candidate_family({"kind": "niche", "niche_kind": "info"}) == "info"


def test_opp_type_family_covers_every_type():
    for t in ("product_arbitrage", "import_export", "wholesale", "refurbishment"):
        assert opp_type_family(t) == "physical"
    assert opp_type_family("local_service") == "local_service"
    assert opp_type_family("micro_saas") == "digital"
    assert opp_type_family("digital_product") == "digital"
    assert opp_type_family("info_product") == "info"


# ------------------------------------------------ apportionment

def test_largest_remainder_conserves_slots():
    for total in (0, 1, 7, 200, 199):
        out = _largest_remainder({f: FAMILIES[f]["weight"] for f in FAMILIES}, total)
        assert sum(out.values()) == total       # never loses or invents a slot


def test_targets_sum_to_budget_and_honour_floor():
    alloc = DiversityAllocator(Config())
    t = alloc.targets(200, yield_by_family={})
    assert sum(t.values()) == 200
    for f in FAMILIES:
        assert t[f] >= alloc.min_floor           # every family clears the floor at a healthy budget


def test_adapt_never_zeroes_a_quiet_family():
    # physical yields everything; the quiet families still keep a real share.
    w = adapt_weights({"physical": 50}, adapt=1.0)
    assert all(w[f] > 0 for f in FAMILIES)       # exploration never collapses to zero
    assert w["physical"] == max(w.values())      # but it does move toward the winner
    assert w["physical"] > FAMILIES["physical"]["weight"]
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_adapt_off_returns_base():
    assert adapt_weights({"physical": 99}, adapt=0.0) == {f: FAMILIES[f]["weight"] for f in FAMILIES}


# ------------------------------------------------ the core anti-monopoly property

def _flood(n_physical, n_local=0, n_b2b=0, n_digital=0):
    out = [{"id": f"p{i}", "kind": "product", "score": 1.5} for i in range(n_physical)]
    out += [{"id": f"l{i}", "kind": "niche", "niche_kind": "local", "score": 0.6} for i in range(n_local)]
    out += [{"id": f"b{i}", "kind": "niche", "niche_kind": "b2b", "score": 0.55} for i in range(n_b2b)]
    out += [{"id": f"d{i}", "kind": "niche", "niche_kind": "digital", "score": 0.5} for i in range(n_digital)]
    return out


def test_physical_flood_cannot_starve_niches():
    alloc = DiversityAllocator(Config())
    cands = _flood(300, n_local=5, n_b2b=4, n_digital=3)      # products would win every slot by score
    keep_ids, rep = alloc.select(cands, 200, yield_by_family={})
    kept = set(keep_ids)
    # Every low-scored niche survives, even though 300 higher-scored products exist.
    assert all(f"l{i}" in kept for i in range(5))
    assert all(f"b{i}" in kept for i in range(4))
    assert all(f"d{i}" in kept for i in range(3))
    assert len(keep_ids) == 200
    # Under-filled families are flagged, not failed.
    assert rep["families"]["local_service"]["under_filled"] is True
    assert rep["families"]["info"]["offered"] == 0 and rep["families"]["info"]["under_filled"] is True


def test_surplus_redistributes_no_slot_wasted():
    alloc = DiversityAllocator(Config())
    # Only physical candidates exist; the niche floors can't be filled, so all
    # 200 slots must still go to physical (never left empty).
    keep_ids, rep = alloc.select(_flood(500), 200, yield_by_family={})
    assert len(keep_ids) == 200
    assert rep["families"]["physical"]["kept"] == 200


def test_budget_never_lowers_the_publish_bar():
    """The allocator only selects what to WATCH; it exposes no path to publish.
    Its output is a set of candidate ids — never an opportunity."""

    keep_ids, rep = DiversityAllocator(Config()).select(_flood(3, n_local=2), 200, {})
    assert all(isinstance(i, str) for i in keep_ids)
    assert "publish" not in rep and "opportunities" not in rep


# ------------------------------------------------ db retention

def test_retain_discovered_keeps_explicit_set(tmp_path):
    from opportunity_os.discovery import Candidate
    from dataclasses import asdict
    store = Store(tmp_path / "r.db")
    for i in range(6):
        store.upsert_discovered(asdict(Candidate(
            kind="product", id=f"disc_p_{i}", name=f"P{i}", source="s", score=2.0 - i * 0.1)))
    store.retain_discovered(["disc_p_1", "disc_p_3"], ttl_days=10)
    active = {r["id"] for r in store.list_discovered(active_only=True, limit=50)}
    assert active == {"disc_p_1", "disc_p_3"}


def test_family_counts_and_yield(tmp_path):
    from opportunity_os.discovery import Candidate
    from dataclasses import asdict
    store = Store(tmp_path / "y.db")
    store.upsert_discovered(asdict(Candidate(kind="product", id="disc_p_a", name="A", source="s", score=1.0)))
    store.upsert_discovered(asdict(Candidate(kind="niche", id="disc_n_b", name="B", source="s",
                                             score=0.6, niche_kind="local")))
    fc = store.active_discovered_family_counts()
    assert fc.get("physical") == 1 and fc.get("local_service") == 1


# ------------------------------------------------ engine integration

def _cfg(tmp_path, **kw):
    kw.setdefault("mode", "live")
    return Config(db_path=tmp_path / "e.db", **kw)


def test_engine_retention_keeps_niches_alive_under_product_flood(tmp_path):
    """The real fix, end to end: a discovery sweep flooding physical products
    plus a few niches must leave the niches WATCHED, where pure top-by-score
    would have expired them all."""

    from opportunity_os.discovery import Candidate, DiscoveryEngine

    class Flood:
        id, name, last_error = "flood", "Flood", ""

        def discover(self):
            out = [Candidate(kind="product", id=f"disc_p_{i}", name=f"P{i}", source="flood",
                             score=1.6, queries={"ebay_us": f"p{i}"}) for i in range(60)]
            out += [Candidate(kind="niche", id="disc_n_bkk_clean", name="bkk airbnb cleaning",
                              source="flood", score=0.6, niche_kind="local"),
                    Candidate(kind="niche", id="disc_n_wholesale_lead", name="supplier leads",
                              source="flood", score=0.55, niche_kind="b2b")]
            return out

        def check(self):
            return True, "ok"

    cfg = _cfg(tmp_path, discovery_scan_cap=40, discovery_max_active=40)
    eng = DiscoveryEngine(cfg, store=Store(cfg.db_path), sources=[Flood()], classify_hook=lambda c: c)
    rep = eng.run()
    active = {r["id"] for r in eng.store.list_discovered(active_only=True, limit=100)}
    assert "disc_n_bkk_clean" in active, "local niche starved by the product flood"
    assert "disc_n_wholesale_lead" in active, "b2b niche starved by the product flood"
    assert "diversity" in rep
    assert rep["diversity"]["families"]["local_service"]["kept"] >= 1


def test_engine_diversity_off_is_legacy_top_by_score(tmp_path):
    from opportunity_os.discovery import Candidate, DiscoveryEngine

    class Src:
        id, name, last_error = "s", "S", ""

        def discover(self):
            return [Candidate(kind="product", id=f"disc_p_{i}", name=f"P{i}", source="s",
                              score=1.6 - i * 0.01, queries={"ebay_us": f"p{i}"}) for i in range(20)] + \
                   [Candidate(kind="niche", id="disc_n_low", name="low niche", source="s",
                              score=0.2, niche_kind="local")]

        def check(self):
            return True, "ok"

    cfg = _cfg(tmp_path, discovery_enabled=True, diversity_enabled=False,
               discovery_scan_cap=5, discovery_max_active=5)
    eng = DiscoveryEngine(cfg, store=Store(cfg.db_path), sources=[Src()], classify_hook=lambda c: c)
    rep = eng.run()
    assert "diversity" not in rep
    active = {r["id"] for r in eng.store.list_discovered(active_only=True, limit=50)}
    assert "disc_n_low" not in active            # legacy: the low niche is expired by score
