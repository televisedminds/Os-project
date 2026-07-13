# Going live — operator guide (Thailand edition)

This is the workflow for running Opportunity OS on **real market data** from your
own server. Total setup time: ~30–45 minutes, most of it waiting for eBay's
developer signup.

## What "live" means here — read this first

| Signal | Source | Nature |
|---|---|---|
| Sell-side price / listing depth / seller count | **eBay Browse API** (your free keys) | observed |
| Sell-through velocity (`sold_7d`) | inferred from listings disappearing between passes | **estimated** (bootstrapped from your watchlist until ~6 passes of history) |
| Buy-side quotes (Yahoo JP via Buyee, Shopee, Facebook groups) | **you**, in `watchlist.json` | user-supplied ground truth |
| Demand momentum for niches | **Reddit** post volume (your free script-app keys) | observed |
| News/why-context | **Google News RSS** | observed, keyless |
| FX (USD/THB/JPY) | **open.er-api.com** | observed, keyless |

Live mode is watchlist-driven: the fleet doesn't scan "all of eBay", it watches
the specific products and niches you curate, 24/7, and only publishes what
survives verification + pessimistic economics. Any source that fails degrades
for that cycle (shown in the dashboard badge tooltip and `/api/health`) instead
of crashing.

**Missing adapters (Yahoo JP, Mercari, Shopee scraping) are deliberate**: those
platforms have no public API; the manual-quote mechanism keeps the pipeline
honest until you add a connector (see "Adding venues" below).

---

## Step 0 — get the keys (one time, all free)

1. **eBay** (required — the sell-side engine)
   * https://developer.ebay.com → register → **Your Account → Application Keys**
   * Create a **Production** keyset. You need `App ID (Client ID)` and
     `Cert ID (Client Secret)`. No OAuth redirect setup needed — we use the
     client-credentials flow. Free tier = 5,000 Browse calls/day; the default
     cadence (each product every 30 min ≈ 48/day/product) fits ~100 products.
2. **Reddit** (recommended — powers niche/demand signals)
   * https://www.reddit.com/prefs/apps → *create another app* → type **script**
     → any name/redirect. You need the id under the app name + the secret.
   * Without it, Reddit's public endpoint is tried, but DigitalOcean IPs are
     usually blocked (403) — niches will rarely publish.
3. **Telegram** (optional — the briefing that finds you)
   * Message **@BotFather** → `/newbot` → copy the token.
   * Message your new bot once (anything), then:
     `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"` → find `"chat":{"id":123456789}`.

## Step 1 — dry-run on your laptop (optional but smart)

```bash
git clone <your-repo> && cd Os-project
pip install -r requirements.txt
cp watchlist.example.json watchlist.json      # edit me
cp .env.example .env                          # add keys
set -a; source .env; set +a
python run.py live-check                      # every line should be ✓
python run.py cycle --live                    # first observation pass
python run.py serve --live                    # dashboard on :8000, LIVE badge
```

## Step 2 — deploy on your DigitalOcean droplet

Your existing droplet is fine — the whole platform is one Python process +
SQLite (a $6/mo 1GB droplet has headroom).

```bash
ssh root@<droplet-ip>
git clone <your-repo> /opt/opportunity-os
cd /opt/opportunity-os
bash deploy/setup_droplet.sh      # creates venv, user, .env → tells you to fill it
nano .env                         # paste your keys
nano watchlist.json               # your products/niches
bash deploy/setup_droplet.sh      # re-run: live-check + installs systemd units
```

That installs:
* **opportunity-os.service** — the platform, auto-cycling every 30 min,
  restarted on failure, surviving reboots;
* **opportunity-os-brief.timer** — Telegram briefing daily at 07:00 Bangkok.

Watch it work: `journalctl -u opportunity-os -f`

**Dashboard access (important):** the server binds to 127.0.0.1 because there
is no login screen. From your machine:

```bash
ssh -L 8000:localhost:8000 root@<droplet-ip>
# then open http://localhost:8000  → header shows a green LIVE FEED badge
```

(If you want it on the open internet, put Caddy in front:
`caddy reverse-proxy --from your.domain --to localhost:8000` + `basic_auth` —
don't expose it bare.)

## Step 3 — the daily operator loop

**Morning (2 minutes).** The 07:00 Telegram briefing arrives: *"I found N
opportunities worth your attention today."* Open the dashboard for anything
interesting; read the why-chain, the cost waterfall (both scenarios), and the
verification transcript before believing anything.

**Acting on a flip.** Follow the playbook steps — it already knows your route
(proxy purchase for JP, Thailand Post vs DHL by weight, CN22, the pre-written
listing with a price floor, Payoneer repatriation). Buy at or below the price
cap; if the cap is gone, the opportunity is gone — don't chase.

**Keep manual quotes honest (5 min, ~twice a week).** Your Buyee/Shopee/
Facebook buy-side prices are inputs the whole engine prices from. Re-check
them, edit `watchlist.json` (also `solution_count`/`providers` on niches as you
scout competitors) — the file is re-read every cycle, no restart needed.

**Close the loop (always).** When a flip finishes or dies, record the outcome
(dashboard form or `POST /api/opportunities/{id}/outcome`). This is not
bookkeeping theater — it retunes scoring weights, source reliability and the
confidence calibration. The platform gets less wrong only if you feed it truth.

**Growing the watchlist.** Add products where you have a real buy-side edge
(TH exclusives, JP proxy access, local thrift). One product = one JSON block.
Start with 5–15 products; expand what earns.

## What to expect

* **Pass 1–5 (first day):** baselines recording; nothing publishes. This is
  correct — z-scores and velocity estimates need history.
* **Day 2–3:** first verified flips appear where your manual buy quote vs
  observed eBay pricing clears the pessimistic waterfall; niches publish once
  Reddit momentum corroborates.
* **Ongoing:** re-verification every 30 min; opportunities die with a reason
  (spread collapsed / inventory gone / demand faded) instead of going stale.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `live-check`: eBay ✗ "invalid_client" | wrong keyset — use **Production** App ID + Cert ID, not sandbox |
| `live-check`: Reddit ✗ 403 | expected without keys on a droplet — create the script app |
| Nothing publishes after 3+ days | spreads genuinely too thin (good — the gate works) or `sold_7d` too low: check demand_estimator evidence in a rejected candidate (`Last cycle` tab) |
| `database was created in 'demo' mode` | demo and live never share a DB — live defaults to `data/live.db`; keep it that way |
| Dashboard empty after reboot | `systemctl status opportunity-os`; journal shows adapter errors per cycle |
| eBay quota worries | calls/day ≈ products × (86400 / auto-cycle). Raise `--auto-cycle` or trim the watchlist |

## Adding venues (when you're ready to grow)

Each new sell-side connector = one class in `opportunity_os/market/adapters.py`
returning `{price, stock, sellers, item_ids}` for a query, registered in
`build_adapters()`, plus the venue in your watchlist `queries`. The rest of the
pipeline (routes, Thai VAT/duty, verification, playbooks) already handles every
venue in `economics.VENUES`. Realistic next steps: an Etsy v3 adapter (official
API, good for the handmade/export angle) and a Buyee/Yahoo JP quote importer.
Respect each platform's ToS and rate limits — that's part of "verified" too.
