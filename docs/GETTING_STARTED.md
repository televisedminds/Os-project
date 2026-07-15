# Getting started (no experience needed)

This guide uses simple words on purpose. Follow it top to bottom.

## What is this thing?

Opportunity OS is a robot researcher. You tell it what to watch (products,
niches). Every 30 minutes it checks prices and demand, does ALL the math —
platform fees, shipping, Thai import tax (7% VAT + duty), delivery to the
buyer — and only shows you deals that are **still profitable after everything**.
Each deal comes with a step-by-step plan ("playbook") telling you exactly what
to do, and a morning message on Telegram.

It finds more than product flips: it also spots **service gaps** (e.g. cleaning
demand in Bangkok), **info products** (guides Thai people are searching for),
**digital tools**, and **B2B shortages**. Products are just the easiest to start with.

---

## Question 1: "Does it work WITHOUT API keys?"

**Yes — and for your Thai use-case, mostly without keys.** Here is the honest table:

| What you want | Needs a key? | How it works |
|---|---|---|
| Try everything with fake data (demo) | ❌ no | built-in simulated market |
| **Thai arbitrage: Facebook → Shopee, AliExpress → TikTok Shop, etc.** | ❌ **no** | Thai platforms have no public price API, so **you type in the prices you see** (2 minutes), and the robot does all fee/tax/shipping math, checks the deal, sizes it, writes the plan, and re-checks it every cycle |
| Live exchange rates (THB/USD/JPY) | ❌ no | fetched automatically |
| News context ("why is this trending?") | ❌ no | fetched automatically |
| Watching **eBay US** prices automatically (sell-to-America deals) | ✅ free key | eBay developer account (guide below) |
| Demand signal from **Reddit** (for niches) | ✅ free key | Reddit app (guide below) |
| **Discovery** — the fleet finds NEW things to watch by itself (products AND business ideas: tools, info products, services) | partly | Google Trends + Hacker News are keyless; eBay + Reddit keys make it much more productive |
| **AI brain** — Claude judges every discovered candidate | ✅ paid key (~$5 lasts months) | console.anthropic.com (guide below) |
| Telegram morning message | ✅ free bot | @BotFather (guide below) |

So: **you can start today with zero keys.** Keys only make more of it automatic.

### Important honesty note about Shopee / TikTok Shop / Facebook

These platforms do not let robots read their prices (no public API; scraping
breaks their rules and gets blocked). So the workflow is: you browse like a
normal person, you see "gimbal ฿1,790, sold 20 this week", and you put those
numbers in one file. Every Thai listing shows its **"ขายแล้ว / sold" counter** —
that's your demand number. The robot cannot know a price you never gave it,
but it will never lie to you about profit either — everything it computes is
from real fees, real tax rules, real FX, and the numbers you saw with your
own eyes.

---

## Part A — try it in 3 commands (demo, fake data, zero keys)

You need Python 3.11+ installed. Then:

```bash
git clone <your-repo-url> && cd Os-project
pip install -r requirements.txt
python run.py serve
```

Open http://127.0.0.1:8000 — you'll see the full product working with a
simulated market (orange "SIMULATED" badge). Click things. Break nothing.

## Part B — go live with ZERO keys (Thai e-commerce mode)

```bash
cp watchlist.example.json watchlist.json
python run.py cycle --live        # first pass
python run.py serve --live        # dashboard with green LIVE badge
```

Now open `watchlist.json` in any text editor. It already contains working
examples of exactly what you asked for:

**Recipe 1 — China import → sell in Thailand** (AliExpress → Shopee/TikTok Shop):
```json
{
  "id": "phone_gimbal_cn",
  "name": "Foldable phone gimbal (creator kit) — CN import",
  "category": "electronics",
  "weight_kg": 0.6,
  "manual_listings": {
    "aliexpress":     { "price_usd": 23.0, "stock": 500, "sellers": 40, "sold_7d": 30 },
    "shopee_th":      { "price_thb": 1790, "stock": 60, "sellers": 18, "sold_7d": 20 },
    "tiktok_shop_th": { "price_thb": 1990, "stock": 45, "sellers": 12, "sold_7d": 16 }
  }
}
```
The robot answers: buy at $23, after CN→TH shipping + Thai duty + 7% VAT +
Shopee fee + courier to buyer, you keep **≈$16/unit (≈฿540), 31% margin**, and
it still survives the pessimistic case. It also tells you TikTok Shop signup
needs a Thai ID card + Thai bank account.

**Recipe 2 — Thai domestic flip** (Facebook Marketplace → Shopee):
copy the `used_iphone13_th` block — buy ฿11,300 on FB, sell ฿14,400 on Shopee,
robot shows ≈฿1,850 profit after Shopee's 8% and delivery, plus the "meet at
BTS, check battery ≥85%" checklist.

**Recipe 3 — export to America** (needs the free eBay key): the `moonbreon`
and `gba` examples watch eBay prices automatically and compare against your
Buyee/Facebook sourcing quotes.

**Recipe 4 — not a product at all**: the `niches` section watches things like
"Thai freelancer tax guide" demand. With the Reddit key it measures demand
automatically; the robot then models revenue, startup cost, payback months.

Edit numbers → save → the next cycle picks it up automatically. Update your
quotes about twice a week (it takes ~2 minutes).

## Part C — the free keys (only when you want them)

**eBay (15 minutes, unlocks automatic USA sell-side watching):**
1. Go to **developer.ebay.com** → *Register* (normal eBay login works).
2. Accept the API agreement. Go to **Your Account → Application Keys**.
3. Under **Production**, click *Create a keyset* (they may ask you to verify).
4. Copy **App ID (Client ID)** and **Cert ID (Client Secret)** into `.env`
   (`cp .env.example .env` first). That's all — no other setup.

**Reddit (5 minutes, unlocks demand signals for niches):**
1. Log in to reddit.com → go to **reddit.com/prefs/apps**.
2. *Create another app* → choose **script** → any name, redirect `http://localhost`.
3. The id is under the app name; the secret is labeled. Put both in `.env`.

**Telegram (5 minutes, morning briefing on your phone):**
1. In Telegram, message **@BotFather** → send `/newbot` → copy the token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR-TOKEN>/getUpdates` in a browser —
   find `"chat":{"id":123456789}` → that number is your chat id. Both go in `.env`.

**Claude / Anthropic (10 minutes, ~$5 — the "AI brain", biggest quality upgrade):**

Without this key, the discovery engine filters candidates by keyword matching
(dumb but free). With it, Claude reads every candidate the fleet finds and
judges it like an analyst would: *"sports news, nothing to sell — drop"* /
*"sealed collectible with resale demand — keep, raise score"*. Every verdict
comes with a reason you can read in the Discovery tab.

1. Go to **console.anthropic.com** → sign up → **API keys** → *Create key*.
2. Add ~$5 of credit (Billing). At the default cadence the AI runs about 8
   batched calls per day ≈ a few **cents** per day, so $5 lasts months.
3. Put the key in `.env` as `ANTHROPIC_API_KEY=sk-ant-...`.
4. Optional: `OOS_AI_MODEL=claude-haiku-4-5` makes it several times cheaper
   (slightly less careful judgment). The default model gives the best verdicts.

Then check everything: `set -a; source .env; set +a; python run.py live-check`
— you want ✓ on every line, including the discovery panel at the bottom
(`google_trends`, `reddit_discovery`, `ebay_discovery`, `ai_brain`).

## Optional: ScrapingDog — automatic Shopee TH prices

You asked: *"can a scraper like scrapingdog.com read the sites that have no
API?"* Yes — that's exactly what it is: a paid service (free trial credits,
then a subscription) that fetches web pages for you through rotating proxies,
so sites don't block your server. We built it in for **Shopee TH**: put your
key in `.env` as `SCRAPINGDOG_API_KEY=...` and add a Thai search to a product:

```json
"queries": { "shopee_th": "กันสั่นมือถือ gimbal พับได้" }
```

The robot then reads Shopee's real prices, sold-counts and stock by itself
(every 4th cycle by default, to save your credits ≈ 12 checks/day/product).

**Be honest with yourself about scraping before paying:**
1. It can **break any time** the website changes — then that source degrades
   until the parser is updated (your manual quotes keep working).
2. It **costs money per request** after the trial.
3. Scraping may conflict with a platform's terms of service. Risk is usually
   blocks, not more — but it's your account and your call.
4. The same trick can be extended to Lazada/TikTok Shop later — each site
   needs its own small parser in `opportunity_os/market/adapters.py`.

Manual quotes (typing the price you see) stay the free, always-works option.

## Doing all of this from your phone (Termius or any SSH app)

Termius (or any SSH app) connected to your droplet is the **same terminal** as
a laptop — every command below is typed the same way. Two phone-specific tips:

* **Keyboard:** commands are English letters/symbols only. If your keyboard is
  set to Thai, tap the globe icon (bottom-left of the keyboard) to switch back.
* **Ctrl key:** `nano` (the text editor used below) needs Ctrl+O to save and
  Ctrl+X to exit. Termius shows a `ctrl` button in the toolbar above the
  keyboard — tap `ctrl`, then tap the letter, instead of holding both at once.

Nothing about setup changes — you're typing into the exact same droplet.
Everything in Parts A–D below is the same command sequence whether you type
it from Termius on your phone or a laptop's terminal.

## Part D — put it on your DigitalOcean server (so it runs 24/7)

```bash
ssh root@<your-droplet-ip>
git clone <your-repo-url> /opt/opportunity-os && cd /opt/opportunity-os
bash deploy/setup_droplet.sh    # sets everything up, then asks you to fill .env
nano .env                       # paste keys (or leave empty for zero-key mode)
nano watchlist.json             # your products
bash deploy/setup_droplet.sh    # run again — installs the always-on service
```

Done. It now researches every 30 minutes forever, survives reboots, and sends
your Telegram briefing at **07:00 Bangkok time** daily. To see the dashboard
from your laptop: `ssh -L 8000:localhost:8000 root@<droplet-ip>` then open
http://localhost:8000.

## Can I actually register / ship / export? (your checklist)

The dashboard's **Thailand lens** tab answers this per platform, and every
opportunity repeats it. Summary:

| Platform | Buy from TH | Sell from TH | What you need to sell |
|---|---|---|---|
| Shopee TH / Lazada TH | ✓ | ✓ | Thai ID + Thai bank account |
| TikTok Shop TH | ✓ | ✓ | Thai ID + Thai bank, approval 1–3 days |
| Facebook MP / Kaidee | ✓ | ✓ | just an account (0% fees, PromptPay) |
| AliExpress / Taobao-style | ✓ (ships TH, 10–20 days) | ✗ | — |
| eBay / Etsy / Amazon US | ✓ | ✓ | passport/ID + Payoneer for USD payouts |
| Mercari / Yahoo JP | ✓ via proxy (Buyee) | ✗ (JP residents only) | — |

Every import into Thailand is priced with **7% VAT (charged even on cheap
parcels since 2024) + duty by category above ฿1,500** (electronics 5%, toys
10%, shoes 30%…). Every export playbook includes the CN22 customs form and
carrier choice. Profits show in **both USD and THB**.

## FAQ

* **"Nothing shows up in live mode!"** — Day 1 records baselines; deals appear
  once history builds (manual-quote deals can appear after 2–3 cycles). Also
  check the *Last cycle* tab: rejected candidates are listed **with the reason**
  (usually "loses money in the pessimistic case" — that's the robot protecting you).
* **"Where do I get sold_7d?"** — Shopee/Lazada/TikTok listings show a public
  sold counter. Monthly number ÷ 4. For Facebook, estimate from how fast posts disappear.
* **Taxes on your profit** — the playbooks remind you to log proceeds for Thai
  personal income tax. This tool computes deal economics, not your tax filing.
* **Is any of this financial advice?** — No. It's arithmetic plus verification.
  The final decision, and the sample order before a big order, is always yours.
