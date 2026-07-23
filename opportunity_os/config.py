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

    # Dashboard security. dashboard_token gates settings/mutating endpoints
    # (bearer token, sent as `Authorization: Bearer` or `X-OOS-Token`). When
    # unset, those endpoints are reachable only from localhost — a public
    # deploy is closed by default. secret_key encrypts stored credentials at
    # rest; if unset a persisted data/.secret_key (0600) is used. See SECURITY.md.
    dashboard_token: str = _env("OOS_DASHBOARD_TOKEN", "")
    secret_key: str = _env("OOS_SECRET_KEY", "")

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
    # Per-pass Serper spend caps: niche demand/supply measurement, and the
    # TH+EN gap-mining discovery source. At the defaults and a 30-min cadence
    # this stays under ~60 credits/day total.
    serper_niche_budget: int = field(default_factory=lambda: int(os.environ.get("OOS_SERPER_NICHE_BUDGET", "12")))
    serper_discovery_budget: int = field(default_factory=lambda: int(os.environ.get("OOS_SERPER_DISCOVERY_BUDGET", "6")))
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
    # Coverage is budgeted, not capped: the tiered scan scheduler (below)
    # keeps API spend flat while the watched universe grows — hot markets
    # rescan every cycle, the long tail rotates. 200 watched markets ≈ 2.5k
    # eBay calls/day at the 30-min cadence (free allowance: 5k).
    discovery_scan_cap: int = field(default_factory=lambda: int(os.environ.get("OOS_DISCOVERY_SCAN_CAP", "150")))
    discovery_max_active: int = field(default_factory=lambda: int(os.environ.get("OOS_DISCOVERY_MAX", "200")))

    # Tiered scanning: hot = watchlist, active opportunities, and anything
    # with a fresh anomaly (rescanned every cycle); warm = the best-scored
    # discoveries; cold = the long tail, rotated with a per-entity offset so
    # every cycle carries a similar call load.
    scan_warm_interval: int = field(default_factory=lambda: int(os.environ.get("OOS_SCAN_WARM_EVERY", "4")))
    scan_cold_interval: int = field(default_factory=lambda: int(os.environ.get("OOS_SCAN_COLD_EVERY", "12")))
    scan_warm_slots: int = field(default_factory=lambda: int(os.environ.get("OOS_SCAN_WARM_SLOTS", "60")))
    scan_hot_anomaly_window: int = field(default_factory=lambda: int(os.environ.get("OOS_SCAN_HOT_WINDOW", "6")))
    discovery_ttl_days: float = field(default_factory=lambda: float(os.environ.get("OOS_DISCOVERY_TTL_DAYS", "10")))
    discovery_trends_geo: str = _env("OOS_DISCOVERY_GEO", "US")
    # Type-diversity budget: reserve a floor share of the watched set for each
    # opportunity family, so the rich-evidence physical flips can't expire every
    # gap-mined Thai niche out of existence. Weights adapt toward families that
    # actually VERIFY, but never below the exploration floor — and a floor only
    # governs how much we WATCH a family, never lowers the bar to publish it.
    diversity_enabled: bool = field(
        default_factory=lambda: os.environ.get("OOS_DIVERSITY", "1") not in ("0", "false", "no"))
    diversity_min_floor: int = field(default_factory=lambda: int(os.environ.get("OOS_DIVERSITY_MIN_FLOOR", "8")))
    diversity_adapt: float = field(default_factory=lambda: float(os.environ.get("OOS_DIVERSITY_ADAPT", "0.5")))
    # Seasonal generator: how far ahead to look for Thai catalysts, and an
    # optional fixed "today" (ISO date) for deterministic tests/replays.
    seasonal_horizon_days: int = field(default_factory=lambda: int(os.environ.get("OOS_SEASONAL_HORIZON", "90")))
    seasonal_today: str = _env("OOS_SEASONAL_TODAY", "")

    # Steady-state venture entry (Milestone 2): a venture niche with steady,
    # non-spiking demand never fires an anomaly, so it was never evaluated — a
    # silent zero. A well-observed niche (enough real demand observations, an
    # OBSERVED supply side, fresh evidence) is surfaced to the SAME council on a
    # second entry path. Eligibility to be EVALUATED is never approval to
    # publish: flat demand is still rejected honestly. All thresholds are
    # configurable — no magic numbers scattered through the code.
    steady_venture_enabled: bool = field(
        default_factory=lambda: os.environ.get("OOS_STEADY_VENTURE", "1") not in ("0", "false", "no"))
    steady_min_demand_points: int = field(default_factory=lambda: int(os.environ.get("OOS_STEADY_MIN_DEMAND", "6")))
    steady_require_observed_supply: bool = field(
        default_factory=lambda: os.environ.get("OOS_STEADY_REQUIRE_SUPPLY", "1") not in ("0", "false", "no"))
    steady_freshness_days: float = field(
        default_factory=lambda: float(os.environ.get("OOS_STEADY_FRESHNESS_DAYS", "14")))
    steady_cooldown_ticks: int = field(default_factory=lambda: int(os.environ.get("OOS_STEADY_COOLDOWN", "6")))
    steady_max_per_cycle: int = field(default_factory=lambda: int(os.environ.get("OOS_STEADY_MAX_PER_CYCLE", "12")))

    # Demand level + stability (Milestone 3): the venture demand-corroboration
    # check was growth-only (trend/social spike), so a large, STABLE, underserved
    # niche could never corroborate even with real demand and a real gap. A
    # durable, high-level demand series is a legitimate, independent signal of
    # real demand — a distinct axis from "is it growing". It NEVER lowers the
    # bar: a niche still needs ≥2 independent signals and must still clear the
    # competition-gap and unit-economics checks. All thresholds configurable.
    #   * level: a niche needs a meaningful recurring demand level to be worth
    #     building for — ~8/day (250/mo) is a defensible micro-business floor;
    #   * stability: measured from the OBSERVED mention series — enough points and
    #     recent demand not collapsing below `retention`× the earlier window.
    venture_min_monthly_demand: int = field(
        default_factory=lambda: int(os.environ.get("OOS_VENTURE_MIN_DEMAND", "250")))
    venture_stability_min_points: int = field(
        default_factory=lambda: int(os.environ.get("OOS_VENTURE_STABILITY_POINTS", "6")))
    venture_stability_retention: float = field(
        default_factory=lambda: float(os.environ.get("OOS_VENTURE_STABILITY_RETENTION", "0.8")))

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
    # Dislocation engine: an individual ask this far below the market's own
    # conservative clearing value is a per-listing flip candidate. The single
    # richest alpha source per API call — one response yields many candidates.
    dislocation_min_edge: float = field(
        default_factory=lambda: float(os.environ.get("OOS_DISLOCATION_MIN_EDGE", "0.32")))
    # Graph fan-out: mine related products from listing titles and feed the
    # strongest as discovery candidates (zero API calls). Off in demo.
    graph_fanout_enabled: bool = field(
        default_factory=lambda: os.environ.get("OOS_GRAPH_FANOUT", "1") not in ("0", "false", "no"))
    # EV allocator: rank scan slots by verified-yield-per-scan (a bandit) once
    # enough history exists, instead of by raw discovery score alone.
    ev_allocator_enabled: bool = field(
        default_factory=lambda: os.environ.get("OOS_EV_ALLOCATOR", "1") not in ("0", "false", "no"))

    # Operator profile used to size positions.
    capital_cap_usd: float = field(default_factory=lambda: float(os.environ.get("OOS_CAPITAL_CAP", "2000")))

    # One research cycle advances the simulated market by one day.
    warmup_ticks: int = 12
    history_window: int = 8               # rolling window for anomaly stats

    def plan(self, name: str | None) -> dict:
        return PLANS.get((name or self.default_plan).lower(), PLANS["pro"])


DEFAULT_CONFIG = Config()
