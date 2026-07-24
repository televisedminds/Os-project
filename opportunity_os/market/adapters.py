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
import re
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

    # Politeness throttle: eBay Browse bursts (watchlist + discovery in one cycle)
    # trip a 429 rate limit. Keep a minimum gap between calls and back off + retry
    # on 429 so a busy cycle degrades to "slower", not "dead".
    _MIN_INTERVAL = 0.34                        # ~3 calls/sec ceiling
    _last_call = 0.0

    def _throttle(self) -> None:
        gap = time.time() - EbayAdapter._last_call
        if gap < self._MIN_INTERVAL:
            time.sleep(self._MIN_INTERVAL - gap)
        EbayAdapter._last_call = time.time()

    def _browse(self, query: str, token: str):
        return self.client.get(
            f"{self.base}/buy/browse/v1/item_summary/search",
            params={"q": query, "limit": "50",
                    "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:US",
                    "sort": "price"},
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})

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
            r = None
            for attempt in range(3):
                self._throttle()
                r = self._browse(query, token)
                if r.status_code == 401:        # token expired mid-flight: refresh + retry
                    self._token = ""
                    token = self._get_token()
                    if not token:
                        return None
                    continue
                if r.status_code == 429:         # rate limited: honour Retry-After, back off
                    if attempt < 2:
                        ra = r.headers.get("Retry-After")
                        wait = float(ra) if (ra and ra.isdigit()) else min(8.0, 1.5 * (2 ** attempt))
                        time.sleep(wait)
                        continue
                    return self._fail(
                        "eBay rate limited (429) after 3 tries — slow the cycle "
                        "(raise OOS_AUTO_CYCLE_SECONDS) or trim the watchlist/discovery volume")
                break
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
    """google.serper.dev — Google results as an API, used as an EVIDENCE
    COLLECTOR, not just a counter:

    * `reddit_posts_7d` — coarse demand momentum (site:reddit.com, past week),
      the Reddit stand-in that feeds mention series;
    * `demand_observation` — what people are actually asking around a niche
      (People-Also-Ask questions, related searches, result depth), in any
      language/geo Google serves (Thai included);
    * `supply_observation` — who currently serves the niche: distinct
      commercial domains in the organic results, the observed stand-in for
      "how many credible solutions/providers exist". This replaces the old
      fabricated solution_count=3 constant with a real, sourced number.

    2,500 free credits at signup; 1 credit per call at num<=10."""

    id = "serper"
    name = "Serper (Google search)"
    URL = "https://google.serper.dev/search"

    # Domains that appear in results but are not a competing provider/solution:
    # social networks, encyclopedias, video, marketplaces of ideas — presence
    # there is DEMAND evidence, not SUPPLY.
    NON_PROVIDER_DOMAINS = (
        "reddit.com", "quora.com", "facebook.com", "youtube.com", "wikipedia.org",
        "twitter.com", "x.com", "pantip.com", "instagram.com", "tiktok.com",
        "medium.com", "linkedin.com", "pinterest.",
    )

    def __init__(self, cfg, client: httpx.Client | None = None):
        super().__init__(cfg, client)
        self.every_n_ticks = max(1, getattr(cfg, "serper_every_n_ticks", 12))

    def configured(self) -> bool:
        return bool(self.cfg.serper_api_key)

    def search(self, query: str, gl: str = "us", hl: str = "en",
               num: int = 10, tbs: str | None = None) -> dict | None:
        """One raw Serper call. Returns the full parsed JSON (organic,
        peopleAlsoAsk, relatedSearches) or None on failure."""

        if not self.configured():
            return self._fail_none("SERPER_API_KEY not set")
        body: dict = {"q": query, "num": num, "gl": gl, "hl": hl}
        if tbs:
            body["tbs"] = tbs
        try:
            r = self.client.post(
                self.URL,
                headers={"X-API-KEY": self.cfg.serper_api_key,
                         "Content-Type": "application/json"},
                json=body)
            r.raise_for_status()
            self.last_error = ""
            return r.json()
        except Exception as e:  # noqa: BLE001
            return self._fail_none(f"Serper search failed: {e}")

    def _fail_none(self, msg: str):
        self.last_error = msg[:300]
        return None

    def reddit_posts_7d(self, query: str) -> int | None:
        """Count of Reddit results Google indexed in the past week (0-10)."""

        data = self.search(f"site:reddit.com {query}", tbs="qdr:w")
        if data is None:
            return None
        return len(data.get("organic", []) or [])

    @staticmethod
    def _domain(url: str) -> str:
        m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
        return m.group(1).lower() if m else ""

    def demand_observation(self, query: str, gl: str = "us", hl: str = "en") -> dict | None:
        """What the internet is asking around this query — stored verbatim so
        every downstream claim traces to a real search result."""

        data = self.search(query, gl=gl, hl=hl)
        if data is None:
            return None
        organic = data.get("organic", []) or []
        return {
            "query": query, "gl": gl, "hl": hl,
            "results": len(organic),
            "questions": [q.get("question", "") for q in
                          (data.get("peopleAlsoAsk", []) or [])[:8] if q.get("question")],
            "related": [r.get("query", "") for r in
                        (data.get("relatedSearches", []) or [])[:8] if r.get("query")],
            "top": [{"title": o.get("title", ""), "url": o.get("link", ""),
                     "snippet": (o.get("snippet", "") or "")[:200]}
                    for o in organic[:5]],
        }

    def supply_observation(self, query: str, gl: str = "us", hl: str = "en") -> dict | None:
        """Who serves this niche today: distinct commercial domains ranking for
        the query. An observed proxy for provider/solution count — honest about
        being a proxy, but sourced, timestamped and reproducible (unlike the
        fabricated constants it replaces)."""

        data = self.search(query, gl=gl, hl=hl)
        if data is None:
            return None
        providers: dict[str, dict] = {}
        for o in data.get("organic", []) or []:
            dom = self._domain(o.get("link", ""))
            if not dom or any(nd in dom for nd in self.NON_PROVIDER_DOMAINS):
                continue
            providers.setdefault(dom, {"domain": dom, "title": o.get("title", "")[:120],
                                       "url": o.get("link", "")})
        return {
            "query": query, "gl": gl, "hl": hl,
            "provider_count": len(providers),
            "providers": list(providers.values())[:8],
            "results": len(data.get("organic", []) or []),
        }

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


class ScrapingDogPageAdapter(BaseAdapter):
    """General page-evidence collector through scrapingdog.com (Phase 7).

    Where the Shopee adapter scrapes ONE known JSON endpoint, this fetches an
    ARBITRARY page (a competitor's site, a supplier page, a directory listing —
    typically a provider URL Serper already surfaced) and extracts normalized
    evidence from the HTML:

    * **competitor pricing** — THB/USD amounts on the page, so a venture's price
      point is grounded in what real providers actually charge instead of an
      estimate;
    * **review/complaint signal** — a coarse count of satisfaction vs
      complaint language (Thai + English), a weak-but-real read on how well the
      incumbent solutions serve the market.

    EXPERIMENTAL, eyes open, same as Shopee: paid, brittle, credit-metered.
    Every extraction is labelled scraped; a fetch failure degrades to no
    evidence, never a crash. Budgeted per pass so a trial key lasts."""

    id = "scrapingdog_page"
    name = "ScrapingDog page evidence"

    # Plausible service/product price bands (drop phone numbers, years, IDs).
    _THB_MIN, _THB_MAX = 50.0, 2_000_000.0
    _USD_MIN, _USD_MAX = 2.0, 60_000.0

    _THB_RE = re.compile(r"(?:฿|บาท|baht|thb)\s?([\d][\d,]{1,9}(?:\.\d{1,2})?)"
                         r"|([\d][\d,]{1,9}(?:\.\d{1,2})?)\s?(?:฿|บาท|baht|thb)", re.I)
    _USD_RE = re.compile(r"(?:\$|usd)\s?([\d][\d,]{1,9}(?:\.\d{1,2})?)", re.I)
    _TAG_RE = re.compile(r"<[^>]+>")

    COMPLAINT_TERMS = ("แพง", "ช้า", "แย่", "ไม่ดี", "ห่วย", "หลอก", "โกง", "รอนาน",
                       "expensive", "overpriced", "slow", "late", "bad", "poor", "awful",
                       "terrible", "avoid", "scam", "rude", "disappointed", "waste")
    POSITIVE_TERMS = ("ดีมาก", "เยี่ยม", "ประทับใจ", "แนะนำ", "คุ้ม", "บริการดี",
                      "great", "excellent", "recommend", "reliable", "fast", "friendly",
                      "professional", "worth", "helpful", "quality")

    def __init__(self, cfg, client: httpx.Client | None = None):
        super().__init__(cfg, client)
        self.every_n_ticks = max(1, cfg.scrape_every_n_ticks)

    def configured(self) -> bool:
        return bool(self.cfg.scrapingdog_api_key)

    def fetch(self, url: str, dynamic: bool = False) -> str | None:
        """Fetch a URL's rendered text via ScrapingDog, or None on failure."""

        if not self.configured():
            return self._fail("SCRAPINGDOG_API_KEY not set")
        try:
            r = self.client.get("https://api.scrapingdog.com/scrape",
                                params={"api_key": self.cfg.scrapingdog_api_key,
                                        "url": url, "dynamic": "true" if dynamic else "false"})
            r.raise_for_status()
            self.last_error = ""
            return r.text
        except Exception as e:  # noqa: BLE001
            return self._fail(f"ScrapingDog fetch failed: {e}")

    def _extract_prices(self, text: str) -> dict:
        from .. import economics
        usd: list[float] = []
        for m in self._THB_RE.finditer(text):
            raw = m.group(1) or m.group(2)
            try:
                v = float(raw.replace(",", ""))
            except (ValueError, AttributeError):
                continue
            if self._THB_MIN <= v <= self._THB_MAX:
                usd.append(round(v / economics.USD_THB, 2))
        for m in self._USD_RE.finditer(text):
            try:
                v = float(m.group(1).replace(",", ""))
            except (ValueError, AttributeError):
                continue
            if self._USD_MIN <= v <= self._USD_MAX:
                usd.append(round(v, 2))
        usd.sort()
        return usd

    def _review_signal(self, text: str) -> dict:
        low = text.lower()
        complaints = sum(low.count(t) for t in self.COMPLAINT_TERMS)
        positives = sum(low.count(t) for t in self.POSITIVE_TERMS)
        total = complaints + positives
        return {"complaint_hits": complaints, "positive_hits": positives,
                "complaint_ratio": round(complaints / total, 2) if total else None}

    def page_evidence(self, url: str, dynamic: bool = False) -> dict | None:
        """Fetch a provider/competitor page and return normalized evidence:
        the observed price range and a review/complaint signal. None on any
        fetch/parse failure (the caller treats missing evidence honestly)."""

        html = self.fetch(url, dynamic=dynamic)
        if html is None:
            return None
        text = self._TAG_RE.sub(" ", html)
        prices = self._extract_prices(text)
        n = len(prices)
        price_block = None
        if n:
            price_block = {
                "n": n,
                "low_usd": prices[0],
                "median_usd": prices[n // 2],
                "high_usd": prices[-1],
            }
        review = self._review_signal(text)
        if price_block is None and review["complaint_hits"] == 0 and review["positive_hits"] == 0:
            return self._fail(f"no price or review signal parsed from {url} "
                              f"(page layout unfamiliar — evidence not extractable)")
        self.last_error = ""
        return {"url": url, "prices": price_block, "review": review,
                "experimental": True}

    def check(self) -> tuple[bool, str]:
        if not self.configured():
            return False, ("optional — set SCRAPINGDOG_API_KEY to let the fleet read competitor "
                           "pricing/reviews off provider pages Serper finds; ventures work without it")
        return True, (f"key set; provider pages scraped every {self.every_n_ticks} cycles "
                      f"(EXPERIMENTAL — extraction is best-effort, degrades to no evidence)")


def build_adapters(cfg, client: httpx.Client | None = None) -> dict[str, BaseAdapter]:
    """The default live adapter set, keyed by venue/source id."""

    out: dict[str, BaseAdapter] = {
        "ebay_us": EbayAdapter(cfg, client),
        "shopee_th": ScrapingDogShopeeAdapter(cfg, client),
        "scrapingdog_page": ScrapingDogPageAdapter(cfg, client),
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
