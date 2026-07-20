"""Real, clickable venue URLs — where to buy, sell, and register.

Search links open the venue's live search for the product so the user lands
one tap away from the actual listings. JP venues link through Buyee because
that's the genuinely actionable path from Thailand. Signup links go to each
platform's seller onboarding.
"""

from __future__ import annotations

from urllib.parse import quote_plus

SEARCH_URLS: dict[str, str] = {
    "ebay_us": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "amazon_us": "https://www.amazon.com/s?k={q}",
    "etsy": "https://www.etsy.com/search?q={q}",
    "shopee_th": "https://shopee.co.th/search?keyword={q}",
    "lazada_th": "https://www.lazada.co.th/catalog/?q={q}",
    "tiktok_shop_th": "https://www.tiktok.com/search?q={q}",
    "facebook_mp_th": "https://www.facebook.com/marketplace/bangkok/search?query={q}",
    "kaidee_th": "https://www.kaidee.com/search?q={q}",
    "aliexpress": "https://www.aliexpress.com/wholesale?SearchText={q}",
    "yahoo_auctions_jp": "https://buyee.jp/item/search/query/{q}",
    "mercari_jp": "https://buyee.jp/mercari/search?keyword={q}",
}

# Templates for linking a *specific* listing when we know its id (exact product,
# not a search). eBay Browse returns ids like "v1|123456789012|0"; the numeric
# middle segment is the legacy item id that ebay.com/itm/{id} resolves.
ITEM_URLS: dict[str, str] = {
    "ebay_us": "https://www.ebay.com/itm/{id}",
    "aliexpress": "https://www.aliexpress.com/item/{id}.html",
    "etsy": "https://www.etsy.com/listing/{id}",
}


def item_url(venue: str, item_id: str | None) -> str | None:
    """Direct link to one listing, or None if we can't build one for this venue."""

    tpl = ITEM_URLS.get(venue)
    if not tpl or not item_id:
        return None
    iid = str(item_id)
    if venue == "ebay_us" and "|" in iid:
        parts = iid.split("|")
        iid = parts[1] if len(parts) > 1 and parts[1] else iid
    return tpl.format(id=iid)


SELLER_SIGNUP_URLS: dict[str, str] = {
    "ebay_us": "https://www.ebay.com/sl/sell",
    "amazon_us": "https://sell.amazon.com/global-selling",
    "etsy": "https://www.etsy.com/sell",
    "shopee_th": "https://seller.shopee.co.th",
    "lazada_th": "https://sellercenter.lazada.co.th",
    "tiktok_shop_th": "https://seller-th.tiktok.com",
    "facebook_mp_th": "https://www.facebook.com/marketplace/create",
    "kaidee_th": "https://www.kaidee.com/post",
}

# Cross-cutting services the playbooks reference.
SERVICE_URLS: dict[str, str] = {
    "buyee": "https://buyee.jp",
    "payoneer": "https://www.payoneer.com",
    "wise": "https://wise.com",
    "thailand_post": "https://www.thailandpost.co.th",
}


def clean_query(title: str) -> str:
    """Product titles carry annotations like '(JP print)' — strip them for search."""

    q = title.split("—")[0].split("(")[0].strip()
    return q or title.strip()


def search_url(venue: str, title_or_query: str) -> str | None:
    tpl = SEARCH_URLS.get(venue)
    return tpl.format(q=quote_plus(clean_query(title_or_query))) if tpl else None


def signup_url(venue: str) -> str | None:
    return SELLER_SIGNUP_URLS.get(venue)
