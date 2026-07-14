"""The live watchlist — what the fleet tracks in live mode.

Live mode can't scan "everything on eBay"; you curate targets and the agents
watch them 24/7. Two kinds of entries:

* **products** — a sell-side marketplace query per venue (observed by real
  adapters) plus optional `manual_listings`: buy-side quotes you found
  yourself (a Buyee search, a Shopee price, a Facebook group offer). Manual
  quotes are honest ground truth you provide; everything downstream — routes,
  fees, duty, verification — runs on top of them.
* **niches** — demand queries (Reddit/news) plus your own supply-side counts
  (how many credible solutions/providers exist — update as you learn).

The file is re-read at the start of every cycle, so edits apply without a
restart. Copy `watchlist.example.json` to `watchlist.json` and make it yours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import economics

NICHE_KINDS = ("digital", "info", "local", "b2b")


@dataclass
class WatchProduct:
    id: str
    name: str
    category: str
    weight_kg: float
    queries: dict[str, str] = field(default_factory=dict)           # venue -> search query
    manual_listings: dict[str, dict] = field(default_factory=dict)  # venue -> quote
    bootstrap_sold_7d: dict[str, int] = field(default_factory=dict)
    reddit_query: str | None = None
    news_query: str | None = None

    def venues(self) -> list[str]:
        return sorted(set(self.queries) | set(self.manual_listings))


@dataclass
class WatchNiche:
    id: str
    name: str
    kind: str
    geo: str
    price_point_usd: float
    reddit_query: str | None = None
    news_query: str | None = None
    base_volume: float = 1000.0
    solution_count: int = 3
    providers: int = 3
    demand_posts: float = 50.0


@dataclass
class Operator:
    """Who is executing — so the AI never recommends the impossible."""

    budget_usd: float | None = None
    avoid_types: list[str] = field(default_factory=list)        # e.g. ["local_service"]
    prefer_categories: list[str] = field(default_factory=list)
    registered_venues: list[str] = field(default_factory=list)  # platforms you already sell on
    has: list[str] = field(default_factory=list)                # e.g. ["payoneer", "buyee account"]


@dataclass
class Watchlist:
    products: list[WatchProduct]
    niches: list[WatchNiche]
    operator: Operator = field(default_factory=Operator)
    path: Path | None = None

    def product(self, pid: str) -> WatchProduct | None:
        return next((p for p in self.products if p.id == pid), None)

    def niche(self, nid: str) -> WatchNiche | None:
        return next((n for n in self.niches if n.id == nid), None)


def validate(w: Watchlist) -> list[str]:
    """Human-readable problems; empty list == valid."""

    problems: list[str] = []
    seen: set[str] = set()
    for p in w.products:
        if p.id in seen:
            problems.append(f"duplicate id '{p.id}'")
        seen.add(p.id)
        if not p.queries and not p.manual_listings:
            problems.append(f"product '{p.id}': needs at least one query or manual listing")
        for v in p.venues():
            if v not in economics.VENUES:
                problems.append(f"product '{p.id}': unknown venue '{v}' "
                                f"(known: {', '.join(sorted(economics.VENUES))})")
        for v, m in p.manual_listings.items():
            if "price_thb" not in m and "price_usd" not in m:
                problems.append(f"product '{p.id}' manual '{v}': needs price_thb or price_usd")
        if p.weight_kg <= 0:
            problems.append(f"product '{p.id}': weight_kg must be > 0")
    for n in w.niches:
        if n.id in seen:
            problems.append(f"duplicate id '{n.id}'")
        seen.add(n.id)
        if n.kind not in NICHE_KINDS:
            problems.append(f"niche '{n.id}': kind must be one of {NICHE_KINDS}")
    return problems


def load(path: Path) -> Watchlist:
    if not path.exists():
        raise FileNotFoundError(
            f"watchlist not found at {path} — copy watchlist.example.json to {path.name} and edit it "
            f"(or point OOS_WATCHLIST at your file).")
    raw = json.loads(path.read_text(encoding="utf-8"))
    products = [WatchProduct(
        id=p["id"], name=p["name"], category=p.get("category", "collectibles"),
        weight_kg=float(p.get("weight_kg", 0.5)),
        queries=p.get("queries", {}) or {},
        manual_listings=p.get("manual_listings", {}) or {},
        bootstrap_sold_7d=p.get("bootstrap_sold_7d", {}) or {},
        reddit_query=p.get("reddit_query"), news_query=p.get("news_query"),
    ) for p in raw.get("products", [])]
    niches = [WatchNiche(
        id=n["id"], name=n["name"], kind=n.get("kind", "info"), geo=n.get("geo", "global"),
        price_point_usd=float(n.get("price_point_usd", 15)),
        reddit_query=n.get("reddit_query"), news_query=n.get("news_query"),
        base_volume=float(n.get("base_volume", 1000)),
        solution_count=int(n.get("solution_count", 3)),
        providers=int(n.get("providers", 3)),
        demand_posts=float(n.get("demand_posts", 50)),
    ) for n in raw.get("niches", [])]
    op_raw = raw.get("operator", {}) or {}
    operator = Operator(
        budget_usd=float(op_raw["budget_usd"]) if op_raw.get("budget_usd") else None,
        avoid_types=list(op_raw.get("avoid_types", [])),
        prefer_categories=list(op_raw.get("prefer_categories", [])),
        registered_venues=list(op_raw.get("registered_venues", [])),
        has=list(op_raw.get("has", [])),
    )
    w = Watchlist(products=products, niches=niches, operator=operator, path=path)
    problems = validate(w)
    if problems:
        raise ValueError("watchlist has problems:\n  - " + "\n  - ".join(problems))
    return w
