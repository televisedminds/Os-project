# Opportunity OS — Architecture, Audit & Scaling Roadmap

The objective function, stated once and used to judge every idea below:

> **Maximize the expected dollar value of published opportunities per unit of
> API budget and operator attention.** Scan count is an input cost, not a KPI.
> 20 high-confidence opportunities beat 20,000 weak ones.

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
**demand acceleration** · **margin expansion** · **trend reversal**.
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
