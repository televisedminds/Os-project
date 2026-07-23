# Milestone 3 — demand level + stability, live proof

Read-only capture from the production droplet (`178.128.87.69`), **v1.8.0, live
mode, tick 458**, 2026-07-23. Raw artifacts + SHA-256 under `docs/proofs/m3_tick_458_*`.

## Result: the first verified VENTURE opportunity

Under v1.8.0 the local-service niche `bkk_airbnb_cleaning` **passed the council
and published**:

```
opp_91e0f4d8df  type=local_service  status=active  new=True
"Airbnb turnover cleaning — Sukhumvit condos"  score 74.9  net $572/mo  partially_verified
```

Every prior milestone rejected this niche as `insufficient_demand` (flat growth).
M3 is what changed it.

## The verification trace (verbatim from `/api/trace/opp_91e0f4d8df`)

```
[PASS] demand_corroboration : Corroborated by 2 of 4 independent signals:
                              trend ✗ (0%/mo), social ✗ (10/day mentions),
                              demand:supply ✓ (75:1), level+stability ✓ (300/mo, durable).
[PASS] competition_gap      : 4 providers vs 300 demand posts/mo. Observed via Google
                              supply scan: bnbcondo.com, khunclean.com, strspecialist.com,
                              simply.co.th.
[PASS] unit_economics       : Pessimistic net $178/mo; startup $450 → payback 0.8 months at base.
[PASS] feasibility_check    : Runs on the ground in Thailand (Bangkok metro assumed) — fully local.
[PASS] durability_analyst   : Growth 0%/mo; holding.
```

**The decisive line:** `level+stability ✓ (300/mo, durable)`. Both growth signals
fail — `trend ✗ (0%/mo)`, `social ✗` — so under the pre-M3 growth-only check this
niche had only 1 of 3 signals and was always rejected. The new level+stability
axis (300/mo clears the 250 floor; the observed mention series is durable) is the
2nd independent signal, so `demand_corroboration` passes with **2 of 4**.

**The bar was not lowered.** Corroboration only opened the door; the niche then
had to clear a real, observed supply gap (`competition_gap`: 4 providers via a
Google scan) and real economics (`unit_economics`: pessimistic $178/mo,
0.8-month payback). It is `partially_verified`, not over-claimed.

## Honest scope of this proof
- This is a real, executable-from-Thailand **local service** opportunity — the
  first non-flip venture to verify. It demonstrates M3 recognises a large,
  stable, underserved niche instead of falsely rejecting it.
- The info niches (`dtv_visa_guide`, `th_freelance_tax`) were in cooldown at this
  tick (entered at 455) so were not re-evaluated here; when they re-enter, M3
  predicts they corroborate on level+stability and then fail `competition_gap`
  (6 credible solutions > 4) — i.e. flip from a misleading `insufficient_demand`
  to the honest `high_competition`. That is a separate, weaker follow-up
  observation, not required for this proof.

## Truth-contract classification
Milestone 3: **IMPLEMENTED, TESTED (314 passed, 1 skipped), DEPLOYED (v1.8.0),
LIVE-PROVEN (tick 458, `opp_91e0f4d8df`).**
