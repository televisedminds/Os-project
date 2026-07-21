# Opportunity OS — Architecture, Audit & Scaling Roadmap

The objective function, stated once and used to judge every idea below:

> **Maximize the expected dollar value of published opportunities per unit of
> API budget and operator attention.** Scan count is an input cost, not a KPI.
> 20 high-confidence opportunities beat 20,000 weak ones.

## 0a. v0.10 — evidence, verification levels, honest funnels (Phases 7–9)

"Verified" used to be a single boolean. Now it means something graded, and
every number behind it is traceable:

* **Evidence ledger (`evidence.py`, Phase 9)** — each opportunity carries a
  list of `EvidenceItem`s: what the claim is OF, its value, its KIND (observed
  / estimated / calculated / assumption / user-supplied / ai-interpretation /
  unknown), the source, whether it counts as an *independent* corroboration,
  the raw-record id and freshness. Economics are always `calculated`, a Browse
  price is `observed` but explicitly labelled *asking, not a sold comp*, and
  counterfeit/condition risk is honestly `unknown` rather than faked.
* **Verification levels (Phase 7)** — `compute_level` grades from the ledger,
  not from "it passed": DISCOVERED → PARTIALLY_VERIFIED → MULTI_SOURCE_VERIFIED
  → EXECUTION_READY → INVALIDATED. An opportunity whose evidence all comes from
  one marketplace is `single_source` and capped at PARTIALLY_VERIFIED; nothing
  reaches EXECUTION_READY on asking prices alone (it needs sold comps or a
  third independent corroboration). Measured on demo: cross-market flips and
  corroborated ventures reach multi-source; single-venue dislocations stay
  partial — exactly the honesty the audit asked for.
* **Rejection taxonomy + funnels (Phase 8)** — every rejection is classified
  into a fixed taxonomy (margin_below_threshold, unsupported_thailand,
  insufficient_supply, low_confidence, stale_data, …) and aggregated per type
  in `rejection_funnel`; `type_funnel` now also breaks down by verification
  level and counts single-source opportunities. So "lots rejected" becomes a
  map of *why*.
* **AI as investigator, not oracle (Phase 9)** — the risk desk is handed the
  evidence ledger as its ONLY ground truth, forbidden from inventing prices/
  volumes/fees/eligibility, and required to name `missing_evidence` instead of
  guessing. Its verdict is stored as an `ai_interpretation` evidence item —
  never as fact — and its named gaps become `unknown` items on the ledger.

## 0b. v0.9 — sources ≠ generators, and a knowledge graph (Phases 3–5)

The diversity problem ("almost every result is an eBay flip") had a structural
cause: the pipeline conflated **data sources** with **opportunity generators**,
and only the flip path had a complete generator. v0.9 separates them:

* **`generators.py` — a generator registry** decoupled from scanners. A source
  collects evidence; a generator combines evidence into a business thesis; the
  same council gates them all. Eight generators register today
  (`cross_market_flip`, `dislocation`, `digital_product`, `info_product`,
  `local_service`, `b2b_service`, `micro_saas`, `bundle_repair`), five+ distinct
  opportunity types, and a broken generator can never break a cycle. Not every
  generator touches a marketplace.
* **Non-flip generators that produce real candidates** — the venture family
  split by kind (digital / info / local-service / B2B), a distinct
  `micro_saas` thesis (strict evidence: real demand growth + genuinely weak
  supply, mutually exclusive with generic digital so demand isn't double-
  counted), and `bundle_repair` — a **refurbishment** thesis mined from a
  product's own ask distribution (buy a for-parts unit far under working comps,
  repair, resell). Refurb economics carry a real parts+labour cost and a
  pessimistic scrap reserve, and only fire on genuinely repairable categories
  (gaming/electronics/cameras/watches) — never a sealed collectible.
* **`graph.py` — the product & opportunity knowledge graph** (Phase 5).
  Normalized nodes (product/brand/model/accessory/part/niche/seller/outcome)
  and typed edges (variant-of, compatible-with, co-listed, previously-
  profitable/rejected). A verified opportunity marks its product profitable and
  seeds co-listing edges from its own page; a **bounded, EV-ranked traversal**
  (`related_hypotheses`) then turns that one win into related hypotheses —
  "profitable Seiko chronograph → check the service dial, the jubilee bracelet"
  — promoted into discovery. Traversal is capped (never unbounded fan-out) and
  scored by expected value per API request, steering toward proven neighbors
  and away from previously-rejected ones.

Measured (demo, same budget): opportunity types went from ~all
`product_arbitrage` to a spread across `product_arbitrage`, `local`,
`micro_saas`, `info`, `refurbish`; a verified discovery now spawns ~20+ related
hypotheses for the fleet to investigate next. Every candidate still faces the
council and the pessimistic economics gate.

## 0. v0.7 — the alpha-density rewrite (the single biggest bottleneck)

**The bottleneck was never compute or scanner count. It was how much alpha we
extracted from each API response.** Every eBay call returns ~50 individual
listings — a whole distribution, every seller, every exact URL — and the old
pipeline collapsed all of that into ONE number (the median price) and threw the
rest away. One API call → at most one signal. That is the ceiling that kept us
at ~3 verified opportunities/day, and no amount of "more scanners" moves it.

The redesign treats every response as a **graph of information**, not a price.
The new `research.py` core turns one call into many independent candidates:

1. **Dislocation detection (the richest source).** Within a single response,
   `find_dislocations` finds individual listings priced far below the market's
   own *conservative* clearing value (median of the cheapest page's upper half
   — deliberately below the true market median). Each hit is an intra-venue
   flip with an **exact URL**, needing no second venue and no price history —
   the edge is *inside* one response. Junk ("for parts", "box only", repros)
   and wrong-model-number variants are filtered before they can masquerade as
   bargains. `find_liquidation` spots one seller dumping several below-fair
   asks — an estate/closing sale worth sweeping.
2. **Full ask-distribution microstructure.** `market_stats` reads fair value,
   p25/p75, dispersion, seller count and **top-seller concentration** from the
   same page — turning "the price" into the shape of the whole market.
3. **Title mining → graph fan-out (free candidates).** Sellers write the
   product graph into their titles. `mine_related` extracts recurring model
   variants / adjacent products across a page and promotes the strongest as
   **new discovery candidates plus graph edges — zero extra API calls.** One
   response about a Game Boy SP seeds the Pokémon-edition variant, the boxed
   edition, the adjacent handheld.
4. **Opportunity propagation.** When an entity produces a *verified* opportunity,
   the pipeline **boosts the discovery priority of its graph neighbors** — so
   finding one edge pulls the fleet toward the cluster around it.

Two more quant-desk pieces sit on top of the richer candidate stream:

5. **EV research-budget allocator (a bandit, not a rota).** `ucb_rank` orders
   the scan tail by *verified research yield per scan* (anomaly/candidate/
   publication rewards) with a UCB exploration bonus, so the fixed API budget
   flows to whatever is actually PRODUCING opportunities while still probing the
   unknown. The `research_yield` ledger accrues every scan/anomaly/candidate/
   publication per entity.
6. **Capital optimizer (allocate, don't just rank).** `allocate_capital` solves
   for the best use of the operator's finite capital: greedy over net-per-dollar-
   per-day, capped per category for diversification, respecting the real budget
   — returning an execution order, deployed vs reserve capital, and expected
   ROI. Surfaced in the daily briefing as `capital_plan`.

**Measured effect (demo, same API budget):** anomalies per cycle ~5 → ~50,
investigated candidates ~5 → ~20, verified active opportunities ~3 → ~14, each
dislocation carrying the exact listing URL to buy. The honesty bar did not move:
every candidate still faces the council and the pessimistic fee waterfall, and
same-venue dislocations must clear the venue's round-trip take — thin ones die
in the gate, which is exactly why the survivors are real.

Everything above is exercised in **demo mode too**: the simulator synthesizes a
faithful page of asks per listing (spread, sellers, junk, recurring variants,
and an occasional genuine dislocation) so the alpha engine runs on demo data
exactly as it runs on eBay data — clearly synthetic, never shown to the user as
real listings.

## 1. The pipeline (target vs actual)

The requested architecture and the shipped one, stage by stage:

| Target stage | Implementation | File |
|---|---|---|
| Raw data sources | Adapters + discovery sources + **plugin registry** (drop-in file = new scanner) | `market/adapters.py`, `discovery.py`, `plugins/` |
| Historical data lake | Append-only, timestamped SQLite series: prices, stock, sellers, sell-through, mentions, niche metrics, headlines, FX | `db.py` (`live_*` tables) |
| Signal detection | 11 independent detectors (see §3) over rolling baselines | `agents/anomaly.py` |
| AI investigation | Why-chain builder + Claude risk desk on every new publication (edge + concrete risks + verdict) | `agents/investigator.py`, `ai.py` |
| Economic simulation | Full cost waterfall (fees, ship legs, TH VAT/duty, US destination duty, FX) in base + pessimistic scenarios, lot-size aware | `economics.py` |
| Risk analysis | Pessimistic-survival gate + AI risk review + Thailand feasibility screen | `pipeline.py`, `thailand.py` |
| Verification council | 6 reliability-weighted checks, critical-veto semantics | `agents/verifiers.py` |
| Opportunity feed | Feed + evidence package + selling kits + instant alerts | `api.py`, `web/` |
| Feedback loop | Outcomes → calibration, source/verifier reliability, scoring weights | `agents/learning.py` |

## 2. Audit — bottlenecks found (and status)

Ordered by impact on expected-value throughput:

1. **Flat rescan schedule** *(FIXED)* — every entity was rescanned every
   cycle, so the eBay budget capped coverage at ~60 markets. Now tiered:
   hot (watchlist, active opportunities, fresh anomalies) every cycle; warm
   (best discoveries) every 4th; cold tail every 12th, offset per entity so
   per-cycle load is flat. **Same budget → ~200 watched markets.**
2. **Signal engine breadth** *(FIXED)* — only 7 detector types. Added 4 that
   read data we already store: `seller_exodus` (competitors leaving),
   `demand_acceleration`, `margin_expansion` (spread widening — catches an
   edge while it grows, not at the top), `trend_reversal` (niche turning up).
3. **Hardcoded source registration** *(FIXED)* — new scanners required core
   edits. Now: one file in `opportunity_os/plugins/`, `@discovery_source` or
   `@venue_adapter("id")`, done. A broken plugin is skipped, never fatal.
4. **No qualitative risk stage** *(FIXED)* — quantitative gates can't see
   counterfeit exposure, platform-policy risk, or fad decay. The AI risk desk
   now reviews every first-time publication and must name the *edge* — if no
   plausible reason exists for the mispricing, that itself is flagged.
5. **Buy-side asymmetry** *(PARTIAL — biggest remaining)* — sell side (eBay)
   is automated; buy side is automated only for Shopee TH (ScrapingDog
   pairing, capped 8). JP proxy venues (Buyee) have no API: cross-border JP
   flips still need manual quotes. See roadmap.
6. **Single-process sequential fetching** *(ACCEPTED for now)* — ~45-65
   HTTP calls per cycle, serial, ≈30-60s per pass at a 30-min cadence. The
   binding constraint is API allowances, not wall time; concurrency buys
   nothing until we're allowance-rich. Revisit at 500+ entities.
7. **Signals/anomalies tables are capped** *(ACCEPTED)* — they're derived
   data, re-derivable from the append-only series. The load-bearing history
   is never overwritten.
8. **No FX/fee history series** *(P2)* — only latest FX is stored.

## 3. Signal detectors (each independent, each a reason to investigate)

Price z-spike · supply crunch · cross-venue spread · social spike ·
search gap · service imbalance · B2B surge · **seller exodus** ·
**demand acceleration** · **margin expansion** · **trend reversal** ·
**price dislocation** (v0.7 — a single ask far below its market's fair value,
the highest-yield detector because it fires per-listing, not per-market) ·
**seller liquidation** (v0.7 — one seller dumping several below-fair asks).
Planned (need data we don't store yet): review velocity, rank changes,
seasonality (needs ≥1y of history — accumulating now).

## 4. Source feasibility matrix (the honest version)

| Requested source | Verdict | Why |
|---|---|---|
| eBay | ✅ live | Browse API, 5k calls/day free — the volume backbone |
| Google Trends, Hacker News, News RSS | ✅ live, keyless | |
| Reddit | ✅ built (key pending approval) | Serper stands in meanwhile |
| Serper (Google) | ✅ built | demand stand-in + future Shopping prices |
| Shopee TH | ✅ live via ScrapingDog | scraping fragility priced in |
| Etsy | 🔶 feasible next | free API, app approval takes days — good P1 plugin |
| Rakuten JP | 🔶 feasible next | free Webservice API — template plugin shipped |
| Mercari US | 🔶 possible | unofficial endpoints, scraper needed |
| Amazon (any country) | 🔶 gated | PA-API needs an affiliate account with 3 sales; realistic path is Keepa (€19/mo) |
| Yahoo Auctions / Mercari JP | 🔶 via paid scraping only | no public API; Buyee scraping is brittle — P1 experiment |
| Lazada / TikTok Shop | 🔶 seller-only APIs | scraper route only |
| Taobao / 1688 | ❌ rejected | requires CN business registration; scraping is an arms race not worth one operator's capital |
| Facebook Marketplace | ❌ rejected | no API, aggressive anti-bot, ToS risk to the operator's personal account |
| X / Twitter | ❌ rejected | $100+/mo for weak commerce signal |
| Patents, import/export stats, gov registrations, filings | ❌ rejected *for this operator* | institutional-grade sources whose signals a $500-capital solo flipper cannot act on; revisit if the platform ever serves B2B users |
| YouTube, Product Hunt, GitHub | 🔶 P2 plugins | free APIs, niche-demand signal for the venture side |

## 5. Roadmap (highest expected-value first)

**P1 — next**
1. *Serper Shopping venue* — Google Shopping as a second US sell-side price
   (corroborates eBay, extends coverage to new-goods retail).
2. *Etsy + Rakuten plugins* — two real, free APIs; Rakuten opens the JP buy
   side legitimately (no proxy scraping).
3. *Probability output on economics* — triangular price/velocity/fee draws →
   P(profit>0) and P10/P50/P90 displayed per deal.
4. *Buyee scraping experiment* behind ScrapingDog — the JP buy side is the
   single most valuable missing dataset; ship as an EXPERIMENTAL adapter with
   the same honesty labels as Shopee.

**P2**
5. FX + fee history series; seasonality detector once a year of data exists.
6. Concurrent fetch pool when entity count clears ~500.
7. Review-velocity + rank-change detectors (requires storing review counts —
   Keepa or Etsy provide them).

**Non-goals** (rejected on the objective function): distributed workers and
queues at this scale (a 1-GB droplet handles 10k entities in SQLite; the
constraint is API allowances), lowering verification gates to inflate counts,
and any source whose signal the operator cannot act on with current capital.

## 6. Writing a scanner plugin (the 5-minute version)

```python
# opportunity_os/plugins/mysource.py
from opportunity_os.discovery import Candidate, DiscoverySource, clean_query, slug
from opportunity_os.plugins import discovery_source

@discovery_source
class MySource(DiscoverySource):
    id, name = "mysource", "My Source"

    def discover(self):
        # fetch → filter hard → return candidates; fail with self._fail(msg)
        return [Candidate(kind="product", id=slug("thing", "disc_p"), name="Thing",
                          source=self.id, score=0.7,
                          queries={"ebay_us": clean_query("thing")})]

    def check(self):
        return True, "ok"
```

Drop the file in, restart. It's judged by the AI brain, capped by the engine,
observed by the tiered scheduler, and gated by the council like everything
else. That contract — *plugins propose, the pipeline disposes* — is what lets
source count grow 100× without the quality bar moving an inch.
