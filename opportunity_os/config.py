"""Runtime configuration for Opportunity OS."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
WEB_DIR = PROJECT_ROOT / "web"

# Subscription plans: the product sells actionable intelligence, not chat.
PLANS = {
    "free": {
        "label": "Free",
        "max_opportunities": 3,
        "playbooks": False,
        "api_access": False,
        "blurb": "3 verified opportunities per week.",
    },
    "pro": {
        "label": "Pro",
        "max_opportunities": None,
        "playbooks": True,
        "api_access": False,
        "blurb": "Daily verified opportunities with full execution playbooks.",
    },
    "team": {
        "label": "Team",
        "max_opportunities": None,
        "playbooks": True,
        "api_access": True,
        "blurb": "Everything in Pro plus shared workflows and API access.",
    },
    "enterprise": {
        "label": "Enterprise",
        "max_opportunities": None,
        "playbooks": True,
        "api_access": True,
        "blurb": "Custom monitoring for specific industries.",
    },
}


def _env(key: str, default: str):
    """default_factory helper — env vars must be read at *instantiation* time
    (a plain dataclass default freezes the value at import time, which breaks
    CLI flags like --live that set os.environ before building Config)."""

    return field(default_factory=lambda: os.environ.get(key, default))


@dataclass
class Config:
    """All tunables in one place. Environment variables override defaults."""

    db_path: Path = field(default_factory=lambda: Path(os.environ.get("OOS_DB", DATA_DIR / "opportunity_os.db")))
    world_seed: int = field(default_factory=lambda: int(os.environ.get("OOS_SEED", "1151")))
    default_plan: str = _env("OOS_PLAN", "pro")
    auto_cycle_seconds: int = field(default_factory=lambda: int(os.environ.get("OOS_AUTO_CYCLE_SECONDS", "0")))

    # "demo" runs the simulator; "live" runs real connectors over the watchlist.
    mode: str = _env("OOS_MODE", "demo")
    watchlist_path: Path = field(default_factory=lambda: Path(os.environ.get("OOS_WATCHLIST",
                                                                             PROJECT_ROOT / "watchlist.json")))
    http_timeout: float = field(default_factory=lambda: float(os.environ.get("OOS_HTTP_TIMEOUT", "20")))
    user_agent: str = _env("OOS_USER_AGENT", "OpportunityOS/0.3 (market research bot)")

    # Live connector credentials (all optional — missing ones degrade gracefully).
    ebay_client_id: str = _env("EBAY_CLIENT_ID", "")
    ebay_client_secret: str = _env("EBAY_CLIENT_SECRET", "")
    ebay_env: str = _env("EBAY_ENV", "production")                    # or "sandbox"
    reddit_client_id: str = _env("REDDIT_CLIENT_ID", "")
    reddit_client_secret: str = _env("REDDIT_CLIENT_SECRET", "")
    scrapingdog_api_key: str = _env("SCRAPINGDOG_API_KEY", "")
    # Scraped venues cost paid credits — fetch them only every Nth cycle.
    scrape_every_n_ticks: int = field(default_factory=lambda: int(os.environ.get("OOS_SCRAPE_EVERY", "4")))
    # Serper (google.serper.dev): web-search demand signal for niches — the
    # stand-in while Reddit approval is pending. Throttled hard: 2,500 free
    # credits should last weeks, not days.
    serper_api_key: str = _env("SERPER_API_KEY", "")
    serper_every_n_ticks: int = field(default_factory=lambda: int(os.environ.get("OOS_SERPER_EVERY", "12")))
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID", "")

    # AI brain (optional): Claude judges discovery candidates — filters noise,
    # fixes categories, rescores — instead of keyword matching. Needs a key
    # from console.anthropic.com. OOS_AI_MODEL=claude-haiku-4-5 runs cheaper.
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "")
    ai_model: str = _env("OOS_AI_MODEL", "claude-opus-4-8")
    ai_enabled: bool = field(
        default_factory=lambda: os.environ.get("OOS_AI", "1") not in ("0", "false", "no"))

    # Discovery engine (live mode): auto-find new products/niches to watch so
    # the fleet isn't limited to the hand-typed watchlist. Off in demo.
    discovery_enabled: bool = field(
        default_factory=lambda: os.environ.get("OOS_DISCOVERY", "1") not in ("0", "false", "no"))
    discover_every_n_ticks: int = field(default_factory=lambda: int(os.environ.get("OOS_DISCOVER_EVERY", "6")))
    # Sized against the free eBay allowance (5,000 calls/day): ~60 discovered
    # + watchlist ≈ 3.4k calls/day at the 30-min cadence — real headroom kept.
    discovery_scan_cap: int = field(default_factory=lambda: int(os.environ.get("OOS_DISCOVERY_SCAN_CAP", "100")))
    discovery_max_active: int = field(default_factory=lambda: int(os.environ.get("OOS_DISCOVERY_MAX", "60")))
    discovery_ttl_days: float = field(default_factory=lambda: float(os.environ.get("OOS_DISCOVERY_TTL_DAYS", "10")))
    discovery_trends_geo: str = _env("OOS_DISCOVERY_GEO", "US")

    # Home base: the operator is in Thailand. Every opportunity is assessed
    # for buy/sell feasibility from Thailand.
    home_country: str = "TH"
    home_timezone: str = "Asia/Bangkok"

    # Publication gates — nothing is shown to the user unless it clears these.
    min_consensus_confidence: float = 0.75
    min_margin_pct: float = 10.0          # base-scenario margin floor
    require_pessimistic_profit: bool = True
    anomaly_zscore: float = 2.0
    cross_venue_spread_pct: float = 0.25  # raw spread that triggers investigation

    # Operator profile used to size positions.
    capital_cap_usd: float = field(default_factory=lambda: float(os.environ.get("OOS_CAPITAL_CAP", "2000")))

    # One research cycle advances the simulated market by one day.
    warmup_ticks: int = 12
    history_window: int = 8               # rolling window for anomaly stats

    def plan(self, name: str | None) -> dict:
        return PLANS.get((name or self.default_plan).lower(), PLANS["pro"])


DEFAULT_CONFIG = Config()
