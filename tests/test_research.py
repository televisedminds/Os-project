"""The quant research core: ask-distribution stats, dislocation detection,
title mining, the EV bandit, and the capital allocator. Pure functions, no
network — this is where the "more alpha per API call" claim is pinned down."""

from opportunity_os import research


def _market(n=10, base=100.0, sellers=6):
    """A synthetic page of asks clustered around `base`, spread across sellers."""

    out = []
    for i in range(n):
        out.append({"item_id": f"i{i}", "title": f"Widget Model X{i % 3}",
                    "price": base * (0.95 + 0.02 * i), "url": f"u{i}",
                    "seller": f"s{i % sellers}", "condition": "USED_GOOD"})
    return out


# ------------------------------------------------------------- market_stats

def test_market_stats_conservative_fair_value():
    s = research.market_stats(_market(n=12, base=100.0))
    # fair = median of the upper half → above the page median, so a dislocation
    # measured against it is a dislocation against any honest number.
    assert s["n"] == 12 and s["sellers"] == 6
    assert s["fair_usd"] >= 100.0
    assert 0 <= s["top_seller_share"] <= 1


def test_market_stats_empty_is_safe():
    s = research.market_stats([])
    assert s["n"] == 0 and s["fair_usd"] == 0.0


# ------------------------------------------------------------ dislocations

def test_find_dislocations_flags_underpriced_real_listing():
    sample = _market(n=12, base=100.0)
    sample.append({"item_id": "cheap", "title": "Widget Model X1 tested working",
                   "price": 55.0, "url": "buyme", "seller": "flipper", "condition": "USED_GOOD"})
    hits = research.find_dislocations(sample, "widget model")
    assert hits and hits[0]["item_id"] == "cheap"
    assert hits[0]["url"] == "buyme"                 # exact URL carried through
    assert hits[0]["edge_pct"] >= 30


def test_dislocation_ignores_junk_and_parts():
    sample = _market(n=12, base=100.0)
    sample.append({"item_id": "junk", "title": "Widget Model X1 FOR PARTS not working",
                   "price": 30.0, "url": "u", "seller": "x", "condition": "FOR_PARTS_OR_NOT_WORKING"})
    hits = research.find_dislocations(sample, "widget model")
    assert all(h["item_id"] != "junk" for h in hits)


def test_dislocation_ignores_wrong_model_variant():
    # A cheap listing whose model number does NOT match the query is a different
    # (cheaper) product, not a bargain — it must not be flagged.
    sample = _market(n=12, base=100.0)
    sample.append({"item_id": "variant", "title": "Widget Model Z9 budget",
                   "price": 40.0, "url": "u", "seller": "x", "condition": "USED_GOOD"})
    hits = research.find_dislocations(sample, "widget model x1 ags-101")
    assert all(h["item_id"] != "variant" for h in hits)


def test_dislocation_needs_a_real_market():
    # Too few listings / sellers → no reliable fair value → no signal.
    thin = [{"item_id": "a", "title": "Widget", "price": 10, "url": "u",
             "seller": "s", "condition": ""},
            {"item_id": "b", "title": "Widget", "price": 100, "url": "u",
             "seller": "s", "condition": ""}]
    assert research.find_dislocations(thin, "widget") == []


def test_find_liquidation_spots_one_seller_dumping():
    sample = _market(n=10, base=100.0)
    for i in range(4):                               # one seller, four below-fair asks
        sample.append({"item_id": f"liq{i}", "title": f"Widget Model X{i}",
                       "price": 70.0, "url": f"l{i}", "seller": "closing_shop",
                       "condition": "USED_GOOD"})
    liq = research.find_liquidation(sample)
    assert liq and liq["seller"] == "closing_shop" and liq["listings"] >= 3


# --------------------------------------------------------------- mine_related

def test_mine_related_finds_recurring_variant_phrases():
    sample = []
    for i in range(6):
        sample.append({"item_id": f"a{i}", "title": "Game Boy pokemon edition pikachu",
                       "price": 100, "url": "u", "seller": f"s{i}", "condition": ""})
    for i in range(4):
        sample.append({"item_id": f"b{i}", "title": "Game Boy tetris bundle",
                       "price": 90, "url": "u", "seller": f"t{i}", "condition": ""})
    related = research.mine_related(sample, "game boy")
    phrases = {r["phrase"] for r in related}
    assert any("pokemon" in p or "pikachu" in p or "tetris" in p for p in phrases)


def test_mine_related_ignores_the_query_itself():
    sample = [{"item_id": f"a{i}", "title": "game boy game boy", "price": 100,
               "url": "u", "seller": f"s{i}", "condition": ""} for i in range(6)]
    related = research.mine_related(sample, "game boy")
    assert all(r["phrase"] != "game boy" for r in related)


# ------------------------------------------------------------------- ucb_rank

def test_ucb_prefers_producers_but_explores_the_unknown():
    rows = [
        {"entity_id": "proven", "scans": 8, "anomalies": 4, "candidates": 3, "published": 2},
        {"entity_id": "dud", "scans": 8, "anomalies": 0, "candidates": 0, "published": 0},
        {"entity_id": "fresh", "scans": 0},          # never scanned → exploration bonus
    ]
    ranked = dict(research.ucb_rank(rows, total_scans=16))
    assert ranked["proven"] > ranked["dud"]          # exploit what yields
    assert ranked["fresh"] > ranked["dud"]           # but still probe the unknown


# ------------------------------------------------------------ allocate_capital

def test_allocate_capital_picks_best_velocity_within_budget():
    opps = [
        {"id": "fast", "title": "Fast flip", "category": "gaming",
         "economics": {"capital_usd": 100, "total_net_usd": 50}, "window_days": 5},
        {"id": "slow", "title": "Slow flip", "category": "cameras",
         "economics": {"capital_usd": 100, "total_net_usd": 50}, "window_days": 30},
        {"id": "toobig", "title": "Too big", "category": "watches",
         "economics": {"capital_usd": 10000, "total_net_usd": 5000}, "window_days": 5},
    ]
    plan = research.allocate_capital(opps, capital_usd=250)
    ids = [p["id"] for p in plan["plan"]]
    assert ids[0] == "fast"                          # best net/$/day goes first
    assert "toobig" not in ids                       # over budget → skipped
    assert plan["deployed_usd"] <= 250
    assert plan["expected_net_usd"] > 0


def test_allocate_capital_enforces_category_concentration_cap():
    opps = [
        {"id": f"g{i}", "title": f"Gaming {i}", "category": "gaming",
         "economics": {"capital_usd": 100, "total_net_usd": 40}, "window_days": 7}
        for i in range(6)
    ]
    plan = research.allocate_capital(opps, capital_usd=1000)
    gaming_spend = sum(p["capital_usd"] for p in plan["plan"] if p["category"] == "gaming")
    assert gaming_spend <= 1000 * research.MAX_CATEGORY_SHARE + 1e-6
