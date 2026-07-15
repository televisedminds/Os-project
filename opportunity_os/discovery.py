"""The discovery engine — auto-find NEW things to watch.

Live mode used to be limited to a hand-typed watchlist: the fleet could only
verify what you already knew about. Discovery removes that ceiling. Each cycle
(throttled) a set of discovery sources sweep the open internet for candidates —
trending products, hot flips, rising demand niches — and the best of them are
promoted into the observed set, where the normal pipeline (observe → anomaly →
investigate → verify → price → publish) takes over.

Design rules, mirrored from the adapters:

* every source degrades gracefully — a failure disables that source for the
  cycle and is reported through `check()`/`last_error`, never crashes a cycle;
* discovery is *candidate generation only* — nothing a source returns is
  trusted; it still has to survive every downstream gate before a user sees it;
* sources are ranked by an interest score and capped, so a noisy source cannot
  flood the watched set; stale discoveries expire.

The keyword relevance filter here is deliberately crude. It is the exact seam
where an LLM classifier drops in next (see `classify_hook`): swap the heuristic
for a model call and discovery gets much sharper without touching anything else.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict

import httpx

from .market import watchlist as wl

# Commerce-intent vocabulary — a trend/post is only worth watching if it looks
# like something can be bought, sold, or built around. Strict on purpose: a few
# good candidates beat a flood of celebrity/sports noise.
COMMERCE_TERMS = {
    "buy", "sell", "sold out", "restock", "preorder", "pre-order", "drop", "release",
    "launch", "deal", "sale", "discount", "price", "resell", "resale", "flip", "profit",
    "margin", "wholesale", "arbitrage", "sneaker", "collectible", "vintage", "retro",
    "limited", "exclusive", "trading card", "pokemon", "lego", "gundam", "figure",
    "watch", "camera", "console", "gadget", "kit", "viral", "tiktok made me buy",
    "amazon", "shopee", "etsy", "gumroad", "saas", "startup", "side project", "tool",
    "template", "guide", "course", "service", "demand", "shortage",
}

# Category inference from keywords in a candidate name.
CATEGORY_HINTS = [
    ("trading_cards", ("pokemon", "pokémon", "trading card", "tcg", "mtg", "yugioh")),
    ("sneakers", ("sneaker", "jordan", "yeezy", "new balance", "dunk", "adidas", "nike")),
    ("lego", ("lego",)),
    ("gaming", ("game boy", "gameboy", "console", "nintendo", "playstation", "xbox", "retro game")),
    ("cameras", ("camera", "lens", "contax", "leica", "canon", "nikon", "film")),
    ("watches", ("watch", "seiko", "rolex", "casio", "chronograph")),
    ("toys", ("gundam", "figure", "funko", "model kit", "plush")),
    ("electronics", ("gadget", "gimbal", "keyboard", "earbuds", "charger", "drone", "gadgets")),
    ("apparel", ("tee", "hoodie", "jacket", "vintage clothing", "streetwear")),
    ("books", ("manga", "book", "comic", "first edition")),
]

# Niche-kind inference for demand/venture candidates.
NICHE_KIND_HINTS = [
    ("digital", ("saas", "app", "tool", "dashboard", "api", "software", "automation", "chrome extension")),
    ("info", ("guide", "course", "ebook", "template", "checklist", "tutorial", "how to")),
    ("local", ("cleaning", "repair", "install", "delivery", "bangkok", "local", "detailing")),
    ("b2b", ("wholesale", "supplier", "b2b", "partnership", "installer", "contractor")),
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "with", "your", "you", "how", "is"}


def slug(text: str, prefix: str) -> str:
    s = _SLUG_RE.sub("_", text.lower()).strip("_")
    return f"{prefix}_{s[:48]}" if s else f"{prefix}_x"


def infer_category(name: str) -> str:
    low = name.lower()
    for cat, keys in CATEGORY_HINTS:
        if any(k in low for k in keys):
            return cat
    return "collectibles"


def infer_niche_kind(name: str) -> str:
    low = name.lower()
    for kind, keys in NICHE_KIND_HINTS:
        if any(k in low for k in keys):
            return kind
    return "info"


def commerce_relevance(text: str) -> float:
    """0..1 — how much a candidate looks like a real buy/sell/build opportunity."""

    low = text.lower()
    hits = sum(1 for term in COMMERCE_TERMS if term in low)
    return min(1.0, hits / 3.0)


def clean_query(name: str) -> str:
    """A short, searchable query from a noisy title."""

    words = [w for w in _SLUG_RE.sub(" ", name.lower()).split() if w not in _STOP]
    return " ".join(words[:6]) or name.strip()


@dataclass
class Candidate:
    """One thing a discovery source thinks is worth watching."""

    kind: str                       # "product" | "niche"
    id: str
    name: str
    source: str
    score: float = 0.0              # discovery interest (higher = watch sooner)
    reason: str = ""
    category: str = "collectibles"
    weight_kg: float = 0.5
    queries: dict[str, str] = field(default_factory=dict)   # venue -> sell-side query
    reddit_query: str | None = None
    news_query: str | None = None
    # niche-only:
    niche_kind: str = "info"
    geo: str = "global"
    price_point_usd: float = 15.0

    def to_watch_product(self) -> wl.WatchProduct:
        return wl.WatchProduct(
            id=self.id, name=self.name, category=self.category, weight_kg=self.weight_kg,
            queries=dict(self.queries), manual_listings={},
            bootstrap_sold_7d={}, reddit_query=self.reddit_query, news_query=self.news_query)

    def to_watch_niche(self) -> wl.WatchNiche:
        return wl.WatchNiche(
            id=self.id, name=self.name, kind=self.niche_kind, geo=self.geo,
            price_point_usd=self.price_point_usd, reddit_query=self.reddit_query,
            news_query=self.news_query, base_volume=1500.0, solution_count=3,
            providers=3, demand_posts=60.0)


class DiscoverySource:
    id: str = "base"
    name: str = "Base discovery"

    def __init__(self, cfg, client: httpx.Client | None = None):
        self.cfg = cfg
        self.client = client or httpx.Client(
            timeout=cfg.http_timeout, follow_redirects=True,
            headers={"User-Agent": cfg.user_agent})
        self.last_error = ""

    def _fail(self, msg: str):
        self.last_error = msg[:300]
        return []

    def discover(self) -> list[Candidate]:      # pragma: no cover - overridden
        return []

    def check(self) -> tuple[bool, str]:        # pragma: no cover - overridden
        return False, "not implemented"


# --------------------------------------------------------------- Google Trends

class GoogleTrendsDiscovery(DiscoverySource):
    """Keyless: Google's daily trending searches, filtered hard for commerce
    intent. Most daily trends are news/sports and are dropped; the few that
    look buyable/buildable become niche or product candidates."""

    id = "google_trends"
    name = "Google Trends (daily RSS)"
    URL = "https://trends.google.com/trending/rss"

    def __init__(self, cfg, client=None):
        super().__init__(cfg, client)
        self.geo = getattr(cfg, "discovery_trends_geo", "US")

    def _traffic(self, text: str | None) -> float:
        if not text:
            return 0.0
        m = re.search(r"([\d,]+)", text)
        return float(m.group(1).replace(",", "")) if m else 0.0

    def discover(self) -> list[Candidate]:
        try:
            r = self.client.get(self.URL, params={"geo": self.geo})
            r.raise_for_status()
            root = ET.fromstring(r.text)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"trends fetch failed: {e}")
        ns = {"ht": "https://trends.google.com/trending/rss"}
        out: list[Candidate] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title or len(title) < 3:
                continue
            snippets = " ".join(
                (n.findtext("ht:news_item_title", default="", namespaces=ns) or "")
                for n in item.findall("ht:news_item", ns))
            rel = commerce_relevance(f"{title} {snippets}")
            if rel < 0.34:                          # needs a real commerce signal
                continue
            traffic = self._traffic(item.findtext("ht:approx_traffic", namespaces=ns))
            score = round(min(1.5, rel + traffic / 200000), 3)
            cat = infer_category(title)
            is_product = cat != "collectibles" or "buy" in snippets.lower()
            if is_product:
                out.append(Candidate(
                    kind="product", id=slug(title, "disc_p"), name=title.title(),
                    source=self.id, score=score, category=cat,
                    reason=f"Trending search (~{int(traffic)}+ searches) with commerce signal.",
                    queries={"ebay_us": clean_query(title)},
                    reddit_query=clean_query(title), news_query=clean_query(title)))
            else:
                out.append(Candidate(
                    kind="niche", id=slug(title, "disc_n"), name=title.title(),
                    source=self.id, score=score, niche_kind=infer_niche_kind(f"{title} {snippets}"),
                    reason=f"Trending topic (~{int(traffic)}+ searches) with demand signal.",
                    reddit_query=clean_query(title), news_query=clean_query(title)))
        return out

    def check(self) -> tuple[bool, str]:
        got = self.discover()
        if self.last_error:
            return False, self.last_error
        return True, f"keyless; {len(got)} commerce-relevant trend(s) this pass (geo={self.geo})"


# --------------------------------------------------------------------- Reddit

class RedditDiscovery(DiscoverySource):
    """Hot posts in commerce/opportunity subreddits — genuinely high signal
    (r/Flipping is people describing profitable flips in real time). Uses the
    Reddit OAuth key when set; public JSON is usually blocked from servers."""

    id = "reddit_discovery"
    name = "Reddit (commerce subreddits)"

    # (subreddit, candidate kind, niche_kind for niche subs)
    DEFAULT_SUBS = [
        ("Flipping", "product", ""),
        ("ThriftStoreHauls", "product", ""),
        ("SideProject", "niche", "digital"),
        ("juststart", "niche", "info"),
        ("EntrepreneurRideAlong", "niche", "info"),
        ("ecommerce", "niche", "b2b"),
    ]

    def __init__(self, cfg, client=None, reddit_adapter=None):
        super().__init__(cfg, client)
        self.subs = getattr(cfg, "discovery_subreddits", None) or self.DEFAULT_SUBS
        self._adapter = reddit_adapter        # reuse an adapter's OAuth token if given

    def _token(self) -> str | None:
        if self._adapter is None and self.cfg.reddit_client_id:
            from .market.adapters import RedditAdapter
            self._adapter = RedditAdapter(self.cfg, self.client)
        if self._adapter is not None and hasattr(self._adapter, "_oauth_token"):
            return self._adapter._oauth_token()
        return None

    def _fetch_sub(self, sub: str, token: str | None) -> list[dict]:
        base = "https://oauth.reddit.com" if token else "https://www.reddit.com"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = self.client.get(f"{base}/r/{sub}/hot.json",
                            params={"limit": "12", "t": "week"}, headers=headers)
        r.raise_for_status()
        return [c.get("data", {}) for c in r.json().get("data", {}).get("children", [])]

    def discover(self) -> list[Candidate]:
        token = self._token()
        out: list[Candidate] = []
        errors = 0
        for sub, kind, niche_kind in self.subs:
            try:
                posts = self._fetch_sub(sub, token)
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.last_error = f"r/{sub}: {e}"[:300]
                continue
            for p in posts:
                title = (p.get("title") or "").strip()
                if not title or len(title) < 8:
                    continue
                score_raw = int(p.get("score", 0)) + 2 * int(p.get("num_comments", 0))
                rel = commerce_relevance(title)
                if kind == "product" and rel < 0.34:
                    continue                       # flipping posts still need a product signal
                score = round(min(2.0, 0.3 + rel + score_raw / 800), 3)
                if kind == "product":
                    out.append(Candidate(
                        kind="product", id=slug(title, "disc_p"), name=title[:70],
                        source=self.id, score=score, category=infer_category(title),
                        reason=f"Hot in r/{sub} ({score_raw} upvotes+comments).",
                        queries={"ebay_us": clean_query(title)},
                        reddit_query=clean_query(title)))
                else:
                    out.append(Candidate(
                        kind="niche", id=slug(title, "disc_n"), name=title[:70],
                        source=self.id, score=score,
                        niche_kind=niche_kind or infer_niche_kind(title),
                        reason=f"Hot in r/{sub} ({score_raw} upvotes+comments).",
                        reddit_query=clean_query(title), news_query=clean_query(title)))
        if not out and errors:
            return self._fail(self.last_error or "all subreddits failed")
        return out

    def check(self) -> tuple[bool, str]:
        if not (self.cfg.reddit_client_id and self.cfg.reddit_client_secret):
            return False, ("needs REDDIT_CLIENT_ID/SECRET (public JSON is blocked from server IPs); "
                           "create a free script app at reddit.com/prefs/apps")
        token = self._token()
        if not token:
            return False, (self._adapter.last_error if self._adapter else "") or "Reddit OAuth failed"
        sub = self.subs[0][0]
        try:                                        # light probe: one subreddit only
            posts = self._fetch_sub(sub, token)
        except Exception as e:  # noqa: BLE001
            return False, f"r/{sub}: {e}"
        return True, f"OAuth OK; r/{sub} → {len(posts)} hot posts; {len(self.subs)} subreddits watched"


# ----------------------------------------------------------------------- eBay

class EbayBrowseDiscovery(DiscoverySource):
    """Surfaces active, in-demand products by browsing seed categories on the
    eBay Browse API and keeping the ones with the deepest, tightest markets.
    Reuses an EbayAdapter's OAuth token/client so credentials live in one place."""

    id = "ebay_discovery"
    name = "eBay Browse (category sweep)"

    DEFAULT_SEEDS = [
        "pokemon booster box japanese", "new balance made in usa", "vintage film camera",
        "retro handheld console", "seiko chronograph vintage", "gundam model kit sealed",
    ]

    def __init__(self, cfg, client=None, ebay_adapter=None):
        super().__init__(cfg, client)
        self.seeds = getattr(cfg, "discovery_ebay_seeds", None) or self.DEFAULT_SEEDS
        self.ebay = ebay_adapter

    def _adapter(self):
        if self.ebay is None:
            from .market.adapters import EbayAdapter
            self.ebay = EbayAdapter(self.cfg, self.client)
        return self.ebay

    def discover(self) -> list[Candidate]:
        ad = self._adapter()
        if hasattr(ad, "configured") and not ad.configured():
            return self._fail("eBay keys not set — discovery source idle")
        out: list[Candidate] = []
        for seed in self.seeds:
            snap = ad.product_snapshot(seed)
            if not snap:
                self.last_error = ad.last_error
                continue
            # A tradeable market: several listings, multiple sellers, and a
            # meaningful gap between the cheapest and the median ask (headroom).
            stock = int(snap.get("stock", 0))
            median = float(snap.get("price", 0))
            floor = float(snap.get("min_price", median) or median)
            if stock < 3 or median <= 0:
                continue
            spread = (median - floor) / median if median else 0.0
            score = round(min(2.0, 0.4 + spread + min(stock, 200) / 400), 3)
            out.append(Candidate(
                kind="product", id=slug(seed, "disc_p"), name=seed.title(),
                source=self.id, score=score, category=infer_category(seed),
                reason=f"{stock} live eBay listings, ${floor:.0f}–${median:.0f} ask spread "
                       f"({spread * 100:.0f}% headroom).",
                queries={"ebay_us": seed}, reddit_query=seed, news_query=seed))
        if not out and self.last_error:
            return self._fail(self.last_error)
        return out

    def check(self) -> tuple[bool, str]:
        ad = self._adapter()
        if hasattr(ad, "configured") and not ad.configured():
            return False, "needs EBAY_CLIENT_ID/SECRET (free at developer.ebay.com)"
        ok, note = ad.check()                       # light probe: OAuth only, no sweep
        return (ok, f"{note}; {len(self.seeds)} seed categories" if ok else note)


# --------------------------------------------------------------------- engine

class DiscoveryEngine:
    """Runs the sources, ranks/dedupes/caps candidates, persists them, and
    merges the live winners into whatever the fleet observes."""

    def __init__(self, cfg, store, sources: list[DiscoverySource] | None = None,
                 classify_hook=None):
        self.cfg = cfg
        self.store = store
        self.sources = sources if sources is not None else build_sources(cfg, store)
        # classify_hook(candidates) -> candidates. Defaults to the AI brain
        # when an Anthropic key is configured; falls back to the keyword
        # heuristics baked into each source otherwise.
        self.ai = None
        if classify_hook is None:
            from .ai import AIClassifier
            self.ai = AIClassifier(cfg)
            if self.ai.configured():
                classify_hook = self.ai.classify
        self.classify_hook = classify_hook
        self.errors: list[str] = []

    def run(self) -> dict:
        """Collect from every source, keep the best, persist. Returns a report."""

        self.errors = []
        found: dict[str, Candidate] = {}
        per_source: dict[str, int] = {}
        for src in self.sources:
            try:
                cands = src.discover()
            except Exception as e:  # noqa: BLE001 - a source must never break a cycle
                self.errors.append(f"{src.id}: {e}")
                continue
            if src.last_error:
                self.errors.append(f"{src.id}: {src.last_error}")
            per_source[src.id] = len(cands)
            for c in cands:
                if c.id not in found or c.score > found[c.id].score:
                    found[c.id] = c
        candidates = list(found.values())
        raw_count = len(candidates)
        if self.classify_hook:
            try:
                candidates = self.classify_hook(candidates) or candidates
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"classify_hook: {e}")
            if self.ai is not None and self.ai.last_error:
                self.errors.append(f"ai: {self.ai.last_error}")
        promoted = 0
        for c in sorted(candidates, key=lambda x: x.score, reverse=True)[:self.cfg.discovery_scan_cap]:
            if self.store.upsert_discovered(asdict(c)):
                promoted += 1
        self.store.expire_discovered(self.cfg.discovery_ttl_days,
                                     self.cfg.discovery_max_active)
        report = {"sources": per_source, "found": len(candidates),
                  "promoted": promoted, "errors": self.errors}
        if self.ai is not None and self.ai.configured():
            report["ai"] = self.ai.last_summary or f"screened {raw_count} candidates"
        return report

    # ---- merge discovered winners into the observed watch entities ----------

    def _active(self) -> list[dict]:
        return self.store.list_discovered(active_only=True, limit=self.cfg.discovery_max_active)

    def extra_products(self, existing_ids: set[str]) -> list[wl.WatchProduct]:
        out = []
        for row in self._active():
            if row["kind"] != "product" or row["id"] in existing_ids:
                continue
            out.append(Candidate(**_candidate_fields(row)).to_watch_product())
        return out

    def extra_niches(self, existing_ids: set[str]) -> list[wl.WatchNiche]:
        out = []
        for row in self._active():
            if row["kind"] != "niche" or row["id"] in existing_ids:
                continue
            out.append(Candidate(**_candidate_fields(row)).to_watch_niche())
        return out

    def status(self) -> list[dict]:
        out = []
        for src in self.sources:
            out.append({"id": src.id, "name": src.name,
                        "ok": not src.last_error, "note": src.last_error or "ok"})
        if self.ai is not None:
            out.append({"id": "ai_brain", "name": "AI brain (Claude judgment)",
                        "ok": self.ai.configured() and not self.ai.last_error,
                        "note": (self.ai.last_error or self.ai.last_summary or "ok")
                        if self.ai.configured() else "no ANTHROPIC_API_KEY — keyword filtering"})
        return out

    def healthcheck(self) -> list[dict]:
        out = []
        for src in self.sources:
            ok, note = src.check()
            out.append({"id": src.id, "name": src.name, "ok": ok, "note": note})
        if self.ai is not None:
            ok, note = self.ai.check()
            out.append({"id": "ai_brain", "name": "AI brain (Claude judgment)", "ok": ok, "note": note})
        return out


_CANDIDATE_KEYS = set(Candidate.__dataclass_fields__)


def _candidate_fields(row: dict) -> dict:
    return {k: v for k, v in row.items() if k in _CANDIDATE_KEYS}


def build_sources(cfg, store, client: httpx.Client | None = None) -> list[DiscoverySource]:
    """Default source set. Reuses live adapters for shared credentials/tokens."""

    client = client or httpx.Client(timeout=cfg.http_timeout, follow_redirects=True,
                                    headers={"User-Agent": cfg.user_agent})
    reddit_ad = ebay_ad = None
    try:
        from .market.adapters import EbayAdapter, RedditAdapter
        reddit_ad = RedditAdapter(cfg, client)
        ebay_ad = EbayAdapter(cfg, client)
    except Exception:  # pragma: no cover
        pass
    return [
        GoogleTrendsDiscovery(cfg, client),
        RedditDiscovery(cfg, client, reddit_adapter=reddit_ad),
        EbayBrowseDiscovery(cfg, client, ebay_adapter=ebay_ad),
    ]
