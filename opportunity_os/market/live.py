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

import hashlib
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
        self.scanned_this_tick: set[str] = set()
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

    # ---- tiered scan scheduler ----------------------------------------------
    # Coverage scales by rescanning what MOVES, not everything: hot entities
    # (your watchlist, live opportunities, fresh anomalies) every cycle; the
    # best discoveries every warm interval; the long tail on slow rotation,
    # offset per entity so the per-cycle call load stays flat.

    @staticmethod
    def _offset(pid: str) -> int:
        return int(hashlib.sha1(pid.encode()).hexdigest()[:6], 16)

    def _build_scan_tiers(self, t: int) -> dict[str, int]:
        """product_id -> rescan interval (in ticks) for this pass.

        Hot entities (watchlist, live opportunities, fresh anomalies) rescan
        every cycle. The rest — the discovered tail — is ordered by the EV
        allocator: a UCB bandit over each entity's verified research yield per
        scan, so the warm slots go to whatever has actually been PRODUCING
        opportunities, with an exploration bonus that still probes the unknown.
        Falls back to raw discovery score until yield history accumulates."""

        hot = {p.id for p in self.watch.products}
        try:
            hot |= {o["entity_id"] for o in self.db.active_opportunities()}
            hot |= {a["entity_id"] for a in self.db.recent_anomalies(100)
                    if a["tick"] >= t - self.cfg.scan_hot_anomaly_window}
        except Exception:  # noqa: BLE001
            pass
        tiers: dict[str, int] = {pid: 1 for pid in hot}
        discovered = self.db.list_discovered(active_only=True, limit=500)

        if self.cfg.ev_allocator_enabled:
            from .. import research
            yrows = {r["entity_id"]: r for r in self.db.yield_rows()}
            total = sum(r.get("scans", 0) for r in yrows.values())
            rows = []
            for d in discovered:
                y = yrows.get(d["id"], {})
                rows.append({"entity_id": d["id"], "scans": y.get("scans", 0),
                             "anomalies": y.get("anomalies", 0),
                             "candidates": y.get("candidates", 0),
                             "published": y.get("published", 0),
                             "prior": 0.5 * float(d.get("score", 0))})
            order = [eid for eid, _ in research.ucb_rank(rows, total)]
        else:
            order = [d["id"] for d in sorted(discovered, key=lambda r: r.get("score", 0),
                                             reverse=True)]

        for i, did in enumerate(order):
            if did in tiers:
                continue
            tiers[did] = (self.cfg.scan_warm_interval if i < self.cfg.scan_warm_slots
                          else self.cfg.scan_cold_interval)
        return tiers

    def _due(self, pid: str, t: int, tiers: dict[str, int]) -> bool:
        interval = tiers.get(pid, self.cfg.scan_cold_interval)
        return interval <= 1 or (t + self._offset(pid)) % interval == 0

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        """One observation pass: fetch, estimate, persist."""

        self.watch = wl.load(self.cfg.watchlist_path)      # pick up edits live
        self.tick_no += 1
        t = self.tick_no
        self.errors = []
        self.scanned_this_tick: set[str] = set()           # entities that cost an API call

        # Discovery sweeps are heavier (many outbound calls) so they run every
        # Nth cycle. New candidates join the observed set immediately below.
        # (n<=1 means every cycle — `t % 1 == 1` is never true, so gate on it.)
        every = max(1, self.cfg.discover_every_n_ticks)
        if self.discovery and (every == 1 or t % every == 1):
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

        tiers = self._build_scan_tiers(t)
        for p in self._all_products():
            due = self._due(p.id, t, tiers)
            fetched: set[str] = set()
            for venue, query in sorted(p.queries.items()):
                if not due:
                    continue                       # not this entity's turn — budget goes to movers
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
                sample = raw.get("sample") or []
                if sample:
                    # The whole page of asks, kept for the research layer:
                    # dislocation detection, seller concentration, title mining.
                    self.db.save_listing_sample(p.id, venue, t, sample)
                    if self.discovery and self.cfg.graph_fanout_enabled:
                        self._fanout(p, query, sample, t)
                fetched.add(venue)
                self.scanned_this_tick.add(p.id)
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
            if due and reddit and p.reddit_query:
                n = reddit.mentions_24h(p.reddit_query)
                if n is not None:
                    self.db.add_live_mention(p.id, "reddit", t, n)
                else:
                    self._err(f"{p.id}/reddit: {reddit.last_error}")
            if due and news and p.news_query:
                headlines += self._fresh_headlines(news, p.id, p.news_query)

        serper = self.adapters.get("serper")
        serper_every = max(1, getattr(serper, "every_n_ticks", 12)) if serper else 12
        serper_pass = (serper is not None and getattr(serper, "configured", lambda: False)()
                       and (serper_every == 1 or t % serper_every == 1))
        serper_budget = int(getattr(self.cfg, "serper_niche_budget", 12))
        for n in self._all_niches():
            count = None
            if reddit and n.reddit_query:
                count = reddit.mentions_24h(n.reddit_query)
                if count is not None:
                    self.db.add_live_mention(n.id, "reddit", t, count)
                else:
                    self._err(f"{n.id}/reddit: {reddit.last_error}")
            # Reddit down or key pending? Serper measures the same demand via
            # Google — Thai niches are measured with their Thai query against
            # google.co.th, so a Bangkok service gap gets a REAL demand series.
            if count is None and serper_pass and serper_budget > 0:
                if n.serper_query_th:
                    obs = serper.demand_observation(n.serper_query_th, gl="th", hl="th")
                    serper_budget -= 1
                    if obs is not None:
                        self.db.add_live_mention(n.id, "serper", t, obs["results"])
                        self.db.add_search_obs(n.id, "serper", "demand", t,
                                               n.serper_query_th, obs, geo="th", lang="th")
                    else:
                        self._err(f"{n.id}/serper: {serper.last_error}")
                elif n.reddit_query:
                    wk = serper.reddit_posts_7d(n.reddit_query)
                    serper_budget -= 1
                    if wk is not None:
                        self.db.add_live_mention(n.id, "serper", t, wk)
                    else:
                        self._err(f"{n.id}/serper: {serper.last_error}")
            # Supply side: OBSERVE who already serves this niche instead of
            # trusting a constant. Slow rotation — one lookup per niche until
            # covered, refreshed only when stale (>3 days) — so it costs a few
            # credits, not a flood.
            if serper_pass and serper_budget > 0:
                sq = n.serper_query_th or n.reddit_query
                if sq:
                    prev = self.db.latest_search_obs(n.id, "supply")
                    stale = prev is None or (time.time() - prev["ts"]) > 3 * 86400
                    if stale:
                        gl, hl = ("th", "th") if n.serper_query_th else ("us", "en")
                        sup = serper.supply_observation(sq, gl=gl, hl=hl)
                        serper_budget -= 1
                        if sup is not None:
                            self.db.add_search_obs(n.id, "serper", "supply", t,
                                                   sq, sup, geo=gl, lang=hl)
                        else:
                            self._err(f"{n.id}/serper-supply: {serper.last_error}")
            self.db.add_live_niche(n.id, t, self._niche_metrics(n, count))
            if news and n.news_query:
                headlines += self._fresh_headlines(news, n.id, n.news_query)

        if headlines:
            self.db.add_live_headlines(headlines, t)
        self.db.meta_set("live_tick", t)

    def _err(self, msg: str) -> None:
        self.errors.append(msg)

    def _fanout(self, p: wl.WatchProduct, query: str, sample: list[dict], t: int) -> None:
        """Graph fan-out: mine related-product phrases from the page's titles
        and register them as discovery candidates + graph edges. This is how
        one API response spawns many future investigations for free — the
        sellers already wrote the product graph into their listing titles."""

        from .. import research
        from ..discovery import Candidate, slug, clean_query, infer_category
        related = research.mine_related(sample, query)
        if not related:
            return
        edges, cands = [], []
        for g in related:
            phrase = g["phrase"]
            cid = slug(phrase, "disc_p")
            edges.append({"dst": cid, "kind": "co_listed", "weight": g["support"]})
            # A model-number variant is a concrete tradeable product; a plain
            # phrase is weaker. Score reflects support and specificity.
            score = round(min(1.6, 0.3 + g["support"] / 8 + (0.4 if g["model_like"] else 0)), 3)
            cands.append(Candidate(
                kind="product", id=cid, name=phrase.title(), source="graph_fanout",
                score=score, category=infer_category(phrase),
                reason=f"Co-listed with '{p.name}' across {g['support']} titles "
                       f"({g['sellers']} sellers) — adjacent market found for free.",
                queries={"ebay_us": clean_query(phrase)},
                reddit_query=clean_query(phrase)))
        self.db.add_graph_edges(p.id, edges, t)
        for c in cands:
            from dataclasses import asdict
            self.db.upsert_discovered(asdict(c))

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
        """Honest niche metrics: every number is observed, user-supplied, or
        explicitly unknown — never an invented constant.

        * hand-typed watchlist niches (base_volume > 0): YOUR baselines, scaled
          by the observed momentum — provenance 'user_supplied';
        * discovered niches: demand comes ONLY from measured series (Reddit
          mentions or Serper results), supply ONLY from a stored Serper supply
          observation. Missing measurement → 0 + provenance 'unknown', and the
          verification council treats unknown supply as research-required."""

        series = self.db.live_mention_series(n.id, "reddit", 30)
        demand_src_series = "reddit"
        if len(series) < 6:                        # Reddit dark → Serper series drives momentum
            series = self.db.live_mention_series(n.id, "serper", 30)
            demand_src_series = "serper"
        momentum, growth = 1.0, 0.0
        if len(series) >= 6:
            recent = fmean(series[-3:])
            base = max(fmean(series[:-3]), 0.5)
            momentum = max(0.5, min(3.0, recent / base))
            growth = max(-50.0, min(80.0, (momentum - 1.0) * 100))

        user_supplied = n.base_volume > 0
        measured_today = mentions_today if mentions_today is not None else (
            series[-1] if series else None)
        if measured_today is not None:
            posts = float(measured_today) * 30
        else:
            posts = float(n.demand_posts) if user_supplied else 0.0

        if user_supplied:
            volume, demand_src = round(n.base_volume * momentum, 1), "user_supplied"
        elif measured_today is not None:
            volume, demand_src = round(posts, 1), "observed"
        else:
            volume, demand_src = 0.0, "unknown"

        sup_obs = self.db.latest_search_obs(n.id, "supply")
        observed_n = int(sup_obs["payload"].get("provider_count", 0)) if sup_obs else None
        # A 0-provider scan is meaningful for a DISCOVERED niche (demand with no
        # visible supply = the gap itself) but too weak to zero out a hand-typed
        # watchlist estimate — those queries are demand-phrased and can miss
        # providers entirely.
        if sup_obs and (observed_n > 0 or not user_supplied):
            supply_n = observed_n
            supply_src = "observed"
            supply_domains = [p.get("domain", "") for p in
                              (sup_obs["payload"].get("providers") or [])[:5]]
        elif user_supplied:
            supply_n, supply_src, supply_domains = n.solution_count, "user_supplied", []
        else:
            supply_n, supply_src, supply_domains = 0, "unknown", []

        return {
            "volume": volume,
            "growth_pct": round(growth, 1),
            "solution_count": supply_n if supply_src != "user_supplied" else n.solution_count,
            "demand_posts": round(posts, 1),
            "providers": supply_n if supply_src != "user_supplied" else n.providers,
            "observed": {"demand": demand_src, "supply": supply_src,
                         "demand_series": demand_src_series},
            "supply_domains": supply_domains,
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

    def listing_sample(self, product_id: str, venue: str) -> list[dict]:
        sample, _ = self.db.get_listing_sample(product_id, venue)
        return sample

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
        out = ["reddit"]
        serper = self.adapters.get("serper")
        if serper is not None and getattr(serper, "configured", lambda: False)():
            out.append("serper")               # council counts it as social corroboration
        return out

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
