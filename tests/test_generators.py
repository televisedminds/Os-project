"""Phase 3-5: generator registry, non-flip generators producing real candidates,
and knowledge-graph traversal into related hypotheses."""

import pytest

from opportunity_os import graph as G
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.generators import build_generators
from opportunity_os.graph import KnowledgeGraph
from opportunity_os.market import SimulatedMarket
from opportunity_os.pipeline import Orchestrator
from opportunity_os import diagnostics as dx


@pytest.fixture
def orch(tmp_path):
    cfg = Config(db_path=tmp_path / "gen.db", auto_cycle_seconds=0)
    store = Store(cfg.db_path)
    world = SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks)
    return Orchestrator(cfg, store, world)


# ------------------------------------------------------ Phase 3: registry

def test_registry_has_at_least_five_generator_types(orch):
    gens = build_generators(orch.cfg)
    types = set()
    for g in gens:
        types.update(g.types)
    assert len(gens) >= 5
    assert len(types) >= 5, f"only {types}"
    # flip and non-flip both represented
    assert any(g.uses_marketplace for g in gens)
    assert any(not g.uses_marketplace for g in gens)


def test_generators_are_decoupled_a_broken_one_does_not_break_others(orch):
    from opportunity_os.generators import GENERATORS, OpportunityGenerator, GenContext

    class Boom(OpportunityGenerator):
        id, name, types = "boom", "boom", ("boom",)
        def generate(self, ctx):
            raise RuntimeError("kaboom")

    GENERATORS.append(Boom)
    try:
        # A cycle still completes and still publishes despite the broken generator.
        r = orch.run_cycle()
        assert r["candidates"] >= 0 and "published" in r
    finally:
        GENERATORS.remove(Boom)


# ----------------------------- Phase 4: 3+ non-flip produce real candidates

def test_at_least_three_non_flip_types_get_verified(orch):
    for _ in range(20):
        orch.run_cycle()
    tf = dx.type_funnel(orch.db)
    non_flip = {k for k in tf["by_type"]
                if k not in ("product_arbitrage", "dislocation")}
    assert len(non_flip) >= 3, f"expected 3+ non-flip types, got {tf['by_type']}"


def test_refurbishment_only_on_repairable_categories_with_real_repair_cost(orch):
    for _ in range(20):
        orch.run_cycle()
    refs = [o for o in orch.db.list_opportunities(status="active", limit=500)
            if o.get("route", {}).get("kind") == "refurbish"]
    from opportunity_os.generators_extra import REPAIRABLE_CATEGORIES
    for o in refs:
        assert o["category"] in REPAIRABLE_CATEGORIES
        assert o["route"]["repair_cost"] > 0                     # real cost, not a relabelled flip
        assert o["economics"]["pessimistic"]["net_usd"] > 0      # still clears the gate
        # the buy link points at the exact for-parts listing
        assert o["route"]["buy_url"]


def test_micro_saas_distinct_from_generic_digital(orch):
    for _ in range(20):
        orch.run_cycle()
    types = dx.type_funnel(orch.db)["by_type"]
    # If a micro_saas published, it must not also be double-counted as digital
    # for the same niche (the generators are mutually exclusive).
    ms = [o for o in orch.db.list_opportunities(status="active", limit=500)
          if o["type"] == "micro_saas"]
    dig = {o["entity_id"] for o in orch.db.list_opportunities(status="active", limit=500)
           if o["type"] == "digital_product"}
    for o in ms:
        assert o["entity_id"] not in dig                         # no double publication


# ------------------------------------ Phase 5: knowledge graph traversal

def test_graph_traversal_is_bounded_and_ev_ranked():
    store = Store(":memory:")
    kg = KnowledgeGraph(store)
    # Build a small neighbourhood around a seed product.
    kg.link("gba_sp", "ags101", G.E_VARIANT, src_type=G.N_PRODUCT, dst_type=G.N_MODEL,
            src_name="Game Boy SP", dst_name="AGS-101", weight=4)
    kg.link("gba_sp", "link_cable", G.E_COMPATIBLE, src_type=G.N_PRODUCT,
            dst_type=G.N_ACCESSORY, dst_name="Link cable", weight=2)
    kg.link("gba_sp", "dead_thing", G.E_CO_LISTED, src_type=G.N_PRODUCT,
            dst_type=G.N_PRODUCT, dst_name="Dead thing", weight=1)
    kg.mark_outcome("dead_thing", "Dead thing", profitable=False)   # previously rejected
    hyps = kg.related_hypotheses("gba_sp", budget=5, max_hops=2)
    names = [h.node_id for h in hyps]
    assert "ags101" in names and "link_cable" in names
    assert "gba_sp" not in names                                 # never returns the seed
    # the previously-rejected neighbour is down-ranked below the good ones
    ev = {h.node_id: h.ev_score for h in hyps}
    if "dead_thing" in ev:
        assert ev["dead_thing"] < ev["ags101"]
    store.close()


def test_publication_marks_graph_and_spawns_hypotheses(orch):
    # Pre-seed a graph edge so a published product has a neighbour to expand to.
    orch.graph.link("seed_product", "seed_variant", G.E_VARIANT,
                    src_type=G.N_PRODUCT, dst_type=G.N_PRODUCT,
                    src_name="Seed", dst_name="Seed Variant", weight=3)
    for _ in range(4):
        orch.run_cycle()
    # A real published product should have recorded a profitable outcome edge.
    profitable = orch.db._conn.execute(
        "SELECT COUNT(*) FROM graph_edges WHERE kind=?", (G.E_PROFITABLE,)).fetchone()[0]
    assert profitable >= 1
    assert orch.graph.stats()["nodes"] > 0


def test_graph_traversal_never_exceeds_budget():
    store = Store(":memory:")
    kg = KnowledgeGraph(store)
    # A dense hub: 50 neighbours. Traversal must still cap at budget.
    for i in range(50):
        kg.link("hub", f"n{i}", G.E_CO_LISTED, src_type=G.N_PRODUCT,
                dst_type=G.N_PRODUCT, dst_name=f"N{i}", weight=1)
    hyps = kg.related_hypotheses("hub", budget=6, max_hops=2)
    assert len(hyps) <= 6
    store.close()
