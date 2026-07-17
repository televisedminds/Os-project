"""LiveMarket — the production data source.

Implements the same `DataSource` protocol the simulator does, but over real
connectors (see `adapters.py`) and a user-curated watchlist. Observations are
persisted in SQLite so rolling baselines, z-scores and sell-through estimates
survive restarts and accumulate across days.

What is observed vs estimated vs user-supplied — kept honest on purpose:

* observed   — sell-side prices/listing counts/seller counts (eBay Browse),
               Reddit post volume, news headlines, FX rates
* estimated  — sell-through velocity (`sold_7d`), inferred from listings
               disappearing between snapshots, EMA-smoothed, bootstrapped
               from the watchlist until enough history exists
* you supply — buy-side quotes for venues without APIs (your Buyee/Shopee/
               Facebook sourcing prices) and niche supply counts; the pipeline
               treats them as ground truth you are accountable for

Every adapter failure degrades that source for the cycle instead of crashing;
`status()` reports what's healthy so the dashboard and `live-check` can show it.
"""

from __future__ import annotations

import time
from statistics import fmean

from .. import economics
from ..config import Config
from ..db import Store
from . import watchlist as wl
from .adapters import BaseAdapter, build_adapters

HISTORY = 60
VELOCITY_EMA = 0.7          # weight on previous estimate
MAX_SALES_PER_DAY = 60      # sanity cap on disappearance-based estimates


class LiveMarket:
    """Real-data implementation of the `DataSource` protocol."""

    def __init__(self, config: Config, store: Store,
                 adapters: dict[str, BaseAdapter] | None = None,
                 discovery=None):
        self.cfg = config
        self.db = store
        self.adapters = adapters if adapters is not None else build_adapters(config)
        self.watch = wl.load(config.watchlist_path)
        self.tick_no = int(store.meta_get("live_tick", 0))
        self.errors: list[str] = []
        self.discovery = discovery
        if self.discovery is None and getattr(config, "discovery_enabled", False):
            from ..discovery import DiscoveryEngine
            self.discovery = DiscoveryEngine(config, store)
        self.discovery_report: dict = {}

    # ---- observed set = curated watchlist + live discovery winners ----------

    def _all_products(self) -> list[wl.WatchProduct]:
        base = list(self.watch.products)
        if self.discovery:
            base += self.discovery.extra_products({p.id for p in base})
        return base

    def _all_niches(self) -> list[wl.WatchNiche]:
        base = list(self.watch.niches)
        if self.discovery:
            base += self.discovery.extra_niches({n.id for n in base})
        return base

    def _product(self, pid: str) -> wl.WatchProduct | None:
        return next((p for p in self._all_products() if p.id == pid), None)

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        """One observation pass: fetch, estimate, persist."""

        self.watch = wl.load(self.cfg.watchlist_path)      # pick up edits live
        self.tick_no += 1
        t = self.tick_no
        self.errors = []

        # Discovery sweeps are heavier (many outbound calls) so they run every
        # Nth cycle. New candidates join the observed set immediately below.
        if self.discovery and (t % max(1, self.cfg.discover_every_n_ticks) == 1):
            self.discovery_report = self.discovery.run()
            for e in self.discovery_report.get("errors", []):
                self._err(f"discovery/{e}")

        fx = self.adapters.get("fx")
        if fx and hasattr(fx, "rates"):
            rates = fx.rates()
            if rates:
                economics.set_fx(usd_thb=rates["THB"], usd_jpy=rates["JPY"])
                self.db.meta_set("live_fx", {"USD_THB": rates["THB"], "USD_JPY": rates["JPY"],
                                             "ts": time.time()})
            else:
                self._err(f"fx: {fx.last_error}")

        reddit = self.adapters.get("reddit")
        news = self.adapters.get("news")
        headlines: list[dict] = []

        for p in self._all_products():
            fetched: set[str] = set()
            for venue, query in sorted(p.queries.items()):
                ad = self.adapters.get(venue)
                if not ad or not hasattr(ad, "product_snapshot"):
                    self._err(f"{p.id}/{venue}: no adapter for this venue yet")
                    continue
                every = getattr(ad, "every_n_ticks", 1)
                if every > 1 and (t % every) != 1:
                    continue                       # scraped venues: save paid credits
                raw = ad.product_snapshot(query)
                if raw is None:
                    self._err(f"{p.id}/{venue}: {ad.last_error}")
                    continue
                # Prefer a real sold-counter (Shopee publishes one) over the
                # disappearance-based estimate.
                sold = (raw["sold_7d_hint"] if raw.get("sold_7d_hint") is not None
                        else self._estimate_sold_7d(p, venue, raw))
                self.db.add_live_snapshot(p.id, venue, t,
                                          {"price": raw["price"], "stock": raw["stock"],
                                           "sellers": raw["sellers"], "sold_7d": sold},
                                          extra={"item_ids": raw.get("item_ids", []),
                                                 "min_price": raw.get("min_price"),
                                                 "query": query})
                fetched.add(venue)
            for venue, m in sorted(p.manual_listings.items()):
                if venue in fetched:
                    continue                       # live scrape beats the manual fallback
                price_usd = m.get("price_usd") or round(m["price_thb"] / economics.USD_THB, 2)
                self.db.add_live_snapshot(p.id, venue, t,
                                          {"price": round(float(price_usd), 2),
                                           "stock": int(m.get("stock", 1)),
                                           "sellers": int(m.get("sellers", 1)),
                                           "sold_7d": int(m.get("sold_7d", 0))},
                                          extra={"manual": True, "note": m.get("note", "")})
            if reddit and p.reddit_query:
                n = reddit.mentions_24h(p.reddit_query)
                if n is not None:
                    self.db.add_live_mention(p.id, "reddit", t, n)
                else:
                    self._err(f"{p.id}/reddit: {reddit.last_error}")
            if news and p.news_query:
                headlines += self._fresh_headlines(news, p.id, p.news_query)

        serper = self.adapters.get("serper")
        serper_every = max(1, getattr(serper, "every_n_ticks", 12)) if serper else 12
        serper_pass = (serper is not None and getattr(serper, "configured", lambda: False)()
                       and (serper_every == 1 or t % serper_every == 1))
        serper_budget = 12                         # hard cap per pass — protects free credits
        for n in self._all_niches():
            count = None
            if reddit and n.reddit_query:
                count = reddit.mentions_24h(n.reddit_query)
                if count is not None:
                    self.db.add_live_mention(n.id, "reddit", t, count)
                else:
                    self._err(f"{n.id}/reddit: {reddit.last_error}")
            # Reddit down or key pending? Serper measures the same demand via
            # Google (site:reddit.com, past week) — coarser, but real.
            if count is None and serper_pass and serper_budget > 0 and n.reddit_query:
                wk = serper.reddit_posts_7d(n.reddit_query)
                serper_budget -= 1
                if wk is not None:
                    self.db.add_live_mention(n.id, "serper", t, wk)
                else:
                    self._err(f"{n.id}/serper: {serper.last_error}")
            self.db.add_live_niche(n.id, t, self._niche_metrics(n, count))
            if news and n.news_query:
                headlines += self._fresh_headlines(news, n.id, n.news_query)

        if headlines:
            self.db.add_live_headlines(headlines, t)
        self.db.meta_set("live_tick", t)

    def _err(self, msg: str) -> None:
        self.errors.append(msg)

    def _fresh_headlines(self, news, entity_id: str, query: str) -> list[dict]:
        got = news.headlines(query, 4)
        if got is None:
            self._err(f"{entity_id}/news: {news.last_error}")
            return []
        seen = {h["text"] for h in self.db.live_headlines_for(entity_id, 30)}
        return [{"entity_id": entity_id, "text": h, "etype": "news"} for h in got if h not in seen]

    def _estimate_sold_7d(self, p: wl.WatchProduct, venue: str, raw: dict) -> int:
        """Velocity from listing disappearance between snapshots (EMA-smoothed)."""

        hist = self.db.live_snapshot_series(p.id, venue, 8)
        prev = hist[-1] if hist else None
        est = float(prev["sold_7d"]) if prev else 0.0
        if prev and prev["extra"].get("item_ids"):
            gone = len(set(prev["extra"]["item_ids"]) - set(raw.get("item_ids", [])))
            interval_days = max((time.time() - prev["ts"]) / 86400, 1 / 48)
            rate = min(gone / interval_days, MAX_SALES_PER_DAY)
            est = VELOCITY_EMA * est + (1 - VELOCITY_EMA) * rate * 7
        if len(hist) < 6:
            est = max(est, float(p.bootstrap_sold_7d.get(venue, 0)))
        return round(est)

    def _niche_metrics(self, n: wl.WatchNiche, mentions_today: int | None) -> dict:
        series = self.db.live_mention_series(n.id, "reddit", 30)
        if len(series) < 6:                        # Reddit dark → Serper series drives momentum
            series = self.db.live_mention_series(n.id, "serper", 30)
        momentum, growth = 1.0, 0.0
        if len(series) >= 6:
            recent = fmean(series[-3:])
            base = max(fmean(series[:-3]), 0.5)
            momentum = max(0.5, min(3.0, recent / base))
            growth = max(-50.0, min(80.0, (momentum - 1.0) * 100))
        posts = (mentions_today * 30) if mentions_today is not None else n.demand_posts
        return {
            "volume": round(n.base_volume * momentum, 1),
            "growth_pct": round(growth, 1),
            "solution_count": n.solution_count,
            "demand_posts": round(float(posts), 1),
            "providers": n.providers,
        }

    # ------------------------------------------------------- DataSource impl

    def product_ids(self) -> list[str]:
        return sorted(p.id for p in self._all_products())

    def venue_ids(self) -> list[str]:
        vids: set[str] = set()
        for p in self._all_products():
            vids.update(p.venues())
        return sorted(vids)

    def product_public(self, product_id: str) -> dict:
        p = self._product(product_id)
        if not p:
            return {"id": product_id, "name": product_id, "category": "collectibles",
                    "weight_kg": 0.5, "venues": []}
        return {"id": p.id, "name": p.name, "category": p.category,
                "weight_kg": p.weight_kg, "venues": p.venues()}

    def _snap_dict(self, row: dict) -> dict:
        return {"price": row["price"], "stock": row["stock"],
                "sellers": row["sellers"], "sold_7d": row["sold_7d"]}

    def listing(self, product_id: str, venue: str) -> dict | None:
        rows = self.db.live_snapshot_series(product_id, venue, 1)
        return self._snap_dict(rows[-1]) if rows else None

    def listings(self, venue: str) -> list[tuple[dict, dict]]:
        out = []
        for pid in self.product_ids():
            if venue in self.product_public(pid)["venues"]:
                snap = self.listing(pid, venue)
                if snap:
                    out.append((self.product_public(pid), snap))
        return out

    def product_history(self, product_id: str, venue: str) -> list[dict]:
        return [self._snap_dict(r) for r in self.db.live_snapshot_series(product_id, venue, HISTORY)]

    def mentions(self, entity_id: str, source: str) -> list[int]:
        return self.db.live_mention_series(entity_id, source, HISTORY)

    def social_sources(self) -> list[str]:
        return ["reddit"]

    def niches(self) -> list[dict]:
        out = []
        for n in sorted(self._all_niches(), key=lambda x: x.id):
            hist = self.db.live_niche_series(n.id, 1)
            metrics = hist[-1] if hist else self._niche_metrics(n, None)
            out.append({"id": n.id, "name": n.name, "kind": n.kind, "geo": n.geo,
                        "price_point_usd": n.price_point_usd, "metrics": metrics})
        return out

    def niche_history(self, niche_id: str) -> list[dict]:
        return self.db.live_niche_series(niche_id, HISTORY)

    def headlines(self, since_tick: int) -> list[dict]:
        return self.db.live_headlines_since(since_tick)

    def event_log(self, entity_id: str) -> list[dict]:
        return [{"tick": h["tick"], "etype": h.get("etype", "news"),
                 "narrative": h["text"], "headline": h["text"], "venue": None}
                for h in self.db.live_headlines_for(entity_id, 10)]

    # ---------------------------------------------------------------- status

    def status(self) -> list[dict]:
        """Last-known adapter state (no network calls — safe for /api/health)."""

        out = []
        for key, ad in sorted(self.adapters.items()):
            configured = ad.configured() if hasattr(ad, "configured") else True
            out.append({"id": key, "name": ad.name,
                        "configured": configured,
                        "ok": configured and not ad.last_error,
                        "note": ad.last_error or ("ok" if configured else "credentials not set")})
        return out

    def healthcheck(self) -> list[dict]:
        """Active adapter checks (network) — used by `run.py live-check`."""

        out = []
        for key, ad in sorted(self.adapters.items()):
            ok, note = ad.check()
            out.append({"id": key, "name": ad.name, "ok": ok, "note": note})
        return out
