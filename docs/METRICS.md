# Before / After — same scan budget (Phase 12, item 15)

All numbers below are from **demo mode** (a deterministic simulated market), so
they are reproducible and use an identical scan budget before and after. Demo
is a stand-in for the real fleet; live numbers scale with API access, but the
*shape* of the change — diversity, honesty, dedup, execution — is what matters.

Reproduce: `OOS_MODE=demo python run.py cycle -n 20` then
`python run.py diagnose_sources` (and the type funnel via
`GET /api/diagnostics/funnel`).

## Opportunity discovery

| Metric | Before (v0.6, "3 opportunities") | After (v0.12) |
|---|---|---|
| Verified active opportunities | ~3 | ~14 |
| Opportunity **types** | 1 (product flip) | 5 — `product_arbitrage`, `refurbish`, `local`, `micro_saas`, `info` |
| Anomalies / cycle | ~5 | ~50 |
| Investigated candidates / cycle | ~5 | ~20 |
| Non-flip generators producing real candidates | 0 | 3+ (venture family + micro-SaaS + refurbishment) |

## Honesty / quality (new — did not exist before)

| Metric | After (v0.12) |
|---|---|
| Verification levels | discovered / partially / multi-source / execution-ready / invalidated |
| Single-source opportunities labelled + capped | yes (never "execution ready" on one marketplace's asking prices) |
| Evidence ledger per opportunity | every claim tagged observed/estimated/calculated/assumption/unknown/ai |
| Thailand executability checked before "execution ready" | yes (10 questions, unknowns block) |
| Duplicate inflation visible | `unique_theses` vs `verified` + inflation ratio |
| Rejections classified | fixed taxonomy (margin/thailand/supply/confidence/stale/…) |

## Deduplication (Phase 6)

Demo happens to produce distinct products (inflation 1.0×), but the mechanism is
proven directly: **20 underpriced listings of one model collapse to 1 thesis**
carrying the depth (qualifying-listing count, price range, inventory, recommended
qty) — see `tests/test_execution.py::test_twenty_listings_of_one_model_collapse`.
In live eBay data, where many listings share a model, this is where the count
stops being inflated.

## Execution (Phase 13 — new)

Every opportunity has a persistent, isolated chat that drives it from research
to realised profit. One full lifecycle (demo):

```
"Which exact product?"        → names the product, warns to match the model number
"Is $25 a good buying price?" → SKIP: net -$9.62 (-16%) — edge gone at that price
"Where do I sell / TH?"       → Thailand executability answers (register, payout, customs)
generate_listing              → a draft (never published by the assistant)
record buy $25 → sell $78     → realised P&L +$53, state = sold, next action known
```

## Test coverage

| | Before | After |
|---|---|---|
| Passing tests | 88 | 179 |
| New test modules | — | security, diagnostics, generators, evidence, execution, chat, acceptance |

## What is still honest-by-omission

* **Sold comps**: eBay Browse gives asking prices only; the ledger says so, and
  execution-ready requires sold comps or 3+ sources. No fake "sold" data.
* **Executability rules** are reference values, not a live Thai Customs / policy
  sync; anything unconfirmed is `unknown`, which blocks execution-ready.
* **The chat needs a live data source** for its refresh/inventory tools and an
  Anthropic key for natural-language phrasing; without them the deterministic
  substance (matching, price decisions, records, state) still works and is
  labelled honestly.
