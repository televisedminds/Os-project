"""Playbook + automation builders.

A verified opportunity is not "go sell Pokémon". It is: this supplier, this
quantity, this route, this customs form, this pre-written listing at this
price, this repricing rule — and this is the part a robot can run for you.
Thailand-specific logistics (proxy buyers, Thailand Post, CN22, Payoneer) are
baked into every product playbook.
"""

from __future__ import annotations

from ..models import AutomationPlan, Playbook, PlaybookStep
from .. import economics


def _carrier(weight_kg: float) -> str:
    if weight_kg <= 2.0:
        return "Thailand Post registered air / ePacket (tracked)"
    return "DHL eCommerce or EMS (volumetric — get a quote first)"


def _listing_for(cand: dict) -> dict:
    item, sell = cand["item"], cand["sell_usd"]
    breakeven = cand["economics"].breakeven_revenue_usd
    price = round(max(breakeven * 1.12, sell * 0.97), 2)
    return {
        "platform": economics.VENUES[cand["sell_venue"]]["name"],
        "title": f"{item['name']} — ships tracked from Thailand"[:80],
        "price_usd": price,
        "pricing_rule": f"Undercut lowest active listing by ~3%; never below ${round(breakeven * 1.12, 2)} "
                        f"(breakeven ${breakeven} + 12%).",
        "description": (f"{item['name']}. Sourced {economics.VENUES[cand['buy_venue']]['name']}, "
                        f"inspected and photographed before dispatch. Tracked international shipping with "
                        f"customs paperwork handled. Message for combined-shipping discounts."),
        "photos": "6+ photos in daylight: front/back, close-ups of condition, serials/authentication marks.",
    }


def build_playbook(cand: dict) -> Playbook:
    if cand["kind"] == "flip":
        return _flip_playbook(cand)
    return _venture_playbook(cand)


def _flip_playbook(cand: dict) -> Playbook:
    econ, feas = cand["economics"], cand["feasibility"]
    buy_v, sell_v = economics.VENUES[cand["buy_venue"]], economics.VENUES[cand["sell_venue"]]
    item = cand["item"]
    listing = _listing_for(cand)
    proxy = feas.requires_proxy

    steps = [
        PlaybookStep(1, f"Secure {econ.qty} units on {buy_v['name']}",
                     (f"Buy through a proxy (Buyee/ZenMarket): add to cart, choose consolidation. "
                      if proxy else "Order directly. ")
                     + f"Price cap ${cand['buy_usd'] * 1.08:.2f}/unit — walk away above it. "
                       f"Capital required ≈ ${econ.capital_usd:,.2f} (฿{econ.thb.get('capital_thb', 0):,.0f}).",
                     automatable=True, tool="saved-search alerts + auto-bid caps", eta="day 0"),
        PlaybookStep(2, "Route the goods",
                     f"{econ.route_note.split('.')[0]}. Carrier: {_carrier(item['weight_kg'])}. "
                     + ("Consolidate at the proxy warehouse to cut per-unit shipping." if proxy else ""),
                     automatable=False, eta="day 0–1"),
        PlaybookStep(3, "Customs paperwork",
                     " ".join(feas.customs_notes) or "Standard CN22 declaration.",
                     automatable=True, tool="pre-filled CN22/CN23 templates", eta="dispatch day"),
        PlaybookStep(4, f"List on {sell_v['name']} (pre-written below)",
                     f"Title: “{listing['title']}”. Price ${listing['price_usd']} — {listing['pricing_rule']}",
                     automatable=True, tool="listing API / bulk lister", eta="day 1"),
        PlaybookStep(5, "Fulfil fast",
                     "Dispatch within 24h of sale, upload tracking immediately — handling time is a ranking factor. "
                     f"Payouts land via {feas.payment_rails[1] if len(feas.payment_rails) > 1 else 'Payoneer'}.",
                     automatable=True, tool="label printing + tracking sync", eta="ongoing"),
        PlaybookStep(6, "Reprice and monitor the window",
                     f"Re-check this opportunity every cycle. Exit (liquidate at breakeven+) if margin compresses "
                     f"below 8% or the platform re-verification invalidates it. Window estimate: "
                     f"{cand['window_days']} days.",
                     automatable=True, tool="repricer bound to this opportunity id", eta="daily"),
        PlaybookStep(7, "Repatriate and record",
                     "Withdraw USD → THB via Payoneer/Wise; log proceeds for Thai personal income tax filing. "
                     "Then record the outcome in Opportunity OS so the models learn.",
                     automatable=False, eta="after sale"),
    ]
    return Playbook(steps=steps, listing=listing,
                    first_action=steps[0].title,
                    timeline_days=round(cand["window_days"] + 3, 1))


def _venture_playbook(cand: dict) -> Playbook:
    niche = cand["niche"]
    kind, geo = niche["kind"], niche["geo"]
    price = niche["price_point_usd"]
    channels = {
        "digital": "Product Hunt, the exact subreddits/forums where the complaint spiked, YouTube tutorials",
        "info": "SEO article cluster + TikTok/YouTube shorts answering the top questions",
        "local": "Facebook groups (คอนโด/หมู่บ้าน), LINE OA broadcast, Google Business Profile",
        "b2b": "Direct outreach to condo juristic offices and dealership service managers",
    }[kind]
    build = {
        "digital": "Ship the smallest tool that solves the spiking query (web app; add LINE login if TH-facing).",
        "info": "Produce the guide/calculator from the top 20 real questions; Thai + English if TH-facing.",
        "local": "Kit + insurance + LINE OA booking flow; service Sukhumvit/Sathorn radius first.",
        "b2b": "Get the certification/partnership prerequisite, then package a fixed-price pilot.",
    }[kind]
    steps = [
        PlaybookStep(1, "Validate with 10 real conversations",
                     f"DM/interview people behind the demand posts before building. Confirm they'd pay ~${price:.0f}.",
                     automatable=False, eta="day 0–2"),
        PlaybookStep(2, "Build the minimum sellable version", build, automatable=(kind in ("digital", "info")),
                     tool="site builder / no-code + payment link", eta="day 2–7"),
        PlaybookStep(3, "Price it",
                     f"Launch at ${price:.0f} ({'฿' + format(round(price * economics.USD_THB), ',') if geo in ('TH', 'Bangkok') else 'USD'}) "
                     f"with a founding-user discount; raise after 10 sales.",
                     automatable=False, eta="day 7"),
        PlaybookStep(4, "Distribute where the demand already is", channels, automatable=True,
                     tool="scheduled posts + answer-the-question content", eta="day 7–14"),
        PlaybookStep(5, "Automate delivery and support",
                     "Payment → access automation; FAQ autoresponder; collect every question asked into the product.",
                     automatable=True, tool="Stripe/Gumroad webhooks, LINE OA auto-reply", eta="day 14+"),
        PlaybookStep(6, "Report outcomes back",
                     "Record revenue/failure in Opportunity OS — verified outcomes tune future scoring.",
                     automatable=False, eta="day 30"),
    ]
    return Playbook(steps=steps, listing=None, first_action=steps[0].title, timeline_days=30.0)


def build_automation(cand: dict) -> AutomationPlan:
    if cand["kind"] == "flip":
        tasks = [
            {"task": "Watch prices/stock on both venues", "automatable": True, "tool": "scanner agents (already running)"},
            {"task": "Auto-buy under price cap", "automatable": True, "tool": "proxy API / bid sniper with hard cap"},
            {"task": "Create and publish listings", "automatable": True, "tool": "marketplace listing API"},
            {"task": "Answer buyer messages", "automatable": True, "tool": "template + LLM draft, human send"},
            {"task": "Print labels, book pickup", "automatable": True, "tool": "carrier API"},
            {"task": "Reprice daily", "automatable": True, "tool": "repricer rule from playbook step 6"},
            {"task": "Customs declarations", "automatable": False, "tool": "pre-filled forms, human signs"},
            {"task": "Inspect / authenticate items", "automatable": False, "tool": "human (photos archived)"},
        ]
        checkpoints = ["Approve total capital outlay before auto-buy runs",
                       "Physical inspection & authentication before listing",
                       "Sign customs declarations"]
    else:
        tasks = [
            {"task": "Monitor demand signals", "automatable": True, "tool": "trend + social scanners"},
            {"task": "Deliver product after payment", "automatable": True, "tool": "payment webhooks"},
            {"task": "Publish distribution content", "automatable": True, "tool": "scheduler"},
            {"task": "Answer pre-sale questions", "automatable": True, "tool": "FAQ bot, human escalation"},
            {"task": "Build the actual product", "automatable": False, "tool": "you (with AI assistance)"},
            {"task": "Customer interviews", "automatable": False, "tool": "human"},
        ]
        checkpoints = ["Validate willingness-to-pay before building", "Review refunds/complaints weekly"]

    coverage = round(100 * sum(t["automatable"] for t in tasks) / len(tasks), 0)
    return AutomationPlan(coverage_pct=coverage, tasks=tasks, human_checkpoints=checkpoints)
