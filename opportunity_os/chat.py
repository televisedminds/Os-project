"""The per-opportunity execution chat (Phase 13).

A `ChatSession` is permanently scoped to ONE opportunity id. It already knows
the whole deal (product, venues, prices, economics, verification, executability,
evidence ledger), keeps its own message history and execution state isolated by
that id, and drives the deal forward with the deterministic tools in
`chat_tools`. It works fully without an LLM — every factual answer comes from a
tool result or the stored evidence, tagged so the operator can see whether it
is LIVE, SAVED, CALCULATED, an ASSUMPTION, or UNKNOWN. When an Anthropic key is
set, Claude turns those grounded facts into natural language; it never invents a
number the tools didn't produce.

The chat researches, recalculates, drafts, validates and records. It never
transacts — no path here buys, pays, publishes a listing, accepts an offer,
cancels or refunds. That stays with the operator.
"""

from __future__ import annotations

import re

from . import chat_tools as T
from . import execution

# tool name -> (callable, needs_world, needs_ai)
TOOLS = {
    "refresh_buy_listings": (lambda s, o, w, ai, p: T.refresh_listings(s, o, w, side="buy"), True, False),
    "refresh_sell_listings": (lambda s, o, w, ai, p: T.refresh_listings(s, o, w, side="sell"), True, False),
    "check_sold_prices": (lambda s, o, w, ai, p: T.check_sold_prices(s, o, w), True, False),
    "recalc_profit": (lambda s, o, w, ai, p: T.recalc_profit(s, o, **_pick(p, "buy_usd", "sell_usd", "qty")), False, False),
    "price_decision": (lambda s, o, w, ai, p: T.price_decision_tool(s, o, float(p["current_buy_usd"]),
                                                                    p.get("current_sell_usd")), False, False),
    "find_alternative_suppliers": (lambda s, o, w, ai, p: T.find_alternative_suppliers(s, o, w), True, False),
    "check_links": (lambda s, o, w, ai, p: T.check_links(s, o, url=p.get("url")), False, False),
    "check_inventory": (lambda s, o, w, ai, p: T.check_inventory(s, o, w), True, False),
    "check_validity": (lambda s, o, w, ai, p: T.check_validity(s, o), False, False),
    "compare_listing": (lambda s, o, w, ai, p: T.compare_listing(s, o, p.get("listing_text", ""),
                                                                 p.get("price_usd")), False, False),
    "generate_listing": (lambda s, o, w, ai, p: T.generate_listing(s, o, ai=ai), False, True),
    "update_checklist": (lambda s, o, w, ai, p: T.update_checklist(s, o, p["step"], bool(p.get("done", True))), False, False),
    "record_transaction": (lambda s, o, w, ai, p: T.record_transaction(s, o, p["kind"], float(p["amount_usd"]),
                                                                       int(p.get("qty", 0)), p.get("note", "")), False, False),
    "escalate_reverification": (lambda s, o, w, ai, p: T.escalate_reverification(s, o), False, False),
}

# When a transaction is recorded, advance the execution state.
_TXN_STATE = {"purchase": execution.PURCHASED, "sale": execution.SOLD}


def _pick(p: dict, *keys):
    return {k: p[k] for k in keys if k in p and p[k] is not None}


class ChatSession:
    def __init__(self, store, opp_id: str, world=None, ai=None):
        self.store = store
        self.opp_id = opp_id
        self.opp = store.get_opportunity(opp_id)
        self.world = world
        self.ai = ai

    # ---- context / state -------------------------------------------------

    def exists(self) -> bool:
        return self.opp is not None

    def state(self) -> dict:
        st = self.store.get_chat_state(self.opp_id)
        na = execution.next_action(st.get("state", execution.NOT_STARTED), self.opp or {})
        return {"state": st.get("state", execution.NOT_STARTED), "next_action": na,
                "reverify_requested": st.get("data", {}).get("reverify_requested", False)}

    def context(self) -> dict:
        """The full, opportunity-scoped context the chat reasons over."""

        o = self.opp or {}
        econ = o.get("economics") or {}
        route = o.get("route") or {}
        return {
            "opportunity_id": self.opp_id,
            "title": o.get("title"), "type": o.get("type"), "category": o.get("category"),
            "subtitle": o.get("subtitle"),
            "buy_venue": route.get("buy_venue"), "sell_venue": route.get("sell_venue"),
            "buy_url": route.get("buy_url"),
            "verification_level": o.get("verification_level"),
            "single_source": o.get("single_source"),
            "net_usd": econ.get("total_net_usd"),
            "capital_usd": econ.get("capital_usd"),
            "margin_pct": (econ.get("base") or {}).get("margin_pct"),
            "window_days": o.get("window_days"),
            "evidence": o.get("evidence", []),
            "executability": o.get("executability", {}),
            "checklist": self.store.chat_checklist(self.opp_id),
            "realized_pnl": self.store.realized_pnl(self.opp_id),
            "links_checked": self.store.chat_links(self.opp_id, 5),
            "state": self.state(),
        }

    def history(self) -> list[dict]:
        return self.store.chat_history(self.opp_id)

    # ---- structured tool execution --------------------------------------

    def run_tool(self, tool: str, params: dict | None = None) -> dict:
        params = params or {}
        if tool not in TOOLS:
            return {"tool": tool, "ok": False, "summary": f"Unknown tool '{tool}'.",
                    "data": {}, "evidence": []}
        fn, _, _ = TOOLS[tool]
        try:
            res = fn(self.store, self.opp, self.world, self.ai, params)
        except (KeyError, ValueError, TypeError) as e:
            return {"tool": tool, "ok": False, "summary": f"Bad parameters for {tool}: {e}",
                    "data": {}, "evidence": []}
        # A recorded purchase/sale is a FACT about what the operator did, so it
        # sets the execution state directly (you can't "buy" without having been
        # ready) rather than requiring a planned transition.
        if tool == "record_transaction" and res.get("ok"):
            target = _TXN_STATE.get(params.get("kind"))
            if target and not execution.is_terminal(self.state()["state"]):
                self.store.set_chat_state(self.opp_id, target, None)
        return res

    def set_state(self, target: str) -> dict:
        cur = self.state()["state"]
        if not execution.can_transition(cur, target):
            return {"ok": False, "summary": f"Can't move from {cur} to {target}.",
                    "state": self.state()}
        self.store.set_chat_state(self.opp_id, target, None)
        return {"ok": True, "summary": f"State → {target}.", "state": self.state()}

    # ---- natural-language turn ------------------------------------------

    def send(self, message: str) -> dict:
        """One conversational turn. Routes the message to a tool by intent, runs
        it, records both sides, and returns an evidence-grounded reply. With an
        AI key, Claude phrases the reply from the tool result; without one, a
        deterministic summary is returned."""

        self.store.add_chat_message(self.opp_id, "user", message)
        if not self.exists():
            reply = ("This opportunity id isn't in the store — I can't answer without its evidence. "
                     "Open it from the feed first.")
            self.store.add_chat_message(self.opp_id, "assistant", reply, [{"label": T.UNKNOWN, "text": reply}])
            return {"reply": reply, "evidence": [{"label": T.UNKNOWN, "text": reply}], "tool": None,
                    "state": self.state()}

        tool, params = self._route(message)
        res = self.run_tool(tool, params) if tool else self._answer_from_context(message)
        reply = self._phrase(message, res)
        self.store.add_chat_message(self.opp_id, "assistant", reply,
                                    res.get("evidence", []), res.get("tool"))
        return {"reply": reply, "ok": res.get("ok", True), "evidence": res.get("evidence", []),
                "tool": res.get("tool"), "data": res.get("data", {}), "state": self.state()}

    # ---- intent routing (deterministic; works with no LLM) ---------------

    def _route(self, msg: str):
        m = msg.lower()
        num = _first_price(msg)
        if num is not None and "฿" in msg:                # THB → USD for the engines
            from . import economics
            num = round(num / economics.USD_THB, 2)
        if any(w in m for w in ("dead", "gone", "not available", "can't find", "cannot find", "unavailable")):
            return "check_links", {}          # then the reply suggests alternatives
        if any(w in m for w in ("link", "url", "where to buy", "buying link")):
            return "check_links", {}
        price_intent = any(w in m for w in ("still good", "good price", "good buy", "good buying",
                                            "buy at", "should i buy", "still buy", "worth buying",
                                            "worth it", "accept", "offer"))
        if price_intent and num is not None:
            return "price_decision", {"current_buy_usd": num}
        if "compare" in m or "this listing" in m:
            return "compare_listing", {"listing_text": msg, "price_usd": num}
        if any(w in m for w in ("recalcul", "recompute", "if shipping", "at ฿", "new price")) and num:
            return "recalc_profit", {"buy_usd": num}
        if any(w in m for w in ("how many", "quantity", "how much should i buy")):
            return "recalc_profit", {}
        if any(w in m for w in ("still valid", "expired", "still active", "invalidated")):
            return "check_validity", {}
        if any(w in m for w in ("listing title", "description", "generate listing", "write the listing", "sell copy")):
            return "generate_listing", {}
        if any(w in m for w in ("alternative", "another listing", "other seller", "different supplier")):
            return "find_alternative_suppliers", {}
        if "refresh" in m and "sell" in m:
            return "refresh_sell_listings", {}
        if "refresh" in m:
            return "refresh_buy_listings", {}
        if "inventory" in m or "in stock" in m or "how many available" in m:
            return "check_inventory", {}
        if any(w in m for w in ("sold comp", "sold price", "recent sales")):
            return "check_sold_prices", {}
        if "re-verif" in m or "reverif" in m or "escalate" in m:
            return "escalate_reverification", {}
        if m.startswith("record ") or "i bought" in m or "i sold" in m or "i paid" in m:
            return "record_transaction", self._parse_txn(msg)
        return None, {}

    def _parse_txn(self, msg: str) -> dict:
        m = msg.lower()
        kind = ("sale" if ("sold" in m or "sale" in m) else
                "expense" if "expense" in m else
                "refund_received" if "refund" in m else "purchase")
        amt = _first_price(msg) or 0.0
        qty_m = re.search(r"(\d+)\s*(?:units?|pcs?|x\b)", m)
        return {"kind": kind, "amount_usd": amt, "qty": int(qty_m.group(1)) if qty_m else 0}

    def _answer_from_context(self, msg: str) -> dict:
        """No tool matched → answer from stored evidence, or admit we can't."""

        m = msg.lower()
        o = self.opp
        if any(w in m for w in ("which product", "exact product", "correct model", "what should i buy",
                                "right model", "which model")):
            title = o.get("title", "").split(" — ")[0]
            cond = next((e["value"] for e in o.get("evidence", []) if e["field"] == "counterfeit_condition_risk"), "")
            return {"tool": None, "ok": True,
                    "summary": f"Buy exactly: {title}. Match the model number in the listing photos before paying.",
                    "data": {"product": title},
                    "evidence": [{"label": T.SAVED, "text": f"Product of record for this opportunity: {title}."},
                                 {"label": T.UNKNOWN, "text": "Condition/counterfeit risk is not independently "
                                  "assessed — verify from the seller's photos."}]}
        sell_intent = (re.search(r"\b(sell|selling)\b", m) or re.search(r"\bwhere\b", m)
                       or any(w in m for w in ("platform", "register", "payout", "document",
                                               "thai", "customs", "import", "export")))
        if sell_intent:
            ex = o.get("executability", {})
            checks = ex.get("checks", [])
            return {"tool": None, "ok": True,
                    "summary": "Thailand executability for this deal:",
                    "data": {"executability": ex},
                    "evidence": [{"label": T.SAVED if c["status"] != "unknown" else T.UNKNOWN,
                                  "text": f"{c['question']} {c['status'].upper()} — {c['detail']}"}
                                 for c in checks[:6]]}
        if any(w in m for w in ("next", "what do i do", "what now")):
            na = self.state()["next_action"]
            return {"tool": None, "ok": True, "summary": f"Next: {na['title']}",
                    "data": {"next_action": na},
                    "evidence": [{"label": T.CALCULATED, "text": f"{na['title']} — {na['why']} {na['todo']}"}]}
        # Genuinely unknown — do NOT invent.
        return {"tool": None, "ok": False,
                "summary": "I don't have evidence to answer that for this opportunity. "
                           "Try: refresh listings, check the link, recalc at a price, or ask what's next.",
                "data": {}, "evidence": [{"label": T.UNKNOWN, "text": "No stored or fetchable evidence covers this."}]}

    def _phrase(self, message: str, res: dict) -> str:
        """Turn a tool result into a reply. Claude phrases it when configured;
        otherwise the deterministic summary IS the reply."""

        base = res.get("summary", "")
        # A dead-link result gets an actionable nudge appended.
        if res.get("tool") == "check_links" and not res.get("ok"):
            alt = self.run_tool("find_alternative_suppliers", {})
            if alt.get("ok"):
                base += " I found alternative live listings — say 'show alternatives'."
        if self.ai is not None and getattr(self.ai, "configured", lambda: False)():
            phrased = self._ai_phrase(message, res)
            if phrased:
                return phrased
        return base

    def _ai_phrase(self, message: str, res: dict) -> str:
        """Ask Claude to phrase the reply from the tool result ONLY. Grounded;
        returns '' on any failure so the deterministic summary stands."""

        try:
            import json
            client = self.ai._get_client()
            system = ("You are an execution assistant for ONE resale/venture opportunity, for an "
                      "operator in Thailand. Answer ONLY from the tool_result and evidence given. "
                      "Never invent prices, links, inventory, fees or eligibility. If the evidence "
                      "doesn't answer it, say you don't have that evidence. Keep it to 2-4 sentences.")
            payload = {"user_message": message, "tool_result": res.get("summary"),
                       "data": res.get("data"), "evidence": res.get("evidence")}
            r = client.messages.create(
                model=self.ai.cfg.ai_model, max_tokens=400, system=system,
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}])
            if getattr(r, "stop_reason", "") == "refusal":
                return ""
            return next((b.text for b in r.content if b.type == "text"), "") or ""
        except Exception:  # noqa: BLE001
            return ""


_CURRENCY_RE = re.compile(r"[฿$]\s*([0-9][0-9,]*\.?[0-9]*)")
_NUMBER_RE = re.compile(r"\b([0-9][0-9,]*\.?[0-9]*)\b")


def _first_price(text: str) -> float | None:
    """Prefer a currency-prefixed amount ('$180'); fall back to the largest
    bare number so '3 units for 180' still reads the 180, not the 3."""

    t = text.replace(",", "")
    cur = _CURRENCY_RE.search(t)
    if cur:
        try:
            return float(cur.group(1))
        except ValueError:
            pass
    nums = []
    for m in _NUMBER_RE.finditer(t):
        try:
            nums.append(float(m.group(1)))
        except ValueError:
            continue
    return max(nums) if nums else None
