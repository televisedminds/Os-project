# Milestone 4 — discovered venture niches reach the watched set (live proof)

Read-only capture from the production droplet, **v1.9.0, tick 468**, 2026-07-23.
Raw artifact + SHA-256: `docs/proofs/m4_tick_468_observability.json` (+ metadata).

## Before vs after

| | Before (v1.8.0) | After (v1.9.0) |
|---|---|---|
| `niche_state` niches | **3** (watchlist only) | **20** (3 watchlist + **17 discovered**) |
| discovered niches assessed in `venture_eval` | **0** (silently absent) | **26**, each with an explicit reason |

## The discovered niches, now in the pipeline

`niche_state` now lists all 17 discovered venture niches (`disc_n_*`) across
local / digital / b2b / info. The `venture_eval` ledger assesses them every cycle
with honest, progressive reasons instead of silence:

```
disc_n_13                     eligible=0  no_observed_provenance    (local)
disc_n_supplier               eligible=0  no_observed_provenance    (b2b)
disc_n_how_do_you_handle_...  eligible=0  still_gathering_evidence  (info)  ← 2 demand points, accumulating
disc_n_saas_developers_...    eligible=0  no_observed_provenance    (b2b)
… (26 discovered assessments total)
```

- `no_observed_provenance` — the niche is watched but Serper hasn't measured its
  demand yet, so it has no observed series. Honest: it needs a first observation
  before it can be steady-eligible.
- `still_gathering_evidence` — `disc_n_how_do_you_handle_the_actual` already has
  **2 demand points**, on its way to the 6 needed. **Measurable intermediate
  progress**, not "wait and see".

## The funnel is now rich and honest

`venture_entry` over the 80-cycle window:

```
entrants: {anomaly: 16, steady_state: 8, both: 1}, deduped_total: 25
verdicts: {verified: 11, rejected: 14, cooldown_no_new_observations: 26,
           no_observed_provenance: 96, still_gathering_evidence: 6}
```

`verified: 11` — the watchlist `local_service` venture (Airbnb cleaning)
re-verifying across cycles; info niches entering via `steady_state`. Discovered
niches contribute the `no_observed_provenance` / `still_gathering_evidence`
rows — visibly progressing, never silent.

## What this proves — and doesn't

**Proven:** the M4 stall is fixed. Discovered venture niches now reach the watched
set (`niche_state` 3→20) and are assessed by the venture pipeline (`venture_eval`
0→26 discovered), with honest reasons and at least one already accumulating real
demand observations.

**Not claimed:** no *discovered* niche has verified yet. That needs its Serper
demand/supply measurement to reach ≥6 points + observed supply — a function of the
measurement-budget rotation over subsequent cycles, not something provable in one
tick. That rotation is the **next bottleneck (M5 candidate):** ensure the
per-cycle Serper niche-measurement budget actually cycles through the 17
discovered niches (not just the 3 hot watchlist niches) so they accumulate.

## Truth-contract classification
Milestone 4: **IMPLEMENTED, TESTED (315 passed, 1 skipped), DEPLOYED (v1.9.0),
LIVE-PROVEN (tick 468)** — for its actual claim: discovered niches now *enter and
are evaluated by* the pipeline. Their eventual *verification* is gated on
measurement accumulation (next milestone).
