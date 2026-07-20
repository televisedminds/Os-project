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


def allocate_capital(opportunities: list[dict], capital_usd: float) -> dict:
    """Greedy plan over verified opportunities: deploy capital where expected
    net per dollar per day is highest, capped per category so one thesis
    can't own the whole book. Input rows are stored opportunity dicts."""

    scored = []
    for o in opportunities:
        econ = o.get("economics") or {}
        cap = float(econ.get("capital_usd", 0) or 0)
        net = float(econ.get("total_net_usd", 0) or 0)
        days = max(1.0, float(o.get("window_days", 7) or 7))
        if cap <= 0 or net <= 0:
            continue
        scored.append({"id": o["id"], "title": o["title"], "category": o.get("category", "?"),
                       "capital_usd": round(cap, 2), "net_usd": round(net, 2),
                       "days": days, "velocity": round(net / cap / days, 4)})
    scored.sort(key=lambda r: r["velocity"], reverse=True)

    plan, skipped = [], []
    remaining = max(0.0, float(capital_usd))
    cat_spend: dict[str, float] = {}
    cat_cap = capital_usd * MAX_CATEGORY_SHARE if capital_usd > 0 else 0.0
    for r in scored:
        cat = r["category"]
        if r["capital_usd"] > remaining:
            skipped.append({**r, "why": f"needs ${r['capital_usd']:,.0f}, only ${remaining:,.0f} left"})
            continue
        if capital_usd > 0 and cat_spend.get(cat, 0.0) + r["capital_usd"] > cat_cap and len(scored) > 1:
            skipped.append({**r, "why": f"category cap — already ${cat_spend.get(cat, 0.0):,.0f} in {cat}"})
            continue
        remaining -= r["capital_usd"]
        cat_spend[cat] = cat_spend.get(cat, 0.0) + r["capital_usd"]
        plan.append({**r, "order": len(plan) + 1})
    deployed = round(sum(r["capital_usd"] for r in plan), 2)
    expected = round(sum(r["net_usd"] for r in plan), 2)
    return {
        "capital_usd": round(float(capital_usd), 2),
        "deployed_usd": deployed,
        "reserve_usd": round(max(0.0, float(capital_usd)) - deployed, 2),
        "expected_net_usd": expected,
        "expected_roi_pct": round(100 * expected / deployed, 1) if deployed else 0.0,
        "plan": plan,
        "skipped": skipped[:5],
    }
