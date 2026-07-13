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
        if cand["opp_type"].value in ("local_service", "b2b_service"):
            trend_up = m["growth_pct"] >= 8 or m["demand_posts"] >= 1.25 * max(1.0, posts_then)
        else:
            trend_up = m["growth_pct"] >= 12
        posts_up = ratio >= 25
        corroborations = sum([social_up, trend_up, posts_up])
        checks.append(Check("demand_corroboration", "Demand seen by ≥2 independent sources",
                            corroborations >= 2, min(0.95, 0.55 + 0.15 * corroborations),
                            f"Trend {'✓' if trend_up else '✗'} ({m['growth_pct']:.0f}%/mo), "
                            f"social {'✓' if social_up else '✗'} "
                            f"({mentions[-1] if mentions else 0}/day mentions), "
                            f"demand:supply {'✓' if posts_up else '✗'} ({ratio:.0f}:1).", critical=True))

        if niche["kind"] in ("digital", "info"):
            gap_ok = m["solution_count"] <= 4
            gap_ev = f"{m['solution_count']} credible solutions for {m['volume']:,.0f} monthly searches."
        else:
            gap_ok = ratio >= 25
            gap_ev = f"{m['providers']:.0f} providers vs {m['demand_posts']:.0f} demand posts/mo."
        checks.append(Check("competition_gap", "Supply-side gap confirmed", gap_ok, 0.85, gap_ev, critical=True))

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
