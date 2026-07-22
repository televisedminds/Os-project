# Opportunity OS — Architecture, Audit & Scaling Roadmap

The objective function, stated once and used to judge every idea below:

> **Maximize the expected dollar value of published opportunities per unit of
> API budget and operator attention.** Scan count is an input cost, not a KPI.
> 20 high-confidence opportunities beat 20,000 weak ones.

## v1.0 — final: the chat interface + the audited whole

v1.0 closes the last mandate item: the execution chat's **interface** — a chat
panel on every opportunity's detail view (state + realised P&L + next action
strip, evidence-tagged history where every reply shows LIVE/SAVED/CALCULATED/
ASSUMPTION/UNKNOWN chips, one-tap prompts, free-text box), a verification-level
chip in the detail header so single-source work is visibly labelled, the exact
`python -m opportunity_os diagnose_sources` entrypoint, and per-source
`candidates_generated` in the health report. The full phase 0–13 mandate was
re-audited item by item against the code with a scripted evidence run — every
check passes, and the acceptance criteria live on as `tests/test_acceptance.py`
+ `tests/test_chat.py` so they cannot silently regress.

![Execution chat](chat_panel.png)

## 0. v0.12 — per-opportunity execution chat + acceptance (Phases 12/13)

Every opportunity now has a **persistent execution workspace** scoped to its id
— not general chat, an assistant that drives ONE deal from research to realised
profit, from Thailand.

* **`chat.py` / `chat_tools.py` / `execution.py`** — a `ChatSession` scoped to
  one opportunity id, with its own message history, checklist, transaction
  ledger and execution state (`not_started → researching → ready_to_buy →
  purchased → in_transit → received → listed → sold → completed`, plus
  cancelled/invalidated). Fifteen deterministic tools: refresh buy/sell
  listings, sold-comp check, recalc profit/qty, price decision, find
  alternative suppliers, live link check, inventory, validity, compare a pasted
  listing, generate a listing draft, update checklist, record purchase/expense/
  sale/refund, escalate re-verification. **It researches, recalculates, drafts,
  validates and records — it never transacts** (no tool buys, pays, publishes,
  accepts an offer, cancels or refunds), which is the human-approval guarantee,
  enforced structurally.
* **`matching.py`** — a product-match engine (EXACT / LIKELY / POSSIBLE_MISMATCH
  / WRONG_PRODUCT / INSUFFICIENT_INFO) so the AGS-101 doesn't turn into the
  cheaper AGS-001; a price-decision engine that re-decides at the *current*
  offered price (BUY / NEGOTIATE / WAIT / SKIP with the max buy price); and a
  live link validator that actually fetches the URL and records when — never
  inventing a listing, price or seller.
* **Grounding + isolation** — every factual answer is tagged LIVE / SAVED /
  CALCULATED / ASSUMPTION / UNKNOWN, comes from a tool result or the stored
  evidence ledger, and the chat says "I don't have that evidence" rather than
  inventing. Claude (when keyed) only phrases grounded facts. Each opportunity's
  workspace is isolated by id — two chats never share state. Everything
  persists across restart.
* **Phase 12** — the acceptance criteria are codified as tests
  (`test_acceptance.py`): source-health report, no silent failures, evidence
  traceable, dedup + metrics, ≥5 generator types, ≥3 non-flip, verification
  levels, Thailand gate, no fabrication, and the chat surface. The 12 chat
  acceptance scenarios are in `test_chat.py`. All run without an LLM — the
  substance is deterministic.

## 0. v0.11 — dedup, Thailand executability, capital optimizer (Phases 6/10/11)

* **Thesis clustering (`clustering.py`, Phase 6)** — twenty underpriced listings
  of one model are ONE market inefficiency. `cluster` collapses opportunities
  sharing a thesis key (type + route + product family) into a single row that
  carries the depth — qualifying-listing count, price range, inventory depth,
  recommended quantity, best confidence — instead of repeating the edge N
  times. The feed clusters by default (`?cluster=false` to see every listing);
  `cluster_metrics` reports raw candidates → unique listings → unique products →
  unique theses with an inflation ratio, so "N verified" is readable for what
  it is.
* **Thailand executability engine (`executability.py`, Phase 10)** — every
  opportunity answers the concrete questions a TH operator has: can a Thai
  resident register on the buy/sell platform, is a Thai-usable payout available,
  is a company required, are there category import/export restrictions, what
  shipping and customs docs apply, are TH taxes in the numbers, is it still
  profitable pessimistically. Each answer is yes/no/estimated/**unknown** —
  and an opportunity cannot be EXECUTION_READY (Phase 7) while any critical
  answer is unknown or a blocker. Nothing is guessed.
* **Capital optimizer v2 (`research.allocate_capital`, Phase 11)** — solves for
  the best use of finite capital, not a ranking: inputs are the budget, a
  per-opportunity cap, a category concentration cap, a risk tolerance
  (conservative prices on pessimistic net and admits only multi-source+;
  aggressive admits all), a liquidity reserve, and a max holding time; outputs
  per pick are quantity, capital, expected profit, WORST-CASE loss (from a
  kind-specific liquidation recovery), completion date and the opportunity cost
  of the tied-up cash — plus portfolio totals and cash remaining.
  **Deduplicated by thesis**, so 20 listings of one edge draw capital once.
  Surfaced in the daily briefing.

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

## 7. v1.1.0 — why the live feed was all eBay-US flips, and the fix

The audit question: *"with eBay, Serper, ScrapingDog and Claude all
configured, why is the visible output still eBay-US buy-and-resell flips?"*
Root causes found (file:line refs are pre-fix):

1. **Discovered niches shipped with fabricated baselines** —
   `discovery.py` `to_watch_niche()` gave every discovered niche
   `base_volume=1500, solution_count=3, providers=3, demand_posts=60`.
   Consequences: the council's "supply-side gap confirmed" check
   (`verifiers.py`, `solution_count <= 4`) passed **by construction**; venture
   economics monetised an invented volume; the evidence ledger labelled the
   invented supply `OBSERVED/market_scan`. Live-mode non-flip output was
   either fiction or (in practice) nothing.
2. **Serper's evidence was discarded** — the adapter returned only
   `len(organic)` for a `site:reddit.com` query. Titles, URLs, snippets,
   People-Also-Ask, related searches — the actual competitor/complaint/gap
   evidence — were thrown away. ScrapingDog was Shopee-price-only.
3. **No source spoke Thai** — trends geo defaulted to US, eBay seeds are
   US-liquid products, the niche-kind classifier only knew English words.
   Thailand local/B2B candidates could not *enter* the funnel.
4. **Scan-budget asymmetry** — products got tiered scans every cycle; niches
   got ≤12 coarse counts every 12th cycle, so venture demand series starved
   while flip evidence compounded.

The v1.1.0 fix, mechanically:

* `SerperAdapter.demand_observation / supply_observation / search` — full
  parsed results stored in the new `live_search_obs` table (query, geo,
  language, timestamp, payload verbatim). Supply = distinct commercial
  domains ranking for the niche (directories/social filtered out) — an
  observed, reproducible proxy replacing the constant `3`.
* `LiveMarket._niche_metrics` now emits **provenance**:
  `observed: {demand: observed|user_supplied|unknown, supply: …}`. Discovered
  niches start at 0/unknown; hand-typed watchlist numbers stay the operator's
  own (and a 0-provider scan never silently overwrites them).
* `verify_venture`: unknown supply **fails** the critical gap check with
  "research required" — a venture can no longer verify on unobserved supply.
  Observed supply cites the domains found. Demo metrics (no provenance key)
  keep simulator semantics.
* `evidence.build_ledger`: venture demand/supply items carry their true kind
  (OBSERVED / USER_SUPPLIED / UNKNOWN) and source (serper / watchlist), so
  the AI risk desk and the UI can no longer be lied to by defaults.
* **`SerperGapDiscovery`** — Thai + English unmet-need mining (rotating
  templates against google.co.th and reddit-scoped English gaps), harvesting
  People-Also-Ask + related searches + result titles through the same
  gap-relevance filter (now Thai-aware, `GAP_PATTERNS_TH`). Thai candidates
  carry `serper_query_th`, so the measurement pass keeps observing their
  demand *in Thai* and their supply. Budgeted: `OOS_SERPER_DISCOVERY_BUDGET`
  (6/sweep) + `OOS_SERPER_NICHE_BUDGET` (12/pass) ≈ under ~60 credits/day.

What this changes about the product: with a Serper key set, the funnel's
non-flip lanes run on real, cited evidence end to end — and when evidence is
missing, the candidate is *visibly* research-required instead of silently
fictional. Without a Serper key, the diagnostics now say exactly that
(`no SERPER_API_KEY — niche demand/supply stays UNOBSERVED (ventures cannot
verify)`), which is the honest description of the old behaviour too — it just
never admitted it.

## 8. v1.2.0 — import/export + wholesale generators (2 of the 4 missing types)

Two of the generators the mandate named (8: import/export, 10: wholesale) did
not exist. Both now do, as **strictly additive** generators with their own
`OppType`s — no existing flip is relabelled, and clustering keeps every thesis
distinct (`test_import_export_is_not_a_duplicate_of_a_flip` proves it).

**`ImportExportGenerator`** (`generators_trade.py`). The profit-max flip picks
one route: global cheapest-buy → dearest-sell. For a Thai operator the most
*executable* route is often a different one, which the greedy flip hides:
- **import** — buy foreign, sell **domestically in Thailand** (PromptPay, no
  export paperwork). Emitted only when the headline flip ships abroad, so it's
  never the same card.
- **export** — source locally in Thailand, sell abroad. Emitted only when the
  headline flip sources abroad.
It gates on **observed destination demand** (a price gap with no sales isn't a
trade) and runs a **Thailand import legal screen** (`thailand.import_restriction`):
a prohibited product — vapes/e-cigs, etc. — is dropped and never suggested; a
licensed one (food → FDA, wireless electronics → NBTC) is surfaced *with* its
permit as a named cost, not hidden. Prices the full customs/VAT/duty waterfall
via the existing `compute_flip`.

**`WholesaleGenerator`** (`generators_trade.py`). Bulk **lots** inside a
venue's own listing sample — "lot of 12", "x20", "case of 24" — whose per-unit
price (`research.parse_lot_size` → price ÷ count) sits below the single-unit
market *on that same venue*, measured from the non-lot singles so a page of
lots can't flatter itself. Buy the lot, break it, resell as singles. A quantity
thesis with its own council check (`verify_wholesale`): the exact lot must
still be live, still below the single-unit fair, into a deep-enough singles
market, clearing pessimistic per-unit fees.

Both re-verify correctly (`_fresh_flip` preserves the stored type for
import/export; `_fresh_wholesale` re-reads the lot from the sample) and face
the same pessimistic-economics gate as everything else. Demo coverage: one JP
product shaped like a real hidden-import market (`casio_fx_jp`) and an
occasional synthetic bulk lot in `listing_sample`, so the two types appear in
the demo feed and the type funnel — clearly labelled synthetic, exactly like
the dislocation/refurbishment demo evidence.

**Still missing from Phase 9** (honest): lead-generation and seasonal/event
generators, the adaptive type-diversity discovery budget (Phase 10), and
ScrapingDog as a general evidence collector (Phase 7). Those remain real gaps.

## 9. v1.3.0 — the type-diversity budget (Phase 10)

The eBay-US-only bias had a third root cause beyond fabricated data (v1.1.0)
and missing generators (v1.2.0): **the watch set itself was monopolized.**
Physical products carry the richest evidence and score highest at discovery, so
the plain top-by-score promotion + expiry (`expire_discovered`) dropped every
gap-mined Thai niche the moment the watch set filled. Non-flip types were
starved of coverage before they could ever verify — measured directly:

```
diversity OFF : watched=50  physical=50  non-physical=0
diversity ON  : watched=50  physical=44  non-physical=6  (local/b2b/digital/info)
```

`diversity.py` allocates the watch budget across opportunity **families**
(physical trade / local service / B2B / digital / info), each with a reserved
floor, adapting the shares toward families that actually verify. Wired into
`DiscoveryEngine.run` at BOTH stages — promotion (which candidates get stored)
and retention (which occupy the finite `discovery_max_active` set) — so a flood
of products can neither out-promote nor out-live the niches.

Two rules are load-bearing:

1. **A family's budget governs how much it is WATCHED, never whether a candidate
   PUBLISHES.** Publishing still requires the full council + pessimistic gate. A
   family with a floor but no qualifying candidates simply under-fills, and the
   report says so (`under_filled: found < target`) — a buying-information state,
   never a weak opportunity waved through to hit a quota.
2. **Adaptation never zeroes a family** (`adapt_weights` floors each at half its
   base weight), so the system keeps buying information on the quiet families
   instead of collapsing onto today's winner. Surplus from under-filled families
   is redistributed by score, so no watch slot is wasted.

Surfaced at `GET /api/diversity` and in the dashboard Discovery tab (per-family
target vs watched vs verified, with an honest "buying info" flag). Knobs:
`OOS_DIVERSITY`, `OOS_DIVERSITY_MIN_FLOOR`, `OOS_DIVERSITY_ADAPT`.

**Remaining Phase-9/10 gaps** (honest): lead-generation and seasonal generators
(2 of the 4 missing types still unbuilt), and ScrapingDog as a general evidence
collector (Phase 7).
