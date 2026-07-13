"""Live data source — the production extension point (NOT yet implemented).

Everything upstream of this file (scanners → anomalies → investigation →
verification → economics → scoring → playbooks → learning) is data-source
agnostic: it consumes the `DataSource` protocol in `market/__init__.py`.
Going to production means implementing that protocol with real adapters and
passing a `LiveMarket` to the `Orchestrator` instead of `SimulatedMarket`.

Suggested adapters, in rough order of value for a Thailand-based operator:

    ebay_us            eBay Browse + Marketplace Insights APIs (sold history)
    amazon_us          Product Advertising API / SP-API (price, rank, offers)
    mercari_jp         unofficial JSON endpoints or a headless scraper
    yahoo_auctions_jp  Yahoo! Auctions API (via proxy-service accounts)
    shopee_th          Shopee open platform (TH seller account required)
    lazada_th          Lazada Open Platform
    etsy               Etsy Open API v3
    google_trends      pytrends or the official alpha API
    reddit / x / tiktok  official APIs where available; keyword-volume vendors otherwise
    news               GDELT / RSS clusters / earnings-call transcript feeds
    fx                 any FX rate API (replace economics.FX_TO_USD)

Each adapter must fill the same shapes the simulator produces (see
`world.py`: `Listing.snapshot()`, niche metric dicts, mention counts,
headlines). Respect every platform's terms of service and rate limits —
that is part of "verified" too.
"""

from __future__ import annotations


class LiveMarket:
    """Implements `DataSource` over real marketplace/social/trend adapters."""

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "LiveMarket is the production extension point. Implement the DataSource "
            "protocol with real adapters (see module docstring), then pass it to "
            "Orchestrator(config, store, world=LiveMarket(...)).")
