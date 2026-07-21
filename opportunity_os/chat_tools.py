"""The execution chat's toolbox (Phase 13).

Fifteen deterministic tools the assistant can run to help complete ONE
opportunity. They research, recalculate, draft, validate and record — they NEVER
transact: no tool here buys, pays, lists publicly, accepts an offer, cancels or
refunds. Those remain the operator's to do; the chat only records that they were
done. That is the human-approval guarantee, enforced structurally by the fact
that no tool has a side effect on the outside world.

Every tool returns a uniform envelope:

    {"tool", "ok", "summary", "data", "evidence": [{"label", "text"}]}

`evidence.label` is one of LIVE / SAVED / CALCULATED / ASSUMPTION / UNKNOWN /
AI so the UI (and the operator) can always see whether a number was just
fetched, recalled from storage, computed, assumed, or simply not known.
"""

from __future__ import annotations

import time

from . import economics, execution, matching, research

# Evidence labels shown to the operator.
LIVE, SAVED, CALCULATED, ASSUMPTION, UNKNOWN, AI = "LIVE", "SAVED", "CALCULATED", "ASSUMPTION", "UNKNOWN", "AI"


def _ev(label: str, text: str) -> dict:
    return {"label": label, "text": text}


def _envelope(tool: str, ok: bool, summary: str, data: dict, evidence: list) -> dict:
    return {"tool": tool, "ok": ok, "summary": summary, "data": data, "evidence": evidence}


# --- 1/2. refresh buying + competing sell listings -------------------------

def refresh_listings(store, opp, world=None, side: str = "buy") -> dict:
    tool = "refresh_buy_listings" if side == "buy" else "refresh_sell_listings"
    route = opp.get("route", {}) or {}
    venue = route.get("buy_venue" if side == "buy" else "sell_venue")
    pid = opp.get("entity_id")
    if not (world and hasattr(world, "listing_sample") and venue):
        return _envelope(tool, False, "No live source available to refresh from.",
                         {"venue": venue}, [_ev(UNKNOWN, "Live listing refresh needs the running data source.")])
    sample = world.listing_sample(pid, venue) or []
    if not sample:
        return _envelope(tool, False, f"No current listings observed on {venue}.",
                         {"venue": venue, "count": 0},
                         [_ev(UNKNOWN, f"No live page captured for this product on {venue} yet.")])
    stats = research.market_stats(sample)
    cheapest = sorted(sample, key=lambda s: float(s.get("price", 0)))[:5]
    return _envelope(tool, True,
                     f"{stats['n']} live {side} listings on {venue}, {stats['sellers']} sellers, "
                     f"fair ≈ ${stats['fair_usd']}.",
                     {"venue": venue, "count": stats["n"], "fair_usd": stats["fair_usd"],
                      "cheapest": [{"title": s.get("title"), "price": s.get("price"),
                                    "url": s.get("url"), "seller": s.get("seller")} for s in cheapest]},
                     [_ev(LIVE, f"Fetched {stats['n']} listings just now from the current page.")])


# --- 3. recent sold prices -------------------------------------------------

def check_sold_prices(store, opp, world=None) -> dict:
    """We observe ASKING prices (eBay Browse), not realised sales. Say so."""

    return _envelope("check_sold_prices", False,
                     "No realised sold-comp source is connected — only asking prices are observed.",
                     {"has_sold_comps": False},
                     [_ev(UNKNOWN, "Sold comps require eBay Marketplace Insights (special access) or a "
                                   "manual sold-price entry. Treat asking prices as an upper bound, not a "
                                   "clearing price.")])


# --- 4/5/6. recalculate profit --------------------------------------------

def recalc_profit(store, opp, buy_usd: float | None = None, sell_usd: float | None = None,
                  qty: int | None = None) -> dict:
    econ0 = opp.get("economics") or {}
    route = opp.get("route", {}) or {}
    bv, sv = route.get("buy_venue"), route.get("sell_venue")
    base = econ0.get("base", {})
    cur_buy = float(buy_usd if buy_usd is not None else (base.get("lines", [{}])[0].get("amount_usd") or 0))
    cur_sell = float(sell_usd if sell_usd is not None else (base.get("revenue_usd") or 0))
    q = int(qty if qty is not None else econ0.get("qty", 1) or 1)
    if not (bv and sv and cur_sell > 0):
        return _envelope("recalc_profit", False, "Not enough economics to recompute (venture or missing data).",
                         {}, [_ev(UNKNOWN, "This opportunity type isn't a priced flip.")])
    item = {"category": opp.get("category", "collectibles"),
            "weight_kg": (opp.get("item") or {}).get("weight_kg", 0.5)}
    repair = float(route.get("repair_cost", 0) or 0)
    e = economics.compute_flip(item, bv, sv, cur_buy, cur_sell, qty=q, extra_cost_usd=repair)
    return _envelope("recalc_profit", True,
                     f"At buy ${cur_buy:.2f} / sell ${cur_sell:.2f} × {q}: "
                     f"${e.total_net_usd:.2f} net ({e.base.margin_pct:.0f}% margin), "
                     f"pessimistic ${e.pessimistic.net_usd * q:.2f}.",
                     {"buy_usd": cur_buy, "sell_usd": cur_sell, "qty": q,
                      "net_usd": e.total_net_usd, "margin_pct": e.base.margin_pct,
                      "pessimistic_net_usd": round(e.pessimistic.net_usd * q, 2),
                      "capital_usd": e.capital_usd, "breakeven_sell_usd": e.breakeven_revenue_usd},
                     [_ev(CALCULATED, "Full fee/shipping/VAT waterfall recomputed at the prices you gave."),
                      _ev(SAVED if buy_usd is None else CALCULATED,
                          "Buy price " + ("from the stored opportunity." if buy_usd is None else "as you supplied."))])


def price_decision_tool(store, opp, current_buy_usd: float, current_sell_usd: float | None = None) -> dict:
    d = matching.price_decision(opp, current_buy_usd, current_sell_usd)
    return _envelope("price_decision", d["recommendation"] != "SKIP",
                     f"{d['recommendation']}: {d['reason']}", d,
                     [_ev(CALCULATED, f"Re-decided at your ${current_buy_usd:.2f} buy price."),
                      _ev(ASSUMPTION, "Sell price is the modelled asking-based estimate unless you gave one.")])


# --- 7. alternative suppliers ---------------------------------------------

def find_alternative_suppliers(store, opp, world=None) -> dict:
    """Surface other live listings of the same product as alternative buys —
    real listings only, never invented."""

    route = opp.get("route", {}) or {}
    venue = route.get("buy_venue")
    pid = opp.get("entity_id")
    if not (world and hasattr(world, "listing_sample") and venue):
        return _envelope("find_alternative_suppliers", False,
                         "No live source to search for alternatives.", {},
                         [_ev(UNKNOWN, "Alternative-supplier search needs the running data source.")])
    sample = world.listing_sample(pid, venue) or []
    disl = (opp.get("route") or {}).get("item_id")
    alts = [s for s in sorted(sample, key=lambda s: float(s.get("price", 0)))
            if s.get("item_id") != disl and not research.looks_junk(s.get("title", ""))][:5]
    if not alts:
        return _envelope("find_alternative_suppliers", False, "No alternative live listings found.",
                         {"count": 0}, [_ev(LIVE, "Checked the current page; no other clean listings.")])
    return _envelope("find_alternative_suppliers", True, f"{len(alts)} alternative live listings.",
                     {"alternatives": [{"title": s.get("title"), "price": s.get("price"),
                                        "url": s.get("url"), "seller": s.get("seller")} for s in alts]},
                     [_ev(LIVE, "These are real listings from the current page, not suggestions.")])


# --- 8. check link alive ---------------------------------------------------

def check_links(store, opp, url: str | None = None, client=None) -> dict:
    target = url or (opp.get("route", {}) or {}).get("buy_url") or ""
    if not target:
        return _envelope("check_links", False, "No link on this opportunity to check.", {},
                         [_ev(UNKNOWN, "This opportunity carries no exact buy URL.")])
    res = matching.check_link(target, client=client)
    store.add_chat_link(opp["id"], target, res["alive"],
                        note=res["note"])
    label = LIVE
    txt = (f"Checked just now: {res['note']}." if res["alive"]
           else f"Checked just now: {res['note']} — the listing may be gone; ask for alternatives.")
    return _envelope("check_links", res["alive"],
                     ("Listing is live." if res["alive"] else "Listing could not be confirmed live."),
                     res, [_ev(label, txt)])


# --- 9. inventory availability --------------------------------------------

def check_inventory(store, opp, world=None) -> dict:
    route = opp.get("route", {}) or {}
    venue = route.get("buy_venue")
    pid = opp.get("entity_id")
    if not (world and hasattr(world, "listing") and venue):
        return _envelope("check_inventory", False, "No live source to check inventory.", {},
                         [_ev(UNKNOWN, "Inventory depth needs the running data source.")])
    snap = world.listing(pid, venue) or {}
    depth = int(snap.get("stock", 0))
    return _envelope("check_inventory", depth > 0,
                     f"{depth} units visible at {venue}." if depth else "No visible inventory.",
                     {"venue": venue, "stock": depth, "sellers": snap.get("sellers")},
                     [_ev(LIVE if depth else UNKNOWN, "Inventory read from the latest observation.")])


# --- 10. still valid? ------------------------------------------------------

def check_validity(store, opp) -> dict:
    stored = store.get_opportunity(opp["id"]) or opp
    status = stored.get("status")
    level = stored.get("verification_level")
    alive = status == "active"
    return _envelope("check_validity", alive,
                     f"Opportunity is {status} (verification: {level}).",
                     {"status": status, "verification_level": level,
                      "invalidation_reason": stored.get("invalidation_reason", "")},
                     [_ev(SAVED, f"Status from the last re-verification cycle: {status}."
                          + (f" Reason: {stored.get('invalidation_reason')}" if not alive else ""))])


# --- 11. compare a listing the operator provides ---------------------------

def compare_listing(store, opp, listing_text: str, price_usd: float | None = None) -> dict:
    product = opp.get("title", "").split(" — ")[0]
    m = matching.match_product(product, listing_text)
    ev = [_ev(CALCULATED, f"Product match: {m['status']} ({m['score']*100:.0f}% of the name present).")]
    if m["differences"]:
        ev.append(_ev(UNKNOWN if m["status"] in ("POSSIBLE_MISMATCH", "INSUFFICIENT_INFO") else CALCULATED,
                      "Differences: " + "; ".join(m["differences"])))
    data = {"match": m}
    summary = f"{m['status']} — " + (m["differences"][0] if m["differences"] else "looks like the right product.")
    if price_usd is not None and m["status"] in ("EXACT_MATCH", "LIKELY_MATCH"):
        pd = matching.price_decision(opp, float(price_usd))
        data["price_decision"] = pd
        ev.append(_ev(CALCULATED, f"At ${price_usd:.2f}: {pd['recommendation']} — {pd['reason']}"))
        summary += f"  Price verdict: {pd['recommendation']}."
    return _envelope("compare_listing", m["status"] in ("EXACT_MATCH", "LIKELY_MATCH"), summary, data, ev)


# --- 12. generate a selling listing ---------------------------------------

def generate_listing(store, opp, ai=None) -> dict:
    econ = opp.get("economics") or {}
    sell = (econ.get("base") or {}).get("revenue_usd")
    if ai is not None and getattr(ai, "configured", lambda: False)():
        kit, err = ai.generate_kit(opp)
        if kit:
            return _envelope("generate_listing", True, "Generated a selling kit with Claude.",
                             {"kit": kit}, [_ev(AI, "Draft copy written by Claude — review before publishing.")])
    # Deterministic template fallback — never blocks on a key.
    title = opp.get("title", "").split(" — ")[0]
    draft = {
        "title": f"{title} — {opp.get('category', '').replace('_', ' ')}".strip(" —"),
        "suggested_price_usd": round(float(sell), 2) if sell else None,
        "description": (f"{title}. Ships from Thailand. Please see photos for exact condition. "
                        f"Message with questions before buying."),
        "note": "Template draft (no AI key) — edit before listing; you publish it, not the assistant.",
    }
    return _envelope("generate_listing", True, "Drafted a listing from a template.",
                     {"draft": draft}, [_ev(CALCULATED, "Template draft; price from the modelled sell estimate.")])


# --- 13. update checklist --------------------------------------------------

def update_checklist(store, opp, step: str, done: bool = True) -> dict:
    store.set_checklist_item(opp["id"], step, done)
    items = store.chat_checklist(opp["id"])
    return _envelope("update_checklist", True,
                     f"Checklist updated: '{step}' → {'done' if done else 'todo'}.",
                     {"checklist": items}, [_ev(SAVED, "Saved to this opportunity's execution checklist.")])


# --- 14. record purchase/expense/sale/refund/profit -----------------------

_LEDGER_KINDS = {"purchase", "expense", "sale", "refund_received", "refund_issued"}


def record_transaction(store, opp, kind: str, amount_usd: float, qty: int = 0, note: str = "") -> dict:
    if kind not in _LEDGER_KINDS:
        return _envelope("record_transaction", False,
                         f"Unknown transaction kind '{kind}'.", {"valid_kinds": sorted(_LEDGER_KINDS)},
                         [_ev(UNKNOWN, "Use purchase/expense/sale/refund_received/refund_issued.")])
    store.add_chat_ledger(opp["id"], kind, float(amount_usd), int(qty), note)
    pnl = store.realized_pnl(opp["id"])
    return _envelope("record_transaction", True,
                     f"Recorded {kind} ${amount_usd:.2f}"
                     + (f" ×{qty}" if qty else "") + f". Realised net now ${pnl['net_usd']:.2f}.",
                     {"pnl": pnl, "ledger": store.chat_ledger(opp["id"])},
                     [_ev(SAVED, "You told me you did this; recorded to this opportunity's ledger only.")])


# --- 15. escalate for full re-verification --------------------------------

def escalate_reverification(store, opp) -> dict:
    """Flags the opportunity for a fresh re-verification on the next cycle. Does
    not itself spend an API call — the cycle does the work."""

    store.set_chat_state(opp["id"], store.get_chat_state(opp["id"]).get("state", "researching"),
                         next_action=None, data={"reverify_requested": True,
                                                 "reverify_requested_at": time.time()})
    return _envelope("escalate_reverification", True,
                     "Flagged for full re-verification on the next research cycle.",
                     {"reverify_requested": True},
                     [_ev(SAVED, "The next cycle will independently re-check price, supply, demand and fees.")])
