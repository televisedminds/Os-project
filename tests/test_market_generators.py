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
    data has accumulated — never mislabelled as a supply defect. (M3: the weak
    case now uses a LOW-volume niche so the level+stability signal can't rescue
    it — genuinely thin demand, not a large stable one.)"""

    from opportunity_os.agents.verifiers import VerificationCouncil
    from opportunity_os.evidence import (classify_rejection, RESEARCH_REQUIRED,
                                         INSUFFICIENT_DEMAND, INSUFFICIENT_SUPPLY)
    council = VerificationCouncil(Config())

    def demand_reason(ds):
        cand = _local_venture_cand(ds)
        v = council.verify_venture(ds, cand)
        dc = next(c for c in v.checks if c.verifier == "demand_corroboration")
        assert not dc.passed            # thin demand, no growth → uncorroborated
        return f"{dc.name} failed — {dc.evidence}"

    # providers=2 → demand:supply 75/2 ≈ 37:1 (✓, a real gap); growth 0 + flat
    # mentions → trend/social fail; volume=100 is below the level floor so
    # level+stability also fails → only 1 of 4 signals.
    young = _NicheDS(_niche(kind="local", providers=2, growth=0, volume=100), mentions=[9, 10, 11])
    mature = _NicheDS(_niche(kind="local", providers=2, growth=0, volume=100), mentions=[10] * 12)

    r_young, r_mature = demand_reason(young), demand_reason(mature)
    # Short series → research gap; long series → soft demand. Neither is a supply defect.
    assert classify_rejection(r_young) == RESEARCH_REQUIRED
    assert classify_rejection(r_mature) == INSUFFICIENT_DEMAND
    assert classify_rejection(r_mature) != INSUFFICIENT_SUPPLY
    assert "6 demand observations" in r_young and "of 4 independent signals" in r_mature


def test_verify_venture_level_stability_corroborates_a_large_stable_niche():
    """M3: a large, STABLE, underserved niche must be recognisable even with flat
    growth — the level+stability signal + the supply gap = 2 independent signals,
    so demand_corroboration passes. This does NOT lower the bar: it still must
    clear the competition-gap and unit-economics checks that follow."""

    from opportunity_os.agents.verifiers import VerificationCouncil
    council = VerificationCouncil(Config())
    # volume 3000/mo (well above the 250 floor), 2 providers → 37:1 gap, flat
    # growth, durable mention series. Two independent signals: gap + level/stability.
    ds = _NicheDS(_niche(kind="local", providers=2, growth=0, volume=3000), mentions=[40] * 12)
    dc = next(c for c in council.verify_venture(ds, _local_venture_cand(ds)).checks
              if c.verifier == "demand_corroboration")
    assert dc.passed and "level+stability ✓" in dc.evidence

    # A DECLINING series of the same size is not durable → level+stability ✗ → fails.
    declining = _NicheDS(_niche(kind="local", providers=2, growth=0, volume=3000),
                         mentions=[80, 70, 60, 40, 25, 15, 10, 8])
    dc2 = next(c for c in council.verify_venture(declining, _local_venture_cand(declining)).checks
               if c.verifier == "demand_corroboration")
    assert not dc2.passed and "level+stability ✗" in dc2.evidence


def test_verify_venture_high_volume_without_gap_still_fails():
    """M3 does not manufacture passes: a large, stable niche with NO supply gap
    (many providers) has only 1 signal (level+stability) → still fails."""
    from opportunity_os.agents.verifiers import VerificationCouncil
    ds = _NicheDS(_niche(kind="local", providers=200, growth=0, volume=3000), mentions=[40] * 12)
    dc = next(c for c in VerificationCouncil(Config()).verify_venture(ds, _local_venture_cand(ds)).checks
              if c.verifier == "demand_corroboration")
    assert not dc.passed            # level+stability ✓ but gap ✗, trend ✗, social ✗ → 1 of 4


def test_estimated_volume_yields_validation_required_not_false_economics(tmp_path):
    """M5: a DISCOVERED niche's demand volume is an estimate scaled from search-
    result counts, not a measured search volume. The economics must NOT conclude
    a pass/fail on it — the honest verdict is validation_required."""
    from opportunity_os.agents.verifiers import VerificationCouncil
    from opportunity_os.evidence import classify_rejection, VALIDATION_REQUIRED
    obs = {"demand": "observed", "supply": "observed", "demand_series": "serper",
           "volume": "estimated"}                        # <-- volume is an ESTIMATE
    ds = _NicheDS(_niche(kind="local", providers=2, growth=0, volume=3000, observed=obs),
                  mentions=[40] * 12)                     # corroborated demand + real 37:1 gap
    v = VerificationCouncil(Config()).verify_venture(ds, _local_venture_cand(ds))
    dc = next(c for c in v.checks if c.verifier == "demand_corroboration")
    gap = next(c for c in v.checks if c.verifier == "competition_gap")
    ue = next(c for c in v.checks if c.verifier == "unit_economics")
    assert dc.passed and gap.passed                       # earlier checks pass
    assert not ue.passed and "validation required" in ue.evidence.lower()
    assert classify_rejection(f"{ue.name} failed — {ue.evidence}") == VALIDATION_REQUIRED


def _venture_cand(ds, opp_type, category):
    niche = ds.niches()[0]
    return {"kind": "venture", "opp_type": opp_type, "entity_id": niche["id"],
            "economics": eco.compute_venture(niche),
            "feasibility": th.feasibility(opp_type.value, category, None, None)}


@pytest.mark.parametrize("kind,opp_type,category", [
    ("local", OppType.LOCAL_SERVICE, "local_services"),
    ("b2b", OppType.B2B_SERVICE, "b2b_services"),
    ("digital", OppType.DIGITAL_PRODUCT, "digital_tools"),
    ("info", OppType.INFO_PRODUCT, "info_products"),
])
def test_estimated_volume_never_verifies_across_all_venture_families(kind, opp_type, category):
    """Check #2/#4: an ESTIMATED demand volume can never yield verified/execute —
    it stays validation_required — for EVERY venture family, not just info. The
    niche still reaches the council (explicit verdict), it just can't pass."""
    from opportunity_os.agents.verifiers import VerificationCouncil
    obs = {"demand": "observed", "supply": "observed", "demand_series": "serper",
           "volume": "estimated"}
    ds = _NicheDS(_niche(kind=kind, providers=2, growth=20, volume=3000, observed=obs),
                  mentions=[10, 20, 30, 40, 50, 60])         # strong, growing, gapped demand
    v = VerificationCouncil(Config()).verify_venture(ds, _venture_cand(ds, opp_type, category))
    ue = next(c for c in v.checks if c.verifier == "unit_economics")
    assert not ue.passed and "validation required" in ue.evidence.lower()
    assert not v.passed                    # estimated volume can NEVER verify / execute_now
    assert v.checks                        # but it DID reach the council with a verdict


def test_economics_inputs_carry_provenance():
    """Check #1: every economic input records its provenance (observed /
    calculated / estimated / user_supplied), on flips and ventures alike."""
    from opportunity_os.models import OppType as _OT
    allowed = {"observed", "calculated", "estimated", "user_supplied", "unknown"}
    # venture with an estimated demand volume
    obs = {"demand": "observed", "supply": "observed", "volume": "estimated", "price": "observed"}
    ve = eco.compute_venture(_niche(kind="info", volume=3000, observed=obs))
    assert ve.input_provenance["demand_volume"] == "estimated"
    assert ve.input_provenance["projected_net"] == "calculated"
    assert set(ve.input_provenance.values()) <= allowed
    # flip: prices observed, fees calculated
    fe = eco.compute_flip({"name": "X", "category": "collectibles", "weight_kg": 0.5},
                          "shopee_th", "ebay_us", 20.0, 60.0)
    assert fe.input_provenance["buy_price"] == "observed"
    assert fe.input_provenance["projected_net"] == "calculated"
    assert set(fe.input_provenance.values()) <= allowed


def test_measured_or_user_supplied_volume_keeps_real_economics_verdict():
    """M5 does not change verdicts for a user-supplied/measured volume — those
    keep the real pass/fail (no 'volume': 'estimated' provenance)."""
    from opportunity_os.agents.verifiers import VerificationCouncil
    obs = {"demand": "user_supplied", "supply": "observed", "demand_series": "serper",
           "volume": "user_supplied"}
    ds = _NicheDS(_niche(kind="local", providers=2, growth=0, volume=3000, observed=obs),
                  mentions=[40] * 12)
    ue = next(c for c in VerificationCouncil(Config()).verify_venture(ds, _local_venture_cand(ds)).checks
              if c.verifier == "unit_economics")
    assert "validation required" not in ue.evidence.lower()   # real economics, not deferred


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
