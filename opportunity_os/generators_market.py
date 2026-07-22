"""Lead-generation and seasonal generators (Phase 9, gens 7 & 11).

These finish the generator set. Like every other generator they read the SAME
evidence the rest of the pipeline collects and face the SAME council — no
generic AI ideas, every candidate traces to real observations.

* **LeadGenGenerator** — a channel business, distinct from providing the
  service yourself (local_service). It fires on a local/B2B niche with OBSERVED
  demand and a supply side that is present-but-thin with weak online presence:
  a handful of providers exist to BUY leads, but few rank online, so a searcher
  can't find them. You capture that search intent and sell the leads. Priced
  per-lead off the underlying service value.

* **SeasonalGenerator** — a dated catalyst. It matches watched products against
  the Thai seasonal calendar (Songkran, 11.11, school terms, …); when a product
  in an event-driven category has a profitable Thailand sell route and we're
  inside the sourcing lead time, it emits a time-boxed flip whose window is the
  run-up to the event and whose expiry is HARD (the event passes, the edge is
  gone). The value a standing flip can't give: act-by-this-date timing.
"""

from __future__ import annotations

import datetime as _dt
import math

from .generators import OpportunityGenerator, GenContext, generator
from .models import InvestigationStep, OppType
from . import economics as eco, thailand as th

# ---- lead generation -------------------------------------------------------

LEADGEN_MIN_DEMAND = 150           # monthly searchers before a channel is worth it
                                   # (a high-value B2B service needs far fewer than a cheap one)
LEADGEN_MAX_PROVIDERS = 6          # few enough that they'd pay for leads
LEADGEN_MIN_PROVIDERS = 1          # someone must exist to sell leads TO
LEAD_VALUE_FRACTION = 0.08         # a lead is worth ~8% of the sale to the buyer
LEADGEN_MIN_NET = 40.0             # min modelled monthly net


@generator
class LeadGenGenerator(OpportunityGenerator):
    id, name = "lead_generation", "Lead generation (sell leads to providers)"
    types = ("lead_generation",)
    uses_marketplace = False

    def generate(self, ctx: GenContext) -> list[dict]:
        # Lead-gen's signal is STRUCTURAL (standing demand + a thin, under-
        # exposed supply side), not an anomaly spike — so it reads every niche,
        # not just the ones that moved this cycle. The council still gates it.
        out = []
        for niche in ctx.ds.niches():
            if niche["kind"] not in ("local", "b2b"):
                continue
            cand = self._build(ctx, niche, ctx.by_entity.get(niche["id"], []))
            if cand:
                out.append(cand)
        return out

    def _build(self, ctx, niche, anomalies):
        m = niche["metrics"]
        prov = m.get("observed") or {}
        # Honesty: only fire on OBSERVED (or operator-supplied) demand and supply.
        # An unknown supply side can't tell us there are providers to sell to.
        if prov.get("supply") == "unknown" or prov.get("demand") == "unknown":
            return None
        volume = m.get("volume", 0)
        providers = int(m.get("providers", m.get("solution_count", 0)))
        if volume < LEADGEN_MIN_DEMAND:
            return None
        if not (LEADGEN_MIN_PROVIDERS <= providers <= LEADGEN_MAX_PROVIDERS):
            return None                     # need a few, under-exposed providers to buy leads

        service_value = float(niche.get("price_point_usd", 0)) or 30.0
        per_lead = round(max(1.5, service_value * LEAD_VALUE_FRACTION), 2)
        # Model the channel with the leadgen params: a share of searchers become
        # sellable leads, priced per-lead. Reuse the venture waterfall.
        lead_niche = {"kind": "leadgen", "price_point_usd": per_lead,
                      "metrics": {"volume": volume}}
        econ = eco.compute_venture(lead_niche)
        if econ.base.net_usd < LEADGEN_MIN_NET or econ.pessimistic.net_usd <= 0:
            return None

        feas = th.feasibility("lead_generation", "b2b_services", None, None)
        domains = m.get("supply_domains", [])
        why = [
            InvestigationStep(
                "What is the lead-gen play?",
                f"~{volume:,.0f} people/mo search for this service, but only {providers} "
                f"provider(s) are visible online{': ' + ', '.join(domains[:3]) if domains else ''}. "
                f"Capture the search intent, sell the leads to those providers.",
                {"volume": volume, "providers": providers}),
            InvestigationStep(
                "Why would providers pay for the leads?",
                f"The underlying job is worth ≈${service_value:,.0f}; a qualified lead is worth "
                f"~{LEAD_VALUE_FRACTION * 100:.0f}% of that (${per_lead:.2f}) to a provider who "
                f"otherwise can't be found online. Few competitors means they need you.",
                {"per_lead_usd": per_lead, "service_value_usd": service_value}),
            InvestigationStep(
                "What are the channel economics?",
                f"≈${econ.base.revenue_usd:,.0f}/mo revenue → ${econ.base.net_usd:,.0f}/mo net "
                f"(pessimistic ${econ.pessimistic.net_usd:,.0f}) after ad spend. "
                f"Startup ≈${econ.capital_usd:,.0f} (landing page + first ad tests).",
                {"net_monthly": econ.base.net_usd}),
            InvestigationStep(
                "How do you deliver the leads?",
                "A simple Thai landing page + LINE/phone form; forward each enquiry to the "
                "provider by LINE. No fulfilment, no inventory — you sell contact, not the service.",
                {"delivery": "LINE/phone hand-off"}),
            InvestigationStep(
                "Can a Thailand operator run it, and what's the risk?",
                "Fully online from Thailand; Thai-language pages are an advantage. Main risk: a "
                "provider stops paying once they have enough work — sign up 2-3 buyers, not one.",
                {"single_buyer_risk": True}),
        ]

        supply_label = "providers"
        return {
            "kind": "venture", "opp_type": OppType.LEAD_GENERATION, "entity_id": niche["id"],
            "title": f"Lead-gen: {niche['name']}",
            "subtitle": f"{volume:,.0f} searches/mo, only {providers} providers online — "
                        f"sell leads at ${per_lead:.2f} each",
            "category": "lead_generation", "niche": niche,
            "buy_venue": None, "sell_venue": None,
            "qty": 1, "velocity": volume / 30.0, "sellers": providers,
            "window_days": 30.0, "economics": econ, "feasibility": feas, "why": why,
            "anomalies": anomalies,
            "sources": sorted({prov.get("demand_series", "serper"), "serper"}),
            "route": {"geo": niche.get("geo", "TH"), "kind": "lead_generation",
                      "providers": providers, "per_lead_usd": per_lead},
        }


# ---- seasonal --------------------------------------------------------------

SEASONAL_MIN_UNIT_NET = 6.0
SEASONAL_MIN_MARKUP = 1.15


@generator
class SeasonalGenerator(OpportunityGenerator):
    id, name = "seasonal", "Seasonal / event opportunity"
    types = ("seasonal",)
    uses_marketplace = True

    def _today(self, cfg) -> _dt.date:
        raw = getattr(cfg, "seasonal_today", "") or ""
        if raw:
            try:
                return _dt.date.fromisoformat(raw)
            except ValueError:
                pass
        return _dt.date.today()

    def generate(self, ctx: GenContext) -> list[dict]:
        today = self._today(ctx.cfg)
        horizon = int(getattr(ctx.cfg, "seasonal_horizon_days", 90))
        events = th.upcoming_events(today, horizon)
        if not events:
            return []
        # Category → the soonest event that drives it (act on the nearest deadline).
        cat_event: dict[str, dict] = {}
        for ev in events:
            for cat in ev["categories"]:
                cat_event.setdefault(cat, ev)

        out = []
        for pid, anomalies in ctx.by_entity.items():
            if pid in ctx.niche_ids:
                continue
            product = ctx.ds.product_public(pid)
            ev = cat_event.get(product.get("category"))
            if not ev or not ev["in_prep_window"]:
                continue                      # only within the sourcing lead time
            cand = self._build(ctx, pid, product, ev, today, anomalies)
            if cand:
                out.append(cand)
        return out

    def _build(self, ctx, pid, product, ev, today, anomalies):
        listings = {v: ctx.ds.listing(pid, v) for v in product.get("venues", [])}
        # Source cheapest reachable, sell into Thailand for the event (domestic,
        # PromptPay, in time for the peak).
        buyable = {v: l for v, l in listings.items()
                   if l and l["stock"] > 0 and eco.VENUES.get(v, {}).get("buy")
                   and th.VENUE_ACCESS.get(v, {}).get("buy")}
        th_sell = {v: l for v, l in listings.items()
                   if l and eco.VENUES.get(v, {}).get("sell")
                   and th.VENUE_ACCESS.get(v, {}).get("sell")
                   and eco.VENUES[v]["country"] == "TH"}
        if not buyable or not th_sell:
            return None
        bv = min(buyable, key=lambda v: buyable[v]["price"])
        sv = max(th_sell, key=lambda v: th_sell[v]["price"])
        buy, sell = buyable[bv], th_sell[sv]
        if sell["price"] <= buy["price"] * SEASONAL_MIN_MARKUP:
            return None

        days_until = ev["days_until"]
        # Window = run-up to the event (+ tail), never longer than the deadline.
        window = float(max(2, min(days_until + th.EVENT_TAIL_DAYS, ev["prep_days"])))
        velocity = max(sell.get("sold_7d", 0) / 7.0, 0.2)
        probe = eco.compute_flip(product, bv, sv, buy["price"], sell["price"], qty=1)
        unit_cost = probe.base.total_cost_usd
        # Size to what can realistically clear before the event.
        sellable_by_event = max(1, math.ceil(velocity * max(1, days_until)))
        qty = max(1, min(int(buy["stock"] * 0.25),
                         int(ctx.cfg.capital_cap_usd / max(1.0, unit_cost)),
                         sellable_by_event))
        econ = eco.compute_flip(product, bv, sv, buy["price"], sell["price"], qty=qty)
        if econ.base.net_usd < SEASONAL_MIN_UNIT_NET or econ.pessimistic.net_usd <= 0:
            return None

        legal = th.import_restriction(product.get("category", ""), product.get("name", "")) \
            if eco.VENUES[bv]["country"] != "TH" else {"allowed": True, "level": "clear", "note": ""}
        if not legal["allowed"]:
            return None

        feas = th.feasibility("seasonal", product.get("category", "collectibles"), bv, sv)
        bname, sname = eco.VENUES[bv]["name"], eco.VENUES[sv]["name"]
        why = [
            InvestigationStep(
                "What is the dated catalyst?",
                f"{ev['name']} on {ev['date']} — {days_until} days away. It drives demand for "
                f"{product.get('category')} in Thailand. {ev['note']}",
                {"event": ev["name"], "date": ev["date"], "days_until": days_until}),
            InvestigationStep(
                "Why act now?",
                f"Sourcing lead time is ~{ev['prep_days']} days; you're inside it. Order now to have "
                f"stock landed and listed for the {days_until}-day run-up.",
                {"prep_days": ev["prep_days"], "in_prep_window": True}),
            InvestigationStep(
                "What is the route and profit?",
                f"Buy {bname} ${buy['price']:.2f} → sell {sname} ${sell['price']:.2f}: "
                f"${econ.base.net_usd:.2f}/unit base ({econ.base.margin_pct:.0f}% margin), "
                f"${econ.pessimistic.net_usd:.2f} pessimistic. {econ.route_note}",
                {"net_base": econ.base.net_usd}),
            InvestigationStep(
                "How many units before the deadline?",
                f"≈{velocity:.1f} sales/day → ~{sellable_by_event} units clear before {ev['date']}; "
                f"recommending {qty}. Don't over-buy — unsold stock after the event loses the premium.",
                {"qty": qty, "sellable_by_event": sellable_by_event}),
            InvestigationStep(
                "When does it expire?",
                f"Hard expiry {ev['expiry_date']}: once {ev['name']} passes, the seasonal premium is "
                f"gone and any remainder sells at the normal price. This is a timing play, not a hold.",
                {"expiry_date": ev["expiry_date"]}),
        ]

        return {
            "kind": "flip", "opp_type": OppType.SEASONAL, "entity_id": pid,
            "title": f"{product['name']} — {ev['name'].split('(')[0].strip()}",
            "subtitle": f"{ev['name'].split('(')[0].strip()} in {days_until}d: buy {bname} "
                        f"${buy['price']:.2f} → sell {sname} ${sell['price']:.2f}",
            "category": product.get("category", "collectibles"), "item": product,
            "buy_venue": bv, "sell_venue": sv, "buy_usd": buy["price"], "sell_usd": sell["price"],
            "qty": qty, "buy_stock": buy["stock"], "velocity": velocity,
            "sellers": sell.get("sellers", 1), "window_days": window,
            "economics": econ, "feasibility": feas, "why": why,
            "anomalies": anomalies, "sources": sorted({bv, sv}),
            "route": {"buy_venue": bv, "sell_venue": sv,
                      "buy_country": eco.VENUES[bv]["country"], "sell_country": "TH",
                      "kind": "seasonal", "event": ev["name"], "event_date": ev["date"],
                      "expiry_date": ev["expiry_date"], "legal_level": legal["level"]},
        }
