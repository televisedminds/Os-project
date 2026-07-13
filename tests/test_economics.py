"""The economics engine is the product's spine — test it hardest."""

import pytest

from opportunity_os import economics, thailand

CARD = {"id": "card", "name": "Test card", "category": "trading_cards", "weight_kg": 0.05}
HEAVY = {"id": "lego", "name": "Heavy set", "category": "lego", "weight_kg": 4.2}


def test_fx_roundtrip():
    thb = economics.convert(100, "USD", "THB")
    assert thb == pytest.approx(3640, rel=0.01)
    assert economics.convert(thb, "THB", "USD") == pytest.approx(100)


def test_flip_waterfall_consistency():
    econ = economics.compute_flip(CARD, "yahoo_auctions_jp", "ebay_us", 18.40, 34.80, qty=1)
    for scn in (econ.base, econ.pessimistic):
        assert scn.total_cost_usd == pytest.approx(sum(l.amount_usd for l in scn.lines), abs=0.02)
        assert scn.net_usd == pytest.approx(scn.revenue_usd - scn.total_cost_usd, abs=0.02)
    labels = [l.label for l in econ.base.lines]
    assert any("Acquisition" in l for l in labels)
    assert any("fees" in l for l in labels)
    assert any("Shipping" in l for l in labels)


def test_pessimistic_is_strictly_worse():
    econ = economics.compute_flip(CARD, "yahoo_auctions_jp", "ebay_us", 18.40, 44.00, qty=5)
    assert econ.pessimistic.net_usd < econ.base.net_usd
    assert econ.pessimistic.revenue_usd < econ.base.revenue_usd


def test_consolidation_amortizes_fixed_costs():
    single = economics.compute_flip(CARD, "yahoo_auctions_jp", "ebay_us", 18.40, 44.00, qty=1)
    lot = economics.compute_flip(CARD, "yahoo_auctions_jp", "ebay_us", 18.40, 44.00, qty=20)
    assert lot.base.net_usd > single.base.net_usd          # per-unit shipping drops with lot size
    assert lot.capital_usd == pytest.approx(lot.base.total_cost_usd * 20, abs=0.5)
    assert lot.total_net_usd == pytest.approx(lot.base.net_usd * 20, abs=0.5)


def test_us_destination_duty_only_in_pessimistic():
    econ = economics.compute_flip(CARD, "yahoo_auctions_jp", "ebay_us", 18.40, 44.00, qty=5)
    assert not any("US duty" in l.label for l in econ.base.lines)
    assert any("US duty" in l.label for l in econ.pessimistic.lines)


def test_th_export_route_has_no_thai_import_lines():
    item = {"id": "tee", "name": "Tee", "category": "apparel", "weight_kg": 0.3}
    econ = economics.compute_flip(item, "kaidee_th", "ebay_us", 12.0, 48.0, qty=5)
    assert not any("Thai import" in l.label for l in econ.base.lines)


def test_jp_route_includes_thai_vat_when_routed_via_th():
    # Force heavy item: direct-forward is expensive per kg, via-TH path exists;
    # whichever wins, the via-TH scenario must contain VAT when evaluated.
    from opportunity_os.economics import _flip_scenario
    scn = _flip_scenario("base", item=CARD, buy_venue="yahoo_auctions_jp", sell_venue="ebay_us",
                         buy_usd=100.0, sell_usd=200.0, path=["JP", "TH", "US"], qty=1, pessimistic=False)
    assert any("Thai import VAT" in l.label for l in scn.lines)


def test_import_charges_de_minimis_boundary():
    duty, vat, notes = thailand.import_charges(cif_usd=10.0, category="trading_cards",
                                               usd_thb=36.4, parcel_cif_usd=10.0)   # ฿364 parcel
    assert duty == 0.0 and vat > 0
    duty2, vat2, _ = thailand.import_charges(cif_usd=10.0, category="trading_cards",
                                             usd_thb=36.4, parcel_cif_usd=100.0)    # ฿3,640 parcel
    assert duty2 == pytest.approx(1.0)                     # 10% of per-unit CIF
    assert vat2 == pytest.approx((10.0 + duty2) * 0.07, abs=0.01)


def test_breakeven_covers_costs():
    econ = economics.compute_flip(CARD, "yahoo_auctions_jp", "ebay_us", 18.40, 44.00, qty=10)
    fees = economics.SELL_FEES["ebay_us"]
    fee_frac = fees["pct"] + fees.get("cross_border_pct", 0) + fees.get("payout_fx_pct", 0)
    assert econ.breakeven_revenue_usd * (1 - fee_frac) == pytest.approx(econ.base.total_cost_usd, rel=0.05)


def test_venture_economics_positive_model():
    niche = {"id": "n", "name": "Tool", "kind": "digital", "geo": "global",
             "price_point_usd": 9.0, "metrics": {"volume": 5000, "growth_pct": 20,
                                                 "solution_count": 2, "demand_posts": 90, "providers": 2}}
    econ = economics.compute_venture(niche)
    assert econ.kind == "venture"
    assert econ.base.revenue_usd > 0
    assert econ.pessimistic.net_usd < econ.base.net_usd
    assert econ.thb["capital_thb"] == pytest.approx(econ.capital_usd * 36.4, rel=0.01)
