# ◆ Opportunity OS

**An opportunity-intelligence platform.** Not another chatbot: a fleet of research agents
observes market signals 24/7, notices anomalies, investigates *why*, verifies candidates
through an independent council, prices them down to the last baht of fees/shipping/tax,
scores them, writes the execution playbook — and then keeps re-verifying them so stale
advice dies the moment the market turns.

The user never asks *"give me a business idea."* The platform wakes up and says:

> **Good morning. I found 12 opportunities worth your attention today** — 2 new since the
> last cycle. 3 previously published opportunities were invalidated as market conditions
> changed. 6 candidates were investigated and rejected before reaching you.

![Opportunity OS dashboard](docs/dashboard.png)

Built for an operator based in **Thailand** 🇹🇭 — every opportunity is screened for
buy/sell feasibility from Thailand (proxy services for JP marketplaces, Thai import
VAT/duty, Thailand Post/EMS routes, Payoneer/Wise/PromptPay rails, CN22 paperwork,
and post-2025 US de-minimis duty risk priced into the pessimistic case).

---

## Quickstart

```bash
pip install -r requirements.txt

python run.py serve            # dashboard + API at http://127.0.0.1:8000
                               # agents auto-run a research cycle every 180s
```

CLI, for the cron-driven lifestyle:

```bash
python run.py cycle -n 3       # run three research cycles right now
python run.py brief            # print the morning briefing to the terminal
python run.py reset            # wipe state and re-seed the demo world
python -m pytest tests/ -q     # 35 tests
```

No database server, no build step, no API keys needed to try it: state is SQLite
(`data/`, auto-created), the frontend is dependency-free vanilla JS, and demo mode runs
on a built-in simulated market (see **Demo mode & honesty** below).

---

## How a cycle works

One research cycle ≈ one market day:

```
        13 scanner agents                    the "why?" engine
  ┌──────────────────────────┐        ┌─────────────────────────────┐
  │ eBay US · Amazon US      │        │ What moved?                 │
  │ Yahoo!Auctions JP        │ signal │ Why is it moving?           │
  │ Mercari JP · Etsy        ├──────► │ Where is inventory?         │
  │ Shopee TH · Lazada TH    │        │ Executable from Thailand?   │
  │ Facebook MP TH · Kaidee  │ anomaly│ Real profit after fees/tax? │
  │ Reddit · TikTok · X      ├──────► │ How many units? Who         │
  │ Google Trends · News     │        │ competes? How long?         │
  └──────────────────────────┘        └──────────────┬──────────────┘
                                                     │ candidate
     ┌───────────────────────────────────────────────▼──────────────┐
     │  VERIFICATION COUNCIL — independent re-checks, any critical  │
     │  failure vetoes: price_verifier · supply_verifier ·          │
     │  demand_estimator · fee_auditor · tax_auditor ·              │
     │  competition_analyst (reliability-weighted consensus)        │
     └───────────────────────────────────────────────┬──────────────┘
                                                     │ verified only
     ┌───────────────────────────────────────────────▼──────────────┐
     │  ECONOMICS GATES: base margin ≥ 10% · pessimistic scenario   │
     │  (−5% price, +15% shipping, +fees, US destination duty,      │
     │  3% mishap reserve) must STILL be profitable                 │
     └───────────────────────────────────────────────┬──────────────┘
                                                     │
        score (10 weighted factors) ► playbook ► automation plan
                                                     │
     ┌───────────────────────────────────────────────▼──────────────┐
     │  PUBLISH … then RE-VERIFY every active opportunity on every  │
     │  later cycle → INVALIDATED with a reason when the edge dies  │
     └──────────────────────────────────────────────────────────────┘
```

The part most tools skip is the last box. Opportunities decay — a restock lands, a
dealer floods the market, a fad fades. Opportunity OS treats **constant re-validation
as the core feature**: in the demo you can literally watch the Pokémon 151 booster-box
flip get published after a US supply shock and then get invalidated seven cycles later
when the distributor restock hits, with the reason attached.

### Every opportunity ships with evidence, not vibes

* **Why-chain** — the full investigation Q&A, grounded in observed data
* **Cost waterfall** — acquisition, proxy fees, every shipping leg, Thai VAT/duty,
  marketplace commissions, payment/FX spread, in USD and THB, base *and* pessimistic
* **Verification council transcript** — each check, its evidence, its confidence
* **Score breakdown** — 10 factors (margin, demand, competition, risk, difficulty,
  capital, time, market size, repeatability, automation), weights visible
* **Thailand lens** — can you buy it, can you sell it, which proxy, which customs form
* **Playbook** — supplier, quantity, price caps, carrier, pre-written listing with a
  pricing rule, repricing/exit rule, payout route
* **Automation plan** — which steps a machine can run, and the human checkpoints

### Categories covered

Product arbitrage (JP auctions → eBay US, TH exclusives → US, thrift → export),
digital builds (micro-SaaS, creator tools), info products (incl. Thai-language niches
like ภาษีฟรีแลนซ์ guides), local services (Bangkok), and B2B gaps (e.g. EV charger
installation partnerships).

### The learning loop

Record what actually happened (`POST /api/opportunities/{id}/outcome`, or the form in
the dashboard). Outcomes update:

* **confidence calibration** — an over-promising system automatically gets humbler,
* **source/verifier reliability** — scanners that fed winning calls gain consensus weight,
* **scoring weights** — fail on competition and the competition factor weighs more.

### Revenue model (implemented as real gates)

| Plan | Gets |
|---|---|
| Free | 3 verified opportunities / week, no playbooks |
| Pro | full daily feed + playbooks + automation plans |
| Team | Pro + API access + shared workflows |
| Enterprise | custom industry monitoring |

Switch the plan picker in the dashboard and watch the feed gate itself.

---

## Demo mode & honesty

**Everything you see in demo mode is simulated.** The scanners consume a deterministic
in-process market (`opportunity_os/market/world.py`) with *causal* events — a US
sellout, a TikTok spike, a restock, a competitor pile-in — that the pipeline has to
discover from observable data. The simulator never hands answers to the agents; it just
makes the world observable, so the entire pipeline is exercised honestly end-to-end.
Fees, duty rates, shipping and FX are approximate early-2026 reference values.

**Going live is an interface swap, not a rewrite.** Scanners talk to the `DataSource`
protocol (`opportunity_os/market/__init__.py`). Implement it with real adapters —
suggested APIs per venue are listed in `opportunity_os/market/live.py` — and pass a
`LiveMarket` to the `Orchestrator`. The hard product problem (verify → price → gate →
re-verify → learn) is already built and tested. The demo UI carries a permanent
**SIMULATED FEED** badge; nothing here is financial advice.

---

## API

| Endpoint | What |
|---|---|
| `GET /api/briefing?plan=` | the morning briefing |
| `GET /api/opportunities?status=&category=&min_score=&q=&plan=` | the feed |
| `GET /api/opportunities/{id}?plan=` | full evidence package |
| `POST /api/opportunities/{id}/outcome` | close the learning loop |
| `POST /api/cycle` | run a research cycle now |
| `GET /api/agents` · `GET /api/signals` · `GET /api/anomalies` | the fleet's raw work |
| `GET /api/learning` | weights, calibration, reliabilities, adjustments |
| `GET /api/stats` · `GET /api/thailand` · `GET /api/plans` · `GET /api/health` | meta |

## Repo map

```
opportunity_os/
  config.py          gates, plans, operator profile (capital cap, TH home base)
  models.py          Signal → Anomaly → Investigation → Verification → Opportunity
  economics.py       venues, fee tables, shipping rate card, routes, cost waterfalls
  thailand.py        platform access from TH, import VAT/duty, customs, payment rails
  market/world.py    deterministic causal market simulator (demo mode)
  market/live.py     production extension point (real adapters go here)
  agents/scanners.py 13 scanner agents (venues, social, trends, news)
  agents/anomaly.py  z-scores, stock crashes, spreads, gaps, imbalances
  agents/investigator.py   the why-chain + candidate builder
  agents/verifiers.py      the verification council
  agents/scoring.py        10-factor explainable scoring engine
  agents/playbook.py       execution playbooks + automation plans
  agents/learning.py       outcomes → calibration/reliability/weight updates
  pipeline.py        the orchestrator (cycle + re-verification) and briefing
  db.py              SQLite store
  api.py             FastAPI app + plan gating + dashboard host
web/                 zero-build dashboard (index.html / style.css / app.js)
tests/               35 tests: economics, thailand, agents, pipeline, API
run.py               serve · cycle · brief · reset
```

## Roadmap to production

1. **Live connectors** (`market/live.py`) — start with eBay Browse + sold-history,
   Mercari/Yahoo JP via proxy-service accounts, pytrends; one venue pair is enough to
   go live for product arbitrage.
2. **Real fee/tariff sync** — marketplace fee schedules, Thai Customs tariff codes,
   carrier rate APIs, live FX.
3. **Execution integrations** — proxy-service auto-bid, listing APIs, label printing
   (the automation plans already mark what to wire).
4. **Accounts & billing** on top of the plan gates that already exist.
5. **Notifications** — LINE/Telegram push of the morning briefing (the briefing
   endpoint is already the payload).
