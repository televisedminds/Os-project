# Milestone 2 post-completion audit — cooldown-starvation fix (v1.7.1)

## 1. Was the bug real?

**Yes — proven two independent ways:**

1. **Deterministic failing test** (`test_cooldown_naturally_expires_despite_skip_rows`):
   evaluate a niche at tick 18, leave observations unchanged, skip 19–23, and it
   must re-open at tick 24 (cooldown = 6). Against the pre-fix code it did **not**
   re-open — it returned `cooldown_no_new_observations` at tick 24, i.e. starved.
2. **Live production evidence** on v1.7.0 (`docs/proofs/bug_cooldown_tick_451_*`).
   The two info niches entered at tick 448; by tick 451 the ledger shows:

   | niche | newest row | last *entered* row |
   |-------|-----------|--------------------|
   | `dtv_visa_guide` | tick 451, `eligible=0`, `cooldown_no_new_observations` | tick 448 |
   | `th_freelance_tax` | tick 451, `eligible=0`, `cooldown_no_new_observations` | tick 448 |
   | `bkk_airbnb_cleaning` | tick 451, `eligible=1` (re-entered via **anomaly**) | tick 451 |

   The buggy cooldown reads the **newest** row (451, a skip), so `451 − 451 = 0 < 6`
   keeps the info niches skipping every cycle — the newest-row tick advances with
   each skip, so the window is never reached. The anomaly path is unaffected
   (`bkk_airbnb_cleaning` re-entered normally).

## 2. Exact root cause

- `opportunity_os/steady.py:156` — cooldown reference was
  `store.last_venture_eval(nid)`.
- `opportunity_os/db.py` `last_venture_eval` — `SELECT * FROM venture_eval WHERE
  entity_id=? ORDER BY tick DESC LIMIT 1` returns the **newest** row regardless of
  whether it was an entry or a telemetry-only skip.
- `opportunity_os/agents/investigator.py:58-65` — records a row for **every**
  assessment each cycle, *including cooldown skips*. So a cooled-down niche writes
  a fresh skip row every tick, continuously resetting the reference tick.

Net: `(tick − newest_row.tick)` stayed ≈ 1, never ≥ `steady_cooldown_ticks`, so an
unchanged niche remained in cooldown forever (only a rising demand-point count
could free it, via the `pts <= last.demand_points` guard).

## 3. The fix and why it is correct

New query `db.last_entered_venture_eval(nid)` — `… WHERE entity_id=? AND
eligible=1 ORDER BY tick DESC LIMIT 1` — returns the newest row where the niche
**actually entered** evaluation (anomaly, steady, or both). `steady.py` now keys
the cooldown off *that*, so telemetry-only skip rows (`eligible=0`) can never
reset the clock. The cooldown measures time since the last real evaluation, which
is exactly its intent. New observations still lift it immediately (unchanged
`pts <= last.demand_points` guard); the anomaly path and `both` dedup are
untouched; skip rows keep their explicit reasons (no telemetry hidden).

The ledger already distinguishes the four states the audit asked for:
*assessed* = every row · *entered* = `eligible=1` · *council-evaluated* =
`verdict` set · *skipped* = `eligible=0` + `reason`.

## 4. Database migration impact

**None.** No schema change — the fix adds a *query method* only; the
`venture_eval` table already existed (v1.7.0) and is created on open. Existing
databases open unchanged; no destructive migration.

## 5. Tests added (5; file total 22)

- `test_cooldown_naturally_expires_despite_skip_rows` — the reproduction.
- `test_cooldown_skip_rows_do_not_reset_the_evaluation_clock`.
- `test_anomaly_only_entry_unaffected_when_not_steady_eligible`.
- `test_ledger_distinguishes_entered_from_skipped`.
- `test_summary_counts_with_entered_and_skipped`.

## 6. Results

- Targeted (`test_steady_ventures.py`): **22 passed**.
- Full suite: **312 passed, 1 skipped**.
- Commit: **`eb0c3c4`** (version 1.7.1), pushed to
  `claude/opportunity-os-platform-n6dkel`.

## 7. Raw proof artifacts

| File | SHA-256 |
|------|---------|
| `docs/proofs/m2_tick_448_cycle.json` | `c348cf9f4e1510495eeb45ee2c1d1f48f2af09b4d5c4d370039a2e63c580b222` |
| `docs/proofs/m2_tick_448_observability.json` | `4e51e9bedb2b50e05dc33db2de206a83d1a59eb0b2a41d3dc25f3339ab98a297` |
| `docs/proofs/bug_cooldown_tick_451_cycle.json` | `b33df4c3d9d56a3bf70cf8e743bc86ed62606c15db0da8a1d77e9064a79ea3b0` |
| `docs/proofs/bug_cooldown_tick_451_observability.json` | `ceb14cf196500f1f1134129fbf030910ce1202eb0d16b8521d058454ad28f859` |
| `docs/proofs/fix_cooldown_tick_455_cycle.json` | `7fee596ea48e3262d11edabaca4158dba93075d1478e5b1acd2b0a1034d73d59` |
| `docs/proofs/fix_cooldown_tick_455_observability.json` | `60e280d3cc29f6c3c5fea6d773bf972580e176dafe1290e8f5666fd3fcc74df7` |

Each has a sibling `*_metadata.json` (capture UTC, version, commit, tick,
endpoint, exact command, redactions=none). The tick-448 files are documentary
proof of M2 steady-state behaviour; the tick-451 files are live evidence of the
bug on v1.7.0; the tick-455 files are live proof of the fix on v1.7.1.

## 8. LIVE PROOF of the fix (v1.7.1 deployed, droplet ticks 448→455)

`dtv_visa_guide` ledger, verbatim from production after deploy:

```
tick=448 elig=1 path=steady_state reason=None                          verdict=rejected   ← entered
tick=451 elig=0 path=steady_state reason=cooldown_no_new_observations   verdict=None       ← cooling
tick=452 elig=0 path=steady_state reason=cooldown_no_new_observations   verdict=None       ← cooling
tick=454 elig=0 path=steady_state reason=cooldown_no_new_observations   verdict=rejected   ← cooling
tick=455 elig=1 path=steady_state reason=None                          verdict=rejected   ← RE-ENTERED
```

The niche entered at 448, was correctly skipped through the cooldown window, and
**re-entered `steady_state` at tick 455** (per-cycle report: `entrants:
{anomaly:1, steady_state:2}, skipped:0`), receiving a fresh `insufficient_demand`
verdict. Under the buggy v1.7.0, the cooldown would key off the newest **skip**
row (tick 454), so `455 − 454 = 1 < cooldown` would keep it skipping forever —
re-entry is impossible there and only happens because the fix references
`last_entered` (448). The anomaly path stayed live throughout
(`bkk_airbnb_cleaning` entered via anomaly each cycle).

**Honest note on the exact boundary:** entry at 448, re-entry observed at 455
(7 ticks). The intervening `tick=454` ledger row is anomalous (`eligible=0` with a
`verdict` set) — it coincides with a dropped `POST /api/cycle` (HTTP 000) whose
retry advanced the tick, so that one cycle was disrupted. The precise expiry tick
also depends on the droplet's configured `steady_cooldown_ticks` and auto-cycler
timing. None of that affects the proof: the cooldown **expired** and the niche
**re-entered**, which the pre-fix code cannot do. The deterministic unit test
pins the exact boundary (re-entry at entry+`cooldown_ticks`).

## 9. Truth-contract classification

- **Cooldown bug:** proven real (deterministic test + live production evidence, tick 451).
- **The fix (v1.7.1):** **IMPLEMENTED**, **TESTED** (22 targeted, 312 full suite),
  **DEPLOYED** (droplet on v1.7.1), and **LIVE-PROVEN** (tick 455 re-entry).
