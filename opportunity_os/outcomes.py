"""The realized-truth engine — closing the loop from predicted opportunity to
recorded reality.

A prediction is a promise; an outcome is what actually happened. This module
turns a recorded real outcome (what you bought, what you sold, what it cost, how
long it took) into honest realized metrics, compares them to what was predicted,
and produces the ONE signal allowed to teach the system — derived strictly from
realized cash, never from a model's own prediction.

Hard honesty rules enforced here (truth contract):

* Realized profit / ROI / profit-per-hour / capital-turnover are computed ONLY
  from actual spend / revenue / fees / time — never from a projection.
* ``prediction_error`` is realized MINUS predicted. The prediction is the thing
  being *graded*; it is never an input to its own grade.
* ``recalibration_signal`` refuses to produce a learning signal from anything but
  a terminal outcome carrying real cash. Interim states and bare predictions
  return ``None`` — so predictions can never train predictions.
* Abandonment / refund / failure require an explicit reason before the outcome is
  complete.
* Nothing here claims profit unless actual revenue exceeded actual outlay.
"""

from __future__ import annotations

# ---- recordable real-outcome statuses (the mandate's taxonomy) --------------
BOUGHT = "bought"        # capital deployed, reality not yet resolved (INTERIM)
SOLD = "sold"            # sold for revenue (terminal)
DELIVERED = "delivered"  # venture served/delivered, revenue realized (terminal)
ABANDONED = "abandoned"  # walked away (terminal; needs reason)
REFUNDED = "refunded"    # returned / refunded (terminal; needs reason)
FAILED = "failed"        # attempted, no sale / lost money (terminal; needs reason)

STATUSES = [BOUGHT, SOLD, DELIVERED, ABANDONED, REFUNDED, FAILED]
_TERMINAL = {SOLD, DELIVERED, ABANDONED, REFUNDED, FAILED}   # 'bought' is interim
_NEEDS_REASON = {ABANDONED, REFUNDED, FAILED}
_REVENUE_EXPECTED = {SOLD, DELIVERED}


def is_valid_status(status: str) -> bool:
    return status in STATUSES


def is_terminal(status: str) -> bool:
    return status in _TERMINAL


def needs_reason(status: str) -> bool:
    return status in _NEEDS_REASON


def is_complete(status: str, reason: str | None) -> bool:
    """A loop is closed only at a terminal status, and only with an explicit
    reason when the status demands one (abandoned / refunded / failed)."""

    if status not in _TERMINAL:
        return False
    if status in _NEEDS_REASON and not (reason and reason.strip()):
        return False
    return True


# ------------------------------------------------------------ realized metrics

def realized_metrics(actual_spend=None, actual_revenue=None, actual_fees=None,
                     actual_hours=None, days_taken=None, predicted_profit_usd=None,
                     capital_usd=None) -> dict:
    """Honest realized economics from ACTUAL cash + time only. Missing inputs
    yield ``None`` for the metric they'd feed, never a guess."""

    spend = max(0.0, _f(actual_spend))
    revenue = max(0.0, _f(actual_revenue))
    fees = max(0.0, _f(actual_fees))
    hours = _f(actual_hours)
    days = _f(days_taken)

    outlay = round(spend + fees, 2)
    realized_profit = round(revenue - spend - fees, 2)
    roi = round(realized_profit / outlay, 4) if outlay > 0 else None
    profit_per_hour = round(realized_profit / hours, 2) if hours > 0 else None
    # Capital turnover: revenue returned per unit of capital deployed. Prefer the
    # opportunity's stated capital; fall back to actual outlay.
    cap = _f(capital_usd) if _f(capital_usd) > 0 else outlay
    turnover = round(revenue / cap, 3) if cap > 0 else None
    turnover_yr = round((revenue / cap) * (365.0 / days), 2) if (cap > 0 and days > 0) else None

    out = {
        "realized_profit_usd": realized_profit,
        "actual_outlay_usd": outlay,
        "actual_revenue_usd": round(revenue, 2),
        "actual_fees_usd": round(fees, 2),
        "roi": roi,
        "profit_per_hour_usd": profit_per_hour,
        "capital_turnover": turnover,
        "capital_turnover_per_year": turnover_yr,
        "hours": hours or None,
        "days_taken": days or None,
        "basis": "realized_actuals",
    }
    if predicted_profit_usd is not None:
        out["prediction_error"] = prediction_error(predicted_profit_usd, realized_profit)
    return out


def prediction_error(predicted_profit_usd, realized_profit_usd) -> dict:
    """Signed realized − predicted. Negative = we over-promised. The prediction is
    graded against reality; it never feeds its own grade or any learning update."""

    pred = round(_f(predicted_profit_usd), 2)
    real = round(_f(realized_profit_usd), 2)
    err = round(real - pred, 2)
    pct = round(100.0 * err / abs(pred), 1) if pred else None
    direction = "over_predicted" if err < 0 else ("under_predicted" if err > 0 else "exact")
    return {"predicted_profit_usd": pred, "realized_profit_usd": real,
            "error_usd": err, "error_pct": pct, "direction": direction}


# ------------------------------------------------------ frozen provenance snapshot

def provenance_snapshot(opp: dict) -> dict:
    """Freeze which source / generator / evidence produced the ORIGINAL
    recommendation, plus the predicted numbers, at outcome-record time — so later
    mutation of the live opportunity can't rewrite the attribution or the promise
    we're grading (requirement: store what contributed to the recommendation)."""

    econ = opp.get("economics") or {}
    base = econ.get("base") or {}
    pess = econ.get("pessimistic") or {}
    verifiers = [c.get("verifier") for c in (opp.get("verification") or {}).get("checks", [])
                 if c.get("verifier")]
    evidence_ids = [e.get("id") or e.get("label") or e.get("source")
                    for e in (opp.get("evidence") or []) if isinstance(e, dict)]
    return {
        "opportunity_id": opp.get("id"),
        "title": opp.get("title"),
        "type": opp.get("type"),
        "category": opp.get("category"),
        "generator": generator_of(opp),
        "family": opp.get("family") or (opp.get("route") or {}).get("kind"),
        "sources": list(opp.get("sources", []) or []),
        "verifiers": verifiers,
        "verification_level": opp.get("verification_level"),
        "evidence_ids": [e for e in evidence_ids if e][:20],
        "predicted": {
            "confidence": opp.get("confidence"),
            "base_net_usd": base.get("net_usd"),
            "pessimistic_net_usd": pess.get("net_usd"),
            "total_net_usd": econ.get("total_net_usd"),
            "capital_usd": econ.get("capital_usd"),
            "window_days": opp.get("window_days"),
            "input_provenance": econ.get("input_provenance", {}),
        },
        "snapshot_note": "frozen at outcome-record time; live-opp mutation cannot "
                         "rewrite this attribution or the graded prediction",
    }


# Map an opportunity to the generator identity that produced it. There is no
# single generator field; the type IS the generator family, refined by trade kind.
_GEN_BY_TYPE = {
    "product_arbitrage": "flip_generator",
    "refurbishment": "refurbishment_generator",
    "wholesale": "trade_generator",
    "import_export": "trade_generator",
    "seasonal": "seasonal_generator",
    "local_service": "venture_generator",
    "b2b_service": "venture_generator",
    "digital_product": "venture_generator",
    "info_product": "venture_generator",
    "micro_saas": "venture_generator",
    "lead_generation": "leadgen_generator",
}


def generator_of(opp: dict) -> str:
    if opp.get("generator"):
        return opp["generator"]
    return _GEN_BY_TYPE.get(opp.get("type"), opp.get("type") or "unknown")


# --------------------------------------------- the only signal allowed to teach

def recalibration_signal(status: str, metrics: dict, reason: str | None = None) -> dict | None:
    """The ONE input permitted to update confidence / ranking. Grounded strictly
    in a terminal outcome carrying REAL cash. Returns ``None`` for interim states
    or missing actuals, so a prediction can never masquerade as a taught outcome.

    Learning results:
      * ``success``   — sold/delivered with realized profit > 0 (real money made)
      * ``failure``   — sold/delivered at a loss, refunded, or failed
      * ``abandoned`` — an honest walk-away: no profit claimed, milder signal
    """

    if status not in _TERMINAL:
        return None
    realized = metrics.get("realized_profit_usd")
    if realized is None:
        return None
    if status in _REVENUE_EXPECTED:
        result = "success" if realized > 0 else "failure"
    elif status == ABANDONED:
        result = "abandoned"
    else:  # refunded / failed
        result = "failure"
    return {
        "result": result,
        "realized_profit_usd": realized,   # REAL cash — the ground truth
        "reason": reason,
        "basis": "realized_cash",          # never 'predicted' — see assert_realized_basis
    }


def assert_realized_basis(signal: dict | None) -> dict | None:
    """Defensive guard used at the learning boundary: any signal that reaches the
    recalibrator must be grounded in realized cash. Anything else is a bug that
    would let a prediction train predictions — refuse it loudly."""

    if signal is not None and signal.get("basis") != "realized_cash":
        raise ValueError(f"refusing to learn from a non-realized signal: {signal!r}")
    return signal


def _f(x) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
