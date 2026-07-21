"""Core domain models.

Everything flowing through the pipeline is a small, serializable dataclass:

    Signal -> Anomaly -> Investigation -> Candidate -> Verification -> Opportunity

`Opportunity` is the only thing users ever see, and it is only published after
the verification council signs off.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class OppType(str, Enum):
    PRODUCT_ARBITRAGE = "product_arbitrage"
    DIGITAL_PRODUCT = "digital_product"
    LOCAL_SERVICE = "local_service"
    B2B_SERVICE = "b2b_service"
    INFO_PRODUCT = "info_product"
    MICRO_SAAS = "micro_saas"
    REFURBISHMENT = "refurbishment"
    IMPORT_EXPORT = "import_export"      # cross-border route into/out of Thailand
    WHOLESALE = "wholesale"             # buy a bulk lot, break it, resell per-unit


class OppStatus(str, Enum):
    ACTIVE = "active"            # verified, still holding on re-verification
    EXECUTED = "executed"        # user recorded an outcome
    INVALIDATED = "invalidated"  # re-verification failed (market moved)
    EXPIRED = "expired"          # time window closed


class AnomalyKind(str, Enum):
    CROSS_VENUE_SPREAD = "cross_venue_spread"
    PRICE_SPIKE = "price_spike"
    SUPPLY_CRUNCH = "supply_crunch"
    SOCIAL_SPIKE = "social_spike"
    SEARCH_GAP = "search_gap"
    SERVICE_IMBALANCE = "service_imbalance"
    B2B_SURGE = "b2b_surge"
    SELLER_EXODUS = "seller_exodus"              # competitors leaving a market
    DEMAND_ACCELERATION = "demand_acceleration"  # sell-through speeding up
    MARGIN_EXPANSION = "margin_expansion"        # cross-venue spread widening
    TREND_REVERSAL = "trend_reversal"            # a declining niche turning up
    PRICE_DISLOCATION = "price_dislocation"      # single ask far below its market
    SELLER_LIQUIDATION = "seller_liquidation"    # one seller dumping below fair


@dataclass
class Signal:
    """A single raw observation emitted by a scanner agent."""

    agent: str
    source: str                 # e.g. "ebay_us", "reddit", "google_trends"
    kind: str                   # e.g. "market_snapshot", "social_mentions"
    entity_id: str
    tick: int
    strength: float = 0.0       # how unusual the observation looked to the scanner
    venue: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Anomaly:
    """A deviation worth investigating, distilled from many signals."""

    kind: AnomalyKind
    entity_id: str
    tick: int
    severity: float             # 0..1
    summary: str
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class InvestigationStep:
    """One question in the why-chain, answered with data."""

    question: str
    finding: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class CostLine:
    label: str
    amount_usd: float
    note: str = ""


@dataclass
class Scenario:
    name: str
    revenue_usd: float
    lines: list[CostLine]
    total_cost_usd: float = 0.0
    net_usd: float = 0.0
    margin_pct: float = 0.0

    def finalize(self) -> "Scenario":
        self.total_cost_usd = round(sum(l.amount_usd for l in self.lines), 2)
        self.net_usd = round(self.revenue_usd - self.total_cost_usd, 2)
        self.margin_pct = round(100.0 * self.net_usd / self.revenue_usd, 1) if self.revenue_usd else 0.0
        return self


@dataclass
class Economics:
    """Full unit economics, per-unit USD, with a pessimistic stress case."""

    kind: str                              # "flip" (per unit) or "venture" (per month)
    base: Scenario
    pessimistic: Scenario
    qty: int = 1
    capital_usd: float = 0.0               # cash needed up-front for recommended qty
    total_net_usd: float = 0.0             # base net x qty (flip) or monthly net (venture)
    breakeven_revenue_usd: float = 0.0
    fx: dict[str, float] = field(default_factory=dict)
    thb: dict[str, float] = field(default_factory=dict)
    route_note: str = ""


@dataclass
class Check:
    """One verifier's independent verdict."""

    verifier: str
    name: str
    passed: bool
    confidence: float           # verifier's own confidence in its verdict
    evidence: str
    critical: bool = False      # a failed critical check vetoes publication


@dataclass
class Verification:
    checks: list[Check]
    consensus: float = 0.0      # reliability-weighted confidence
    passed: bool = False
    tick: int = 0


@dataclass
class ScoreBreakdown:
    factors: dict[str, float]        # each 0..100, higher is better
    weights: dict[str, float]        # sum to 1.0 (learned over time)
    contributions: dict[str, float] = field(default_factory=dict)
    overall: float = 0.0


@dataclass
class Feasibility:
    """Can the operator, based in Thailand, actually execute this?"""

    can_buy: bool
    can_sell: bool
    buy_notes: list[str] = field(default_factory=list)
    sell_notes: list[str] = field(default_factory=list)
    requires_proxy: bool = False
    payment_rails: list[str] = field(default_factory=list)
    customs_notes: list[str] = field(default_factory=list)


@dataclass
class PlaybookStep:
    order: int
    title: str
    detail: str
    automatable: bool = False
    tool: str = ""
    eta: str = ""


@dataclass
class Playbook:
    steps: list[PlaybookStep]
    listing: dict[str, Any] | None = None   # pre-generated title/description/pricing
    first_action: str = ""
    timeline_days: float = 0.0


@dataclass
class AutomationPlan:
    coverage_pct: float
    tasks: list[dict[str, Any]] = field(default_factory=list)
    human_checkpoints: list[str] = field(default_factory=list)


@dataclass
class Opportunity:
    id: str
    type: OppType
    status: OppStatus
    category: str
    title: str
    subtitle: str
    entity_id: str
    route: dict[str, Any]                   # buy/sell venue, countries, path
    tick_created: int
    tick_updated: int
    window_days: float
    confidence: float
    economics: Economics
    verification: Verification
    score: ScoreBreakdown
    feasibility: Feasibility
    why_chain: list[InvestigationStep]
    playbook: Playbook
    automation: AutomationPlan
    sources: list[str] = field(default_factory=list)
    invalidation_reason: str = ""
    # Phase 7/9: the evidence ledger and the level it earns.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    verification_level: str = "discovered"
    single_source: bool = True
    # Phase 10: the Thailand executability report.
    executability: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def opportunity_id(opp_type: str, entity_id: str, buy: str | None, sell: str | None) -> str:
    """Stable identity so re-detections update rather than duplicate."""

    raw = f"{opp_type}|{entity_id}|{buy or '-'}|{sell or '-'}"
    return "opp_" + hashlib.sha1(raw.encode()).hexdigest()[:10]
