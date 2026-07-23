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

Each has a sibling `*_metadata.json` (capture UTC, version, commit, tick,
endpoint, exact command, redactions=none). The tick-448 files are documentary
proof of M2 steady-state behaviour; the tick-451 files are live evidence of the
bug on v1.7.0.

## 8. Truth-contract classification

- **Cooldown bug:** proven real (deterministic test + live production evidence).
- **The fix (v1.7.1):** **IMPLEMENTED** and **TESTED** (22 targeted, 312 full
  suite). **NOT YET LIVE-PROVEN** — the droplet still runs v1.7.0; I have read +
  `POST /api/cycle` access but **no deploy access** (SSH / `git pull` /
  `systemctl restart`), so I cannot deploy the fix myself.

## 9. To close the live-proof gate (requires deployment)

```
cd /opt/opportunity-os && git pull origin claude/opportunity-os-platform-n6dkel && sudo systemctl restart opportunity-os
```

After deploy I will advance the same niches past tick +6 and capture a fresh
`docs/proofs/fix_cooldown_tick_<n>_*` showing an info niche that was in cooldown
**re-entering** `steady_state` once the window expires — the live proof of the
fix. A rejection verdict on re-entry is acceptable; thresholds will not be tuned.
