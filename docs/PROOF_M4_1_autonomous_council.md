# Milestone 4.1 — autonomous discovery→council, live proof

Production droplet, **v1.10.0**, burst cadence (`OOS_SERPER_EVERY=1`, reverted
after), **tick 495**, 2026-07-23. Raw artifacts + SHA-256 in
`docs/proofs/m41_council_tick_495_*` and `docs/proofs/m41_tick_481_*`.

## Part A — the scheduler allocates fairly (tick 481)

Before the fix, watchlist niches drained the Serper budget every pass, starving
11/17 discovered niches at 0 demand points. After (tick 481, verbatim):

```
budget 12 → selected 12 · auto_selected 12 · manual_selected 0
3 watchlist niches = evidence_sufficient (0 budget) · 6 deferred (queue 1-6)
demand-ready niches selected for supply @ priority 582 (unlocks eligibility)
never-scanned niches bootstrapped for demand @ 562 (aging) · max_starvation_age 482
```

All budget now flows to discovered niches; deferrals are explicit with queue
positions; aging guarantees the deferred rise next pass. No silent starvation.

## Part B — a genuinely auto-discovered niche reaches the council (tick 495)

**The 20-point trace** for `disc_n_how_do_you_handle_the_actual_situation_with_so_m`:

| # | Element | Value |
|---|---------|-------|
| 1 | Discovery record | source **hackernews**: *"Unmet-need post on Hacker News (6 points, 12 comments asking for this)"* |
| 2 | Normalized niche ID | `disc_n_how_do_you_handle_the_actual_situation_with_so_m` |
| 3 | Proof of automatic origin | discovered by the HackerNews source, **first seen 2026-07-18** (5 days before) |
| 4 | Not in manual watchlist | id prefix `disc_`; the 3 hand-typed niches are `bkk_airbnb_cleaning` / `dtv_visa_guide` / `th_freelance_tax` — this is none of them |
| 5 | Initial state | `no_observed_provenance` (0 demand, no supply) |
| 6 | Scheduler priority/reason | bootstrapped for demand at priority ~562 (base + aging), then supply once demand ≥ 1 |
| 7 | Demand measurement selected | `selected_for_demand_scan` across burst passes |
| 8 | Real demand query + count | Serper `"do handle actual situation so much"` (EN) → **10 results** @ 2026-07-23T11:52Z |
| 9 | Stored demand observations | accrued to **6 demand-series points** |
| 10 | `still_gathering_evidence` | recorded each pass 0→…→5 points |
| 11 | Supply measurement selected | `selected_for_supply_scan` once demand-ready |
| 12 | Real supply result | supply **observed**, **3 providers** |
| 13 | Stored supply observation | `provenance.supply = observed` |
| 14 | Demand level/direction | 6 points, demand:supply **100:1** |
| 15 | Independent source count | Serper demand + Serper supply (HackerNews origin) |
| 16 | Eligibility | 6 ≥ `steady_min_demand_points` + observed supply → **steady-eligible** |
| 17 | Selected generator | **InfoProductGenerator** (info family) |
| 18 | Entry path | **`steady_state`** — reached evaluation with **no anomaly** |
| 19 | Council verdict | **REJECTED — `margin_below_threshold`**: *"Positive at 45% of modelled demand failed — Pessimistic net $-4/mo; startup $60 → payback 60 months at base."* |
| 20 | Final ledger state | `venture_eval` tick 495 `eligible=1 verdict=rejected`; tick 496 `eligible=0` (post-eval cooldown) |

Demand **corroborated** (6 points, 100:1 gap, level+stability) and competition
passed — the niche died honestly on **unit economics** (a $60 info product with
negative pessimistic net). An honest rejection, exactly what the milestone
accepts. **No evidence fabricated; `steady_min_demand_points` stayed 6.** The
burst only changed measurement *cadence* (a cost knob), never an evidence
threshold. Thai discovered niches (e.g. *ช่วยแนะนำร้าน/โรงงานทำเฟอร์นิเจอร์ไม้แท้*) reached the
council via the anomaly path (`research_required`) in the same window.

## Truth-contract classification

**Milestone 4 (full autonomous discovery→council): IMPLEMENTED, TESTED (328
passed), DEPLOYED (v1.10.0), LIVE-PROVEN (tick 495).** A genuinely
auto-discovered production niche, never in the watchlist, progressed through real
demand + supply research to eligibility, the correct generator, the existing
council, and an explicit honest verdict.

## Remaining limitations / next bottleneck
- Scope is the four core venture generators (local/b2b/digital/info); micro-SaaS,
  lead-gen and seasonal keep the anomaly path.
- At the **default** cadence (`OOS_SERPER_EVERY=12`) accumulation to eligibility
  takes many ticks; the burst was a one-time proof accelerator. The next
  bottleneck is **economics for info/digital ventures** — the first auto-niche to
  the council failed on pessimistic net, suggesting the info-product economics
  model (price point vs. build cost/payback) is where genuinely underserved
  discovered niches will most often die. That is the honest next milestone.
