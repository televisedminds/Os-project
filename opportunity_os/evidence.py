"""The evidence ledger + verification levels + rejection taxonomy (Phases 7-9).

Every factual claim behind an opportunity is recorded as an `EvidenceItem`
tagged with WHERE it came from, WHAT KIND of claim it is (observed vs estimated
vs calculated vs assumed vs unknown), WHEN it was seen and the raw-record id it
traces to. This is the honest backbone the rest hangs on:

* **Phase 9** — the ledger is what the AI investigator is allowed to reason
  over. It may summarise, challenge and name *missing* evidence, but every
  number it uses must already be an OBSERVED/ESTIMATED item here; it may not
  invent prices, volumes, fees or eligibility.
* **Phase 7** — a `VerificationLevel` is computed from the ledger, not from a
  single "it passed" boolean. An opportunity whose evidence all comes from one
  marketplace is labelled SINGLE-SOURCE and capped below "execution ready", and
  an asking-price-only flip can never claim it has sold comps it doesn't have.
* **Phase 8** — rejections are classified into a fixed taxonomy so the funnels
  can show *why* candidates die, per source and per type.

Kinds are deliberately blunt so the UI can colour them and the AI can respect
them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum

# ---- evidence kinds -------------------------------------------------------

OBSERVED = "observed"          # measured from a live source (a real listing/post/rate)
ESTIMATED = "estimated"        # inferred from observations (sell-through from disappearance)
CALCULATED = "calculated"      # derived arithmetic (the fee/tax waterfall)
ASSUMPTION = "assumption"      # a modelling assumption (return rate, conversion)
USER_SUPPLIED = "user_supplied"   # a manual watchlist quote the operator is accountable for
AI_INTERPRETATION = "ai_interpretation"   # Claude's judgement — never a source of fact
UNKNOWN = "unknown"            # a field we could not establish (named, not hidden)


class VerificationLevel(str, Enum):
    DISCOVERED = "discovered"                     # exists, not yet independently checked
    PARTIALLY_VERIFIED = "partially_verified"     # passed gates but single-source / asking-only
    MULTI_SOURCE_VERIFIED = "multi_source_verified"   # ≥2 independent sources corroborate
    EXECUTION_READY = "execution_ready"           # multi-source + TH-executable + fresh
    INVALIDATED = "invalidated"                   # failed re-verification


@dataclass
class EvidenceItem:
    field: str                 # what this is evidence OF ("buy_price", "demand", ...)
    value: str                 # human-readable value
    kind: str                  # one of the kinds above
    source: str                # "ebay_us", "reddit", "economics", "thailand", ...
    independent: bool = False  # counts toward independent-source corroboration
    ref: str = ""              # raw-record id (listing item_id, snapshot ref) if any
    ts: float = 0.0            # when observed (0 = n/a)
    freshness: str = "n/a"     # "live" | "recent" | "stale" | "n/a"
    is_sold_comp: bool = False # True only for realised SALE prices, not asking prices

    def dict(self) -> dict:
        return asdict(self)


# ---- freshness ------------------------------------------------------------

def _freshness(ts: float, mode: str) -> str:
    if not ts:
        return "n/a"
    if mode != "live":
        return "live"                              # demo is a deterministic replay
    age_h = (time.time() - ts) / 3600
    if age_h <= 6:
        return "live"
    if age_h <= 48:
        return "recent"
    return "stale"


# ---- building the ledger --------------------------------------------------

def build_ledger(cand: dict, verification, mode: str = "demo",
                 latest_ts: float = 0.0) -> list[dict]:
    """Construct the evidence ledger for a candidate from what is actually
    known — the observed listings/quotes, the estimated velocity, the
    calculated economics, and the Thailand feasibility screen."""

    items: list[EvidenceItem] = []
    econ = cand.get("economics")
    fresh = _freshness(latest_ts, mode)

    if cand["kind"] in ("flip",):
        bv, sv = cand.get("buy_venue"), cand.get("sell_venue")
        disl = cand.get("dislocation")
        # buy-side price
        buy_kind = USER_SUPPLIED if _is_manual(cand, bv) else OBSERVED
        items.append(EvidenceItem(
            "buy_price", f"${cand.get('buy_usd', 0):.2f} on {bv}", buy_kind, bv or "?",
            independent=True, ref=(disl or {}).get("item_id", ""), ts=latest_ts, freshness=fresh))
        # sell-side price — CRUCIAL honesty: eBay Browse is ASKING price, not a
        # sold comp. We say so, and the verification level refuses to pretend.
        same_venue = bv == sv
        items.append(EvidenceItem(
            "sell_price", f"${cand.get('sell_usd', 0):.2f} asking on {sv}", OBSERVED, sv or "?",
            independent=not same_venue, ts=latest_ts, freshness=fresh, is_sold_comp=False))
        # velocity / sell-through
        items.append(EvidenceItem(
            "sell_through", f"≈{cand.get('velocity', 0):.1f} units/day", ESTIMATED,
            sv or "?", independent=False, ts=latest_ts, freshness=fresh))
        items.append(EvidenceItem(
            "competition", f"{cand.get('sellers', 0)} active sellers", OBSERVED, sv or "?",
            independent=False, ts=latest_ts, freshness=fresh))
    else:  # venture
        niche = cand.get("niche", {})
        m = niche.get("metrics", {})
        prov = m.get("observed") or {}
        # Demand: kind follows its real provenance. 'user_supplied' means the
        # operator typed the baseline into the watchlist; 'unknown' is named,
        # not hidden.
        demand_kind = {"observed": OBSERVED, "user_supplied": USER_SUPPLIED,
                       "unknown": UNKNOWN}.get(prov.get("demand"), OBSERVED)
        for src in cand.get("sources", []):
            if src in ("reddit", "serper", "google_trends", "news"):
                items.append(EvidenceItem(
                    "demand", f"{m.get('volume', 0):,.0f} demand events/mo, "
                    f"{m.get('growth_pct', 0):.0f}%/mo", demand_kind, src,
                    independent=demand_kind == OBSERVED, ts=latest_ts, freshness=fresh))
        supply_kind = {"observed": OBSERVED, "user_supplied": USER_SUPPLIED,
                       "unknown": UNKNOWN}.get(prov.get("supply"), OBSERVED)
        supply_src = {"observed": "serper", "user_supplied": "watchlist"}.get(
            prov.get("supply"), "market_scan")
        doms = m.get("supply_domains") or []
        supply_val = (f"{m.get('solution_count', m.get('providers', 0))} existing "
                      f"solutions/providers" + (f" ({', '.join(doms[:4])})" if doms else ""))
        if supply_kind == UNKNOWN:
            supply_val = "not yet observed — research required"
        items.append(EvidenceItem(
            "supply", supply_val, supply_kind, supply_src,
            independent=supply_kind == OBSERVED, ts=latest_ts, freshness=fresh))
        # Phase 7: competitor pricing scraped off a real provider page grounds
        # the price point — an INDEPENDENT observed source (ScrapingDog), tagged
        # experimental so nobody mistakes a scrape for an official feed.
        if prov.get("price") == "observed":
            review = m.get("competitor_review") or {}
            cr = review.get("complaint_ratio")
            note = (f"; reviews {int(cr * 100)}% complaints" if cr is not None else "")
            items.append(EvidenceItem(
                "competitor_pricing",
                f"provider page priced ≈${niche.get('price_point_usd', 0):.2f} (scraped){note}",
                OBSERVED, "scrapingdog", independent=True, ts=latest_ts, freshness=fresh))

    # economics — always CALCULATED, never observed
    if econ is not None:
        base = getattr(econ, "base", None) or (econ.get("base") if isinstance(econ, dict) else None)
        net = _attr(econ, "total_net_usd")
        items.append(EvidenceItem(
            "net_profit", f"${net:.2f} base, ${_attr(base, 'net_usd', 0):.2f}/unit", CALCULATED,
            "economics", independent=False))
        items.append(EvidenceItem(
            "fees_and_tax", "full fee/shipping/VAT waterfall applied", CALCULATED, "economics"))

    # Thailand executability
    feas = cand.get("feasibility")
    if feas is not None:
        can_buy = _attr(feas, "can_buy", False)
        can_sell = _attr(feas, "can_sell", False)
        rails = _attr(feas, "payment_rails", []) or []
        items.append(EvidenceItem(
            "thailand_executable",
            f"buy={'yes' if can_buy else 'no'}, sell={'yes' if can_sell else 'no'}, "
            f"payout={'/'.join(rails[:2]) if rails else 'unknown'}",
            CALCULATED if (can_buy and can_sell) else ASSUMPTION, "thailand"))

    # counterfeit / condition risk — honestly UNKNOWN unless a source informs it
    items.append(EvidenceItem(
        "counterfeit_condition_risk", "not independently assessed", UNKNOWN,
        "none", independent=False))

    return [i.dict() for i in items]


def _is_manual(cand: dict, venue) -> bool:
    return venue in (cand.get("item", {}).get("manual_venues", []) or [])


def _attr(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---- verification level ---------------------------------------------------

def independent_sources(ledger: list[dict]) -> set[str]:
    """Distinct sources that contributed OBSERVED/ESTIMATED, independent evidence."""

    return {i["source"] for i in ledger
            if i.get("independent") and i["kind"] in (OBSERVED, ESTIMATED, USER_SUPPLIED)}


def has_sold_comps(ledger: list[dict]) -> bool:
    return any(i.get("is_sold_comp") for i in ledger)


def is_single_source(ledger: list[dict]) -> bool:
    return len(independent_sources(ledger)) <= 1


def compute_level(ledger: list[dict], verification, feasibility,
                  passed_gates: bool, executable: bool = True) -> VerificationLevel:
    """Grade an opportunity by the QUALITY of its evidence, not just whether it
    passed. Single-source / asking-price-only work is honestly capped, and
    EXECUTION_READY additionally requires the Thailand executability engine to
    have no unknown/blocking answers (Phase 10)."""

    if not passed_gates:
        return VerificationLevel.DISCOVERED
    n_sources = len(independent_sources(ledger))
    can_buy = _attr(feasibility, "can_buy", False)
    can_sell = _attr(feasibility, "can_sell", False)
    rails = _attr(feasibility, "payment_rails", []) or []
    fresh = all(i.get("freshness") in ("live", "recent", "n/a") for i in ledger
                if i["kind"] in (OBSERVED, ESTIMATED))

    if n_sources < 2:
        return VerificationLevel.PARTIALLY_VERIFIED     # single-source — never "fully verified"
    level = VerificationLevel.MULTI_SOURCE_VERIFIED
    if (executable and can_buy and can_sell and rails and fresh
            and (has_sold_comps(ledger) or n_sources >= 3)):
        level = VerificationLevel.EXECUTION_READY
    return level


# ---- rejection taxonomy (Phase 8) -----------------------------------------

# Fixed categories so the funnels can aggregate "why candidates die".
RESEARCH_REQUIRED = "research_required"      # evidence gap named by the council, not a defect
INSUFFICIENT_DEMAND = "insufficient_demand"
NO_SOLD_COMPS = "no_sold_comps"
MARGIN_BELOW_THRESHOLD = "margin_below_threshold"
SHIPPING_TOO_EXPENSIVE = "shipping_too_expensive"
UNSUPPORTED_THAILAND = "unsupported_thailand"
DUPLICATE_THESIS = "duplicate_thesis"
SINGLE_SOURCE = "single_source_evidence"
COUNTERFEIT_RISK = "counterfeit_risk"
INSUFFICIENT_SUPPLY = "insufficient_supply"
HIGH_COMPETITION = "high_competition"
LOW_CONFIDENCE = "low_confidence"
STALE_DATA = "stale_data"
API_FAILURE = "api_failure"
EXCLUDED_BY_OPERATOR = "excluded_by_operator"
OTHER = "other"


def classify_rejection(reason: str) -> str:
    """Map a free-text rejection reason to a fixed taxonomy category."""

    r = (reason or "").lower()
    # Venture verdicts first. The council writes a failed check as
    # "<label> failed — <evidence>", and the demand check's evidence literally
    # contains "demand:supply" — so these MUST resolve before the generic
    # 'supply' keyword branch, or a demand failure is mislabelled as a supply
    # defect (exactly the bug the live funnel exposed).
    if "research required" in r or "never observed" in r:
        # Still accumulating observations, or a supply side never scanned — an
        # evidence gap the council named, not a defect.
        return RESEARCH_REQUIRED
    if "demand seen by" in r or "independent signals" in r or "independent sources" in r:
        # demand_corroboration failed with ENOUGH data: demand is real but not
        # corroborated as growing by ≥2 signals — soft demand, not a supply fault.
        return INSUFFICIENT_DEMAND
    if "gap confirmed" in r and ("providers vs" in r or "credible solutions" in r):
        # A measured, served market — providers/solutions exist and the gap is
        # too thin to enter. That is competition, not missing supply.
        return HIGH_COMPETITION
    if "avoid list" in r or "operator profile" in r:
        return EXCLUDED_BY_OPERATOR
    if "blocked from th" in r or "thailand" in r or "customs" in r or "tax_auditor" in r:
        return UNSUPPORTED_THAILAND
    if "margin" in r or "pessimistic" in r or "fee" in r or "loses $" in r:
        return MARGIN_BELOW_THRESHOLD
    if "shipping" in r:
        return SHIPPING_TOO_EXPENSIVE
    if "confidence" in r or "consensus" in r:
        return LOW_CONFIDENCE
    if "inventory" in r or "stock" in r or "supply" in r or "exhausted" in r:
        return INSUFFICIENT_SUPPLY
    if "disappeared" in r or "no longer" in r or "elapsed" in r or "stale" in r:
        return STALE_DATA
    if "competition" in r or "sellers" in r:
        return HIGH_COMPETITION
    if "demand" in r:
        return INSUFFICIENT_DEMAND
    if "counterfeit" in r or "junk" in r or "variant" in r or "authentic" in r:
        return COUNTERFEIT_RISK
    if "api" in r or "failed" in r or "error" in r:
        return API_FAILURE
    return OTHER
