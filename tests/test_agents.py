"""Scanners, anomaly detection, investigation, verification, scoring."""

from opportunity_os.agents import build_fleet, AnomalyDetector, Investigator, VerificationCouncil, ScoringEngine
from opportunity_os.agents.scoring import DEFAULT_WEIGHTS
from opportunity_os.agents import build_automation, build_playbook
from opportunity_os.models import AnomalyKind


def _tick_past_events(world, n=2):
    world.fast_forward(n)


def test_fleet_covers_venues_and_sources(world):
    fleet = build_fleet(world)
    ids = {a.id for a in fleet}
    assert len(fleet) == len(world.venue_ids()) + 5     # venues + 3 social + trends + news
    assert {"scan_ebay_us", "scan_shopee_th", "scan_tiktok_shop_th", "scan_aliexpress",
            "scan_reddit", "scan_news"} <= ids


def test_scanners_emit_signals(world):
    _tick_past_events(world)
    for agent in build_fleet(world):
        sigs = agent.scan(world)
        if agent.id == "scan_news":
            continue                       # only emits on event days
        assert sigs, f"{agent.id} emitted nothing"
        for s in sigs:
            assert s.entity_id and s.kind and s.tick == world.tick_no


def test_supply_shock_is_detected(cfg, world):
    _tick_past_events(world)               # card supply shock fires at warmup+1
    anomalies = AnomalyDetector(cfg).detect(world)
    kinds_for_card = {a.kind for a in anomalies if a.entity_id == "pokemon_card_214"}
    assert kinds_for_card & {AnomalyKind.PRICE_SPIKE, AnomalyKind.SUPPLY_CRUNCH,
                             AnomalyKind.CROSS_VENUE_SPREAD}


def test_investigator_builds_grounded_why_chain(cfg, world):
    _tick_past_events(world)
    anomalies = AnomalyDetector(cfg).detect(world)
    cands = Investigator(cfg).build_candidates(world, anomalies)
    card = next(c for c in cands if c["entity_id"] == "pokemon_card_214")
    assert card["buy_venue"] == "yahoo_auctions_jp" and card["sell_venue"] == "ebay_us"
    questions = [s.question for s in card["why"]]
    assert len(questions) >= 7
    # the why-chain must surface the actual cause planted in the world
    cause_step = card["why"][1]
    assert "sold out" in cause_step.finding
    assert card["economics"].base.net_usd > 0
    assert card["qty"] >= 1


def test_council_passes_good_candidate_and_vetoes_bad(cfg, world):
    _tick_past_events(world)
    anomalies = AnomalyDetector(cfg).detect(world)
    cands = Investigator(cfg).build_candidates(world, anomalies)
    card = next(c for c in cands if c["entity_id"] == "pokemon_card_214")
    council = VerificationCouncil(cfg)

    v = council.verify_flip(world, card)
    assert v.passed and 0 < v.consensus <= 1
    assert all(c.evidence for c in v.checks)

    greedy = dict(card, qty=10 ** 6)       # wants more units than exist
    v2 = council.verify_flip(world, greedy)
    assert not v2.passed
    assert any(c.critical and not c.passed and c.verifier == "supply_verifier" for c in v2.checks)


def test_scoring_is_explainable_and_monotonic(cfg, world):
    _tick_past_events(world)
    anomalies = AnomalyDetector(cfg).detect(world)
    cands = Investigator(cfg).build_candidates(world, anomalies)
    card = next(c for c in cands if c["entity_id"] == "pokemon_card_214")
    scorer = ScoringEngine(DEFAULT_WEIGHTS, cfg.capital_cap_usd)

    s = scorer.score(card, automation_coverage_pct=70)
    assert 0 <= s.overall <= 100
    assert abs(sum(s.contributions.values()) - s.overall) < 0.5
    assert set(s.factors) == set(DEFAULT_WEIGHTS)

    s_low_auto = scorer.score(card, automation_coverage_pct=10)
    assert s_low_auto.overall < s.overall


def test_playbook_and_automation_are_concrete(cfg, world):
    _tick_past_events(world)
    anomalies = AnomalyDetector(cfg).detect(world)
    cands = Investigator(cfg).build_candidates(world, anomalies)
    card = next(c for c in cands if c["entity_id"] == "pokemon_card_214")

    pb = build_playbook(card)
    assert len(pb.steps) >= 6
    assert pb.listing and pb.listing["price_usd"] > 0 and len(pb.listing["title"]) <= 80
    assert any("proxy" in s.detail.lower() or "proxy" in s.title.lower() for s in pb.steps)
    assert any("CN22" in s.detail or "customs" in s.title.lower() for s in pb.steps)

    auto = build_automation(card)
    assert 0 < auto.coverage_pct <= 100
    assert auto.human_checkpoints


class _FakeDS:
    """Minimal DataSource with crafted histories for the new detectors."""

    tick_no = 20

    def product_ids(self):
        return ["p1"]

    def product_public(self, pid):
        return {"id": "p1", "name": "Widget", "category": "electronics",
                "weight_kg": 0.5, "venues": ["shopee_th", "ebay_us"]}

    def listing(self, pid, venue):
        return ({"price": 20.0, "stock": 50, "sellers": 10, "sold_7d": 14}
                if venue == "shopee_th" else
                {"price": 60.0, "stock": 30, "sellers": 4, "sold_7d": 35})

    def product_history(self, pid, venue):
        if venue == "shopee_th":       # flat buy side
            return [{"price": 20.0, "stock": 50, "sellers": 10, "sold_7d": 14}] * 10
        # sell side: sellers collapsing 10 -> 4, velocity 1/day -> 5/day,
        # spread vs the flat buy side widening as the price climbs
        out = []
        for i in range(10):
            out.append({"price": 30.0 + i * 3.5, "stock": 30,
                        "sellers": 10 if i < 8 else 4,
                        "sold_7d": 7 if i < 8 else 35})
        return out

    def mentions(self, eid, src):
        return []

    def social_sources(self):
        return ["reddit"]

    def niches(self):
        return [{"id": "n1", "name": "Turnaround niche", "kind": "digital", "geo": "global",
                 "price_point_usd": 9.0,
                 "metrics": {"volume": 1300, "growth_pct": 5, "solution_count": 2,
                             "demand_posts": 50, "providers": 2}}]

    def niche_history(self, nid):
        return ([{"volume": 1000.0, "growth_pct": 0, "solution_count": 2,
                  "demand_posts": 50, "providers": 2}] * 8
                + [{"volume": 1250.0, "growth_pct": 8, "solution_count": 2,
                    "demand_posts": 55, "providers": 2},
                   {"volume": 1300.0, "growth_pct": 9, "solution_count": 2,
                    "demand_posts": 60, "providers": 2},
                   {"volume": 1350.0, "growth_pct": 10, "solution_count": 2,
                    "demand_posts": 60, "providers": 2}])


def test_new_detectors_fire_on_crafted_histories():
    from opportunity_os.config import Config
    kinds = {a.kind for a in AnomalyDetector(Config()).detect(_FakeDS())}
    assert AnomalyKind.SELLER_EXODUS in kinds
    assert AnomalyKind.DEMAND_ACCELERATION in kinds
    assert AnomalyKind.MARGIN_EXPANSION in kinds
    assert AnomalyKind.TREND_REVERSAL in kinds
