"""The investigator — the agent that keeps asking "why?".

A normal tool would stop at "price is rising". The investigator runs the
chain: why is it rising → is supply shrinking → where is inventory still
available → can it be executed from Thailand → what's the profit after fees,
shipping and tax → how many units → who competes → how long will it last.
Every answer is pulled from data, and the whole chain ships with the
opportunity so users can audit the reasoning.
"""

from __future__ import annotations

import math
from statistics import fmean

from ..config import Config
from ..models import Anomaly, AnomalyKind, InvestigationStep, OppType
from .. import economics, thailand

NICHE_TYPE = {"digital": OppType.DIGITAL_PRODUCT, "info": OppType.INFO_PRODUCT,
              "local": OppType.LOCAL_SERVICE, "b2b": OppType.B2B_SERVICE}

NICHE_CATEGORY = {"digital": "digital_tools", "info": "info_products",
                  "local": "local_services", "b2b": "b2b_services"}


class Investigator:
    def __init__(self, config: Config):
        self.cfg = config

    # ------------------------------------------------------------------ main

    def build_candidates(self, ds, anomalies: list[Anomaly]) -> list[dict]:
        by_entity: dict[str, list[Anomaly]] = {}
        for a in anomalies:
            by_entity.setdefault(a.entity_id, []).append(a)

        niche_ids = {n["id"] for n in ds.niches()}
        candidates = []
        for entity_id in sorted(by_entity):
            group = by_entity[entity_id]
            if entity_id in niche_ids:
                cand = self._venture_candidate(ds, entity_id, group)
                if cand:
                    candidates.append(cand)
            else:
                # A product can yield BOTH a cross-venue flip AND one or more
                # per-listing dislocation flips — they are distinct, separately
                # priced opportunities with distinct identities.
                cand = self._flip_candidate(ds, entity_id, group)
                if cand:
                    candidates.append(cand)
                candidates += self._dislocation_candidates(ds, entity_id, group)
        return candidates

    # ------------------------------------------------------- dislocation flips

    def _dislocation_candidates(self, ds, pid: str, anomalies: list[Anomaly]) -> list[dict]:
        """Turn PRICE_DISLOCATION anomalies (a specific listing far below its
        market's fair value) into intra-venue flip candidates: buy that exact
        listing, resell at the market's own conservative clearing price. The
        pessimistic fee waterfall then decides whether the edge is real after
        the venue takes its cut both ways — only fat dislocations survive, and
        each survivor ships with an exact URL."""

        disl = [a for a in anomalies if a.kind == AnomalyKind.PRICE_DISLOCATION]
        if not disl:
            return []
        product = ds.product_public(pid)
        out = []
        seen: set[str] = set()
        for a in sorted(disl, key=lambda x: x.severity, reverse=True)[:3]:
            ev = a.evidence[0]
            venue, item_id = ev["venue"], ev.get("item_id", "")
            if item_id in seen or not economics.VENUES.get(venue):
                continue
            seen.add(item_id)
            ask, fair = float(ev["ask_usd"]), float(ev["fair_usd"])
            # Resell at conservative fair value on the SAME venue (you become
            # one more seller at the going rate). Round-trip fees are paid to
            # one venue — the gate below is where thin dislocations die.
            econ = economics.compute_flip(product, venue, venue, ask, fair, qty=1)
            feas = thailand.feasibility(OppType.PRODUCT_ARBITRAGE.value, product["category"], venue, venue)
            agg = ds.listing(pid, venue) or {}
            velocity = max(agg.get("sold_7d", 0) / 7.0, 0.2)
            vname = economics.VENUES[venue]["name"]
            why = [
                InvestigationStep(
                    "What is mispriced?",
                    a.summary,
                    {"item_id": item_id, "ask_usd": ask, "fair_usd": fair,
                     "edge_pct": ev.get("edge_pct")}),
                InvestigationStep(
                    "Why does the edge exist?",
                    f"One listing sits {ev.get('edge_pct', 0):.0f}% under the market's own "
                    f"clearing value while {ev.get('market_sellers', 0)} other sellers hold ~"
                    f"${fair:.2f}. Usual causes: a weak title, wrong category, an ending auction, "
                    f"or a seller who wants out — none of which change what the item IS.",
                    {"market_n": ev.get("market_n"), "market_sellers": ev.get("market_sellers")}),
                InvestigationStep(
                    "Is it the real product (not junk or a variant)?",
                    "Passed the junk filter (no 'for parts', 'box only', repro) and the "
                    "model-number match against the market query before being surfaced.",
                    {"condition": ev.get("condition", "")}),
                InvestigationStep(
                    "What is the real profit after fees both ways?",
                    f"Buy ${ask:.2f}, resell ${fair:.2f} on {vname}: "
                    f"${econ.base.net_usd:.2f} net ({econ.base.margin_pct:.0f}% margin), "
                    f"${econ.pessimistic.net_usd:.2f} pessimistic. You pay {vname}'s fee on the "
                    f"resale, so only a wide dislocation clears.",
                    {"net_base": econ.base.net_usd, "net_pessimistic": econ.pessimistic.net_usd}),
                InvestigationStep(
                    "Can a Thailand operator execute it?",
                    f"Same-venue relist: buy the listing to a US prep/reship address, then "
                    f"re-list on {vname}. Buy: {'yes' if feas.can_buy else 'no'}; "
                    f"Sell: {'yes' if feas.can_sell else 'no'}. {feas.buy_notes[0]}",
                    {"requires_proxy": feas.requires_proxy}),
                InvestigationStep(
                    "How long will it last?",
                    "Until this specific listing sells — re-verification drops it the moment "
                    "it disappears (edge taken) or is repriced up.",
                    {"single_listing": True}),
            ]
            out.append({
                "kind": "flip",
                "opp_type": OppType.PRODUCT_ARBITRAGE,
                "entity_id": pid,
                "title": f"{product['name']} — underpriced listing",
                "subtitle": f"Buy one {vname} listing ${ask:.2f} → resell ${fair:.2f} "
                            f"({ev.get('edge_pct', 0):.0f}% under fair)",
                "category": product["category"],
                "item": product,
                "buy_venue": venue, "sell_venue": venue,
                "buy_usd": ask, "sell_usd": fair,
                "qty": 1, "buy_stock": 1, "velocity": velocity,
                "sellers": int(ev.get("market_sellers", 1)),
                "window_days": round(min(10.0, max(2.0, ev.get("market_n", 10) / max(0.5, velocity))), 1),
                "economics": econ, "feasibility": feas, "why": why,
                "anomalies": [a], "sources": sorted({venue}),
                "dislocation": {"item_id": item_id, "url": ev.get("url", ""),
                                "ask_usd": ask, "fair_usd": fair,
                                "min_edge": self.cfg.dislocation_min_edge},
                "route": {"buy_venue": venue, "sell_venue": venue,
                          "buy_country": economics.VENUES[venue]["country"],
                          "sell_country": economics.VENUES[venue]["country"],
                          "kind": "dislocation", "item_id": item_id,
                          "buy_url": ev.get("url", "")},
            })
        return out

    # ----------------------------------------------------------------- flips

    def _recent_cause(self, ds, entity_id: str, lookback: int = 8) -> dict | None:
        events = [e for e in ds.event_log(entity_id) if e["tick"] > ds.tick_no - lookback]
        return events[-1] if events else None

    def _flip_candidate(self, ds, pid: str, anomalies: list[Anomaly]) -> dict | None:
        product = ds.product_public(pid)
        listings = {v: ds.listing(pid, v) for v in product["venues"]}

        buyable = {v: l for v, l in listings.items()
                   if l and l["stock"] > 0 and economics.VENUES[v]["buy"]
                   and thailand.VENUE_ACCESS.get(v, {}).get("buy")}
        sellable = {v: l for v, l in listings.items()
                    if l and economics.VENUES[v]["sell"]
                    and thailand.VENUE_ACCESS.get(v, {}).get("sell")}
        if not buyable or not sellable:
            return None
        buy_venue = min(buyable, key=lambda v: buyable[v]["price"])
        sell_venue = max(sellable, key=lambda v: sellable[v]["price"])
        if buy_venue == sell_venue:
            return None
        buy, sell = buyable[buy_venue], sellable[sell_venue]
        if sell["price"] <= buy["price"] * 1.15:
            return None

        cause = self._recent_cause(ds, pid)
        sell_hist = ds.product_history(pid, sell_venue)
        old_price = fmean([h["price"] for h in sell_hist[-9:-1]]) if len(sell_hist) > 2 else sell["price"]
        velocity = max(sell["sold_7d"] / 7.0, 0.1)

        # Probe unit economics at lot size 1, size the position, then reprice the
        # whole lot — consolidation changes per-unit shipping, so qty feeds back
        # into the waterfall.
        probe = economics.compute_flip(product, buy_venue, sell_venue, buy["price"], sell["price"], qty=1)
        unit_cost = probe.base.total_cost_usd
        qty = max(1, min(int(buy["stock"] * 0.25),
                         int(self.cfg.capital_cap_usd / max(1.0, unit_cost)),
                         math.ceil(velocity * 10)))
        econ = economics.compute_flip(product, buy_venue, sell_venue, buy["price"], sell["price"], qty=qty)

        window_days = round(min(14.0, max(2.0, (sell["stock"] + qty) / max(0.5, velocity))), 1)
        feas = thailand.feasibility(OppType.PRODUCT_ARBITRAGE.value, product["category"], buy_venue, sell_venue)

        why = [
            InvestigationStep(
                "What moved?",
                " · ".join(a.summary for a in anomalies[:2]),
                {"anomalies": [a.kind.value for a in anomalies]}),
            InvestigationStep(
                "Why is it moving?",
                cause["narrative"] if cause else
                "No single discrete cause found — gradual demand drift against static supply.",
                {"headline": cause["headline"] if cause else None,
                 "sell_price_baseline": round(old_price, 2), "sell_price_now": sell["price"]}),
            InvestigationStep(
                "Where is inventory still available?",
                ", ".join(f"{economics.VENUES[v]['name']}: {l['stock']} units @ ${l['price']:.2f}"
                          for v, l in sorted(buyable.items(), key=lambda kv: kv[1]["price"])),
                {"buy_venue": buy_venue, "buy_stock": buy["stock"]}),
            InvestigationStep(
                "Can it be executed from Thailand?",
                f"Buy: {'yes' + (' (via proxy)' if feas.requires_proxy else '') if feas.can_buy else 'no'} — "
                f"{feas.buy_notes[0]}  Sell: {'yes' if feas.can_sell else 'no'} — {feas.sell_notes[0]}",
                {"requires_proxy": feas.requires_proxy}),
            InvestigationStep(
                "What is the real profit after fees, shipping and tax?",
                f"${econ.base.net_usd:.2f}/unit base ({econ.base.margin_pct:.0f}% margin), "
                f"${econ.pessimistic.net_usd:.2f}/unit in the pessimistic case. {econ.route_note}",
                {"net_base": econ.base.net_usd, "net_pessimistic": econ.pessimistic.net_usd}),
            InvestigationStep(
                "How many units are actually available?",
                f"{buy['stock']} units visible at the buy venue; recommending {qty} "
                f"(25% of visible depth, capital cap ${self.cfg.capital_cap_usd:,.0f}, ~10 days of demand).",
                {"qty": qty, "capital_usd": econ.capital_usd}),
            InvestigationStep(
                "Who is competing?",
                f"{sell['sellers']} active sellers on {economics.VENUES[sell_venue]['name']}; "
                f"sell-through ≈ {velocity:.1f} units/day.",
                {"sellers": sell["sellers"], "velocity_per_day": round(velocity, 2)}),
            InvestigationStep(
                "How long will the window last?",
                f"≈ {window_days} days at current sell-through before visible supply normalises"
                + ("; restock risk flagged by news." if cause and cause["etype"] == "supply_shock" else "."),
                {"window_days": window_days}),
        ]

        sources = sorted({sell_venue, buy_venue}
                         | ({"news"} if cause else set())
                         | ({"reddit", "tiktok", "x"} if any(a.kind.value == "social_spike" for a in anomalies) else set()))

        return {
            "kind": "flip",
            "opp_type": OppType.PRODUCT_ARBITRAGE,
            "entity_id": pid,
            "title": product["name"],
            "subtitle": f"Buy {economics.VENUES[buy_venue]['name']} ${buy['price']:.2f} → "
                        f"sell {economics.VENUES[sell_venue]['name']} ${sell['price']:.2f}",
            "category": product["category"],
            "item": product,
            "buy_venue": buy_venue, "sell_venue": sell_venue,
            "buy_usd": buy["price"], "sell_usd": sell["price"],
            "qty": qty, "buy_stock": buy["stock"], "velocity": velocity, "sellers": sell["sellers"],
            "window_days": window_days,
            "economics": econ, "feasibility": feas, "why": why,
            "anomalies": anomalies, "sources": sources,
            "route": {"buy_venue": buy_venue, "sell_venue": sell_venue,
                      "buy_country": economics.VENUES[buy_venue]["country"],
                      "sell_country": economics.VENUES[sell_venue]["country"]},
        }

    # -------------------------------------------------------------- ventures

    def _venture_candidate(self, ds, nid: str, anomalies: list[Anomaly]) -> dict | None:
        niche = next((n for n in ds.niches() if n["id"] == nid), None)
        if not niche:
            return None
        m = niche["metrics"]
        cause = self._recent_cause(ds, nid)
        hist = ds.niche_history(nid)
        vol_then = hist[-9]["volume"] if len(hist) >= 9 else m["volume"]
        econ = economics.compute_venture(niche)
        feas = thailand.feasibility(NICHE_TYPE[niche["kind"]].value, NICHE_CATEGORY[niche["kind"]], None, None)
        window_days = 21.0 if (cause and cause["etype"] in ("search_spike", "b2b_surge")) else 30.0
        supply_label = "credible solutions" if niche["kind"] in ("digital", "info") else "active providers"
        supply_n = m["solution_count"] if niche["kind"] in ("digital", "info") else m["providers"]

        why = [
            InvestigationStep("What is the demand signal?",
                              " · ".join(a.summary for a in anomalies[:2]),
                              {"volume": m["volume"], "growth_pct": m["growth_pct"],
                               "volume_8_ticks_ago": vol_then}),
            InvestigationStep("Why now?",
                              cause["narrative"] if cause else
                              "Steady compounding demand; no discrete trigger required.",
                              {"headline": cause["headline"] if cause else None}),
            InvestigationStep("Who currently serves it?",
                              f"{supply_n:.0f} {supply_label} for "
                              f"~{m['volume']:,.0f} monthly demand events — an underserved gap.",
                              {"supply": supply_n}),
            InvestigationStep("Does it fit a Thailand-based operator?",
                              feas.buy_notes[0],
                              {"geo": niche["geo"]}),
            InvestigationStep("What are the unit economics?",
                              f"≈ ${econ.base.revenue_usd:,.0f}/mo revenue → ${econ.base.net_usd:,.0f}/mo net "
                              f"(pessimistic ${econ.pessimistic.net_usd:,.0f}). Startup ≈ ${econ.capital_usd:,.0f}. "
                              f"{econ.route_note}",
                              {"net_monthly": econ.base.net_usd}),
            InvestigationStep("How durable is the window?",
                              f"≈ {window_days:.0f} days of first-mover advantage before the gap closes"
                              + ("; spike-driven — move fast." if window_days < 30 else "."),
                              {"window_days": window_days}),
        ]

        return {
            "kind": "venture",
            "opp_type": NICHE_TYPE[niche["kind"]],
            "entity_id": nid,
            "title": niche["name"],
            "subtitle": f"{m['volume']:,.0f} demand events/mo, {m['growth_pct']:.0f}%/mo growth, "
                        f"{supply_n:.0f} {supply_label}",
            "category": NICHE_CATEGORY[niche["kind"]],
            "niche": niche,
            "buy_venue": None, "sell_venue": None,
            "qty": 1, "velocity": m["volume"] / 30.0, "sellers": int(supply_n),
            "window_days": window_days,
            "economics": econ, "feasibility": feas, "why": why,
            "anomalies": anomalies,
            "sources": sorted({"google_trends", "reddit"} | ({"news"} if cause else set())),
            "route": {"geo": niche["geo"], "kind": niche["kind"]},
        }


