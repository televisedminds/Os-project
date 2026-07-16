# API keys — which ones, why, where, and exactly how

Plain-language guide to every key this platform can use. **You don't need the
terminal anymore:** open the dashboard → **⚙ Keys** tab → paste → Save. Keys
apply instantly (no restart) and are stored only in your local database. The
`.env` file still works too; a key saved in the app wins over `.env`.

> **Do this first if your dashboard is on the public internet:** run
> `bash deploy/setup_https_dashboard.sh` on your server. It adds HTTPS and a
> password. Without it, anything you type (including keys) travels unencrypted,
> and strangers who find the page could change your settings. The Keys tab
> shows a red warning when it detects this.

## Priority order (what to get, in order)

| # | Key | Cost | Unlocks | Time |
|---|-----|------|---------|------|
| 1 | **eBay** (2 values) | Free | Sell-side prices for US flips + eBay discovery. Without it, cross-border deals stay dark. | ~15 min |
| 2 | **Claude / Anthropic** | ~$5 credit lasts months | The AI brain (judges every discovered candidate) **and** one-tap Selling Kits (your TH/EN listings, written for you). Biggest quality jump per baht. | ~10 min |
| 3 | **Telegram** (2 values) | Free | Morning briefing + instant push the moment a new deal verifies. | ~5 min |
| 4 | **Reddit** (2 values) | Free | Demand signals for niches + business-gap discovery (r/Flipping, r/SomebodyMakeThis…). *May involve a wait — see below.* | 5 min–weeks |
| 5 | **ScrapingDog** | $10 one-time (pay-as-you-go) | Automatic Shopee TH prices instead of typing quotes. | ~10 min |

Skip for now: SerpAPI (expensive for what it adds here), X/Twitter API,
anything else. Keepa is a "later" option — see the bottom.

---

## 1. eBay Browse API — free, and the one with a hidden trap

**What it powers here:** live sell-side prices, listing counts, sellers and
exact item links for every eBay-tracked product, plus the eBay discovery sweep.

**Steps:**
1. Go to [developer.ebay.com](https://developer.ebay.com) → **Register**. Your
   normal eBay buyer login works; accept the API License Agreement.
2. Top-right menu → **Your Account → Application Keys**.
3. Under **Production** (not Sandbox!) click **Create a keyset**. eBay may ask
   you to verify your phone/identity.
4. **⚠ The trap:** your new Production keyset starts **disabled**. eBay
   requires every app to either subscribe to "marketplace account deletion"
   notifications or **opt out**. On the Application Keys page, click the
   alert/link next to your keyset → choose **opt out** (this tool only reads
   public listing data and stores no eBay users' personal data) → give that
   one-line reason → submit. The keyset turns usable within minutes.
5. Copy **App ID (Client ID)** → paste into *eBay Client ID* in the Keys tab.
   Copy **Cert ID (Client Secret)** → *eBay Client Secret*. Save → **🧪 Test**.

**Limits:** the default free allowance (thousands of calls/day) is far more
than this app uses (a few hundred/day). No payment method needed, ever.

## 2. Claude / Anthropic — the AI brain + selling kits

**What it powers here:** ① every discovery sweep gets judged by Claude
("sports news — drop" / "sealed collectible with resale demand — keep"), and
② the **✨ Selling Kit** button: ready-to-paste eBay listing (EN), Shopee/
TikTok listing (TH), and the Thai message to send the source seller.

**Steps:**
1. [console.anthropic.com](https://console.anthropic.com) → sign up (email or
   Google).
2. Left menu → **Billing** → add credit. The minimum (~$5) is plenty: the
   brain runs ~8 small batched calls/day and a kit costs a few cents, so $5
   typically lasts months.
3. Left menu → **API keys** → **Create key** → name it `opportunity-os` →
   copy the `sk-ant-…` value **immediately** (it's shown only once).
4. Paste into *Claude / Anthropic API key* in the Keys tab → Save → Test.

**Cost control:** the default model gives the best judgment. If you want it
several times cheaper, set `OOS_AI_MODEL=claude-haiku-4-5` in `.env`.

## 3. Telegram — free alerts to your phone

**What it powers here:** the 07:00 morning briefing **and** an instant push
the moment a *new* opportunity verifies (deal windows are short — this alone
can be worth the 5 minutes).

**Steps:**
1. In Telegram, open [@BotFather](https://t.me/BotFather) → send `/newbot` →
   pick any name → copy the **token** (`123456:ABC-…`).
2. Open your new bot's chat and send it any message (e.g. "hi") — bots can't
   message you first.
3. In a browser open
   `https://api.telegram.org/bot<YOUR-TOKEN>/getUpdates`
   and find `"chat":{"id":123456789…}` — that number is your **chat id**.
4. Paste both into the Keys tab → Save → **🧪 Test** (you should receive a
   test message on your phone immediately).

## 4. Reddit — free, but the rules changed in late 2025

**What it powers here:** demand measurement for niches (posts/day for your
tracked topics) and the business-gap discovery sweep (r/Flipping,
r/SomebodyMakeThis, r/sweatystartup, r/SaaS…).

**Steps (try this first — takes 5 minutes when it works):**
1. Log in at reddit.com → go to
   [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps).
2. **Create another app** → type **script** → any name → redirect URI
   `http://localhost:8080` → Create.
3. The **client id** is the short string under the app's name (top-left of
   its box); the **secret** is labeled. Paste both into the Keys tab.

**What changed:** Reddit tightened API access around November 2025
("Responsible Builder" rules). The free tier still exists — 100 requests/
minute per app, personal non-commercial use (this tool uses a tiny fraction
of that) — but several reports say **new** app registrations can require
manual approval with a multi-week wait, and some accounts can't self-serve
at prefs/apps anymore. If step 2 is blocked for you or your new app returns
errors: submit Reddit's API access request (linked from their Developer
Platform / Data API help pages), describe it as *personal, non-commercial
market research*, and wait for approval. Everything else in this platform
keeps working while you wait — Reddit only adds signal, it isn't required.

## 5. ScrapingDog — $10 one-time, automates your Shopee quotes

**What it powers here:** automatic Shopee TH prices/sold-counts/stock for any
product with a `shopee_th` query — replacing the numbers you type by hand.

**Steps:**
1. [scrapingdog.com](https://www.scrapingdog.com) → sign up. New accounts get
   a small pile of **free trial credits with no credit card** — enough to see
   it working.
2. When the trial runs out, don't buy the monthly plan — use
   **Pay-As-You-Go: $10 = 25,000 credits that never expire.** This app scrapes
   each watched Shopee product only every 4th cycle (~12 checks/day/product),
   so $10 realistically lasts months for a handful of products.
3. Dashboard → copy your API key → paste into the Keys tab → Save.

**Honesty note:** scraping can break whenever Shopee changes their site, and
may conflict with platform terms — risk is usually a block, and your manual
quotes always keep working as the fallback.

## Later, only if you go deep on Amazon flips: Keepa

[Keepa](https://keepa.com) is the gold standard for Amazon price + sales-rank
history. The practical entry point is their **€19/month subscription**, which
includes API access at 1 request/minute — enough to watch a small list. Their
dedicated API plans (€49+/month) are overkill here. Not integrated yet — tell
Claude when you have the key and the adapter gets built like the others.

---

## Where keys live & how to remove them

Keys saved in the app are stored in your local SQLite database
(`data/live.db` on a droplet) and **never sent back to the browser** — the
Keys tab only ever shows a masked preview like `sk-…9f2 (108 chars)`. "Clear"
on a key falls back to whatever `.env` has. If you ever suspect a key leaked:
delete/rotate it at the provider (eBay/Anthropic/etc.) first, then paste the
new one.
