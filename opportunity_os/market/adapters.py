"""Real connectors. Every adapter degrades gracefully: on any failure it
returns None and reports the problem through `check()`/`last_error` instead of
crashing a cycle. Respect each platform's terms and rate limits — the default
live cadence (one cycle every 30 minutes) stays far inside eBay's free tier.

Verified transports:
  * eBay Browse API      — OAuth2 client-credentials (needs free developer keys)
  * Reddit search        — OAuth2 app-only if keys are set, else public JSON
                           (public JSON is often blocked from datacenter IPs;
                           create a free "script" app for reliable server use)
  * Google News RSS      — keyless
  * open.er-api.com FX   — keyless (frankfurter.dev fallback)
"""

from __future__ import annotations

import base64
import statistics
import time
import xml.etree.ElementTree as ET

import httpx


class BaseAdapter:
    id: str = "base"
    name: str = "Base"

    def __init__(self, cfg, client: httpx.Client | None = None):
        self.cfg = cfg
        self.client = client or httpx.Client(
            timeout=cfg.http_timeout, follow_redirects=True,
            headers={"User-Agent": cfg.user_agent})
        self.last_error: str = ""

    def _fail(self, msg: str):
        self.last_error = msg[:300]
        return None

    def check(self) -> tuple[bool, str]:  # pragma: no cover - overridden
        return False, "not implemented"


# --------------------------------------------------------------------- eBay

class EbayAdapter(BaseAdapter):
    """eBay Browse API — the sell-side observation engine.

    Snapshot semantics: `price` is the median asking price of active
    fixed-price listings for the query (robust to junk listings), `stock` is
    total matching listings, `sellers` is unique sellers on the first page,
    and `sold_7d` is estimated upstream from listing disappearance between
    snapshots (plus a watchlist bootstrap until history accumulates).
    """

    id = "ebay_us"
    name = "eBay US (Browse API)"

    def __init__(self, cfg, client: httpx.Client | None = None):
        super().__init__(cfg, client)
        self.base = ("https://api.sandbox.ebay.com" if cfg.ebay_env == "sandbox"
                     else "https://api.ebay.com")
        self._token: str = ""
        self._token_expiry: float = 0.0

    def configured(self) -> bool:
        return bool(self.cfg.ebay_client_id and self.cfg.ebay_client_secret)

    def _get_token(self) -> str | None:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        creds = base64.b64encode(
            f"{self.cfg.ebay_client_id}:{self.cfg.ebay_client_secret}".encode()).decode()
        try:
            r = self.client.post(
                f"{self.base}/identity/v1/oauth2/token",
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials",
                      "scope": "https://api.ebay.com/oauth/api_scope"})
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 7200))
            return self._token
        except Exception as e:  # noqa: BLE001
            return self._fail(f"eBay OAuth failed: {e}")

    def product_snapshot(self, query: str) -> dict | None:
        """-> {price, stock, sellers, item_ids, min_price, sample} or None.

        One API call, fully strip-mined: besides the aggregate stats, `sample`
        carries EVERY listing on the page (id, title, exact URL, price, seller,
        condition) so downstream research can measure the ask distribution,
        spot individually mispriced listings, read seller concentration and
        mine related-product phrases — all without further requests.
        """

        if not self.configured():
            return self._fail("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
        token = self._get_token()
        if not token:
            return None
        try:
            r = self.client.get(
                f"{self.base}/buy/browse/v1/item_summary/search",
                params={"q": query, "limit": "50",
                        "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:US",
                        "sort": "price"},
                headers={"Authorization": f"Bearer {token}",
                         "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
            if r.status_code == 401:            # token expired mid-flight: retry once
                self._token = ""
                token = self._get_token()
                if not token:
                    return None
                r = self.client.get(
                    f"{self.base}/buy/browse/v1/item_summary/search",
                    params={"q": query, "limit": "50",
                            "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:US",
                            "sort": "price"},
                    headers={"Authorization": f"Bearer {token}",
                             "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
            r.raise_for_status()
            body = r.json()
            items = body.get("itemSummaries", []) or []
            sample = []
            for i in items:
                if i.get("price", {}).get("currency", "USD") != "USD":
                    continue
                sample.append({
                    "item_id": i.get("itemId", ""),
                    "title": (i.get("title") or "")[:140],
                    "price": float(i["price"]["value"]),
                    "url": i.get("itemWebUrl", ""),
                    "seller": i.get("seller", {}).get("username", "?"),
                    "condition": i.get("condition", ""),
                })
            prices = [s["price"] for s in sample]
            if not prices:
                return self._fail(f"no priced results for query '{query}'")
            self.last_error = ""
            return {
                "price": round(statistics.median(prices), 2),
                "min_price": round(min(prices), 2),
                "stock": int(body.get("total", len(items))),
                "sellers": len({s["seller"] for s in sample}),
                "item_ids": [s["item_id"] for s in sample],
                "sample": sample,
            }
        except Exception as e:  # noqa: BLE001
            return self._fail(f"eBay search failed: {e}")

    def check(self) -> tuple[bool, str]:
        if not self.configured():
            return False, "needs EBAY_CLIENT_ID + EBAY_CLIENT_SECRET (free at developer.ebay.com)"
        if self._get_token():
            return True, f"OAuth OK against {self.base}"
        return False, (self.last_error + " — if eBay's Application Keys page says 'Your Keyset "
                       "is currently disabled', click its 'exemption' link, switch ON 'Not "
                       "persisting eBay data', Confirm, pick a reason, Submit. Instant fix.")


# ------------------------------------------------------------------- Reddit

class RedditAdapter(BaseAdapter):
    """Posts-per-24h for a query — the live social demand signal."""

    id = "reddit"
    name = "Reddit"

    def __init__(self, cfg, client: httpx.Client | None = None):
        super().__init__(cfg, client)
        self._token: str = ""
        self._token_expiry: float = 0.0

    def _oauth_token(self) -> str | None:
        if not (self.cfg.reddit_client_id and self.cfg.reddit_client_secret):
            return None
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        try:
            r = self.client.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(self.cfg.reddit_client_id, self.cfg.reddit_client_secret),
                data={"grant_type": "client_credentials"})
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 3600))
            return self._token
        except Exception as e:  # noqa: BLE001
            self._fail(f"Reddit OAuth failed: {e}")
            return None

    def mentions_24h(self, query: str) -> int | None:
        token = self._oauth_token()
        url = "https://oauth.reddit.com/search" if token else "https://www.reddit.com/search.json"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = self.client.get(url, params={"q": query, "sort": "new", "t": "day", "limit": "100"},
                                headers=headers)
            r.raise_for_status()
            data = r.json()
            children = data.get("data", {}).get("children", [])
            self.last_error = ""
            return len(children)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"Reddit search failed ({'oauth' if token else 'public'}): {e}")

    def check(self) -> tuple[bool, str]:
        n = self.mentions_24h("thailand")
        if n is not None:
            mode = "OAuth" if self.cfg.reddit_client_id else "public JSON"
            return True, f"{mode} OK ({n} posts/24h for test query)"
        hint = ("" if self.cfg.reddit_client_id else
                " — public JSON is often blocked from server IPs; create a free script app "
                "at reddit.com/prefs/apps and set REDDIT_CLIENT_ID/SECRET")
        return False, self.last_error + hint


# --------------------------------------------------------------------- News

class NewsAdapter(BaseAdapter):
    """Google News RSS — keyless headlines per query."""

    id = "news"
    name = "News (Google News RSS)"

    def headlines(self, query: str, max_items: int = 6) -> list[str] | None:
        try:
            r = self.client.get("https://news.google.com/rss/search",
                                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
            r.raise_for_status()
            root = ET.fromstring(r.text)
            titles = [item.findtext("title") or "" for item in root.iter("item")]
            self.last_error = ""
            return [t for t in titles if t][:max_items]
        except Exception as e:  # noqa: BLE001
            return self._fail(f"news fetch failed: {e}")

    def check(self) -> tuple[bool, str]:
        h = self.headlines("thailand economy", 2)
        return (True, f"OK ({len(h)} headlines for test query)") if h is not None else (False, self.last_error)


# ----------------------------------------------------------------------- FX

class FxAdapter(BaseAdapter):
    """Live USD→THB/JPY. Primary open.er-api.com, fallback frankfurter.dev."""

    id = "fx"
    name = "FX rates"

    def rates(self) -> dict | None:
        try:
            r = self.client.get("https://open.er-api.com/v6/latest/USD")
            r.raise_for_status()
            body = r.json()
            if body.get("result") == "success":
                self.last_error = ""
                return {"THB": float(body["rates"]["THB"]), "JPY": float(body["rates"]["JPY"])}
        except Exception as e:  # noqa: BLE001
            self._fail(f"er-api failed: {e}")
        try:
            r = self.client.get("https://api.frankfurter.dev/v1/latest",
                                params={"base": "USD", "symbols": "THB,JPY"})
            r.raise_for_status()
            rates = r.json()["rates"]
            self.last_error = ""
            return {"THB": float(rates["THB"]), "JPY": float(rates["JPY"])}
        except Exception as e:  # noqa: BLE001
            return self._fail(f"both FX sources failed (last: {e})")

    def check(self) -> tuple[bool, str]:
        r = self.rates()
        return (True, f"OK — USD/THB {r['THB']:.2f}, USD/JPY {r['JPY']:.1f}") if r else (False, self.last_error)


# ------------------------------------------------------------------- Serper

class SerperAdapter(BaseAdapter):
    """google.serper.dev — Google results as an API. Used here as a demand
    signal for niches: how many Reddit discussions Google saw this week for a
    query (site:reddit.com …, past-week filter). It measures the same thing
    the Reddit API does, more coarsely — which makes it the working stand-in
    while a Reddit key is pending approval. 2,500 free credits at signup;
    1 credit per call at num<=10."""

    id = "serper"
    name = "Serper (Google search)"
    URL = "https://google.serper.dev/search"

    def __init__(self, cfg, client: httpx.Client | None = None):
        super().__init__(cfg, client)
        self.every_n_ticks = max(1, getattr(cfg, "serper_every_n_ticks", 12))

    def configured(self) -> bool:
        return bool(self.cfg.serper_api_key)

    def reddit_posts_7d(self, query: str) -> int | None:
        """Count of Reddit results Google indexed in the past week (0-10)."""

        if not self.configured():
            return self._fail("SERPER_API_KEY not set")
        try:
            r = self.client.post(
                self.URL,
                headers={"X-API-KEY": self.cfg.serper_api_key,
                         "Content-Type": "application/json"},
                json={"q": f"site:reddit.com {query}", "num": 10, "tbs": "qdr:w"})
            r.raise_for_status()
            organic = r.json().get("organic", []) or []
            self.last_error = ""
            return len(organic)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"Serper search failed: {e}")

    def check(self) -> tuple[bool, str]:
        if not self.configured():
            return False, ("optional — set SERPER_API_KEY (serper.dev, 2,500 free searches, "
                           "no card) to measure niche demand while your Reddit key is pending")
        n = self.reddit_posts_7d("thailand")
        if n is None:
            return False, self.last_error
        return True, (f"OK ({n} Reddit results this week for test query); throttled to every "
                      f"{self.every_n_ticks} cycles to protect your free credits")


# -------------------------------------------------------------- ScrapingDog

class ScrapingDogShopeeAdapter(BaseAdapter):
    """Shopee TH search snapshots fetched through scrapingdog.com.

    EXPERIMENTAL, eyes open: ScrapingDog is a paid proxy-scraping API (free
    trial credits, then a subscription). It fetches pages/JSON that have no
    official API — but scraping can break whenever the site changes, can be
    blocked, and may conflict with the platform's terms of service. Each fetch
    costs credits, so scraped venues run only every Nth cycle
    (OOS_SCRAPE_EVERY, default 4 → every ~2 hours at the default cadence).
    Manual watchlist quotes remain the always-works fallback.
    """

    id = "shopee_th"
    name = "Shopee TH (via ScrapingDog)"

    SHOPEE_SEARCH = ("https://shopee.co.th/api/v4/search/search_items"
                     "?by=sales&keyword={q}&limit=30&newest=0&order=desc&page_type=search")

    def __init__(self, cfg, client: httpx.Client | None = None):
        super().__init__(cfg, client)
        self.every_n_ticks = max(1, cfg.scrape_every_n_ticks)

    def configured(self) -> bool:
        return bool(self.cfg.scrapingdog_api_key)

    def product_snapshot(self, query: str) -> dict | None:
        if not self.configured():
            return self._fail("SCRAPINGDOG_API_KEY not set (optional — manual quotes still work)")
        try:
            target = self.SHOPEE_SEARCH.format(q=httpx.QueryParams({"q": query})["q"])
            r = self.client.get("https://api.scrapingdog.com/scrape",
                                params={"api_key": self.cfg.scrapingdog_api_key,
                                        "url": target, "dynamic": "false"})
            r.raise_for_status()
            data = r.json()
            items = [it.get("item_basic", it) for it in (data.get("items") or [])]
            rows = []
            for it in items:
                price_thb = float(it.get("price", 0)) / 100000.0     # Shopee stores price ×1e5
                if price_thb <= 0:
                    continue
                rows.append({"price_thb": price_thb,
                             "sold_month": int(it.get("sold", 0)),
                             "stock": int(it.get("stock", 0)),
                             "shop": it.get("shopid"),
                             "itemid": str(it.get("itemid", "")),
                             "name": (it.get("name") or "")[:140]})
            if not rows:
                return self._fail(f"no parseable Shopee results for '{query}' "
                                  f"(site layout may have changed — update the parser)")
            from .. import economics
            prices_usd = sorted(r_["price_thb"] / economics.USD_THB for r_ in rows)
            median = prices_usd[len(prices_usd) // 2]
            self.last_error = ""
            return {
                "price": round(median, 2),
                "min_price": round(prices_usd[0], 2),
                "stock": sum(r_["stock"] for r_ in rows),
                "sellers": len({r_["shop"] for r_ in rows}),
                "sold_7d_hint": round(sum(r_["sold_month"] for r_ in rows) / 4),
                "item_ids": [r_["itemid"] for r_ in rows],
                "sample": [{"item_id": r_["itemid"], "title": r_["name"],
                            "price": round(r_["price_thb"] / economics.USD_THB, 2),
                            "url": f"https://shopee.co.th/product/{r_['shop']}/{r_['itemid']}",
                            "seller": str(r_["shop"]), "condition": ""} for r_ in rows],
            }
        except Exception as e:  # noqa: BLE001
            return self._fail(f"ScrapingDog/Shopee fetch failed: {e}")

    def check(self) -> tuple[bool, str]:
        if not self.configured():
            return False, ("optional — set SCRAPINGDOG_API_KEY (scrapingdog.com, free trial credits) "
                           "to auto-watch Shopee TH; manual quotes work without it")
        return True, (f"key set; scraped every {self.every_n_ticks} cycles to save credits "
                      f"(EXPERIMENTAL — parser can break when Shopee changes)")


def build_adapters(cfg, client: httpx.Client | None = None) -> dict[str, BaseAdapter]:
    """The default live adapter set, keyed by venue/source id."""

    out: dict[str, BaseAdapter] = {
        "ebay_us": EbayAdapter(cfg, client),
        "shopee_th": ScrapingDogShopeeAdapter(cfg, client),
        "reddit": RedditAdapter(cfg, client),
        "serper": SerperAdapter(cfg, client),
        "news": NewsAdapter(cfg, client),
        "fx": FxAdapter(cfg, client),
    }
    from ..plugins import VENUE_ADAPTERS, load_plugins
    load_plugins()
    for venue_id, cls in VENUE_ADAPTERS.items():  # drop-in venues, zero core edits
        try:
            out[venue_id] = cls(cfg, client)
        except Exception:  # noqa: BLE001
            pass
    return out
