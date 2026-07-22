"""v1.4.0 — the lead-generation and seasonal generators (Phase 9, gens 7 & 11).

These finish the 11-generator set. Both stand on real evidence and face the
council: lead-gen on a niche's observed demand + thin under-exposed supply,
seasonal on a real dated Thai catalyst matched to a watched product with a live
Thai sell route.
"""

import datetime as _dt

import pytest

from opportunity_os import economics as eco, thailand as th
from opportunity_os.config import Config
from opportunity_os.generators import GenContext
from opportunity_os.generators_market import (LeadGenGenerator, SeasonalGenerator,
                                              LEADGEN_MAX_PROVIDERS)
from opportunity_os.models import OppType


# ------------------------------------------------ seasonal calendar

def test_upcoming_events_filters_by_horizon_and_prep():
    today = _dt.date(2026, 3, 20)                 # ~24 days before Songkran (Apr 13)
    evs = th.upcoming_events(today, horizon_days=60)
    names = [e["name"] for e in evs]
    assert any("Songkran" in n for n in names)
    songkran = next(e for e in evs if "Songkran" in e["name"])
    assert songkran["days_until"] == 24 and songkran["in_prep_window"] is True
    assert songkran["expiry_date"] == "2026-04-16"
    # An event far outside the horizon is excluded.
    assert not any("Mother" in n for n in th.upcoming_events(today, horizon_days=30))


def test_upcoming_events_rolls_to_next_year():
    today = _dt.date(2026, 12, 30)                # after all this year's events
    evs = th.upcoming_events(today, horizon_days=60)
    # Chinese New Year next Feb should be picked up as next-year.
    cny = next(e for e in evs if "Chinese New Year" in e["name"])
    assert cny["date"].startswith("2027-")


# ------------------------------------------------ economics + feasibility

def test_leadgen_economics_scales_with_volume_and_price():
    lo = eco.compute_venture({"kind": "leadgen", "price_point_usd": 5.0, "metrics": {"volume": 500}})
    hi = eco.compute_venture({"kind": "leadgen", "price_point_usd": 30.0, "metrics": {"volume": 500}})
    assert hi.base.net_usd > lo.base.net_usd      # higher-value leads → more net
    assert lo.kind == "venture"


def test_feasibility_for_new_types():
    lg = th.feasibility("lead_generation", "b2b_services", None, None)
    assert lg.can_buy and lg.can_sell and "lead" in lg.buy_notes[0].lower()
    se = th.feasibility("seasonal", "electronics", "aliexpress", "shopee_th")
    assert se.can_buy and se.can_sell           # venue-screened like a flip


# ------------------------------------------------ lead-gen generator

class _NicheDS:
    tick_no = 5

    def __init__(self, niche, mentions=None):
        self._niche = niche
        self._mentions = mentions or [5, 5, 6, 6, 7, 8, 9]

    def niches(self):
        return [self._niche]

    def social_sources(self):
        return ["reddit"]

    def mentions(self, nid, src):
        return self._mentions

    def niche_history(self, nid):
        return [dict(self._niche["metrics"]) for _ in range(10)]


def _niche(volume=250, providers=5, kind="b2b", growth=20, price=380.0, observed=None):
    m = {"volume": volume, "growth_pct": growth, "solution_count": providers,
         "demand_posts": 75.0, "providers": providers}
    if observed is not None:
        m["observed"] = observed
    return {"id": "n1", "name": "EV wallbox install (TH)", "kind": kind, "geo": "TH",
            "price_point_usd": price, "metrics": m}


def _ctx(ds, cfg=None):
    return GenContext(ds=ds, anomalies=[], by_entity={}, niche_ids={"n1"},
                      store=None, cfg=cfg or Config(), investigator=None, graph=None)


def test_lead_gen_fires_on_thin_supply_high_value_niche():
    cands = LeadGenGenerator().generate(_ctx(_NicheDS(_niche())))
    assert len(cands) == 1
    c = cands[0]
    assert c["opp_type"] == OppType.LEAD_GENERATION
    assert c["route"]["per_lead_usd"] == round(380.0 * 0.08, 2)
    assert c["economics"].pessimistic.net_usd > 0
    assert len(c["why"]) == 5


@pytest.mark.parametrize("kw,reason", [
    ({"volume": 50}, "demand too low"),
    ({"providers": 20}, "too many providers — they don't need leads"),
    ({"providers": 0}, "no providers to sell leads to"),
    ({"kind": "digital"}, "not a local/b2b service"),
])
def test_lead_gen_rejects_wrong_shapes(kw, reason):
    assert LeadGenGenerator().generate(_ctx(_NicheDS(_niche(**kw)))) == [], reason


def test_lead_gen_drops_unknown_supply():
    n = _niche(observed={"demand": "observed", "supply": "unknown", "demand_series": "serper"})
    assert LeadGenGenerator().generate(_ctx(_NicheDS(n))) == []


def test_verify_venture_accepts_lead_gen_gap():
    from opportunity_os.agents.verifiers import VerificationCouncil
    ds = _NicheDS(_niche())
    cand = LeadGenGenerator().generate(_ctx(ds))[0]
    v = VerificationCouncil(Config()).verify_venture(ds, cand)
    gap = next(c for c in v.checks if c.verifier == "competition_gap")
    assert gap.passed and "lead market" in gap.evidence.lower()
    assert v.passed


def test_verify_venture_rejects_lead_gen_with_too_many_providers():
    from opportunity_os.agents.verifiers import VerificationCouncil
    # 12 providers: craft the candidate, then verify against a saturated market.
    cand = LeadGenGenerator().generate(_ctx(_NicheDS(_niche(providers=LEADGEN_MAX_PROVIDERS))))[0]
    saturated = _NicheDS(_niche(providers=12))
    v = VerificationCouncil(Config()).verify_venture(saturated, cand)
    gap = next(c for c in v.checks if c.verifier == "competition_gap")
    assert not gap.passed


def _local_venture_cand(ds):
    """A minimal local-service venture candidate, enough to face the council."""
    niche = ds.niches()[0]
    return {"kind": "venture", "opp_type": OppType.LOCAL_SERVICE, "entity_id": niche["id"],
            "economics": eco.compute_venture(niche),
            "feasibility": th.feasibility("local_service", "local_services", None, None)}


def test_verify_venture_names_demand_verdict_research_vs_weak():
    """v1.6.1: an uncorroborated venture must be named honestly — a research gap
    while the demand series is still short (<6 points), soft demand once enough
    data has accumulated — never mislabelled as a supply defect. This is the
    exact production case: a real 75:1 supply gap, flat trend, no social spike."""

    from opportunity_os.agents.verifiers import VerificationCouncil
    from opportunity_os.evidence import (classify_rejection, RESEARCH_REQUIRED,
                                         INSUFFICIENT_DEMAND, INSUFFICIENT_SUPPLY)
    council = VerificationCouncil(Config())

    def demand_reason(ds):
        cand = _local_venture_cand(ds)
        v = council.verify_venture(ds, cand)
        dc = next(c for c in v.checks if c.verifier == "demand_corroboration")
        assert not dc.passed            # flat trend + no social spike → uncorroborated
        return f"{dc.name} failed — {dc.evidence}"

    # providers=2 → demand:supply 75/2 ≈ 37:1 (✓, a real gap); growth 0 + flat
    # mentions → the OTHER two signals fail, so corroboration < 2.
    young = _NicheDS(_niche(kind="local", providers=2, growth=0), mentions=[9, 10, 11])
    mature = _NicheDS(_niche(kind="local", providers=2, growth=0), mentions=[10] * 12)

    r_young, r_mature = demand_reason(young), demand_reason(mature)
    # Short series → research gap; long series → soft demand. Neither is a supply defect.
    assert classify_rejection(r_young) == RESEARCH_REQUIRED
    assert classify_rejection(r_mature) == INSUFFICIENT_DEMAND
    assert classify_rejection(r_mature) != INSUFFICIENT_SUPPLY
    assert "6 demand observations" in r_young and "of 3 independent signals" in r_mature


# ------------------------------------------------ seasonal generator

class _SeasonalDS:
    tick_no = 5

    def __init__(self, venues, category="electronics", name="Waterproof pouch"):
        self._v, self._cat, self._name = venues, category, name

    def product_public(self, pid):
        return {"id": pid, "name": self._name, "category": self._cat,
                "weight_kg": 0.1, "venues": list(self._v)}

    def listing(self, pid, venue):
        return dict(self._v[venue]) if venue in self._v else None


def _pouch_ds():
    return _SeasonalDS({
        "aliexpress":      {"price": 5.5, "stock": 800, "sellers": 60, "sold_7d": 40},
        "shopee_th":       {"price": 22.0, "stock": 70, "sellers": 20, "sold_7d": 24},
        "tiktok_shop_th":  {"price": 24.0, "stock": 55, "sellers": 14, "sold_7d": 18},
    })


def _seasonal_ctx(ds, today):
    cfg = Config(seasonal_today=today)
    return GenContext(ds=ds, anomalies=[], by_entity={"pouch": []}, niche_ids=set(),
                      store=None, cfg=cfg, investigator=None, graph=None)


def test_seasonal_fires_in_prep_window_with_hard_expiry():
    ds = _pouch_ds()
    cands = SeasonalGenerator().generate(_seasonal_ctx(ds, "2026-03-20"))   # 24d before Songkran
    assert cands, "seasonal did not fire inside the prep window"
    c = cands[0]
    assert c["opp_type"] == OppType.SEASONAL
    assert c["route"]["event"].startswith("Songkran")
    assert c["route"]["expiry_date"] == "2026-04-16"
    assert c["sell_venue"] in ("shopee_th", "tiktok_shop_th")   # sells into Thailand
    assert c["economics"].pessimistic.net_usd > 0
    assert c["window_days"] <= 45


def test_seasonal_silent_outside_prep_window():
    # 200 days before Songkran → no event in the 90-day horizon touches electronics.
    ds = _pouch_ds()
    assert SeasonalGenerator().generate(_seasonal_ctx(ds, "2025-09-25")) == []


def test_seasonal_requires_a_thai_sell_route():
    # Same product but only US venues → no domestic Thai sale, no seasonal play.
    ds = _SeasonalDS({
        "aliexpress": {"price": 5.5, "stock": 800, "sellers": 60, "sold_7d": 40},
        "ebay_us":    {"price": 24.0, "stock": 55, "sellers": 14, "sold_7d": 18},
    })
    assert SeasonalGenerator().generate(_seasonal_ctx(ds, "2026-03-20")) == []


# ------------------------------------------------ diversity mapping

def test_new_types_map_to_families():
    from opportunity_os.diversity import opp_type_family
    assert opp_type_family("lead_generation") == "b2b"
    assert opp_type_family("seasonal") == "physical"


# ------------------------------------------------ demo integration

@pytest.fixture(scope="module")
def demo_built():
    from opportunity_os.db import Store
    from opportunity_os.market import SimulatedMarket
    from opportunity_os.pipeline import Orchestrator
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    cfg = Config(db_path=tmp / "m.db", auto_cycle_seconds=0, seasonal_today="2026-03-20")
    store = Store(cfg.db_path)
    world = SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks)
    orch = Orchestrator(cfg, store, world)
    for _ in range(16):
        orch.run_cycle()
    return orch, store


def test_demo_publishes_lead_gen_and_seasonal(demo_built):
    _, store = demo_built
    types = {o["type"] for o in store.list_opportunities(status="active", limit=500)}
    assert "lead_generation" in types
    assert "seasonal" in types


def test_all_eleven_generator_types_exist():
    from opportunity_os.generators import build_generators
    ids = {g.id for g in build_generators()}
    assert ids == {"cross_market_flip", "dislocation", "digital_product", "info_product",
                   "local_service", "b2b_service", "micro_saas", "bundle_repair",
                   "import_export", "wholesale", "lead_generation", "seasonal"}


def test_seasonal_reverifies_without_becoming_a_venture(demo_built):
    orch, store = demo_built
    before = [o for o in store.list_opportunities(status="active", limit=500)
              if o["type"] in ("seasonal", "lead_generation")]
    assert before
    orch.run_cycle()
    after = {o["id"] for o in store.list_opportunities(status="active", limit=500)}
    survived = {o["type"] for o in before if o["id"] in after}
    assert "seasonal" in survived or "lead_generation" in survived
