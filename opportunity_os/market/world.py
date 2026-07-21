"""A deterministic simulated market for demo mode.

The world is not random noise: events (a US sellout, a TikTok spike, a
distributor restock, competitor pile-ins) are scheduled with real narratives,
and their effects propagate into prices, stock, seller counts, social buzz and
search volume. The agent pipeline has to *discover* these causes from
observable data — the simulator never hands an answer to the pipeline.

One tick == one simulated market day == one research cycle.
Seeded RNG + sorted iteration ⇒ fully reproducible replays.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class Listing:
    price: float
    stock: int
    sellers: int
    velocity: float                     # units/day (EMA)
    initial_stock: int = 0
    no_restock_until: int = 0

    def snapshot(self) -> dict:
        return {"price": round(self.price, 2), "stock": self.stock, "sellers": self.sellers,
                "sold_7d": round(self.velocity * 7)}


@dataclass
class Product:
    id: str
    name: str
    category: str
    weight_kg: float
    venues: dict[str, Listing]
    buzz: dict[str, float]                                  # base mentions/day per source
    history: dict[str, list[dict]] = field(default_factory=dict)
    mentions_history: dict[str, list[int]] = field(default_factory=dict)
    temp_effects: list[dict] = field(default_factory=list)  # {until, demand_mult, buzz_mult}
    events: list[dict] = field(default_factory=list)

    def demand_mult(self, tick: int) -> float:
        m = 1.0
        for e in self.temp_effects:
            if e["until"] >= tick:
                m *= e.get("demand_mult", 1.0)
        return m

    def buzz_mult(self, tick: int) -> float:
        m = 1.0
        for e in self.temp_effects:
            if e["until"] >= tick:
                m *= e.get("buzz_mult", 1.0)
        return m


@dataclass
class Niche:
    id: str
    name: str
    kind: str                       # digital | info | local | b2b
    geo: str
    price_point_usd: float
    metrics: dict                   # volume, growth_pct, solution_count, demand_posts, providers
    buzz: dict[str, float]
    history: list[dict] = field(default_factory=list)
    mentions_history: dict[str, list[int]] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)


@dataclass
class MarketEvent:
    tick: int
    etype: str
    target: str
    venue: str | None
    params: dict
    narrative: str
    headline: str


SOCIAL_SOURCES = ["reddit", "tiktok", "x"]
HISTORY_CAP = 90


def _p(price: float, stock: int, sellers: int, sold_7d: int) -> Listing:
    return Listing(price=price, stock=stock, sellers=sellers, velocity=sold_7d / 7.0, initial_stock=stock)


class SimulatedMarket:
    """Implements the `DataSource` protocol over a synthetic-but-causal world."""

    def __init__(self, seed: int = 1151, warmup: int = 12):
        self.rng = random.Random(seed)
        self.tick_no = 0
        self.warmup = warmup
        self.products: dict[str, Product] = {}
        self._niches: dict[str, Niche] = {}
        self.scheduled: list[MarketEvent] = []
        self._headlines: list[dict] = []
        self._build_catalog(event_base=warmup)
        for _ in range(warmup):
            self.tick()

    # ------------------------------------------------------------------ setup

    def _build_catalog(self, event_base: int) -> None:
        P, N = self.products, self._niches
        b = event_base  # events land after warmup so baselines are clean

        def add(p: Product):
            P[p.id] = p

        add(Product("pokemon_card_214", "Pokémon SV 'Moonbreon' #214 alt-art (JP print)", "trading_cards", 0.05,
                    {"yahoo_auctions_jp": _p(18.40, 183, 24, 21), "ebay_us": _p(26.50, 40, 12, 38)},
                    {"reddit": 9, "tiktok": 6, "x": 5}))
        add(Product("pokemon_151_bb", "Pokémon 151 Booster Box (JP, sealed)", "trading_cards", 0.90,
                    {"yahoo_auctions_jp": _p(38.00, 92, 31, 15), "mercari_jp": _p(39.50, 60, 18, 12),
                     "ebay_us": _p(78.00, 55, 20, 26)},
                    {"reddit": 12, "tiktok": 8, "x": 6}))
        add(Product("lego_10281", "LEGO 10281 Bonsai Tree (retired)", "lego", 1.30,
                    {"shopee_th": _p(48.00, 26, 7, 6), "amazon_us": _p(79.00, 18, 9, 14), "ebay_us": _p(72.00, 30, 11, 12)},
                    {"reddit": 4, "tiktok": 2, "x": 1}))
        add(Product("lego_75290", "LEGO 75290 Mos Eisley Cantina (retired)", "lego", 4.20,
                    {"shopee_th": _p(285.00, 6, 3, 1), "ebay_us": _p(415.00, 12, 8, 3)},
                    {"reddit": 3, "tiktok": 1, "x": 1}))
        add(Product("seiko_6139", "Seiko 6139-6002 'Pogue' chronograph (vintage)", "watches", 0.35,
                    {"yahoo_auctions_jp": _p(310.00, 14, 9, 3), "ebay_us": _p(520.00, 9, 7, 4)},
                    {"reddit": 5, "tiktok": 1, "x": 2}))
        add(Product("gba_sp_101", "Game Boy Advance SP AGS-101 (boxed)", "gaming", 0.45,
                    {"yahoo_auctions_jp": _p(72.00, 48, 22, 9), "mercari_jp": _p(78.00, 35, 15, 8),
                     "ebay_us": _p(129.00, 26, 14, 18)},
                    {"reddit": 7, "tiktok": 4, "x": 2}))
        add(Product("contax_t2", "Contax T2 35mm compact (film camera)", "cameras", 0.55,
                    {"yahoo_auctions_jp": _p(410.00, 11, 8, 2), "ebay_us": _p(585.00, 8, 9, 3)},
                    {"reddit": 4, "tiktok": 5, "x": 2}))
        add(Product("sony_wm1a", "Sony NW-WM1A Walkman DAP (used, mint)", "audio", 0.50,
                    {"yahoo_auctions_jp": _p(240.00, 16, 10, 3), "ebay_us": _p(355.00, 10, 8, 4)},
                    {"reddit": 3, "tiktok": 1, "x": 1}))
        add(Product("pilot_823", "Pilot Custom 823 fountain pen (JP)", "stationery", 0.15,
                    {"yahoo_auctions_jp": _p(155.00, 30, 12, 5), "ebay_us": _p(255.00, 15, 9, 6)},
                    {"reddit": 3, "tiktok": 1, "x": 1}))
        add(Product("nb_2002r_th", "New Balance 2002R 'Bangkok' exclusive colorway", "sneakers", 1.40,
                    {"shopee_th": _p(98.00, 40, 14, 9), "ebay_us": _p(185.00, 12, 10, 7)},
                    {"reddit": 5, "tiktok": 7, "x": 3}))
        add(Product("durian_chips", "Premium durian chips 500g (Thai brand)", "food", 0.55,
                    {"shopee_th": _p(7.50, 400, 25, 60), "ebay_us": _p(24.00, 30, 6, 22), "amazon_us": _p(26.00, 22, 5, 30)},
                    {"reddit": 3, "tiktok": 10, "x": 4}))
        add(Product("artisan_keycaps", "Artisan keycap set — Thai maker (36 caps)", "handmade", 0.25,
                    {"facebook_mp_th": _p(55.00, 20, 4, 4), "etsy": _p(128.00, 8, 5, 5)},
                    {"reddit": 6, "tiktok": 3, "x": 2}))
        add(Product("lv_speedy_vtg", "Louis Vuitton Speedy 30 (vintage, good condition)", "luxury_bags", 1.00,
                    {"yahoo_auctions_jp": _p(410.00, 22, 16, 4), "ebay_us": _p(690.00, 18, 15, 6)},
                    {"reddit": 3, "tiktok": 4, "x": 2}))
        add(Product("manga_slam_dunk", "Slam Dunk complete set JP (31 vols, used)", "books", 6.50,
                    {"yahoo_auctions_jp": _p(45.00, 30, 20, 3), "ebay_us": _p(110.00, 12, 9, 2)},
                    {"reddit": 2, "tiktok": 1, "x": 1}))
        add(Product("vintage_tees", "Vintage 90s band tees (TH thrift, graded bundle)", "apparel", 0.30,
                    {"kaidee_th": _p(12.00, 35, 8, 5), "ebay_us": _p(48.00, 25, 18, 12)},
                    {"reddit": 4, "tiktok": 6, "x": 2}))
        add(Product("pg_unicorn", "PG 1/60 Unicorn Gundam kit (sealed)", "toys", 3.50,
                    {"yahoo_auctions_jp": _p(155.00, 25, 14, 4), "ebay_us": _p(260.00, 10, 7, 3)},
                    {"reddit": 5, "tiktok": 2, "x": 2}))
        add(Product("phone_gimbal", "Foldable phone gimbal stabiliser (creator kit)", "electronics", 0.60,
                    {"aliexpress": _p(23.00, 500, 40, 30), "shopee_th": _p(52.00, 60, 18, 20),
                     "tiktok_shop_th": _p(58.00, 45, 12, 16)},
                    {"reddit": 3, "tiktok": 9, "x": 2}))
        add(Product("mech_kb_kit", "75% hot-swap mechanical keyboard kit", "electronics", 1.10,
                    {"aliexpress": _p(38.00, 300, 25, 12), "shopee_th": _p(75.00, 35, 10, 8),
                     "tiktok_shop_th": _p(79.00, 20, 6, 5)},
                    {"reddit": 8, "tiktok": 5, "x": 3}))
        add(Product("used_iphone13", "iPhone 13 128GB (used, Thai set, good battery)", "electronics", 0.40,
                    {"facebook_mp_th": _p(310.00, 6, 5, 2), "kaidee_th": _p(318.00, 4, 4, 1),
                     "shopee_th": _p(395.00, 14, 20, 6)},
                    {"reddit": 2, "tiktok": 3, "x": 1}))

        def addn(n: Niche):
            N[n.id] = n

        addn(Niche("yt_chapter_tool", "YouTube chapter/timestamp exporter (creator tool)", "digital", "global", 9.0,
                   {"volume": 5400, "growth_pct": 12, "solution_count": 3, "demand_posts": 95, "providers": 3},
                   {"reddit": 8, "tiktok": 2, "x": 6}))
        addn(Niche("th_freelance_tax", "Thai freelancer tax calculator + filing guide (ภาษีฟรีแลนซ์)", "info", "TH", 15.0,
                   {"volume": 8200, "growth_pct": 18, "solution_count": 2, "demand_posts": 140, "providers": 2},
                   {"reddit": 5, "tiktok": 6, "x": 8}))
        addn(Niche("line_shop_analytics", "LINE OA shop analytics dashboard (TH merchants)", "digital", "TH", 29.0,
                   {"volume": 2600, "growth_pct": 22, "solution_count": 1, "demand_posts": 60, "providers": 1},
                   {"reddit": 2, "tiktok": 4, "x": 5}))
        addn(Niche("bkk_pressure_washing", "Driveway/roof pressure washing — Bangkok rainy season", "local", "Bangkok", 45.0,
                   {"volume": 340, "growth_pct": 8, "solution_count": 12, "demand_posts": 340, "providers": 12},
                   {"reddit": 1, "tiktok": 3, "x": 1}))
        addn(Niche("airbnb_cleaning", "Airbnb turnover cleaning — Sukhumvit condos", "local", "Bangkok", 32.0,
                   {"volume": 280, "growth_pct": 11, "solution_count": 9, "demand_posts": 280, "providers": 9},
                   {"reddit": 2, "tiktok": 2, "x": 1}))
        addn(Niche("ev_charger_install", "EV wallbox installation partnerships (Thailand)", "b2b", "TH", 380.0,
                   {"volume": 190, "growth_pct": 20, "solution_count": 6, "demand_posts": 75, "providers": 6},
                   {"reddit": 2, "tiktok": 1, "x": 4}))
        addn(Niche("dtv_visa_guide", "DTV visa application walkthrough (nomads relocating to TH)", "info", "TH", 19.0,
                   {"volume": 12500, "growth_pct": 28, "solution_count": 4, "demand_posts": 210, "providers": 4},
                   {"reddit": 12, "tiktok": 9, "x": 10}))
        addn(Niche("wedding_invites_th", "Bilingual Thai–English wedding e-invitation templates", "digital", "TH", 12.0,
                   {"volume": 3100, "growth_pct": 9, "solution_count": 2, "demand_posts": 55, "providers": 2},
                   {"reddit": 2, "tiktok": 5, "x": 2}))

        self.scheduled = [
            MarketEvent(b + 1, "supply_shock", "pokemon_card_214", "ebay_us",
                        {"stock_mult": 0.15, "sellers_delta": -8, "price_jump": 0.42, "no_restock_ticks": 9},
                        "Three major US sellers sold out within 24 hours; remaining eBay US listings repriced upward. "
                        "Japanese inventory still abundant.",
                        "US singles squeeze: Moonbreon #214 supply craters on eBay US"),
            MarketEvent(b + 1, "viral_spike", "durian_chips", None,
                        {"demand_mult": 2.5, "buzz_mult": 6.0, "ticks": 6},
                        "#durianchips taste-test format went viral on TikTok US; US listings selling through fast.",
                        "TikTok food creators push durian chips into US mainstream"),
            MarketEvent(b + 2, "supply_shock", "pokemon_151_bb", "ebay_us",
                        {"stock_mult": 0.30, "sellers_delta": -5, "price_jump": 0.18, "no_restock_ticks": 6},
                        "US distributors pushed restock ETA out four weeks; sealed 151 boxes drying up on eBay US.",
                        "Sealed Pokémon 151 restock slips a month in the US"),
            MarketEvent(b + 2, "search_spike", "yt_chapter_tool", None,
                        {"volume_mult": 1.45, "growth": 45},
                        "A platform change broke creators' old chapter workflow; searches for exporters jumped, "
                        "and the top three tools have poor reviews.",
                        "Creators scramble for YouTube chapter tooling after workflow change"),
            MarketEvent(b + 3, "viral_spike", "nb_2002r_th", None,
                        {"demand_mult": 1.8, "buzz_mult": 4.0, "ticks": 8},
                        "Sneaker media covered the Thailand-exclusive 2002R colorway; US resale interest climbing.",
                        "Thailand-exclusive 2002R catches US sneakerhead attention"),
            MarketEvent(b + 3, "provider_shortage", "bkk_pressure_washing", None,
                        {"providers_delta": -5, "posts_mult": 1.6},
                        "Early rainy season: mold/algae complaints spiked in Bangkok Facebook groups while several "
                        "crews left the market.",
                        "Bangkok rainy season arrives early; cleaning crews booked out"),
            MarketEvent(b + 4, "b2b_surge", "ev_charger_install", None,
                        {"volume_mult": 1.5, "growth": 38},
                        "Thai EV registrations keep surging; condo juristic offices are tendering wallbox installs "
                        "and certified installers are scarce.",
                        "Thai EV boom outruns certified charger installers"),
            MarketEvent(b + 4, "viral_spike", "mech_kb_kit", None,
                        {"demand_mult": 2.2, "buzz_mult": 5.0, "ticks": 7},
                        "A Thai tech TikToker's build video blew up; hot-swap kits selling through on "
                        "Shopee TH and TikTok Shop while AliExpress supply stays cheap and deep.",
                        "Custom keyboard fever hits Thai TikTok"),
            MarketEvent(b + 6, "competitor_entry", "seiko_6139", "ebay_us",
                        {"sellers_delta": 9, "price_drop": 0.12, "stock_add": 25},
                        "A Japanese dealer liquidated a Pogue collection onto eBay US; ask prices sliding.",
                        "Vintage Pogue chronograph prices soften as dealer stock floods in"),
            MarketEvent(b + 9, "restock", "pokemon_151_bb", "ebay_us",
                        {"stock_add": 140, "price_drop": 0.24, "sellers_delta": 6},
                        "The delayed US distributor restock landed early; sealed 151 supply normalised overnight.",
                        "Pokémon 151 restock lands — sealed prices normalise"),
            MarketEvent(b + 10, "hype_fade", "dtv_visa_guide", None,
                        {"volume_mult": 0.55, "growth": -18},
                        "DTV search interest cooling as early-mover guides saturate the niche.",
                        "DTV visa content wave crests"),
        ]

    # ------------------------------------------------------------------- tick

    def tick(self) -> None:
        self.tick_no += 1
        t = self.tick_no
        for ev in [e for e in self.scheduled if e.tick == t]:
            self._apply_event(ev)

        for pid in sorted(self.products):
            p = self.products[pid]
            p.temp_effects = [e for e in p.temp_effects if e["until"] >= t]
            dmult = p.demand_mult(t)
            for vid in sorted(p.venues):
                l = p.venues[vid]
                mean_sold = max(0.0, l.velocity * dmult)
                sold = max(0, round(self.rng.gauss(mean_sold, max(0.4, mean_sold * 0.35)))) if l.stock > 0 else 0
                sold = min(sold, l.stock)
                l.stock -= sold
                l.velocity = 0.8 * l.velocity + 0.2 * sold
                drift = self.rng.gauss(0, 0.012)
                pressure = 0.02 if (l.stock < max(3, l.initial_stock * 0.12) and dmult >= 1) else \
                           (-0.005 if l.stock > l.initial_stock * 0.9 else 0.0)
                l.price = max(0.5, l.price * (1 + drift + pressure))
                if l.stock < l.initial_stock * 0.2 and t >= l.no_restock_until and self.rng.random() < 0.20:
                    l.stock += self.rng.randint(2, max(3, l.initial_stock // 8))
                if self.rng.random() < 0.06:
                    l.sellers = max(1, l.sellers + self.rng.choice([-1, 1]))
                p.history.setdefault(vid, []).append(l.snapshot())
                p.history[vid] = p.history[vid][-HISTORY_CAP:]
            bmult = p.buzz_mult(t)
            for src in SOCIAL_SOURCES:
                mu = p.buzz.get(src, 0) * bmult
                m = max(0, round(self.rng.gauss(mu, max(0.6, mu ** 0.5))))
                p.mentions_history.setdefault(src, []).append(m)
                p.mentions_history[src] = p.mentions_history[src][-HISTORY_CAP:]

        for nid in sorted(self._niches):
            n = self._niches[nid]
            m = n.metrics
            m["volume"] = max(10, m["volume"] * (1 + m["growth_pct"] / 100 / 30 + self.rng.gauss(0, 0.015)))
            m["demand_posts"] = max(1, m["demand_posts"] * (1 + m["growth_pct"] / 100 / 30 + self.rng.gauss(0, 0.03)))
            if self.rng.random() < 0.04:
                m["solution_count"] += 1          # competitors eventually notice gaps
            if self.rng.random() < 0.05:
                m["providers"] = max(1, m["providers"] + self.rng.choice([-1, 1]))
            n.history.append({k: round(v, 1) if isinstance(v, float) else v for k, v in m.items()})
            n.history = n.history[-HISTORY_CAP:]
            for src in SOCIAL_SOURCES:
                mu = n.buzz.get(src, 0) * (1 + max(0.0, m["growth_pct"]) / 40)
                mm = max(0, round(self.rng.gauss(mu, max(0.6, mu ** 0.5))))
                n.mentions_history.setdefault(src, []).append(mm)
                n.mentions_history[src] = n.mentions_history[src][-HISTORY_CAP:]

    def _apply_event(self, ev: MarketEvent) -> None:
        t = self.tick_no
        rec = {"tick": t, "etype": ev.etype, "narrative": ev.narrative, "headline": ev.headline, "venue": ev.venue}
        self._headlines.append({"tick": t, "entity_id": ev.target, "text": ev.headline, "etype": ev.etype})
        if ev.target in self.products:
            p = self.products[ev.target]
            p.events.append(rec)
            if ev.etype == "supply_shock" and ev.venue:
                l = p.venues[ev.venue]
                l.stock = max(0, int(l.stock * ev.params["stock_mult"]))
                l.sellers = max(1, l.sellers + ev.params["sellers_delta"])
                l.price *= 1 + ev.params["price_jump"]
                l.no_restock_until = t + ev.params.get("no_restock_ticks", 5)
            elif ev.etype == "restock" and ev.venue:
                l = p.venues[ev.venue]
                l.stock += ev.params["stock_add"]
                l.sellers += ev.params.get("sellers_delta", 0)
                l.price *= 1 - ev.params["price_drop"]
            elif ev.etype == "competitor_entry" and ev.venue:
                l = p.venues[ev.venue]
                l.sellers += ev.params["sellers_delta"]
                l.stock += ev.params.get("stock_add", 0)
                l.price *= 1 - ev.params["price_drop"]
            elif ev.etype == "viral_spike":
                p.temp_effects.append({"until": t + ev.params.get("ticks", 5),
                                       "demand_mult": ev.params["demand_mult"],
                                       "buzz_mult": ev.params["buzz_mult"]})
        elif ev.target in self._niches:
            n = self._niches[ev.target]
            n.events.append(rec)
            m = n.metrics
            if ev.etype in ("search_spike", "b2b_surge", "hype_fade"):
                m["volume"] *= ev.params["volume_mult"]
                m["growth_pct"] = ev.params["growth"]
            elif ev.etype == "provider_shortage":
                m["providers"] = max(1, m["providers"] + ev.params["providers_delta"])
                m["demand_posts"] *= ev.params["posts_mult"]

    def fast_forward(self, n: int) -> None:
        for _ in range(n):
            self.tick()

    # ------------------------------------------------------- DataSource impl

    def product_ids(self) -> list[str]:
        return sorted(self.products)

    def venue_ids(self) -> list[str]:
        vids: set[str] = set()
        for p in self.products.values():
            vids.update(p.venues)
        return sorted(vids)

    def listings(self, venue: str) -> list[tuple[dict, dict]]:
        out = []
        for pid in sorted(self.products):
            p = self.products[pid]
            if venue in p.venues:
                out.append((self.product_public(pid), p.venues[venue].snapshot()))
        return out

    def listing(self, product_id: str, venue: str) -> dict | None:
        p = self.products.get(product_id)
        if not p or venue not in p.venues:
            return None
        return p.venues[venue].snapshot()

    def listing_sample(self, product_id: str, venue: str) -> list[dict]:
        """Synthesize the page of individual asks a real Browse response would
        carry, from the aggregate listing. Deterministic per (product, venue,
        tick) so demo replays are stable. Faithful in structure — a spread of
        asks around the median, a handful of sellers, some junk, recurring
        variant phrases, and *occasionally* a genuinely underpriced listing —
        so the dislocation and title-mining engines are exercised on demo data
        exactly as they would be on eBay data. Clearly synthetic, never sold
        to the user as real listings."""

        p = self.products.get(product_id)
        if not p or venue not in p.venues:
            return []
        lst = p.venues[venue]
        if lst.stock <= 0 or lst.price <= 0:
            return []
        import hashlib
        seed = int(hashlib.sha1(f"{product_id}|{venue}|{self.tick_no}".encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        n = max(6, min(int(lst.stock), 24, lst.sellers * 4 or 12))
        variants = ["complete in box", "cib tested", "japan import", "us seller",
                    f"{p.category} lot", "free shipping", "mint condition"]
        # Category-plausible adjacent products (accessories/variants) that recur
        # across titles the way real eBay listings do — the raw material for the
        # knowledge-graph expansion ("profitable console → check the accessory").
        _ADJACENT = {
            "gaming": ["everdrive cart", "region free"],
            "cameras": ["leather case", "flash unit"],
            "watches": ["jubilee bracelet", "service dial"],
            "electronics": ["travel adapter", "spare battery"],
            "toys": ["display stand", "diorama base"],
        }
        adjacent = _ADJACENT.get(p.category, ["display case", "acrylic stand"])
        sellers = [f"sim_seller_{i}" for i in range(max(3, min(lst.sellers, 8)))]
        sample = []
        for i in range(n):
            mult = rng.uniform(0.9, 1.35) * (1.0 + 0.12 * (i / max(1, n)))  # sorted-ish ascending
            price = round(lst.price * mult, 2)
            extra = f" with {rng.choice(adjacent)}" if rng.random() < 0.4 else ""
            title = f"{p.name} {rng.choice(variants)}{extra}"
            if rng.random() < 0.12:
                title += " for parts"                       # junk the filters must drop
            sample.append({
                "item_id": f"sim{seed}{i:02d}", "title": title, "price": price,
                "url": f"https://www.ebay.com/itm/sim{seed}{i:02d}",
                "seller": rng.choice(sellers), "condition": "USED_GOOD"})
        # Occasionally a real dislocation: one seller lists ~40% under fair.
        if rng.random() < 0.28 and n >= 8:
            disl_seller = rng.choice(sellers)
            sample.append({
                "item_id": f"sim{seed}X", "title": f"{p.name} {rng.choice(variants)}",
                "price": round(lst.price * rng.uniform(0.5, 0.62), 2),
                "url": f"https://www.ebay.com/itm/sim{seed}X",
                "seller": disl_seller, "condition": "USED_GOOD"})
        # Occasionally a genuinely cheap for-parts/broken unit — the raw
        # material for a refurbishment thesis (buy broken, fix, resell working).
        if rng.random() < 0.22 and n >= 8 and lst.price >= 30:
            sample.append({
                "item_id": f"sim{seed}P", "title": f"{p.name} for parts not working",
                "price": round(lst.price * rng.uniform(0.28, 0.42), 2),
                "url": f"https://www.ebay.com/itm/sim{seed}P",
                "seller": rng.choice(sellers), "condition": "FOR_PARTS_OR_NOT_WORKING"})
        return sample

    def product_history(self, product_id: str, venue: str) -> list[dict]:
        return list(self.products[product_id].history.get(venue, []))

    def product_public(self, product_id: str) -> dict:
        p = self.products[product_id]
        return {"id": p.id, "name": p.name, "category": p.category, "weight_kg": p.weight_kg,
                "venues": sorted(p.venues)}

    def mentions(self, entity_id: str, source: str) -> list[int]:
        ent = self.products.get(entity_id) or self._niches.get(entity_id)
        return list(ent.mentions_history.get(source, [])) if ent else []

    def social_sources(self) -> list[str]:
        return list(SOCIAL_SOURCES)

    def niches(self) -> list[dict]:
        out = []
        for nid in sorted(self._niches):
            n = self._niches[nid]
            out.append({"id": n.id, "name": n.name, "kind": n.kind, "geo": n.geo,
                        "price_point_usd": n.price_point_usd,
                        "metrics": {k: round(v, 1) if isinstance(v, float) else v for k, v in n.metrics.items()}})
        return out

    def niches_map(self) -> dict:
        return self._niches

    def niche_history(self, niche_id: str) -> list[dict]:
        return list(self._niches[niche_id].history)

    def headlines(self, since_tick: int) -> list[dict]:
        return [h for h in self._headlines if h["tick"] > since_tick]

    def event_log(self, entity_id: str) -> list[dict]:
        ent = self.products.get(entity_id) or self._niches.get(entity_id)
        return list(ent.events) if ent else []
