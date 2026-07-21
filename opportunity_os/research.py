"""The quant research core — extract maximum alpha from every API response.

The objective function: **verified opportunities per API request.** An API
response is not "a price" — it is ~50 individual asks, each with a seller, a
title, a condition and an exact URL. This module turns that microstructure
into independent alpha sources that cost zero additional API calls:

* `market_stats`     — the full ask distribution (fair value, percentiles,
                       dispersion, seller concentration) instead of one median;
* `find_dislocations`— individual listings priced far below the market's own
                       clearing range: intra-venue flips with an exact URL.
                       No history needed, no second venue needed — the edge is
                       INSIDE a single response;
* `mine_related`     — title n-gram mining: every response names the model
                       variants, accessories and adjacent products around it.
                       Those become new candidates (graph fan-out) for free;
* `ucb_rank`         — the research-budget allocator: scan slots go to the
                       entities with the best verified-yield per scan, with an
                       exploration bonus for the unexplored (a bandit, not a
                       rota);
* `allocate_capital` — given verified opportunities and finite capital, an
                       execution plan: what to do first, what to skip, what
                       the deployed dollars are expected to return.

Honesty rules, same as everywhere else in this codebase: fair value estimates
are deliberately conservative (the median of the CHEAPEST page of the market,
not the whole market), junk asks are filtered before they can flatter the
tail, and nothing here publishes anything — every candidate still faces the
council and the pessimistic economics gate.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from statistics import fmean, median

# --------------------------------------------------------------------------
# Ask-distribution statistics
# --------------------------------------------------------------------------

# Listings whose titles contain these are not the product — they are parts,
# empties, reproductions or accessories masquerading in the cheap tail. The
# cheapest results of any sorted search are MOSTLY this; filtering them is
# what separates a dislocation signal from a junk detector.
JUNK_TERMS = (
    "for parts", "parts only", "not working", "broken", "as-is", "as is",
    "box only", "case only", "manual only", "cover only", "shell only",
    "empty box", "no console", "no game", "read description", "read desc",
    "repro", "reproduction", "replica", "custom label", "photo of", "picture of",
    "poster", "sticker", "decal", "keychain", "charm", "miniature", "dollhouse",
    "damaged", "cracked", "faulty", "untested", "spares", "repair",
)

BAD_CONDITIONS = {"FOR_PARTS_OR_NOT_WORKING"}

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def looks_junk(title: str, condition: str = "") -> bool:
    low = f" {title.lower()} "
    if condition and condition.upper() in BAD_CONDITIONS:
        return True
    return any(term in low for term in JUNK_TERMS)


def title_matches_query(title: str, query: str, min_cover: float = 0.6) -> bool:
    """Does this listing plausibly BE the queried product? Requires most query
    tokens present, and every model-number-like token (letters+digits, e.g.
    'ags-101', '2002r') present — those are the tokens that distinguish the
    valuable variant from its cheap sibling."""

    q = [t for t in _tokens(query) if len(t) > 1]
    if not q:
        return True
    ttl = set(_tokens(title))
    hits = sum(1 for t in q if t in ttl)
    for t in q:
        if any(c.isdigit() for c in t) and any(c.isalpha() for c in t) and t not in ttl:
            return False                     # model number missing → wrong variant
    return hits / len(q) >= min_cover


def _pctl(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def market_stats(sample: list[dict], total: int | None = None) -> dict:
    """Distribution + microstructure stats from one response's listings.

    `fair_usd` is the median of the UPPER HALF of the (price-sorted) page.
    Because search pages are fetched sorted by price ascending, the page is
    the cheapest slice of the whole market — so this "fair value" is still
    below the true market median whenever total > page size. Deliberately
    conservative: a dislocation against this number is a dislocation against
    any honest number.
    """

    prices = sorted(float(s["price"]) for s in sample if float(s.get("price", 0)) > 0)
    n = len(prices)
    if n == 0:
        return {"n": 0, "fair_usd": 0.0, "p25": 0.0, "p75": 0.0,
                "dispersion": 0.0, "sellers": 0, "top_seller_share": 0.0}
    upper = prices[n // 2:] or prices
    fair = median(upper)
    p25, p75 = _pctl(prices, 0.25), _pctl(prices, 0.75)
    sellers = Counter(s.get("seller", "?") for s in sample)
    top_share = (max(sellers.values()) / n) if sellers else 0.0
    return {
        "n": n,
        "total": int(total if total is not None else n),
        "fair_usd": round(fair, 2),
        "p25": round(p25, 2),
        "p75": round(p75, 2),
        "dispersion": round((p75 - p25) / fair, 3) if fair else 0.0,
        "sellers": len(sellers),
        "top_seller_share": round(top_share, 3),
    }


# --------------------------------------------------------------------------
# Dislocations — the per-listing alpha inside a single response
# --------------------------------------------------------------------------

MIN_MARKET_DEPTH = 8        # listings needed before "fair value" means anything
MIN_MARKET_SELLERS = 5      # distinct sellers needed (one seller ≠ a market)
MIN_PRICE_USD = 12.0        # below this, fees eat everything — not worth a look
MAX_DISCOUNT = 0.85         # >85% off is a scam/junk prior, not a bargain
DEFAULT_MIN_EDGE = 0.30     # ask must sit ≥30% below conservative fair value


def find_dislocations(sample: list[dict], query: str, *,
                      min_edge: float = DEFAULT_MIN_EDGE,
                      max_hits: int = 3) -> list[dict]:
    """Individual asks sitting far below the market's own clearing range.

    Returns at most `max_hits` per response, deepest discount first. Every
    hit carries the exact listing (id/url/title/seller) and the conservative
    fair value it was measured against. These are CANDIDATES — the council
    and the fee waterfall still decide whether any of them publish.
    """

    stats = market_stats(sample)
    fair = stats["fair_usd"]
    if (stats["n"] < MIN_MARKET_DEPTH or stats["sellers"] < MIN_MARKET_SELLERS
            or fair < MIN_PRICE_USD):
        return []
    hits = []
    for s in sample:
        price = float(s.get("price", 0))
        if price < MIN_PRICE_USD or price >= fair * (1 - min_edge):
            continue
        if price < fair * (1 - MAX_DISCOUNT):
            continue                                   # too good ⇒ almost surely not real
        title = s.get("title", "")
        if looks_junk(title, s.get("condition", "")):
            continue
        if not title_matches_query(title, query):
            continue
        hits.append({
            "item_id": s.get("item_id", ""),
            "title": title,
            "url": s.get("url", ""),
            "seller": s.get("seller", "?"),
            "condition": s.get("condition", ""),
            "ask_usd": round(price, 2),
            "fair_usd": fair,
            "edge_pct": round(100 * (1 - price / fair), 1),
            "market_n": stats["n"],
            "market_sellers": stats["sellers"],
        })
    hits.sort(key=lambda h: h["ask_usd"] / max(0.01, h["fair_usd"]))
    return hits[:max_hits]


def find_liquidation(sample: list[dict], *, min_listings: int = 3,
                     min_avg_discount: float = 0.12) -> dict | None:
    """One seller holding several below-fair asks at once — the signature of
    an estate sale, a store closing, or someone who wants out fast. Their
    whole inventory is worth a manual look, not just the item we searched."""

    stats = market_stats(sample)
    fair = stats["fair_usd"]
    if stats["n"] < MIN_MARKET_DEPTH or fair < MIN_PRICE_USD:
        return None
    by_seller: dict[str, list[dict]] = {}
    for s in sample:
        if float(s.get("price", 0)) <= 0 or looks_junk(s.get("title", ""), s.get("condition", "")):
            continue
        by_seller.setdefault(s.get("seller", "?"), []).append(s)
    for seller, rows in by_seller.items():
        if seller in ("?", "") or len(rows) < min_listings:
            continue
        discounts = [1 - float(r["price"]) / fair for r in rows]
        avg = fmean(discounts)
        if avg >= min_avg_discount:
            return {
                "seller": seller,
                "listings": len(rows),
                "avg_discount_pct": round(100 * avg, 1),
                "fair_usd": fair,
                "items": [{"title": r.get("title", ""), "price": float(r["price"]),
                           "url": r.get("url", "")} for r in rows[:6]],
            }
    return None


# --------------------------------------------------------------------------
# Title mining — the free candidate graph inside every response
# --------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "or", "by",
    "new", "used", "oem", "authentic", "genuine", "original", "official",
    "free", "fast", "shipping", "ship", "usa", "us", "lot", "rare", "vintage",
    "tested", "works", "working", "great", "good", "very", "mint", "nice",
    "condition", "complete", "cib", "sealed", "boxed", "box", "no", "only",
}


def _is_model_token(tok: str) -> bool:
    return any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)


def mine_related(sample: list[dict], base_query: str, *,
                 min_support: int = 3, max_out: int = 5) -> list[dict]:
    """Recurring phrases across listing titles that are NOT the thing we
    searched for: model variants, adjacent products, editions. Each is a
    candidate node for the discovery graph — found without a single extra
    API call, because sellers already wrote the product graph into their
    titles.

    Returns [{phrase, support, sellers, model_like}] strongest first.
    """

    base = set(_tokens(base_query))
    grams: Counter = Counter()
    gram_sellers: dict[str, set] = {}
    for s in sample:
        title = s.get("title", "")
        if looks_junk(title):
            continue
        toks = [t for t in _tokens(title) if len(t) >= 2]
        seen_in_title: set[str] = set()
        for size in (2, 3):
            for i in range(len(toks) - size + 1):
                gram = toks[i:i + size]
                if gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS:
                    continue
                if all(t in base or t in _STOPWORDS for t in gram):
                    continue                      # just re-describing the query
                if not any(len(t) >= 3 and any(c.isalpha() for c in t) for t in gram):
                    continue
                phrase = " ".join(gram)
                if len(phrase) < 7 or phrase in seen_in_title:
                    continue
                seen_in_title.add(phrase)
                grams[phrase] += 1
                gram_sellers.setdefault(phrase, set()).add(s.get("seller", "?"))
    out = []
    for phrase, support in grams.most_common(40):
        if support < min_support or len(gram_sellers.get(phrase, ())) < 2:
            continue
        model_like = any(_is_model_token(t) for t in phrase.split())
        out.append({"phrase": phrase, "support": support,
                    "sellers": len(gram_sellers[phrase]), "model_like": model_like})
    # Model-number phrases outrank plain-word phrases at equal support: they
    # name concrete tradeable variants, not adjectives.
    out.sort(key=lambda g: (g["model_like"], g["support"]), reverse=True)
    # Drop phrases fully contained in a stronger phrase we already kept.
    kept: list[dict] = []
    for g in out:
        if any(g["phrase"] in k["phrase"] for k in kept):
            continue
        kept.append(g)
        if len(kept) >= max_out:
            break
    return kept


# --------------------------------------------------------------------------
# Research-budget allocation — a bandit over scan slots
# --------------------------------------------------------------------------

UCB_EXPLORE = 0.6           # exploration weight: higher = try unknowns sooner
YIELD_ANOMALY = 1.0         # reward: an anomaly detected on this entity
YIELD_CANDIDATE = 2.0       # reward: survived investigation into a candidate
YIELD_PUBLISHED = 6.0       # reward: cleared the council and published


def ucb_rank(rows: list[dict], total_scans: int) -> list[tuple[str, float]]:
    """Rank entities by expected research value per scan, UCB1-style.

    Each row: {entity_id, scans, anomalies, candidates, published, prior}.
    Never-scanned entities get the exploration bonus at full strength, so new
    discoveries are probed before the budget settles onto proven producers —
    exploit what yields, but keep buying information.
    """

    t = max(2, total_scans)
    ranked = []
    for r in rows:
        scans = max(0, int(r.get("scans", 0)))
        reward = (YIELD_ANOMALY * r.get("anomalies", 0)
                  + YIELD_CANDIDATE * r.get("candidates", 0)
                  + YIELD_PUBLISHED * r.get("published", 0)
                  + float(r.get("prior", 0.0)))
        exploit = reward / (scans + 2.0)              # +2: mild optimistic prior
        explore = UCB_EXPLORE * math.sqrt(math.log(t) / (scans + 1.0))
        ranked.append((r["entity_id"], round(exploit + explore, 4)))
    ranked.sort(key=lambda kv: kv[1], reverse=True)
    return ranked


# --------------------------------------------------------------------------
# Capital allocation — from a ranked list to an execution plan
# --------------------------------------------------------------------------

MAX_CATEGORY_SHARE = 0.6    # concentration cap: ≤60% of capital in one category
BENCHMARK_DAILY = 0.0005    # ~18%/yr alternative return, for opportunity cost
# Fraction of capital recoverable in a fire-sale, by opportunity kind — drives
# the honest worst-case loss (you can usually dump goods; a launched venture's
# startup spend is mostly sunk).
_LIQUIDATION_RECOVERY = {"flip": 0.70, "dislocation": 0.70, "refurbish": 0.50,
                         "venture": 0.15, "default": 0.5}
_RISK_MIN_LEVEL = {"conservative": 3, "balanced": 2, "aggressive": 1}   # verification-level rank
_LEVEL_RANK = {"execution_ready": 4, "multi_source_verified": 3,
               "partially_verified": 2, "discovered": 1, "invalidated": 0}


def allocate_capital(opportunities: list[dict], capital_usd: float, *,
                     max_per_opportunity: float | None = None,
                     max_category_share: float = MAX_CATEGORY_SHARE,
                     risk_tolerance: str = "balanced",
                     liquidity_reserve_pct: float = 0.0,
                     max_holding_days: float | None = None) -> dict:
    """Solve for the best use of finite capital — not just a ranking (Phase 11).

    Inputs beyond the opportunity list: a per-opportunity cap, a per-category
    concentration cap, a risk tolerance (which verification levels qualify and
    whether to price on base or pessimistic net), a liquidity reserve to keep
    uninvested, and a maximum holding time. Deduplicated by thesis so 20
    listings of one edge don't each draw capital.

    Outputs per pick: quantity, capital, expected profit, WORST-CASE loss,
    expected completion date, and opportunity cost of the tied-up cash; plus
    portfolio totals and cash remaining.
    """

    import datetime as _dt

    reserve = max(0.0, float(capital_usd) * max(0.0, min(1.0, liquidity_reserve_pct)))
    investable = max(0.0, float(capital_usd) - reserve)
    min_level = _RISK_MIN_LEVEL.get(risk_tolerance, 2)
    conservative = risk_tolerance == "conservative"
    today = _dt.date.today()

    # Dedup by thesis: keep the strongest opportunity per market edge.
    from .clustering import thesis_key
    best_by_thesis: dict[str, dict] = {}
    for o in opportunities:
        k = thesis_key(o)
        if k not in best_by_thesis or _net(o) > _net(best_by_thesis[k]):
            best_by_thesis[k] = o

    scored, filtered_out = [], []
    for o in best_by_thesis.values():
        econ = o.get("economics") or {}
        cap = float(econ.get("capital_usd", 0) or 0)
        base_net = float(econ.get("total_net_usd", 0) or 0)
        pess_unit = float((econ.get("pessimistic") or {}).get("net_usd", 0) or 0)
        qty = int(econ.get("qty", 1) or 1)
        pess_net = pess_unit * qty
        net = pess_net if conservative else base_net
        days = max(1.0, float(o.get("window_days", 7) or 7))
        # A stored ACTIVE opportunity has passed the gates, so absent-level
        # means at least partially-verified (not "discovered").
        level = o.get("verification_level") or "partially_verified"
        kind = (o.get("route", {}) or {}).get("kind") or ("venture" if econ.get("kind") == "venture" else "flip")
        if cap <= 0 or net <= 0:
            continue
        if _LEVEL_RANK.get(level, 0) < min_level:
            filtered_out.append({"id": o["id"], "title": o["title"],
                                 "why": f"{level} below '{risk_tolerance}' risk floor"})
            continue
        if max_holding_days and days > max_holding_days:
            filtered_out.append({"id": o["id"], "title": o["title"],
                                 "why": f"{days:.0f}-day hold exceeds your {max_holding_days:.0f}-day limit"})
            continue
        recovery = _LIQUIDATION_RECOVERY.get(kind, _LIQUIDATION_RECOVERY["default"])
        scored.append({
            "id": o["id"], "title": o["title"], "category": o.get("category", "?"),
            "verification_level": level, "qty": qty,
            "capital_usd": round(cap, 2), "expected_profit_usd": round(net, 2),
            "worst_case_loss_usd": round(cap * (1 - recovery), 2),
            "days": days, "velocity": round(net / cap / days, 4),
            "completion_date": (today + _dt.timedelta(days=round(days))).isoformat(),
        })
    scored.sort(key=lambda r: r["velocity"], reverse=True)

    plan, skipped = [], list(filtered_out)
    remaining = investable
    cat_spend: dict[str, float] = {}
    cat_cap = investable * max_category_share if investable > 0 else 0.0
    per_opp_cap = float(max_per_opportunity) if max_per_opportunity else float("inf")
    for r in scored:
        cat = r["category"]
        take = min(r["capital_usd"], per_opp_cap)
        # The per-opportunity cap can only trim a MULTI-unit position (buy fewer
        # units); a single indivisible lot over the cap is skipped, not fractioned.
        if take < r["capital_usd"] and r["qty"] <= 1:
            skipped.append({"id": r["id"], "title": r["title"],
                            "why": f"${r['capital_usd']:,.0f} single lot exceeds your "
                                   f"${per_opp_cap:,.0f} per-opportunity cap"})
            continue
        if take > remaining:
            skipped.append({"id": r["id"], "title": r["title"],
                            "why": f"needs ${r['capital_usd']:,.0f}, only ${remaining:,.0f} investable left"})
            continue
        if investable > 0 and cat_spend.get(cat, 0.0) + take > cat_cap and len(scored) > 1:
            skipped.append({"id": r["id"], "title": r["title"],
                            "why": f"category cap — already ${cat_spend.get(cat, 0.0):,.0f} in {cat}"})
            continue
        # Scale down proportionally if the per-opportunity cap trims a multi-lot.
        scale = take / r["capital_usd"] if r["capital_usd"] else 1.0
        opp_cost = round(take * BENCHMARK_DAILY * r["days"], 2)
        remaining -= take
        cat_spend[cat] = cat_spend.get(cat, 0.0) + take
        plan.append({**r, "order": len(plan) + 1, "capital_usd": round(take, 2),
                     "qty": max(1, int(r["qty"] * scale)),
                     "expected_profit_usd": round(r["expected_profit_usd"] * scale, 2),
                     "worst_case_loss_usd": round(r["worst_case_loss_usd"] * scale, 2),
                     "opportunity_cost_usd": opp_cost})
    deployed = round(sum(r["capital_usd"] for r in plan), 2)
    expected = round(sum(r["expected_profit_usd"] for r in plan), 2)
    worst = round(sum(r["worst_case_loss_usd"] for r in plan), 2)
    completion = max((r["completion_date"] for r in plan), default=None)
    return {
        "capital_usd": round(float(capital_usd), 2),
        "risk_tolerance": risk_tolerance,
        "reserve_usd": round(reserve + max(0.0, remaining), 2),
        "deployed_usd": deployed,
        "cash_remaining_usd": round(float(capital_usd) - deployed, 2),
        "expected_net_usd": expected,
        "expected_roi_pct": round(100 * expected / deployed, 1) if deployed else 0.0,
        "worst_case_loss_usd": worst,
        "expected_completion": completion,
        "plan": plan,
        "skipped": skipped[:6],
    }


def _net(o: dict) -> float:
    return float((o.get("economics") or {}).get("total_net_usd", 0) or 0)
