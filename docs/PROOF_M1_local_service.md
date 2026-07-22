# Milestone 1 — live source-to-verdict proof (local-service)

Read-only capture from the production droplet (`178.128.87.69`), **v1.6.0, live
mode, tick 443**, 2026-07-22. Every number below was pulled from the running API
(`/api/observability`, `/api/diagnostics/funnel`), not from a fixture. Where the
droplet has not yet been upgraded to v1.6.1, that is stated explicitly rather
than glossed.

## The niche

`bkk_airbnb_cleaning` — "Airbnb turnover cleaning — Sukhumvit condos"
(kind `local`, geo Bangkok, price point $32 **estimated**).

## Source → verdict trace (the 9 requested elements)

| # | Element | Live value |
|---|---------|-----------|
| 1 | Serper queries | **English supply scan (live-logged):** `bangkok airbnb cleaning`. This watchlist niche is English-configured, so **no Thai query fired for it** — the Thai path (`serper_query_th` + Thai gap-mining templates) is implemented and unit-tested but did not run for this entity. Do not read this row as a live Thai-query proof. |
| 2 | Raw result count | **4** provider results returned |
| 3 | Parsed demand observations | Demand provenance = **`user_supplied`** (operator baseline: 300 searches/mo, 280 demand-posts/mo), momentum tracked via the Serper mention series. **Not independently observed demand** — labelled as such. |
| 4 | Parsed supply observations | **`observed` — 4 providers**, domains: `bnbcondo.com`, `khunclean.com`, `strspecialist.com`, `simply.co.th` |
| 5 | niche_state points | **13 / 6** needed — accumulation satisfied |
| 6 | Generator output | `LocalServiceGenerator` produced a `local_service` candidate (family total: 32 candidates over the last 120 cycles) |
| 7 | Council verdict | **`demand_corroboration` FAILED (critical):** `Trend ✗ (0%/mo), social ✗ (10/day mentions), demand:supply ✓ (75:1)` → 1 of 3 signals, below the ≥2 bar. `competition_gap` **PASSED** (75:1 gap is real). |
| 8 | Final status | **REJECTED.** Category on live v1.6.0 = `insufficient_supply` — the **mislabel** (supply is precisely what is abundant-of-opportunity). Under v1.6.1 (tested, **not yet deployed**) = `insufficient_demand` (13 ≥ 6 points, demand real but flat). |
| 9 | Evidence URLs + timestamps | Serper supply `bangkok airbnb cleaning` @ **2026-07-22T05:55:37.114Z** → 4 providers · ScrapingDog `https://bnbcondo.com/housekeeping.php` @ **2026-07-22T05:55:38.295Z** |

## ScrapingDog evidence trace

| Field | Value |
|-------|-------|
| URL fetched | `https://bnbcondo.com/housekeeping.php` (top provider domain from the Serper supply scan) |
| Timestamp | 2026-07-22T05:55:38.295Z |
| Price evidence | **none parsed** (`prices: null`) → price point stays `estimated` ($32), honestly labelled — no invented median |
| Review/complaint signal | `{positive_hits: 1, complaint_hits: 1, complaint_ratio: 0.5}` |
| Consumed by | niche competitor-evidence layer: grounds the price point **only if** a median is parsed (here none, so `estimated` is kept); review signal attaches to the thesis |

This is a real, end-to-end **non-eBay → generator → council** path: Serper
(supply) + ScrapingDog (competitor page) fed a `local_service` candidate that
faced the same council as every flip. Neither source touches eBay.

## Source-level record counts (non-eBay observations reach the pipeline)

| Source | Observations stored | Published |
|--------|--------------------:|----------:|
| ebay_us | 11,429 | 217 |
| shopee_th | 1,329 | 1 |
| facebook_mp_th | 886 | 2 |
| aliexpress | 443 | 1 |
| tiktok_shop_th | 443 | 1 |
| yahoo_auctions_jp | 443 | 0 |
| serper | 50 | 0 |
| scrapingdog | 2 | 0 |

eBay dominates the **flip** funnel, but it is **not** the only source: five
non-eBay marketplaces plus Serper and ScrapingDog all carry real, growing
observation counts, and Serper/ScrapingDog observations route into the venture
generators (proven by the trace above).

## Exact zero-output reason for every non-flip family

| Family | Candidates | Watched | Exact reason |
|--------|-----------:|--------:|--------------|
| `local_service` | 32 | 2 | Generated and evaluated; **0 published, 35 rejected** — all on `demand_corroboration` (demand real, 75:1 gap real, but **flat trend / no social spike**). Not a supply fault; mislabelled `insufficient_supply` on v1.6.0, corrected to `insufficient_demand` on v1.6.1. |
| `b2b` | 0 | 3 | **No candidate ever generated.** Root cause (code-verified, `investigator.py:43-45`): `by_entity` is built **only from anomalies**, and `_VentureGenerator.generate` iterates `by_entity`. These niches have flat/steady demand, so no acceleration anomaly fires, so they never enter candidate generation. |
| `digital` | 0 | 8 | Same as b2b — anomaly-gated generation; steady demand, no anomaly, no candidate. |
| `info` | 0 | 4 | Same — and note two info niches (`DTV visa walkthrough`, `Thai freelancer tax`) sit at **13/6 points with observed supply**, yet still produce zero candidates because their demand is steady, not accelerating. |

**The next real bottleneck (Milestone 2 target):** venture candidate generation
is gated on **flip-style acceleration anomalies**, but healthy service/info
demand is *steady*, not spiking — so a well-observed steady niche never gets
evaluated. The local niche differs only in that its Serper mention series moved
enough to trip an anomaly. Fixing this — a steady-demand generation path for
ventures — is the honest next slice, not a bigger claim.

## Capability status (truth contract)

**Full test suite:** `290 passed, 1 skipped` (pytest, whole repo, 2026-07-22).

**IMPLEMENTED AND LIVE-PROVEN (v1.6.0 on the droplet):**
- venture candidates enter `niche_state` (3 niches, 13/6 points, `niche_state_error: null`)
- local-service candidates remain in the `local_service` family (not leaked to physical)
- the false `insufficient_supply` labelling was identified from the live reason string
- Serper supply → observation → generator → council → rejection is a real, timestamped chain
- ScrapingDog URL → parsed evidence → price/review provenance is a real, timestamped chain
- non-eBay observations reach generator routing

**IMPLEMENTED AND LIVE-PROVEN (v1.6.1 deployed, droplet tick 446, 2026-07-22):**
- the rejection-label split now fires live. A fresh cycle rejected the same
  `bkk_airbnb_cleaning` niche with the new self-classifying reason and the
  correct category:
  > `type=local  category=insufficient_demand`
  > `Demand seen by ≥2 independent sources failed — 13 observations but only 1 of
  > 3 independent signals (need ≥2): trend ✗ (0%/mo), social ✗ (10/day mentions),
  > demand:supply ✓ (75:1). Demand is real but not corroborated as growing.`
- i.e. a real 75:1 supply gap with 13 accumulated points is now named
  `insufficient_demand` (weak, flat demand), no longer the false
  `insufficient_supply`. (The aggregate `/api/diagnostics/funnel` still shows
  historical v1.6.0 records classified at rejection time; only *new* rejections
  carry the v1.6.1 category, so the funnel shifts as the 120-cycle window rolls.)

**DESIGNED ONLY (Milestone 2):**
- a steady-demand generation path so well-observed b2b/digital/info niches are
  actually evaluated instead of silently skipped.
