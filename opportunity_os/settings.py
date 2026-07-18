"""In-app API key management — paste keys in the dashboard, no terminal.

Keys entered in the ⚙ Keys tab are stored in SQLite (meta key "api_keys") and
applied onto the live Config object immediately — adapters read the config at
call time, so no restart is needed. Precedence: a key saved in the app wins;
an empty app value falls back to the .env / environment value; both empty =
not set. `.env` keeps working exactly as before for people who prefer it.

Security rules, non-negotiable:
* raw key values are NEVER returned by any endpoint — only masked previews;
* values are stored only in the local SQLite file, never logged.
The dashboard itself has no login — anyone who can open it can SAVE keys and
trigger tests, which is why live deployments should sit behind the bundled
HTTPS+password proxy (deploy/setup_https_dashboard.sh). The UI warns when it
detects plain HTTP.
"""

from __future__ import annotations

import os

META_KEY = "api_keys"

# env var → (config attribute, label, secret?, group, signup url, one-line help)
KEY_FIELDS: dict[str, dict] = {
    "EBAY_CLIENT_ID": {
        "attr": "ebay_client_id", "label": "eBay Client ID (App ID)", "secret": False,
        "group": "Market data — free keys",
        "url": "https://developer.ebay.com",
        "help": "Free. developer.ebay.com → Application Keys → Production. If it says 'Keyset "
                "currently disabled': click 'exemption' → toggle ON 'Not persisting eBay data' "
                "→ Confirm → pick a reason → Submit. Enables instantly.",
    },
    "EBAY_CLIENT_SECRET": {
        "attr": "ebay_client_secret", "label": "eBay Client Secret (Cert ID)", "secret": True,
        "group": "Market data — free keys",
        "url": "https://developer.ebay.com",
        "help": "The 'Cert ID' shown next to the App ID in the same Production keyset.",
    },
    "REDDIT_CLIENT_ID": {
        "attr": "reddit_client_id", "label": "Reddit Client ID", "secret": False,
        "group": "Market data — free keys",
        "url": "https://www.reddit.com/prefs/apps",
        "help": "Free, personal use. reddit.com/prefs/apps → create app → type 'script'. In the "
                "required 'redirect uri' box just type http://localhost:8080 — it is never used. "
                "The id is the short string under the app name.",
    },
    "REDDIT_CLIENT_SECRET": {
        "attr": "reddit_client_secret", "label": "Reddit Client Secret", "secret": True,
        "group": "Market data — free keys",
        "url": "https://www.reddit.com/prefs/apps",
        "help": "The 'secret' field of the same script app.",
    },
    "SERPER_API_KEY": {
        "attr": "serper_api_key", "label": "Serper API key (Google search)", "secret": True,
        "group": "Market data — free keys",
        "url": "https://serper.dev",
        "help": "Free: 2,500 searches at signup, no card (after that the minimum top-up is $50 — "
                "the free tier is the value). Measures niche demand via Google while your Reddit "
                "key is pending. 2-minute signup with a Google account.",
    },
    "ANTHROPIC_API_KEY": {
        "attr": "anthropic_api_key", "label": "Claude / Anthropic API key", "secret": True,
        "group": "AI brain + selling kits",
        "url": "https://console.anthropic.com",
        "help": "console.anthropic.com → API keys → Create key, then add ~$5 credit under "
                "Billing. Powers discovery judgment and one-tap selling kits (a few cents/day).",
    },
    "TELEGRAM_BOT_TOKEN": {
        "attr": "telegram_bot_token", "label": "Telegram bot token", "secret": True,
        "group": "Phone alerts",
        "url": "https://t.me/BotFather",
        "help": "Message @BotFather → /newbot → copy the token. Then send your new bot any "
                "message once so it can reply to you.",
    },
    "TELEGRAM_CHAT_ID": {
        "attr": "telegram_chat_id", "label": "Telegram chat id", "secret": False,
        "group": "Phone alerts",
        "url": "https://t.me/BotFather",
        "help": "Open api.telegram.org/bot<TOKEN>/getUpdates in a browser after messaging your "
                "bot — your chat id is the number in \"chat\":{\"id\":…}.",
    },
    "SCRAPINGDOG_API_KEY": {
        "attr": "scrapingdog_api_key", "label": "ScrapingDog API key", "secret": True,
        "group": "Optional — auto Shopee prices",
        "url": "https://www.scrapingdog.com",
        "help": "Optional, paid. Pay-as-you-go: $10 buys 25,000 credits that never expire "
                "(≈ months of Shopee watching). Manual quotes keep working without it.",
    },
}


def mask(value: str) -> str:
    """Preview a stored value without revealing it."""

    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return f"{v[:3]}…{v[-4:]} ({len(v)} chars)"


def stored(store) -> dict:
    return store.meta_get(META_KEY, {}) or {}


def load_into(cfg, store) -> list[str]:
    """Apply app-saved keys onto the running config. Returns the env names applied."""

    applied = []
    saved = stored(store)
    for env, spec in KEY_FIELDS.items():
        val = (saved.get(env) or "").strip()
        if val:
            setattr(cfg, spec["attr"], val)
            applied.append(env)
    return applied


def save(cfg, store, updates: dict) -> list[str]:
    """Merge updates into the stored keys and apply them to the live config.
    An empty value clears the app-saved key and falls back to the environment.
    Raises ValueError on unknown names. Returns the env names that changed."""

    unknown = [k for k in updates if k not in KEY_FIELDS]
    if unknown:
        raise ValueError(f"unknown key(s): {', '.join(sorted(unknown))} — "
                         f"valid: {', '.join(KEY_FIELDS)}")
    saved = stored(store)
    changed = []
    for env, raw in updates.items():
        val = (raw or "").strip()
        spec = KEY_FIELDS[env]
        if val:
            if saved.get(env) != val:
                changed.append(env)
            saved[env] = val
            setattr(cfg, spec["attr"], val)
        else:
            if env in saved:
                changed.append(env)
            saved.pop(env, None)
            setattr(cfg, spec["attr"], os.environ.get(env, ""))   # fall back to .env
    store.meta_set(META_KEY, saved)
    return changed


def status(cfg, store) -> list[dict]:
    """Masked, safe-to-serve view of every managed key."""

    saved = stored(store)
    out = []
    for env, spec in KEY_FIELDS.items():
        app_val = (saved.get(env) or "").strip()
        env_val = (os.environ.get(env) or "").strip()
        current = app_val or env_val
        out.append({
            "name": env, "label": spec["label"], "help": spec["help"], "url": spec["url"],
            "group": spec["group"], "secret": spec["secret"],
            "set": bool(current),
            "source": "app" if app_val else ("env" if env_val else ""),
            "masked": mask(current),
        })
    return out


def run_checks(cfg) -> list[dict]:
    """Live-test every service whose keys are set (network). Services without
    keys are skipped, so with nothing configured this makes no calls at all."""

    out = []

    def add(id_, label, ok, note):
        out.append({"id": id_, "label": label, "ok": ok, "note": note[:300]})

    if cfg.ebay_client_id and cfg.ebay_client_secret:
        from .market.adapters import EbayAdapter
        ok, note = EbayAdapter(cfg).check()
        add("ebay", "eBay Browse API", ok, note)
    if cfg.reddit_client_id and cfg.reddit_client_secret:
        from .market.adapters import RedditAdapter
        ok, note = RedditAdapter(cfg).check()
        add("reddit", "Reddit", ok, note)
    if cfg.serper_api_key:
        from .market.adapters import SerperAdapter
        ok, note = SerperAdapter(cfg).check()
        add("serper", "Serper (Google search demand)", ok, note)
    if cfg.anthropic_api_key:
        from .ai import AIClassifier
        ok, note = AIClassifier(cfg).check()
        add("anthropic", "Claude (AI brain + kits)", ok, note)
    if cfg.scrapingdog_api_key:
        from .market.adapters import ScrapingDogShopeeAdapter
        ok, note = ScrapingDogShopeeAdapter(cfg).check()
        add("scrapingdog", "ScrapingDog / Shopee TH", ok, note)
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        from .notify import send_telegram
        ok, note = send_telegram(cfg, "⚙️ Opportunity OS: test message — your alerts work.")
        add("telegram", "Telegram alerts", ok,
            "test message sent — check your phone" if ok else note)
    return out
