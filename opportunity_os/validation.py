"""Observed-economics validation — promote an ESTIMATED projection to a MEASURED
one when, and only when, real cash has been recorded (Backlog #22).

M5 made the system honest about not knowing: a venture whose demand volume is only
*estimated* is priced anyway but can never verify — it stays `validation_required`
forever. That is truthful, but it is a dead end. This module is the exit: once the
operator actually validates the niche with real money, the projection is re-priced
from that recorded cash, its provenance is promoted `estimated → observed`, and the
opportunity can finally be judged on real numbers.

The rules that keep this honest:

* **Only recorded cash triggers it.** The single accepted source of truth is a
  realized outcome (`sold`/`delivered`) carrying actual revenue *and* a period to
  normalise it over. Search-result counts are never used — inferring volume from
  them was precisely the M5 fabrication.
* **Never invent an observation.** No evidence → no recompute, and the opportunity
  stays `validation_required`.
* **Never downgrade.** An `observed` input is never rewritten back to `estimated`.
* **Never extrapolate upward.** One observed month becomes both the base and the
  conservative case (see `economics.recompute_venture_from_observed`).
* **Every promotion is audited** with the before/after economics and the evidence
  that justified it, so a number that improved can always be traced to the cash
  that improved it.
"""

from __future__ import annotations

from dataclasses import asdict

from . import economics

# Recorded-outcome statuses that constitute evidence of real demand. A refund or a
# failure is real cash too, but it is evidence the thesis DIDN'T hold — those flow
# through the learning engine, not into a re-priced projection.
_REVENUE_STATUSES = ("sold", "delivered")

# Opportunity type -> the venture cost model that priced it.
_TYPE_TO_KIND = {
    "digital_product": "digital", "micro_saas": "digital",
    "info_product": "info",
    "local_service": "local",
    "b2b_service": "b2b",
    "lead_generation": "leadgen",
}


def venture_kind(opp: dict) -> str | None:
    """Which venture cost model priced this opportunity (None if it isn't one)."""

    return _TYPE_TO_KIND.get(opp.get("type"))


def estimated_inputs(econ: dict | None) -> list[str]:
    """Which economic inputs are still unproven estimates."""

    ip = (econ or {}).get("input_provenance") or {}
    return sorted(k for k, v in ip.items() if v == "estimated")


def needs_revalidation(opp: dict) -> bool:
    """True when this opportunity's projection rests on an estimated input that
    recorded cash could replace."""

    return bool(venture_kind(opp)) and bool(estimated_inputs(opp.get("economics")))


def observed_monthly_revenue(store, opp_id: str) -> dict | None:
    """The strongest REAL evidence of monthly revenue for this opportunity, or
    None. Read strictly from recorded outcomes — never modelled, never inferred
    from search volume.

    A period is required: revenue without a number of days cannot be normalised to
    a monthly figure, and guessing the period would fabricate a rate.
    """

    getter = getattr(store, "get_realized_outcome", None)
    if getter is None:
        return None
    rec = getter(opp_id)
    if not rec or rec.get("status") not in _REVENUE_STATUSES:
        return None
    m = rec.get("metrics") or {}
    if not m.get("has_actuals"):
        return None                       # an empty form is not evidence
    revenue = m.get("actual_revenue_usd")
    days = m.get("days_taken")
    try:
        revenue = float(revenue)
        days = float(days)
    except (TypeError, ValueError):
        return None
    if revenue <= 0 or days <= 0:
        return None                       # no revenue, or no period to rate it over
    monthly = round(revenue * 30.0 / days, 2)
    return {
        "monthly_revenue_usd": monthly,
        "actual_revenue_usd": round(revenue, 2),
        "days_taken": days,
        "outcome_status": rec.get("status"),
        "source": "recorded_outcome",
        "basis": "observed_cash",
        "note": (f"${revenue:,.2f} actually received over {days:.0f} days "
                 f"→ ${monthly:,.2f}/month observed"),
    }


def revalidate(opp: dict, observed: dict) -> tuple[dict, dict] | None:
    """Re-price the opportunity from observed cash. Returns
    ``(new_economics_dict, audit)`` or None when it doesn't apply.

    Refuses to run without a venture cost model or without observed evidence, and
    refuses to overwrite inputs that are already `observed` (no downgrades)."""

    kind = venture_kind(opp)
    if not kind or not observed:
        return None
    old = opp.get("economics") or {}
    if not estimated_inputs(old):
        return None                       # nothing estimated left to promote
    price = _price_point(opp, observed)
    new = economics.recompute_venture_from_observed(
        kind, price, observed["monthly_revenue_usd"])
    new_d = asdict(new)
    # Never silently drop an input that was ALREADY observed by another route.
    old_prov = old.get("input_provenance") or {}
    for k, v in old_prov.items():
        if v == "observed":
            new_d["input_provenance"][k] = "observed"
    audit = {
        "opportunity_id": opp.get("id"),
        "trigger": "observed_cash",
        "promoted_inputs": estimated_inputs(old),
        "evidence": observed,
        "before": {
            "monthly_net_usd": (old.get("base") or {}).get("net_usd"),
            "pessimistic_net_usd": (old.get("pessimistic") or {}).get("net_usd"),
            "revenue_usd": (old.get("base") or {}).get("revenue_usd"),
            "input_provenance": old_prov,
        },
        "after": {
            "monthly_net_usd": new_d["base"]["net_usd"],
            "pessimistic_net_usd": new_d["pessimistic"]["net_usd"],
            "revenue_usd": new_d["base"]["revenue_usd"],
            "input_provenance": new_d["input_provenance"],
        },
        "note": ("Projection re-priced from recorded cash; demand volume promoted "
                 "estimated → observed, so this opportunity can now be judged on "
                 "real numbers instead of staying validation_required."),
    }
    return new_d, audit


def _price_point(opp: dict, observed: dict) -> float:
    """The price real customers paid, when we can see it; else the modelled one."""

    econ = opp.get("economics") or {}
    for key in ("price_point_usd",):
        if econ.get(key):
            return float(econ[key])
    base = econ.get("base") or {}
    # Fall back to the modelled price implied by the original projection.
    rev, cust = base.get("revenue_usd"), base.get("customers")
    if rev and cust:
        try:
            return max(0.01, float(rev) / float(cust))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    route = opp.get("route") or {}
    return float(route.get("price_point_usd") or observed.get("monthly_revenue_usd") or 1.0)
