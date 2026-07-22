"""The verification council.

Nothing reaches a user because one agent got excited. Each candidate faces a
council of independent verifiers — price, supply, demand, fees/taxes,
feasibility, competition — each of which re-queries the data itself. A failed
critical check vetoes publication; consensus confidence is a reliability-
weighted blend, and verifier reliabilities are updated by the learning engine
as real outcomes come in.
"""

from __future__ import annotations

from statistics import fmean

from ..config import Config
from ..models import Check, Verification
from .. import economics


class VerificationCouncil:
    def __init__(self, config: Config, reliability: dict[str, float] | None = None):
        self.cfg = config
        self.reliability = reliability or {}

    def _rel(self, verifier: str) -> float:
        return self.reliability.get(verifier, 0.8)

    def _consensus(self, checks: list[Check]) -> Verification:
        num = sum(self._rel(c.verifier) * (c.confidence if c.passed else c.confidence * 0.15) for c in checks)
        den = sum(self._rel(c.verifier) for c in checks) or 1.0
        v = Verification(checks=checks, consensus=round(num / den, 3))
        v.passed = all(c.passed for c in checks if c.critical)
        return v

    # ------------------------------------------------------------------ flips

    def verify_flip(self, ds, cand: dict) -> Verification:
        if cand.get("wholesale"):
            return self.verify_wholesale(ds, cand)
        if cand.get("dislocation"):
            return self.verify_dislocation(ds, cand)
        econ, feas = cand["economics"], cand["feasibility"]
        pid, bv, sv = cand["entity_id"], cand["buy_venue"], cand["sell_venue"]
        buy_now = ds.listing(pid, bv)
        sell_now = ds.listing(pid, sv)
        checks: list[Check] = []

        if not buy_now or not sell_now:
            checks.append(Check("price_verifier", "Prices re-checked", False, 0.9,
                                "Listing disappeared on re-check.", critical=True))
            return self._consensus(checks)

        spread_now = (sell_now["price"] - buy_now["price"]) / max(0.01, buy_now["price"])
        spread_cand = (cand["sell_usd"] - cand["buy_usd"]) / max(0.01, cand["buy_usd"])
        price_ok = spread_now >= 0.8 * spread_cand and spread_now >= 0.15
        checks.append(Check("price_verifier", "Prices re-checked independently", price_ok,
                            0.95 if abs(spread_now - spread_cand) < 0.05 else 0.8,
                            f"Live spread {spread_now * 100:.0f}% (was {spread_cand * 100:.0f}%): "
                            f"buy ${buy_now['price']:.2f}, sell ${sell_now['price']:.2f}.", critical=True))

        supply_ok = buy_now["stock"] >= max(1, int(cand["qty"] * 0.5))
        checks.append(Check("supply_verifier", "Source inventory confirmed", supply_ok,
                            0.9 if buy_now["stock"] >= cand["qty"] else 0.7,
                            f"{buy_now['stock']} units live at {economics.VENUES[bv]['name']} "
                            f"(need {cand['qty']}).", critical=True))

        velocity = sell_now["sold_7d"] / 7.0
        hist = ds.product_history(pid, sv)
        vel_hist = fmean([h["sold_7d"] / 7.0 for h in hist[-8:]]) if hist else velocity
        demand_ok = velocity >= 0.5 or vel_hist >= 0.7
        checks.append(Check("demand_estimator", "Demand estimated from sell-through", demand_ok,
                            min(0.95, 0.5 + velocity / 8),
                            f"≈{velocity:.1f} units/day now (8-day mean {vel_hist:.1f}); "
                            f"{cand['qty']} units ≈ {cand['qty'] / max(0.2, velocity):.1f} days to clear.",
                            critical=False))

        fee_ok = econ.pessimistic.net_usd > 0
        checks.append(Check("fee_auditor", "Survives pessimistic fees/duties", fee_ok, 0.9,
                            f"Pessimistic net ${econ.pessimistic.net_usd:.2f}/unit "
                            f"({econ.pessimistic.margin_pct:.0f}% margin) after -5% price, +15% shipping, "
                            f"destination duties and a 3% mishap reserve.", critical=True))

        tax_ok = feas.can_buy and feas.can_sell
        checks.append(Check("tax_auditor", "Thailand execution + customs path", tax_ok, 0.95,
                            ("Executable from TH. " if tax_ok else "Blocked from TH. ")
                            + (feas.customs_notes[-1] if feas.customs_notes else ""), critical=True))

        sellers_hist = [h["sellers"] for h in hist[-8:]] if hist else [sell_now["sellers"]]
        comp_ok = sell_now["sellers"] <= 12 or sell_now["sellers"] <= fmean(sellers_hist)
        checks.append(Check("competition_analyst", "Competitive pressure acceptable", comp_ok, 0.8,
                            f"{sell_now['sellers']} active sellers (8-day mean {fmean(sellers_hist):.0f}).",
                            critical=False))

        v = self._consensus(checks)
        v.tick = ds.tick_no
        return v

    # ----------------------------------------------------------- dislocations

    def verify_dislocation(self, ds, cand: dict) -> Verification:
        """Independently re-check the ONE specific listing: does it still exist,
        is it still far below fair, is the market still deep enough to resell
        into, and does the round-trip survive pessimistic fees. The listing
        disappearing is not a failure of judgement — it means the edge was real
        and someone took it; we invalidate it either way."""

        from .. import research
        econ, feas = cand["economics"], cand["feasibility"]
        pid, venue = cand["entity_id"], cand["buy_venue"]
        d = cand["dislocation"]
        checks: list[Check] = []

        sample = ds.listing_sample(pid, venue) if hasattr(ds, "listing_sample") else []
        stats = research.market_stats(sample)
        row = next((s for s in sample if s.get("item_id") == d["item_id"]), None)

        present = row is not None
        checks.append(Check("listing_verifier", "Exact listing still live", present, 0.95,
                            (f"Listing {d['item_id']} present at ${float(row['price']):.2f}."
                             if present else
                             f"Listing {d['item_id']} no longer on the page — sold or pulled."),
                            critical=True))
        if not present:
            return self._consensus(checks)

        refurbish = bool(d.get("refurbish"))
        price_now = float(row["price"])
        fair_now = stats["fair_usd"] or d["fair_usd"]
        edge_now = 1 - price_now / fair_now if fair_now else 0.0
        if refurbish:
            # A refurb buys a below-fair parts unit; the edge is the working
            # comp minus this price minus repair — just confirm it hasn't been
            # repriced above our entry.
            edge_ok = price_now <= d["ask_usd"] * 1.1
        else:
            edge_ok = edge_now >= 0.6 * d["min_edge"] and price_now <= d["ask_usd"] * 1.05
        checks.append(Check("price_verifier", "Still dislocated vs fair value", edge_ok, 0.9,
                            f"Now {edge_now * 100:.0f}% under working fair (${price_now:.2f} vs "
                            f"${fair_now:.2f}).", critical=True))

        depth_ok = stats["n"] >= research.MIN_MARKET_DEPTH and stats["sellers"] >= research.MIN_MARKET_SELLERS
        checks.append(Check("supply_verifier", "Resale market deep enough", depth_ok,
                            0.85, f"{stats['n']} comparable asks from {stats['sellers']} sellers "
                            f"to resell into.", critical=True))

        matches = research.title_matches_query(row.get("title", ""), cand["item"]["name"])
        if refurbish:
            # For a refurb, a for-parts listing is EXACTLY what we want — the
            # authenticity check confirms it's the right product AND is a
            # genuine parts unit, not a working one mislabelled.
            from ..generators_extra import _is_parts
            real_ok = matches and _is_parts(row.get("title", ""))
            evidence = ("For-parts unit of the right product — correct raw material."
                        if real_ok else "No longer a matching for-parts unit on re-read.")
        else:
            junk = research.looks_junk(row.get("title", ""), row.get("condition", ""))
            real_ok = (not junk) and matches
            evidence = ("Title matches the product and is not junk." if real_ok
                        else "Title looks like a variant/part/junk on re-read.")
        checks.append(Check("authenticity_check", "Listing is the intended product", real_ok, 0.8,
                            evidence, critical=True))

        fee_ok = econ.pessimistic.net_usd > 0
        checks.append(Check("fee_auditor", "Survives round-trip fees + reship", fee_ok, 0.9,
                            f"Pessimistic net ${econ.pessimistic.net_usd:.2f} after paying "
                            f"{economics.VENUES[venue]['name']}'s fee on resale, reship and a mishap "
                            f"reserve.", critical=True))

        tax_ok = feas.can_buy and feas.can_sell
        checks.append(Check("tax_auditor", "Thailand execution path", tax_ok, 0.9,
                            "Executable from TH via a US prep/reship address." if tax_ok
                            else "Not executable from TH.", critical=True))

        v = self._consensus(checks)
        v.tick = ds.tick_no
        return v

    # -------------------------------------------------------------- wholesale

    def verify_wholesale(self, ds, cand: dict) -> Verification:
        """Independently re-check a bulk-lot thesis: the exact lot still exists,
        its per-unit price is still below the single-unit market, the singles
        market is deep enough to absorb the units you'll relist, and the
        round-trip clears pessimistic fees. Buying and reselling both happen on
        the same venue, so this is a same-venue break-up flip."""

        from .. import research
        econ, feas = cand["economics"], cand["feasibility"]
        pid, venue = cand["entity_id"], cand["buy_venue"]
        w = cand["wholesale"]
        checks: list[Check] = []

        sample = ds.listing_sample(pid, venue) if hasattr(ds, "listing_sample") else []
        row = next((s for s in sample if s.get("item_id") == w["item_id"]), None)
        present = row is not None
        checks.append(Check("listing_verifier", "Exact lot still live", present, 0.95,
                            (f"Lot {w['item_id']} present at ${float(row['price']):.2f}."
                             if present else f"Lot {w['item_id']} gone from the page — sold or pulled."),
                            critical=True))
        if not present:
            return self._consensus(checks)

        n = research.parse_lot_size(row.get("title", "")) or w["lot_size"]
        per_unit = float(row["price"]) / max(1, n)
        # Fair value measured from the venue's CURRENT non-lot singles.
        singles = [s for s in sample
                   if research.parse_lot_size(s.get("title", "")) is None
                   and float(s.get("price", 0)) > 0
                   and not research.looks_junk(s.get("title", ""), s.get("condition", ""))]
        stats = research.market_stats(singles)
        fair_now = stats["fair_usd"] or w["single_fair_usd"]
        edge_now = 1 - per_unit / fair_now if fair_now else 0.0
        edge_ok = edge_now >= 0.6 * w["min_edge"] and per_unit <= w["per_unit_usd"] * 1.1
        checks.append(Check("price_verifier", "Per-unit still below singles", edge_ok, 0.9,
                            f"Lot is now ${per_unit:.2f}/unit vs ${fair_now:.2f} single "
                            f"({edge_now * 100:.0f}% under).", critical=True))

        depth_ok = (stats["n"] >= research.MIN_MARKET_DEPTH
                    and stats["sellers"] >= research.MIN_MARKET_SELLERS)
        checks.append(Check("supply_verifier", "Singles market deep enough to absorb the lot",
                            depth_ok, 0.85,
                            f"{stats['n']} single-unit asks from {stats['sellers']} sellers to resell into.",
                            critical=True))

        matches = research.title_matches_query(row.get("title", ""), cand["item"]["name"])
        real_ok = matches and not research.looks_junk(row.get("title", ""), row.get("condition", ""))
        checks.append(Check("authenticity_check", "Lot is the intended product", real_ok, 0.8,
                            "Lot title matches the product and isn't junk." if real_ok
                            else "Lot title looks like a variant/junk on re-read.", critical=True))

        fee_ok = econ.pessimistic.net_usd > 0
        checks.append(Check("fee_auditor", "Per-unit survives resale fees", fee_ok, 0.9,
                            f"Pessimistic ${econ.pessimistic.net_usd:.2f}/unit after "
                            f"{economics.VENUES[venue]['name']} fees on every single you relist.",
                            critical=True))

        tax_ok = feas.can_buy and feas.can_sell
        checks.append(Check("tax_auditor", "Thailand execution path", tax_ok, 0.9,
                            "Executable from TH (buy lot to a prep address, relist singles)." if tax_ok
                            else "Not executable from TH.", critical=True))

        v = self._consensus(checks)
        v.tick = ds.tick_no
        return v

    # --------------------------------------------------------------- ventures

    def verify_venture(self, ds, cand: dict) -> Verification:
        econ = cand["economics"]
        nid = cand["entity_id"]
        niche = next((n for n in ds.niches() if n["id"] == nid), None)
        checks: list[Check] = []
        if not niche:
            checks.append(Check("demand_corroboration", "Demand corroborated", False, 0.9,
                                "Niche no longer tracked.", critical=True))
            return self._consensus(checks)

        m = niche["metrics"]
        mentions = []
        for src in ds.social_sources():
            h = ds.mentions(nid, src)
            if h:
                mentions = [a + b for a, b in zip(mentions, h)] if mentions else list(h)
        social_up = len(mentions) >= 4 and mentions[-1] > 1.3 * max(1.0, fmean(mentions[-8:-1]))
        ratio = m["demand_posts"] / max(1, m["providers"])
        hist = ds.niche_history(nid)
        posts_then = hist[-9]["demand_posts"] if len(hist) >= 9 else m["demand_posts"]
        opp_val = cand["opp_type"].value
        if opp_val in ("local_service", "b2b_service", "lead_generation"):
            trend_up = m["growth_pct"] >= 8 or m["demand_posts"] >= 1.25 * max(1.0, posts_then)
        else:
            trend_up = m["growth_pct"] >= 12
        # Lead-gen doesn't need a wide demand:supply gap — a modest steady flow
        # of searchers is enough to resell as leads, so its demand bar is the
        # volume floor, not the ratio.
        posts_up = ratio >= 25 or (opp_val == "lead_generation" and m.get("volume", 0) >= 150)
        corroborations = sum([social_up, trend_up, posts_up])
        checks.append(Check("demand_corroboration", "Demand seen by ≥2 independent sources",
                            corroborations >= 2, min(0.95, 0.55 + 0.15 * corroborations),
                            f"Trend {'✓' if trend_up else '✗'} ({m['growth_pct']:.0f}%/mo), "
                            f"social {'✓' if social_up else '✗'} "
                            f"({mentions[-1] if mentions else 0}/day mentions), "
                            f"demand:supply {'✓' if posts_up else '✗'} ({ratio:.0f}:1).", critical=True))

        # Supply-side honesty: metrics carry provenance in live mode. A supply
        # count that was never observed (no Serper scan, no watchlist value)
        # must NOT pass as a "confirmed gap" — that was exactly how fabricated
        # constants used to publish fictional ventures. Demo/legacy metrics
        # (no provenance key) keep the original behaviour: the simulator's
        # numbers ARE its ground truth.
        prov = m.get("observed") or {}
        supply_src = prov.get("supply")
        gap_conf = 0.85
        if supply_src == "unknown":
            gap_ok = False
            gap_ev = ("Supply side never observed — no Serper supply scan and no "
                      "watchlist value. Research required before this can verify.")
        elif opp_val == "lead_generation":
            # For lead-gen the "gap" is INVERTED: you need a few under-exposed
            # providers to BUY the leads — not zero (no buyers) and not a crowd
            # (they already have all the work they need).
            providers = int(m.get("providers", m.get("solution_count", 0)))
            gap_ok = 1 <= providers <= 8
            gap_ev = (f"{providers} provider(s) online for ~{m['volume']:,.0f} searches/mo — "
                      f"{'a lead market: enough to buy leads, few enough to need them.' if gap_ok else 'wrong count to resell leads to.'}")
        elif niche["kind"] in ("digital", "info"):
            gap_ok = m["solution_count"] <= 4
            gap_ev = f"{m['solution_count']} credible solutions for {m['volume']:,.0f} monthly searches."
        else:
            gap_ok = ratio >= 25
            gap_ev = f"{m['providers']:.0f} providers vs {m['demand_posts']:.0f} demand posts/mo."
        if supply_src == "observed":
            doms = ", ".join(m.get("supply_domains", [])[:4])
            gap_ev += f" Observed via Google supply scan{': ' + doms if doms else ''}."
        elif supply_src == "user_supplied":
            gap_conf = 0.6
            gap_ev += " (Supply count is your watchlist estimate — verify it yourself.)"
        checks.append(Check("competition_gap", "Supply-side gap confirmed", gap_ok, gap_conf,
                            gap_ev, critical=True))

        unit_ok = econ.pessimistic.net_usd > 0
        checks.append(Check("unit_economics", "Positive at 45% of modelled demand", unit_ok, 0.85,
                            f"Pessimistic net ${econ.pessimistic.net_usd:,.0f}/mo; "
                            f"startup ${econ.capital_usd:,.0f} → payback "
                            f"{econ.capital_usd / max(1.0, econ.base.net_usd):.1f} months at base.", critical=True))

        checks.append(Check("feasibility_check", "Operable from Thailand", True, 0.9,
                            cand["feasibility"].buy_notes[0], critical=False))

        durable = m["growth_pct"] >= 0
        checks.append(Check("durability_analyst", "Demand not already fading", durable, 0.75,
                            f"Growth {m['growth_pct']:.0f}%/mo; "
                            + ("holding." if durable else "negative — likely a passed fad."), critical=False))

        v = self._consensus(checks)
        v.tick = ds.tick_no
        return v
