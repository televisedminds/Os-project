"""Scanner agents — the always-on observation layer.

Each agent watches one venue or signal source and emits raw `Signal`s.
Scanners do not judge; they only observe and flag how unusual an observation
looks. Judgment happens downstream (anomaly detection → investigation →
verification).
"""

from __future__ import annotations

from statistics import fmean

from ..models import Signal
from .. import economics


class ScannerAgent:
    def __init__(self, agent_id: str, name: str, source: str, description: str):
        self.id = agent_id
        self.name = name
        self.source = source
        self.description = description

    def scan(self, ds) -> list[Signal]:  # pragma: no cover - abstract
        raise NotImplementedError


class VenueScanner(ScannerAgent):
    """Watches every listing on one marketplace."""

    def __init__(self, venue: str):
        meta = economics.VENUES[venue]
        super().__init__(f"scan_{venue}", f"{meta['name']} Scanner", venue,
                         f"Prices, stock depth, seller counts and sell-through on {meta['name']}.")
        self.venue = venue

    def scan(self, ds) -> list[Signal]:
        out = []
        for product, listing in ds.listings(self.venue):
            hist = ds.product_history(product["id"], self.venue)
            prev_price = hist[-2]["price"] if len(hist) >= 2 else listing["price"]
            change = (listing["price"] - prev_price) / prev_price if prev_price else 0.0
            out.append(Signal(agent=self.id, source=self.venue, kind="market_snapshot",
                              entity_id=product["id"], tick=ds.tick_no, venue=self.venue,
                              strength=round(abs(change), 4),
                              payload={**listing, "prev_price": prev_price,
                                       "category": product["category"]}))
        return out


class SocialScanner(ScannerAgent):
    """Watches mention volume for every tracked entity on one social source."""

    def __init__(self, source: str, label: str):
        super().__init__(f"scan_{source}", f"{label} Scanner", source,
                         f"Mention volume and momentum across {label}.")

    def scan(self, ds) -> list[Signal]:
        out = []
        entities = ds.product_ids() + [n["id"] for n in ds.niches()]
        for eid in entities:
            hist = ds.mentions(eid, self.source)
            if not hist:
                continue
            cur = hist[-1]
            base = fmean(hist[-8:-1]) if len(hist) >= 3 else max(1, cur)
            ratio = cur / max(1.0, base)
            out.append(Signal(agent=self.id, source=self.source, kind="social_mentions",
                              entity_id=eid, tick=ds.tick_no, strength=round(ratio, 2),
                              payload={"mentions": cur, "baseline": round(base, 1)}))
        return out


class TrendsScanner(ScannerAgent):
    """Search interest, growth, and how many solutions already serve a query."""

    def __init__(self):
        super().__init__("scan_google_trends", "Google Trends Scanner", "google_trends",
                         "Search volume, growth and solution-gap metrics for tracked niches.")

    def scan(self, ds) -> list[Signal]:
        out = []
        for niche in ds.niches():
            m = niche["metrics"]
            out.append(Signal(agent=self.id, source=self.source, kind="search_trend",
                              entity_id=niche["id"], tick=ds.tick_no,
                              strength=round(max(0.0, m["growth_pct"]) / 25, 2),
                              payload={**m, "kind": niche["kind"], "geo": niche["geo"]}))
        return out


class NewsScanner(ScannerAgent):
    """Headlines, distributor notes, policy changes."""

    def __init__(self):
        super().__init__("scan_news", "News & Filings Scanner", "news",
                         "Headlines, distributor/restock notices, policy changes, earnings notes.")

    def scan(self, ds) -> list[Signal]:
        return [Signal(agent=self.id, source="news", kind="headline",
                       entity_id=h["entity_id"], tick=ds.tick_no, strength=1.0,
                       payload={"text": h["text"], "etype": h["etype"]})
                for h in ds.headlines(ds.tick_no - 1)]


def build_fleet(ds) -> list[ScannerAgent]:
    """One agent per venue + social + trends + news. 13 agents on the default world."""

    fleet: list[ScannerAgent] = [VenueScanner(v) for v in ds.venue_ids()]
    fleet += [SocialScanner("reddit", "Reddit"), SocialScanner("tiktok", "TikTok"), SocialScanner("x", "X")]
    fleet += [TrendsScanner(), NewsScanner()]
    return fleet
