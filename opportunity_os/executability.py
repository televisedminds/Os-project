"""The Thailand executability engine (Phase 10).

Every opportunity must be answerable from Thailand before it can be called
"execution ready". This module produces a structured report that answers, for
one opportunity, the concrete questions a Thailand-based operator actually has —
and, crucially, marks anything it cannot establish as UNKNOWN rather than
guessing. `execution_ready()` is True only when nothing critical is unknown and
nothing is a hard blocker.

Each answer is a `Check` with a status:

* yes / no       — established from venue-access + category rules
* estimated      — derived (e.g. taxes are included in the priced waterfall)
* unknown        — genuinely not established; blocks EXECUTION_READY

Rates/rules are the same reference values the rest of the demo uses; production
syncs the official Thai Customs schedule + platform policy pages.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from . import thailand

# yes/no/estimated/unknown
YES, NO, ESTIMATED, UNKNOWN = "yes", "no", "estimated", "unknown"

# Categories with real Thai import/export friction. Honest, conservative flags —
# "restricted" means extra paperwork/licensing, not necessarily banned.
_IMPORT_RESTRICTED = {
    "food": "Thai FDA (อย.) import notification for consumables; sample testing possible.",
    "electronics": "Radio-emitting devices (wireless) may need NBTC type approval.",
    "audio": "Wireless audio may need NBTC approval; lithium-battery transport rules apply.",
    "watches": "High-value: expect customs valuation scrutiny; keep purchase invoices.",
    "luxury_bags": "High-value + brand: counterfeit seizure risk; keep authentication proof.",
}
_EXPORT_RESTRICTED = {
    "food": "US-bound food: FDA Prior Notice required.",
    "luxury_bags": "Brand goods: platform authentication + insurance recommended.",
    "watches": "High-value: insured registered mail; authentication photos.",
}


@dataclass
class Check:
    question: str
    status: str            # yes | no | estimated | unknown
    detail: str
    critical: bool = True  # unknown on a critical check blocks EXECUTION_READY

    def dict(self) -> dict:
        return asdict(self)


def assess(opp_or_cand: dict) -> dict:
    """Full executability report for one opportunity/candidate dict."""

    o = opp_or_cand
    route = o.get("route", {}) or {}
    econ = o.get("economics") or {}
    is_product = o.get("type") in ("product_arbitrage", "refurbishment") or o.get("kind") == "flip"
    category = o.get("category", "")
    checks: list[Check] = []

    if is_product:
        bv, sv = route.get("buy_venue"), route.get("sell_venue")
        buy = thailand.VENUE_ACCESS.get(bv or "", {})
        sell = thailand.VENUE_ACCESS.get(sv or "", {})
        checks.append(Check(
            "Can a Thai resident buy on the source platform?",
            YES if buy.get("buy") else (UNKNOWN if not buy else NO),
            buy.get("buy_note", "Unknown venue — verify access before committing.")
            + (" (via proxy)" if buy.get("proxy") else "")))
        checks.append(Check(
            "Can a Thai resident sell/list on the sell platform?",
            YES if sell.get("sell") else (UNKNOWN if not sell else NO),
            sell.get("sell_note", "Unknown venue — verify seller onboarding.")))
        # Payout
        checks.append(Check(
            "Is a Thailand-usable payout method available?",
            YES, "eBay/Amazon/Etsy → Payoneer/Wise to a Thai bank; local venues → PromptPay/COD.",
            critical=True))
        # Company requirement — a NEGATIVE-framed check: NO is the good answer,
        # so it informs but does not gate; only a YES (company required) is the
        # blocker, and that surfaces in `blockers`.
        needs_company = sv in ("aliexpress",) or (sv and sell.get("sell") is False)
        checks.append(Check(
            "Is a Thai company required to operate this?",
            YES if needs_company else NO,
            "This sell venue requires a registered business — not personal." if needs_company
            else "A personal Thai ID + bank is enough for these venues; no company registration needed.",
            critical=False))
        # Category import/export restrictions — informational (extra paperwork,
        # not a hard block), so non-gating.
        imp = _IMPORT_RESTRICTED.get(category)
        checks.append(Check(
            "Are there import restrictions for this category into Thailand?",
            YES if imp else NO,
            imp or "No special Thai import licensing for this category (standard duty + 7% VAT).",
            critical=False))
        exp = _EXPORT_RESTRICTED.get(category)
        buy_country = route.get("buy_country")
        sell_country = route.get("sell_country")
        cross_border = buy_country != sell_country
        checks.append(Check(
            "Can the item legally be imported/exported on this route?",
            YES if not exp else ESTIMATED,
            (exp or "Standard CN22/CN23 declaration; no prohibited-goods flags for this category.")
            if cross_border else "Domestic route — no cross-border customs."))
        # Shipping options
        checks.append(Check(
            "What shipping is realistically available?",
            YES,
            "Inbound: proxy-forward / forwarder / direct. Outbound: Thailand Post EMS/ePacket or DHL. "
            "Last-mile TH: Kerry/Flash/J&T.", critical=False))
        # Customs docs
        feas = o.get("feasibility") or {}
        customs = feas.get("customs_notes") or []
        checks.append(Check(
            "What customs documentation is needed?",
            YES if not cross_border or customs else ESTIMATED,
            "; ".join(customs) if customs else
            ("Domestic — none." if not cross_border else "CN22/CN23 + commercial invoice."),
            critical=False))
    else:
        # Ventures (digital/info/local/b2b/micro-saas) — no customs, but the
        # money-in question still matters.
        checks.append(Check("Can it be operated from Thailand?", YES,
                            "Built/served from Thailand; digital ones distribute globally.", critical=True))
        checks.append(Check("Is a Thailand-usable payout method available?", YES,
                            "Stripe/Gumroad/Paddle for digital; PromptPay/bank for local/B2B.", critical=True))
        checks.append(Check("Is a Thai company required?", NO,
                            "Can start as a sole operator; register a company only past the VAT threshold "
                            "(฿1.8M/yr revenue).", critical=False))
        checks.append(Check("Are there category/legal restrictions?", NO,
                            "No import/export; standard content/licensing hygiene applies.", critical=False))

    # Taxes included in the priced economics? (both kinds)
    has_tax_line = _has_tax_line(econ)
    checks.append(Check(
        "Are Thailand-specific taxes/fees included in the numbers?",
        ESTIMATED if has_tax_line else UNKNOWN,
        "Import VAT/duty + marketplace + payout FX are in the priced waterfall."
        if has_tax_line else "Tax lines not detected in the economics — treat profit as pre-tax.",
        critical=True))
    # Still profitable after TH-specific costs?
    pess = _pessimistic_net(econ)
    checks.append(Check(
        "Is it still profitable after Thailand-specific costs (pessimistic)?",
        YES if pess is not None and pess > 0 else (UNKNOWN if pess is None else NO),
        f"Pessimistic net ${pess:.2f} after all modelled TH costs." if pess is not None
        else "Pessimistic economics unavailable.", critical=True))

    report = [c.dict() for c in checks]
    return {
        "checks": report,
        "unknowns": [c["question"] for c in report if c["status"] == UNKNOWN],
        "blockers": [c["question"] for c in report if c["status"] == NO and c["critical"]],
        "execution_ready": execution_ready(report),
    }


def execution_ready(checks: list[dict]) -> bool:
    """True only when no critical check is UNKNOWN or a hard NO."""

    for c in checks:
        if c["critical"] and c["status"] in (UNKNOWN, NO):
            return False
    return True


def _has_tax_line(econ: dict) -> bool:
    try:
        lines = (econ.get("base") or {}).get("lines") or []
        return any("vat" in (l.get("label", "").lower()) or "duty" in (l.get("label", "").lower())
                   or "fee" in (l.get("label", "").lower()) for l in lines)
    except Exception:  # noqa: BLE001
        return False


def _pessimistic_net(econ: dict):
    try:
        return float((econ.get("pessimistic") or {}).get("net_usd"))
    except Exception:  # noqa: BLE001
        return None
