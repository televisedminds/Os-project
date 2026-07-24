"""Execution guide — turns a selected action into a driven, dated, single-next-step
plan you actually follow from Thailand.

The pieces already existed and were honest on their own: ``build_playbook`` (the
step-by-step), ``executability.assess`` (can Thailand actually do it), the
lifecycle state machine (``execution.next_action``), and the projected money
timeline on the action card. What was missing was a SINGLE coherent guide that:

* reconciles the TWO progress signals — playbook step check-offs and the chat
  lifecycle state — into one *effective stage* (checking off "list it" and the
  chat having recorded a purchase must not disagree about where the deal is);
* surfaces the ONE next concrete step for the stage you are actually at, instead
  of a static wall of seven steps; and
* anchors the projected money/time schedule to the day you started, so you can
  see where you are on the clock ("day 4 of ~20 — customs paperwork is due").

Honesty boundary (truth contract): everything here is *plan* progress and
*projected* money. Which steps you have ticked and how many days have elapsed are
facts; the dollar amounts and the day-N milestones are projections from the
priced economics, not realised cash. Realised-cash tracking and calibration are
the Outcome-learning milestone — this guide never claims money was made.
"""

from __future__ import annotations

import re
import time

from . import execution, executability

# The lifecycle ladder, as an index so two stages can be compared / maxed.
_ORDER = [execution.NOT_STARTED, execution.RESEARCHING, execution.READY_TO_BUY,
          execution.PURCHASED, execution.IN_TRANSIT, execution.RECEIVED,
          execution.LISTED, execution.SOLD, execution.COMPLETED]
_IDX = {s: i for i, s in enumerate(_ORDER)}


def _is_flip(opp: dict) -> bool:
    return opp.get("type") in ("product_arbitrage", "refurbishment") \
        or (opp.get("economics") or {}).get("kind") == "flip"


# Which lifecycle stage a COMPLETED playbook step implies. Keyed by 1-based step
# order (PlaybookStep.order). The playbooks are stable code we own; a step index
# with no mapping simply doesn't advance the stage (safe default).
_FLIP_STEP_STAGE = {
    1: execution.PURCHASED,    # secured units on the buy venue
    2: execution.IN_TRANSIT,   # routed the goods
    3: execution.IN_TRANSIT,   # customs paperwork
    4: execution.LISTED,       # listed on the sell venue
    5: execution.LISTED,       # fulfil fast
    6: execution.LISTED,       # reprice / monitor
    7: execution.SOLD,         # repatriate + record
}
_VENTURE_STEP_STAGE = {
    1: execution.RESEARCHING,  # validate with 10 conversations
    2: execution.PURCHASED,    # built the minimum sellable version (startup spend)
    3: execution.PURCHASED,    # priced it
    4: execution.LISTED,       # distributed where demand is
    5: execution.LISTED,       # automated delivery/support
    6: execution.SOLD,         # reported outcomes
}


def _step_stage_map(opp: dict) -> dict[int, str]:
    return _FLIP_STEP_STAGE if _is_flip(opp) else _VENTURE_STEP_STAGE


def stage_from_steps(opp: dict, done_steps) -> str:
    """Furthest lifecycle stage implied by the checked-off playbook steps."""

    mp = _step_stage_map(opp)
    best = execution.NOT_STARTED
    for s in done_steps or []:
        stage = mp.get(int(s))
        if stage and _IDX[stage] > _IDX[best]:
            best = stage
    return best


def effective_stage(opp: dict, done_steps, chat_state: str) -> str:
    """One stage, reconciling both progress signals: the furthest-along of the
    chat lifecycle state and the stage implied by ticked playbook steps.

    This is the fix for the two-source disconnect — ticking "list it" advances
    the stage even if the chat machine was never touched, and a purchase recorded
    in chat advances it even if no box was ticked.

    Terminal off-ramps win outright: a CANCELLED or INVALIDATED deal must keep
    saying so. Collapsing them onto the ladder would reset the guide to
    "not started" and cheerfully tell you to buy an edge that re-verification has
    already killed."""

    if chat_state in (execution.CANCELLED, execution.INVALIDATED):
        return chat_state
    a = chat_state if chat_state in _IDX else execution.NOT_STARTED
    b = stage_from_steps(opp, done_steps)
    return a if _IDX[a] >= _IDX[b] else b


# ------------------------------------------------------------ the single next step

def next_step(opp: dict, stage: str, action_card: dict | None = None) -> dict:
    """The ONE next unfinished move for the stage you're actually at, enriched
    with the concrete route/price/capital numbers so it reads as 'do exactly
    this', not a category label."""

    na = execution.next_action(stage, opp)
    concrete = _concrete_hint(opp, stage, action_card)
    return {
        "stage": stage,
        "title": na["title"],
        "why": na["why"],
        "todo": na["todo"],
        "concrete": concrete,          # the specific numbers for this step, if any
        "is_terminal": na["is_terminal"],
    }


def _concrete_hint(opp: dict, stage: str, action_card: dict | None) -> str:
    """Stage-specific hard numbers pulled from the priced action card."""

    a = action_card or opp.get("action") or {}
    if _is_flip(opp) and a.get("type") == "flip":
        buy, sell = a.get("buy") or {}, a.get("sell") or {}
        if stage in (execution.NOT_STARTED, execution.RESEARCHING, execution.READY_TO_BUY):
            mx = buy.get("max_price_usd")
            venue = buy.get("venue", "the source")
            cap = a.get("invest_usd")
            bits = []
            if mx is not None:
                bits.append(f"Buy on {venue} at ≤ ${mx:,.0f}/unit — walk away above it")
            if cap is not None:
                bits.append(f"capital ≈ ${cap:,.0f}")
            return "; ".join(bits)
        if stage in (execution.RECEIVED, execution.LISTED):
            price = sell.get("price_usd")
            venue = sell.get("venue", "the sell venue")
            if price is not None:
                return f"List on {venue} at ~${price:,.0f}"
        if stage == execution.SOLD:
            return "Withdraw USD → THB via Payoneer/Wise, then record the realised result"
    else:
        econ = opp.get("economics") or {}
        price = (econ.get("base") or {}).get("price_point_usd") or econ.get("price_point_usd")
        if stage in (execution.NOT_STARTED, execution.RESEARCHING) and price:
            return f"Confirm ~10 people will pay ≈ ${float(price):,.0f} before building"
    return ""


# ------------------------------------------------------------ dated money/time schedule

_DAY = 86400.0


def _milestones(opp: dict, action_card: dict | None) -> list[dict]:
    """Raw (undated-status) milestones: prefer the priced money timeline; fall
    back to the playbook step ETAs for ventures that have no cash timeline."""

    a = action_card or opp.get("action") or {}
    mt = a.get("money_timeline") or []
    if mt:
        return [{"day": m.get("day"), "label": m.get("label"),
                 "amount_usd": m.get("amount_usd")} for m in mt]
    # Fallback: derive from playbook step ETAs ("day 2–7" -> 2).
    out = []
    for st in ((opp.get("playbook") or {}).get("steps") or []):
        day = _first_day(st.get("eta", ""))
        out.append({"day": day, "label": st.get("title"), "amount_usd": None})
    return out


def _first_day(eta: str):
    m = re.search(r"\d+", eta or "")
    return int(m.group()) if m else None


def schedule(opp: dict, action_card: dict | None, started_ts: float | None,
             now: float | None = None) -> dict:
    """Projected money/time milestones, anchored to the day the deal started.

    Each milestone is marked done / today / upcoming by ELAPSED days when the
    deal has a real start; before that it's 'planned' (relative to whichever day
    you start). Amounts and days are PROJECTED — never realised."""

    now = now if now is not None else time.time()
    ms = _milestones(opp, action_card)
    anchored = started_ts is not None
    # max(0, …): a future start_ts (clock skew between hosts) must not report a
    # negative day or mark every milestone "upcoming" forever.
    elapsed = max(0.0, (now - started_ts) / _DAY) if anchored else None
    out = []
    for m in ms:
        day = m.get("day")
        if not anchored or day is None:
            status = "planned"
        elif day < int(elapsed):
            status = "done"          # its projected day has passed
        elif day == int(elapsed):
            status = "today"
        else:
            status = "upcoming"
        out.append({**m, "status": status})
    return {
        "anchored": anchored,
        "day": int(elapsed) if anchored else None,
        "started_ts": started_ts,
        "basis": "projected",
        "note": "Days and dollar amounts are PROJECTED from the priced economics, "
                "not realised. 'Done' means a milestone's projected day has passed, "
                "not that cash was received.",
        "milestones": out,
    }


# ------------------------------------------------------------ Thailand-readiness gate

def readiness(opp: dict) -> dict:
    """Compact 'can I actually run this from Thailand?' gate from the
    executability report, with honest unknowns/blockers surfaced up front."""

    rep = opp.get("executability")
    if not rep:
        try:
            rep = executability.assess(opp)
        except Exception:  # noqa: BLE001
            rep = {}
    checks = rep.get("checks", [])
    return {
        "ready": bool(rep.get("execution_ready")),
        "unknowns": rep.get("unknowns", []),
        "blockers": rep.get("blockers", []),
        "checks": [{"question": c.get("question"), "status": c.get("status"),
                    "detail": c.get("detail")} for c in checks],
    }


# ------------------------------------------------------------ compose

def build_guide(opp: dict, done_steps=None, chat_state: str = execution.NOT_STARTED,
                started_ts: float | None = None, action_card: dict | None = None,
                now: float | None = None) -> dict:
    """The full execution guide for one selected action."""

    done_steps = list(done_steps or [])
    stage = effective_stage(opp, done_steps, chat_state)
    steps = (opp.get("playbook") or {}).get("steps") or []
    step_map = _step_stage_map(opp)
    first_open = next((s["order"] for s in steps if s["order"] not in done_steps), None)
    step_view = [{
        "order": s["order"], "title": s.get("title"), "eta": s.get("eta"),
        "automatable": s.get("automatable", False), "tool": s.get("tool"),
        "done": s["order"] in done_steps,
        "is_next": s["order"] == first_open,
        "implies_stage": step_map.get(s["order"]),
    } for s in steps]
    n = len(steps)
    return {
        "opportunity_id": opp.get("id"),
        "title": opp.get("title"),
        "kind": "flip" if _is_flip(opp) else "venture",
        "stage": stage,
        "started": started_ts is not None,
        "readiness": readiness(opp),
        "next_step": next_step(opp, stage, action_card),
        "schedule": schedule(opp, action_card, started_ts, now),
        "progress": {"done": len(done_steps), "total": n,
                     "pct": round(100 * len(done_steps) / n) if n else 0},
        "steps": step_view,
        "basis": "PLAN progress + PROJECTED money; realised cash is tracked "
                 "separately (Outcome learning), not here.",
    }


def compact(guide: dict) -> dict:
    """The one-glance summary embedded in /api/today so the selected action shows
    'here's exactly what to do now' without a second fetch."""

    ns = guide.get("next_step", {})
    rd = guide.get("readiness", {})
    concrete = ns.get("concrete")
    line = concrete or ns.get("todo") or ns.get("title") or ""
    return {
        "stage": guide.get("stage"),
        "next": ns.get("title"),
        "next_do": line,
        "ready_in_thailand": rd.get("ready", False),
        "open_questions": len(rd.get("unknowns", [])) + len(rd.get("blockers", [])),
        "progress_pct": (guide.get("progress") or {}).get("pct", 0),
        "on_day": (guide.get("schedule") or {}).get("day"),
    }
