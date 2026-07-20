"""Anomaly detection — noticing, not searching.

The platform doesn't run keyword searches. It maintains rolling baselines for
every entity it watches and reacts when reality deviates: price z-scores,
stock crashes, cross-venue spreads, social spikes, search gaps, service
imbalances. Each anomaly is a reason to investigate, not yet an opportunity.
"""

from __future__ import annotations

from statistics import fmean, pstdev

from ..config import Config
from ..models import Anomaly, AnomalyKind
from .. import economics, research, thailand


class AnomalyDetector:
    def __init__(self, config: Config):
        self.cfg = config

    # -- helpers -----------------------------------------------------------

    def _zscore(self, series: list[float]) -> float:
        if len(series) < 4:
            return 0.0
        prev, cur = series[:-1], series[-1]
        window = prev[-self.cfg.history_window:]
        mu = fmean(window)
        sd = max(pstdev(window), abs(mu) * 0.005, 1e-6)
        return (cur - mu) / sd

    def _dislocations(self, ds, pid: str, product: dict) -> list[Anomaly]:
        """Underpriced individual listings + single-seller liquidations, read
        from the last full page of asks the adapter captured for each venue."""

        if not hasattr(ds, "listing_sample"):
            return []
        out: list[Anomaly] = []
        t = ds.tick_no
        for vid in product["venues"]:
            v = economics.VENUES.get(vid, {})
            acc = thailand.VENUE_ACCESS.get(vid, {})
            # A same-venue dislocation flip means buying the underpriced listing
            # and RE-listing it on that same venue — so the venue must support
            # both buying and selling, and be reachable both ways from Thailand.
            if not (v.get("buy") and v.get("sell") and acc.get("buy") and acc.get("sell")):
                continue
            sample = ds.listing_sample(pid, vid)
            if not sample:
                continue
            query = product.get("name", "")
            for hit in research.find_dislocations(sample, query,
                                                  min_edge=self.cfg.dislocation_min_edge):
                out.append(Anomaly(
                    kind=AnomalyKind.PRICE_DISLOCATION, entity_id=pid, tick=t,
                    severity=min(1.0, hit["edge_pct"] / 100),
                    summary=f"{product['name']}: a listing on {economics.VENUES[vid]['name']} is "
                            f"${hit['ask_usd']:.2f} — {hit['edge_pct']:.0f}% below the market's "
                            f"${hit['fair_usd']:.2f} clearing value ({hit['market_n']} asks, "
                            f"{hit['market_sellers']} sellers).",
                    evidence=[{"venue": vid, **hit}]))
            liq = research.find_liquidation(sample)
            if liq:
                out.append(Anomaly(
                    kind=AnomalyKind.SELLER_LIQUIDATION, entity_id=pid, tick=t,
                    severity=min(1.0, liq["avg_discount_pct"] / 40),
                    summary=f"{product['name']}: seller '{liq['seller']}' is holding "
                            f"{liq['listings']} below-fair asks on {economics.VENUES[vid]['name']} "
                            f"(avg {liq['avg_discount_pct']:.0f}% under ${liq['fair_usd']:.2f}) — "
                            f"a liquidation worth sweeping.",
                    evidence=[{"venue": vid, **liq}]))
        return out

    # -- detection ---------------------------------------------------------

    def detect(self, ds) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        t = ds.tick_no

        for pid in ds.product_ids():
            product = ds.product_public(pid)
            listings = {v: ds.listing(pid, v) for v in product["venues"]}

            # Per-listing dislocations: the alpha INSIDE a single response.
            # A sell-side venue's latest page of asks is mined for individual
            # listings priced far below the market's own conservative clearing
            # value, and for a single seller dumping several at once. Zero extra
            # API calls — the page was already fetched for the aggregate stats.
            anomalies += self._dislocations(ds, pid, product)

            for vid in product["venues"]:
                hist = ds.product_history(pid, vid)
                prices = [h["price"] for h in hist]
                z = self._zscore(prices)
                if z >= self.cfg.anomaly_zscore:
                    anomalies.append(Anomaly(
                        kind=AnomalyKind.PRICE_SPIKE, entity_id=pid, tick=t,
                        severity=min(1.0, z / 5),
                        summary=f"{product['name']}: price on {economics.VENUES[vid]['name']} is "
                                f"{z:.1f}σ above its {self.cfg.history_window}-day baseline "
                                f"(${prices[-1]:.2f} vs ~${fmean(prices[-self.cfg.history_window-1:-1]):.2f}).",
                        evidence=[{"venue": vid, "price": prices[-1], "zscore": round(z, 2),
                                   "recent_prices": prices[-6:]}]))

                stocks = [h["stock"] for h in hist]
                if len(stocks) >= 5:
                    base_stock = fmean(stocks[-self.cfg.history_window - 1:-1])
                    cur = stocks[-1]
                    sellers_then = hist[-min(len(hist), 4)]["sellers"]
                    sellers_now = hist[-1]["sellers"]
                    if base_stock >= 5 and cur < base_stock * 0.45:
                        anomalies.append(Anomaly(
                            kind=AnomalyKind.SUPPLY_CRUNCH, entity_id=pid, tick=t,
                            severity=min(1.0, 1 - cur / base_stock),
                            summary=f"{product['name']}: visible stock on {economics.VENUES[vid]['name']} "
                                    f"collapsed to {cur} units (baseline ~{base_stock:.0f}); "
                                    f"active sellers {sellers_then}→{sellers_now}.",
                            evidence=[{"venue": vid, "stock": cur, "baseline_stock": round(base_stock, 1),
                                       "sellers_before": sellers_then, "sellers_now": sellers_now}]))

            # Cross-venue spread: cheapest buyable vs richest sellable venue.
            buyable = {v: l for v, l in listings.items()
                       if l and l["stock"] > 0 and economics.VENUES[v]["buy"]
                       and thailand.VENUE_ACCESS.get(v, {}).get("buy")}
            sellable = {v: l for v, l in listings.items()
                        if l and economics.VENUES[v]["sell"]
                        and thailand.VENUE_ACCESS.get(v, {}).get("sell")}
            if buyable and sellable:
                bv = min(buyable, key=lambda v: buyable[v]["price"])
                sv = max(sellable, key=lambda v: sellable[v]["price"])
                if bv != sv:
                    spread = (sellable[sv]["price"] - buyable[bv]["price"]) / buyable[bv]["price"]
                    if spread >= self.cfg.cross_venue_spread_pct:
                        anomalies.append(Anomaly(
                            kind=AnomalyKind.CROSS_VENUE_SPREAD, entity_id=pid, tick=t,
                            severity=min(1.0, spread),
                            summary=f"{product['name']}: {spread * 100:.0f}% raw spread — "
                                    f"${buyable[bv]['price']:.2f} on {economics.VENUES[bv]['name']} vs "
                                    f"${sellable[sv]['price']:.2f} on {economics.VENUES[sv]['name']}.",
                            evidence=[{"buy_venue": bv, "buy_price": buyable[bv]["price"],
                                       "buy_stock": buyable[bv]["stock"],
                                       "sell_venue": sv, "sell_price": sellable[sv]["price"],
                                       "sell_velocity_7d": sellable[sv]["sold_7d"]}]))
                    # Margin expansion: the gap itself is WIDENING — catching
                    # a spread while it grows beats finding it at the top.
                    bh, sh = ds.product_history(pid, bv), ds.product_history(pid, sv)
                    n = min(len(bh), len(sh))
                    if n >= 6:
                        spreads = [(sh[i]["price"] - bh[i]["price"]) / max(0.01, bh[i]["price"])
                                   for i in range(-n, 0)]
                        then = fmean(spreads[:-3]) if len(spreads) > 3 else spreads[0]
                        if spreads[-1] >= then + 0.10 and spreads[-1] >= self.cfg.cross_venue_spread_pct:
                            anomalies.append(Anomaly(
                                kind=AnomalyKind.MARGIN_EXPANSION, entity_id=pid, tick=t,
                                severity=min(1.0, spreads[-1] - then),
                                summary=f"{product['name']}: spread widening — "
                                        f"{then * 100:.0f}% → {spreads[-1] * 100:.0f}% "
                                        f"({economics.VENUES[bv]['name']} → {economics.VENUES[sv]['name']}).",
                                evidence=[{"buy_venue": bv, "sell_venue": sv,
                                           "spread_then": round(then, 3),
                                           "spread_now": round(spreads[-1], 3)}]))

            for vid in product["venues"]:
                hist = ds.product_history(pid, vid)
                if len(hist) < 6:
                    continue
                # Seller exodus: competitors leaving = pricing power arriving.
                sellers = [h["sellers"] for h in hist]
                base_sellers = fmean(sellers[-self.cfg.history_window - 1:-1])
                if base_sellers >= 5 and sellers[-1] <= base_sellers * 0.7:
                    anomalies.append(Anomaly(
                        kind=AnomalyKind.SELLER_EXODUS, entity_id=pid, tick=t,
                        severity=min(1.0, 1 - sellers[-1] / base_sellers),
                        summary=f"{product['name']}: active sellers on {economics.VENUES[vid]['name']} "
                                f"fell {sellers[-1]}/{base_sellers:.0f} vs baseline — competitors leaving.",
                        evidence=[{"venue": vid, "sellers_now": sellers[-1],
                                   "sellers_baseline": round(base_sellers, 1)}]))
                # Demand acceleration: the market is speeding up under the listings.
                vels = [h["sold_7d"] / 7.0 for h in hist]
                base_vel = fmean(vels[-self.cfg.history_window - 1:-1])
                if vels[-1] >= 1.0 and base_vel > 0.2 and vels[-1] >= 1.5 * base_vel:
                    anomalies.append(Anomaly(
                        kind=AnomalyKind.DEMAND_ACCELERATION, entity_id=pid, tick=t,
                        severity=min(1.0, vels[-1] / (base_vel * 3)),
                        summary=f"{product['name']}: sell-through on {economics.VENUES[vid]['name']} "
                                f"accelerated to {vels[-1]:.1f}/day (baseline {base_vel:.1f}/day).",
                        evidence=[{"venue": vid, "velocity_now": round(vels[-1], 2),
                                   "velocity_baseline": round(base_vel, 2)}]))

            totals = []
            for src in ds.social_sources():
                h = ds.mentions(pid, src)
                if h:
                    totals = [a + b for a, b in zip(totals, h)] if totals else list(h)
            z = self._zscore([float(x) for x in totals]) if totals else 0.0
            if z >= self.cfg.anomaly_zscore:
                anomalies.append(Anomaly(
                    kind=AnomalyKind.SOCIAL_SPIKE, entity_id=pid, tick=t,
                    severity=min(1.0, z / 6),
                    summary=f"{product['name']}: social mentions {z:.1f}σ above baseline "
                            f"({totals[-1]}/day across Reddit+TikTok+X).",
                    evidence=[{"mentions_today": totals[-1], "series": totals[-8:]}]))

        for niche in ds.niches():
            m, nid = niche["metrics"], niche["id"]
            growth, solutions = m["growth_pct"], m["solution_count"]
            ratio = m["demand_posts"] / max(1, m["providers"])
            if niche["kind"] in ("digital", "info") and ((growth >= 15 and solutions <= 3) or growth >= 25):
                anomalies.append(Anomaly(
                    kind=AnomalyKind.SEARCH_GAP, entity_id=nid, tick=t,
                    severity=min(1.0, max(growth, 10) / 60),
                    summary=f"{niche['name']}: ~{m['volume']:,.0f} monthly searches growing {growth:.0f}%/mo "
                            f"with only {solutions} credible solutions.",
                    evidence=[{"volume": m["volume"], "growth_pct": growth, "solutions": solutions}]))
            elif niche["kind"] == "local" and ratio >= 35:
                anomalies.append(Anomaly(
                    kind=AnomalyKind.SERVICE_IMBALANCE, entity_id=nid, tick=t,
                    severity=min(1.0, ratio / 90),
                    summary=f"{niche['name']} ({niche['geo']}): {m['demand_posts']:.0f} demand posts/mo vs "
                            f"{m['providers']:.0f} active providers ({ratio:.0f}:1).",
                    evidence=[{"demand_posts": m["demand_posts"], "providers": m["providers"],
                               "ratio": round(ratio, 1)}]))
            elif niche["kind"] == "b2b" and growth >= 25:
                anomalies.append(Anomaly(
                    kind=AnomalyKind.B2B_SURGE, entity_id=nid, tick=t,
                    severity=min(1.0, growth / 60),
                    summary=f"{niche['name']}: demand growing {growth:.0f}%/mo against "
                            f"{m['providers']:.0f} qualified providers.",
                    evidence=[{"volume": m["volume"], "growth_pct": growth, "providers": m["providers"]}]))

            # Trend reversal: a niche that was flat/declining just turned up —
            # the earliest (and least crowded) moment to enter.
            nh = ds.niche_history(nid)
            if len(nh) >= 9:
                vols = [x["volume"] for x in nh]
                older, recent = fmean(vols[-9:-3]), fmean(vols[-3:])
                was_flat = fmean(vols[-9:-3]) <= fmean(vols[:-8] or vols[-9:-3]) * 1.02
                if was_flat and older > 0 and recent >= older * 1.2:
                    anomalies.append(Anomaly(
                        kind=AnomalyKind.TREND_REVERSAL, entity_id=nid, tick=t,
                        severity=min(1.0, recent / older - 1),
                        summary=f"{niche['name']}: demand turned upward — "
                                f"{older:,.0f} → {recent:,.0f}/mo after a flat stretch.",
                        evidence=[{"volume_before": round(older, 1), "volume_recent": round(recent, 1)}]))

        return anomalies
