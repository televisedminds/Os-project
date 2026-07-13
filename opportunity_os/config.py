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


@dataclass
class Config:
    """All tunables in one place. Environment variables override defaults."""

    db_path: Path = field(default_factory=lambda: Path(os.environ.get("OOS_DB", DATA_DIR / "opportunity_os.db")))
    world_seed: int = int(os.environ.get("OOS_SEED", "1151"))
    default_plan: str = os.environ.get("OOS_PLAN", "pro")
    auto_cycle_seconds: int = int(os.environ.get("OOS_AUTO_CYCLE_SECONDS", "0"))

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
    capital_cap_usd: float = float(os.environ.get("OOS_CAPITAL_CAP", "2000"))

    # One research cycle advances the simulated market by one day.
    warmup_ticks: int = 12
    history_window: int = 8               # rolling window for anomaly stats

    def plan(self, name: str | None) -> dict:
        return PLANS.get((name or self.default_plan).lower(), PLANS["pro"])


DEFAULT_CONFIG = Config()
