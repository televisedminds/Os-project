# Milestone 2 — steady-state venture entry, live proof

Read-only capture from the production droplet (`178.128.87.69`), **v1.7.0, live
mode**, fresh cycle at **tick 448**, 2026-07-22. Every number below was pulled
from the running API (`POST /api/cycle`, `/api/observability`), not a fixture.

## The bug, proven fixed

Before v1.7.0, the info family produced **zero** candidates: its niches had
steady demand, never fired an anomaly, and so were never evaluated (silent zero).
Under v1.7.0 the same cycle report shows:

```
candidates_by_type: {product_arbitrage: 223, info_product: 2, local_service: 1,
                     refurbishment: 9, wholesale: 5}
```

`info_product` went **0 → 2**, and both entered through the new path.

## Entry-path funnel (this cycle, tick 448)

```
venture_entry: {
  entrants: {anomaly: 0, steady_state: 2, both: 1},
  deduped_total: 3, eligible: 3, skipped: 0,
  verdicts: {rejected: 3},
  by_family_entry: {
    local_service: {anomaly: 0, steady_state: 0, both: 1},
    info:          {anomaly: 0, steady_state: 2, both: 0}
  }
}
```

- **2 info niches entered via `steady_state`** (zero anomalies) — the silent-zero
  families are now evaluated.
- **1 local niche entered via `both`** (anomaly + steady) — **deduplicated to a
  single evaluation** (`deduped_total: 3`, not 4).
- All 3 received a **real council verdict** (`rejected`), not silence.

## venture_eval ledger (per-niche evidence + verdict)

| entity_id | entry_path | family | demand pts | supply | src | freshness (d) | ratio | verdict | category |
|-----------|-----------|--------|-----------:|-------:|----:|--------------:|------:|---------|----------|
| `bkk_airbnb_cleaning` | both | local_service | 14 | 4 | 2 | 0.78 | 75:1 | rejected | insufficient_demand |
| `dtv_visa_guide` | steady_state | info | 14 | 6 | 2 | 0.78 | 50:1 | rejected | insufficient_demand |
| `th_freelance_tax` | steady_state | info | 14 | 6 | 2 | 0.78 | 50:1 | rejected | insufficient_demand |

## One complete live steady-state trace — `dtv_visa_guide`

| # | Element | Value |
|---|---------|-------|
| 1 | Normalized niche ID | `dtv_visa_guide` |
| 2 | Opportunity family | `info` (info_product) |
| 3 | Demand observation count | **14** demand-series points (meter: 14/6) |
| 4 | Supply observation count | **6** credible solutions (observed) |
| 5 | Source count | 2 |
| 6 | Freshness | **0.78 days** since last observation |
| 7 | Anomaly status | **none** — no anomaly fired for this niche |
| 8 | Steady-state eligibility | **eligible** (≥6 points, observed supply, fresh, real provenance) |
| 9 | Selected generator | `InfoProductGenerator` (niche_kind `info`) |
| 10 | **Entry path** | **`steady_state`** — entered evaluation WITHOUT an anomaly |
| 11 | Council inputs | 14 demand points · 6 solutions · ~12,000 monthly searches · demand:supply 50:1 · growth 0%/mo |
| 12 | Council verdict | `demand_corroboration` FAILED (1 of 3 signals: trend ✗ 0%/mo, social ✗, demand:supply ✓ 50:1) + `competition_gap` FAILED (6 solutions > 4 for info) |
| 13 | Final status | **REJECTED — `insufficient_demand`** ("Demand is real but not corroborated as growing") |
| 14 | Evidence URLs + timestamps | Serper supply `DTV visa thailand` (EN) @ **2026-07-22T05:55:34.435Z** → 6 results. Supply domains: `thaievisa.go.th`, `thaiconsulatela.thaiembassy.org`, `siam-legal.com`, `thaiembassy.com`, `washingtondc.thaiembassy.org`. (No ScrapingDog fetch for this niche yet — budgeted; honestly absent.) |

**Exact council reason string (verbatim from production):**
> Demand seen by ≥2 independent sources failed — 14 observations but only 1 of 3
> independent signals (need ≥2): trend ✗ (0%/mo), social ✗ (10/day mentions),
> demand:supply ✓ (50:1). Demand is real but not corroborated as growing.;
> Supply-side gap confirmed failed — 6 credible solutions for 12,000 monthly
> searches. Observed via Google supply scan: thaievisa.go.th,
> thaiconsulatela.thaiembassy.org, siam-legal.com, thaiembassy.com.

This is the milestone's proof: a real production info niche entered the venture
evaluation path **without an anomaly** and received a **real, traceable council
verdict**. The silent-gate bug is fixed.

## Classifications — before vs after

| | Before v1.7.0 | After v1.7.0 (live) |
|---|---|---|
| info family | `candidates: 0` — silent zero | `candidates: 2`, both `steady_state`, verdict `insufficient_demand` |
| local family | `candidates` only when an anomaly fired | also enters steadily; `both` when anomaly + steady coincide, deduped once |
| entry visibility | none | `venture_entry` funnel + `venture_eval` ledger per niche |

## Pass conditions (all met)

1. Anomaly-driven generation still works ✓ (223 flips; local entered via `both`).
2. Steady niches enter without an anomaly ✓ (`dtv_visa_guide`, `th_freelance_tax`).
3. Steady entities pass through the same council ✓ (all 3 verdicts).
4. No fabricated evidence ✓ (real Serper supply scan, observed provenance).
5. Both-path entities evaluated once ✓ (`deduped_total: 3`).
6. Entry-path metadata stored + visible ✓ (`venture_eval` ledger).
7. Per-family funnel separates anomaly vs steady ✓ (`by_family_entry`).
8. Empty families carry explicit reasons, no silent zeros ✓ (skip reasons recorded; 0 skipped this cycle).
9. ≥1 real production niche enters via `steady_state` ✓.
10. That niche receives a traceable verdict ✓ (`insufficient_demand`).
11. Full suite passes ✓ (`307 passed, 1 skipped`).
12. Committed and pushed ✓.

## Capability status (truth contract)

**IMPLEMENTED AND LIVE-PROVEN (v1.7.0, tick 448):** steady-state entry, dedup,
entry-path metadata, per-family funnel, real council verdicts on niches that used
to be silently skipped.

## Remaining limitations
- Scope is the 4 core venture generators (`local`, `b2b`, `digital`, `info`);
  micro-SaaS, lead-gen and seasonal still use the anomaly path only.
- No steady niche has *passed* the council yet — every one so far has flat demand
  and is honestly rejected `insufficient_demand`. That is correct behaviour, not
  a defect: eligibility to evaluate is not approval to publish.

## Next highest-value bottleneck exposed by production
Every observed venture niche is now evaluated, and every one fails
`demand_corroboration` because its demand is **flat** (trend ✗ 0%/mo, no social
spike) even with a real supply gap. The next honest question is **whether the
corroboration bar fits steady service/info demand**: a niche with 12,000 monthly
searches, 50:1 gap, and stable (not growing) demand may be a genuine opportunity
that the growth-oriented `demand_corroboration` check is designed to reject. The
next slice is to measure demand *level and stability* as a distinct, honest
signal — without lowering the bar or manufacturing a pass.
