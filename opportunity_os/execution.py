"""The execution state machine for a single opportunity (Phase 13).

An opportunity chat is not general conversation — it drives one deal from
research to realised profit. This module defines the lifecycle, the legal
transitions between states, and — crucially — the NEXT unfinished action at
each state, so the assistant always knows what to tell the operator to do next.

States progress forward; CANCELLED and INVALIDATED are terminal off-ramps that
can be reached from anywhere.
"""

from __future__ import annotations

# Lifecycle states (in order).
NOT_STARTED = "not_started"
RESEARCHING = "researching"
READY_TO_BUY = "ready_to_buy"
PURCHASED = "purchased"
IN_TRANSIT = "in_transit"
RECEIVED = "received"
LISTED = "listed"
SOLD = "sold"
COMPLETED = "completed"
CANCELLED = "cancelled"
INVALIDATED = "invalidated"

_ORDER = [NOT_STARTED, RESEARCHING, READY_TO_BUY, PURCHASED, IN_TRANSIT,
          RECEIVED, LISTED, SOLD, COMPLETED]
_TERMINAL = {COMPLETED, CANCELLED, INVALIDATED}

# Allowed forward transitions (plus the universal off-ramps below).
_NEXT = {
    NOT_STARTED: [RESEARCHING, READY_TO_BUY],
    RESEARCHING: [READY_TO_BUY],
    READY_TO_BUY: [PURCHASED],
    PURCHASED: [IN_TRANSIT, RECEIVED],
    IN_TRANSIT: [RECEIVED],
    RECEIVED: [LISTED],
    LISTED: [SOLD],
    SOLD: [COMPLETED],
}


def can_transition(current: str, target: str) -> bool:
    if target in (CANCELLED, INVALIDATED):
        return current not in _TERMINAL
    return target in _NEXT.get(current, [])


def is_terminal(state: str) -> bool:
    return state in _TERMINAL


# The next concrete action to take at each state, product-flip flavoured for
# arbitrage/refurbishment and venture-flavoured for build-it opportunities.
def next_action(state: str, opp: dict) -> dict:
    """-> {title, why, todo} — the single next unfinished step."""

    is_flip = opp.get("type") in ("product_arbitrage", "refurbishment")
    route = opp.get("route", {}) or {}
    buy = route.get("buy_venue", "the source")
    sell = route.get("sell_venue", "the sell venue")

    if is_flip:
        table = {
            NOT_STARTED: ("Confirm the exact product before anything else",
                          "The cheaper variant of this item resells for much less — buying the wrong one wipes the edge.",
                          "Ask the chat 'which exact product?' and verify the listing photos match the model/variant."),
            RESEARCHING: ("Validate the buy listing is live at the expected price",
                          "Prices and availability move; the edge only exists at the modelled buy price.",
                          "Use 'check the link' — the chat confirms the URL is live and reports today's price."),
            READY_TO_BUY: (f"Buy on {buy} — then record the purchase",
                           "This is the money step; record it so the chat can track your real P&L.",
                           "Purchase at or below the max buy price, then 'record purchase <amount> <qty>'."),
            PURCHASED: ("Route the goods and mark in-transit",
                        "Consolidation/forwarding choices change your per-unit shipping.",
                        "Arrange shipping to your prep/TH address, then tell the chat 'mark in transit'."),
            IN_TRANSIT: ("Inspect on arrival",
                         "Condition determines whether you list, discount, or return.",
                         "When it arrives, 'mark received' and inspect against the listing description."),
            RECEIVED: (f"List on {sell} (generate the listing)",
                       "A good title/price is a ranking and conversion factor.",
                       "Use 'generate selling listing', then 'mark listed' once it's live."),
            LISTED: ("Sell, then record the sale",
                     "Recording the sale closes the loop and feeds the learning engine.",
                     "When it sells, 'record sale <amount>'."),
            SOLD: ("Repatriate funds and record realised profit",
                   "Compare predicted vs realised so the models calibrate to YOUR results.",
                   "Withdraw USD→THB, then 'complete' to log predicted-vs-realised profit."),
            COMPLETED: ("Done — nothing left on this deal.", "", ""),
            CANCELLED: ("Cancelled.", "", ""),
            INVALIDATED: ("Invalidated — the edge is gone; do not buy more.",
                          "Re-verification found the opportunity no longer holds.",
                          "Stop buying inventory; review the invalidation reason on the opportunity."),
        }
    else:
        table = {
            NOT_STARTED: ("Confirm the demand thesis and the first customer",
                          "A venture with no concrete first customer is an idea, not an opportunity.",
                          "Ask the chat to summarise the demand evidence and name a first-customer channel."),
            RESEARCHING: ("Scope the smallest sellable version",
                          "Ship a v1 that one customer will pay for, not the full vision.",
                          "Define the minimum offer + price, then 'mark ready to buy' (i.e. ready to build)."),
            READY_TO_BUY: ("Build/launch the minimum offer — record the startup spend",
                           "Track real startup cost against the model.",
                           "Stand up the offer, then 'record purchase <startup cost>'."),
            PURCHASED: ("Get it in front of demand",
                        "Distribution is the whole game for ventures.",
                        "Publish + run the first acquisition test, then 'mark listed'."),
            LISTED: ("Land the first paying customer — record the sale",
                     "First revenue validates the whole thesis.",
                     "On first payment, 'record sale <amount>'."),
            SOLD: ("Record realised monthly economics",
                   "Compare to the model to calibrate future venture scoring.",
                   "'complete' to log predicted-vs-realised."),
            COMPLETED: ("Done.", "", ""),
            CANCELLED: ("Cancelled.", "", ""),
            INVALIDATED: ("Invalidated — demand faded; stop investing.", "", ""),
        }
        # ventures skip the physical transit/receive states
    title, why, todo = table.get(state, table[NOT_STARTED])
    return {"state": state, "title": title, "why": why, "todo": todo,
            "is_terminal": is_terminal(state)}
