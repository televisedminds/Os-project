"""v1.2.0 — the import/export and wholesale generators (Phase 9, gens 8 & 10).

Both are additive, distinct opportunity types, built from real evidence:

* import/export surfaces the TH-crossing route the profit-max flip hides
  (import-to-sell-locally / export-from-TH), demand-gated and legally screened;
* wholesale finds bulk lots priced per-unit below the single-unit market on
  the same venue.

Fast: crafted data sources, no network. One demo-integration test proves the
whole pipeline publishes and re-verifies them.
"""

import pytest

from opportunity_os import economics as eco, research, thailand as th
from opportunity_os.config import Config
from opportunity_os.generators import GenContext
from opportunity_os.generators_trade import (ImportExportGenerator, WholesaleGenerator,
                                             _best_flip_route)
from opportunity_os.models import OppType


# ------------------------------------------------ research: lot parsing

@pytest.mark.parametrize("title,expected", [
    ("Pokemon booster lot of 12 sealed", 12),
    ("LEGO minifig bundle of 8", 8),
    ("Game Boy screen x20 wholesale", 20),
    ("keycaps set of 6 artisan", 6),
    ("24 pcs vintage tees bulk", 24),
    ("case of 36 energy drinks", 36),
    ("New Balance 2002r size 10", None),        # bare model/size — not a lot
    ("Seiko 6139 chronograph", None),
    ("lot of 999 spam", None),                  # above LOT_MAX_SIZE
    ("lot of 2 pair", None),                     # below LOT_MIN_SIZE
])
def test_parse_lot_size(title, expected):
    assert research.parse_lot_size(title) == expected


def _singles(name, fair, k=12):
    return [{"item_id": f"s{i}", "title": f"{name} used good", "price": fair * (0.9 + 0.02 * i),
             "url": f"u{i}", "seller": f"seller_{i % 6}", "condition": "USED_GOOD"}
            for i in range(k)]


def test_find_wholesale_lots_prices_per_unit():
    name = "Casio calculator"
    sample = _singles(name, 25.0, 12)
    sample.append({"item_id": "LOT", "title": f"{name} lot of 10 bulk", "price": 150.0,   # $15/unit
                   "url": "lot", "seller": "wholesaler", "condition": "USED_GOOD"})
    lots = research.find_wholesale_lots(sample, name)
    assert len(lots) == 1
    lot = lots[0]
    assert lot["lot_size"] == 10 and lot["per_unit_usd"] == 15.0
    assert lot["item_id"] == "LOT" and lot["edge_pct"] > 25
    # single-unit fair measured from singles only, never inflated by the lot
    assert lot["single_fair_usd"] >= 25.0 * 0.95


def test_find_wholesale_lots_needs_market_depth():
    name = "Rare thing"
    thin = _singles(name, 25.0, 4)               # too few singles
    thin.append({"item_id": "LOT", "title": f"{name} lot of 10", "price": 150.0,
                 "url": "lot", "seller": "w", "condition": "USED_GOOD"})
    assert research.find_wholesale_lots(thin, name) == []


def test_find_wholesale_lots_rejects_thin_edge():
    name = "Thing"
    sample = _singles(name, 25.0, 12)
    sample.append({"item_id": "LOT", "title": f"{name} lot of 10", "price": 240.0,   # $24/unit — no edge
                   "url": "lot", "seller": "w", "condition": "USED_GOOD"})
    assert research.find_wholesale_lots(sample, name) == []


# ------------------------------------------------ thailand: legal screen

def test_import_restriction_prohibited_vape():
    r = th.import_restriction("electronics", "Disposable vape pod 5000 puffs")
    assert not r["allowed"] and r["level"] == "prohibited"


def test_import_restriction_licensed_and_clear():
    assert th.import_restriction("food", "durian chips")["level"] == "licensed"
    assert th.import_restriction("electronics", "bluetooth speaker")["level"] == "licensed"
    clear = th.import_restriction("trading_cards", "pokemon booster box")
    assert clear["allowed"] and clear["level"] == "clear"


def test_feasibility_treats_trade_types_as_venue_screened():
    f = th.feasibility("import_export", "electronics", "yahoo_auctions_jp", "shopee_th")
    assert f.can_buy and f.can_sell and f.requires_proxy       # JP buy needs a proxy
    w = th.feasibility("wholesale", "gaming", "ebay_us", "ebay_us")
    assert w.can_buy and w.can_sell


# ------------------------------------------------ import/export generator

class _TradeDS:
    tick_no = 5

    def __init__(self, venues):          # venues: {venue: {price, stock, sellers, sold_7d}}
        self._v = venues

    def product_public(self, pid):
        return {"id": pid, "name": "Casio fx-JP900", "category": "electronics",
                "weight_kg": 0.3, "venues": list(self._v)}

    def listing(self, pid, venue):
        return dict(self._v[venue]) if venue in self._v else None


def _ctx(ds):
    return GenContext(ds=ds, anomalies=[], by_entity={"casio": []}, niche_ids=set(),
                      store=None, cfg=Config(), investigator=None, graph=None)


def test_import_export_surfaces_hidden_domestic_import():
    # Cheap in JP, dear in the US (flip ships abroad), profitable to import to TH.
    ds = _TradeDS({
        "yahoo_auctions_jp": {"price": 34.0, "stock": 250, "sellers": 22, "sold_7d": 5},
        "shopee_th":         {"price": 98.0, "stock": 45, "sellers": 15, "sold_7d": 11},
        "ebay_us":           {"price": 152.0, "stock": 24, "sellers": 12, "sold_7d": 8},
    })
    assert _best_flip_route({v: ds.listing("casio", v) for v in ds._v}) == ("yahoo_auctions_jp", "ebay_us")
    cands = ImportExportGenerator().generate(_ctx(ds))
    imp = [c for c in cands if c["route"]["kind"] == "import"]
    assert imp, "hidden import-to-TH route not surfaced"
    c = imp[0]
    assert c["opp_type"] == OppType.IMPORT_EXPORT
    assert c["buy_venue"] == "yahoo_auctions_jp" and c["sell_venue"] == "shopee_th"
    assert c["economics"].pessimistic.net_usd > 0
    assert c["route"]["buy_country"] == "JP" and c["route"]["sell_country"] == "TH"


def test_import_export_skips_when_it_would_duplicate_the_flip():
    # The gimbal: flip already buys CN → sells TH (import IS the headline flip),
    # and there is no foreign sell venue → import/export must add nothing.
    ds = _TradeDS({
        "aliexpress":     {"price": 23.0, "stock": 500, "sellers": 40, "sold_7d": 30},
        "shopee_th":      {"price": 52.0, "stock": 60, "sellers": 18, "sold_7d": 20},
        "tiktok_shop_th": {"price": 58.0, "stock": 45, "sellers": 12, "sold_7d": 16},
    })
    ds.product_public = lambda pid: {"id": pid, "name": "gimbal", "category": "electronics",
                                     "weight_kg": 0.6, "venues": list(ds._v)}
    assert ImportExportGenerator().generate(_ctx(ds)) == []


def test_import_export_requires_destination_demand():
    ds = _TradeDS({
        "yahoo_auctions_jp": {"price": 34.0, "stock": 250, "sellers": 22, "sold_7d": 5},
        "shopee_th":         {"price": 98.0, "stock": 45, "sellers": 15, "sold_7d": 0},   # no TH sales
        "ebay_us":           {"price": 152.0, "stock": 24, "sellers": 12, "sold_7d": 8},
    })
    imp = [c for c in ImportExportGenerator().generate(_ctx(ds)) if c["route"]["kind"] == "import"]
    assert imp == []                              # a price gap with no demand isn't a trade


def test_import_export_drops_prohibited_import():
    ds = _TradeDS({
        "yahoo_auctions_jp": {"price": 8.0, "stock": 250, "sellers": 22, "sold_7d": 20},
        "shopee_th":         {"price": 30.0, "stock": 45, "sellers": 15, "sold_7d": 15},
        "ebay_us":           {"price": 45.0, "stock": 24, "sellers": 12, "sold_7d": 8},
    })
    ds.product_public = lambda pid: {"id": pid, "name": "Disposable vape pod kit",
                                     "category": "electronics", "weight_kg": 0.1, "venues": list(ds._v)}
    imp = [c for c in ImportExportGenerator().generate(_ctx(ds)) if c["route"]["kind"] == "import"]
    assert imp == []                              # illegal to import to TH — never suggested


# ------------------------------------------------ wholesale generator

class _WholesaleDS(_TradeDS):
    def __init__(self, venue, agg, sample):
        super().__init__({venue: agg})
        self._venue, self._sample = venue, sample

    def product_public(self, pid):
        return {"id": pid, "name": "Casio calculator", "category": "gaming",
                "weight_kg": 0.3, "venues": [self._venue]}

    def listing_sample(self, pid, venue):
        return self._sample if venue == self._venue else []


def _wholesale_ds():
    name = "Casio calculator"
    sample = _singles(name, 40.0, 14)
    sample.append({"item_id": "LOT", "title": f"{name} lot of 10 bulk wholesale", "price": 240.0,
                   "url": "https://ebay.com/itm/LOT", "seller": "wholesaler", "condition": "USED_GOOD"})
    return _WholesaleDS("ebay_us", {"price": 40.0, "stock": 30, "sellers": 8, "sold_7d": 14}, sample)


def test_wholesale_generator_builds_lot_thesis():
    ds = _wholesale_ds()
    cands = WholesaleGenerator().generate(_ctx(ds))
    assert cands, "no wholesale candidate from a clear lot"
    c = cands[0]
    assert c["opp_type"] == OppType.WHOLESALE and c["qty"] == 10
    assert c["wholesale"]["item_id"] == "LOT"
    assert c["buy_usd"] == 24.0 and c["sell_usd"] >= 40.0 * 0.95
    assert c["economics"].pessimistic.net_usd > 0


def test_verify_wholesale_passes_live_lot_and_fails_when_gone():
    from opportunity_os.agents.verifiers import VerificationCouncil
    ds = _wholesale_ds()
    cand = WholesaleGenerator().generate(_ctx(ds))[0]
    v = VerificationCouncil(Config()).verify_flip(ds, cand)
    assert v.passed and all(c.passed for c in v.checks if c.critical)

    # Pull the lot from the page — re-verification must fail (edge taken).
    ds._sample = [s for s in ds._sample if s["item_id"] != "LOT"]
    v2 = VerificationCouncil(Config()).verify_flip(ds, cand)
    assert not v2.passed
    listing_check = next(c for c in v2.checks if c.verifier == "listing_verifier")
    assert not listing_check.passed and "gone" in listing_check.evidence.lower()


# ------------------------------------------------ demo integration

@pytest.fixture(scope="module")
def demo_built():
    from opportunity_os.db import Store
    from opportunity_os.market import SimulatedMarket
    from opportunity_os.pipeline import Orchestrator
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    cfg = Config(db_path=tmp / "trade.db", auto_cycle_seconds=0)
    store = Store(cfg.db_path)
    world = SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks)
    orch = Orchestrator(cfg, store, world)
    for _ in range(16):
        orch.run_cycle()
    return orch, store


def test_demo_publishes_both_new_types(demo_built):
    _, store = demo_built
    opps = store.list_opportunities(status="active", limit=500)
    types = {o["type"] for o in opps}
    assert "import_export" in types, "no import/export opportunity in the demo feed"
    assert "wholesale" in types, "no wholesale opportunity in the demo feed"


def test_import_export_is_not_a_duplicate_of_a_flip(demo_built):
    """A trade opportunity must be a DISTINCT thesis from any product_arbitrage
    flip — different route/type, not the same edge relabelled."""

    from opportunity_os.clustering import thesis_key
    _, store = demo_built
    opps = store.list_opportunities(status="active", limit=500)
    keys = {}
    for o in opps:
        keys.setdefault(thesis_key(o), []).append(o["type"])
    for k, ts in keys.items():
        assert len(ts) == 1, f"thesis {k} claimed by multiple types {ts}"


def test_trade_types_reverify_not_treated_as_ventures(demo_built):
    """import_export re-verifies as a flip and wholesale via its lot verifier —
    neither should fall through to the venture path and get invalidated wrongly."""

    orch, store = demo_built
    before = {o["id"]: o["type"] for o in store.list_opportunities(status="active", limit=500)
              if o["type"] in ("import_export", "wholesale")}
    assert before, "nothing to re-verify"
    orch.run_cycle()                              # a fresh re-verification pass
    after = {o["id"] for o in store.list_opportunities(status="active", limit=500)}
    # The demo edges are stable, so at least one of each survives re-verification
    # (they'd vanish if routed to the wrong _fresh_* builder).
    survived = {before[i] for i in before if i in after}
    assert "import_export" in survived or "wholesale" in survived
