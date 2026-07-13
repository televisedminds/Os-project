"""The Thailand lens: what's actually executable from Bangkok."""

from opportunity_os import thailand


def test_jp_marketplaces_are_buy_only_via_proxy():
    f = thailand.feasibility("product_arbitrage", "trading_cards", "yahoo_auctions_jp", "ebay_us")
    assert f.can_buy and f.requires_proxy
    assert f.can_sell                                   # eBay accepts TH sellers
    blocked = thailand.feasibility("product_arbitrage", "gaming", "yahoo_auctions_jp", "mercari_jp")
    assert not blocked.can_sell                         # Mercari JP needs JP residency


def test_local_venues_fully_accessible():
    f = thailand.feasibility("product_arbitrage", "handmade", "facebook_mp_th", "etsy")
    assert f.can_buy and f.can_sell and not f.requires_proxy


def test_ventures_always_operable_from_th():
    for t in ("digital_product", "info_product", "local_service", "b2b_service"):
        f = thailand.feasibility(t, "digital_tools", None, None)
        assert f.can_buy and f.can_sell


def test_customs_notes_added_for_special_categories():
    f = thailand.feasibility("product_arbitrage", "food", "shopee_th", "ebay_us")
    assert any("FDA" in n for n in f.customs_notes)
    f2 = thailand.feasibility("product_arbitrage", "audio", "yahoo_auctions_jp", "ebay_us")
    assert any("lithium" in n.lower() for n in f2.customs_notes)


def test_profile_shape():
    p = thailand.profile()
    assert p["vat_rate"] == 0.07
    assert p["de_minimis_thb"] == 1500.0
    assert "ebay_us" in p["venue_access"]
    assert p["payment_rails"]
