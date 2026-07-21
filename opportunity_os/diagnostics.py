"""Source diagnostics — prove which integrations actually work, and trace
where records die on the way to a published opportunity.

Two honest questions this answers:

1. *Is each source actually functional right now?* `probe_sources` makes a
   real, safe test call to every source and classifies the result — never
   "it's fine because a key is set". Statuses: HEALTHY, DEGRADED, AUTH_FAILED,
   QUOTA_EXHAUSTED, PARSER_BROKEN, NO_DATA, DISABLED.

2. *Why are almost all published opportunities eBay flips?* `source_funnel`
   and `type_funnel` count real records at each pipeline stage, per source and
   per opportunity type, from what is actually stored — so "62 verified" can
   be checked against how many are independent theses vs the same edge counted
   many times.

Nothing here fabricates numbers. A field we cannot truthfully obtain from a
source (e.g. an API that doesn't report remaining quota) is reported as
`None`/"unknown", never guessed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

# ------------------------------------------------------------------ statuses

HEALTHY = "HEALTHY"                 # real call succeeded and returned usable data
DEGRADED = "DEGRADED"              # works but throttled / partial / stale
AUTH_FAILED = "AUTH_FAILED"       # credential present but rejected
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
PARSER_BROKEN = "PARSER_BROKEN"   # data returned but nothing parsed out of it
NO_DATA = "NO_DATA"               # call ok, zero records (query or market empty)
DISABLED = "DISABLED"             # not configured / turned off


def _classify_error(msg: str) -> str:
    low = (msg or "").lower()
    if any(w in low for w in ("oauth", "401", "403", "unauthor", "invalid key",
                              "invalid api", "forbidden", "authentication")):
        return AUTH_FAILED
    if any(w in low for w in ("quota", "credit", "insufficient", "429", "rate limit",
                              "exhaust", "limit reached", "payment")):
        return QUOTA_EXHAUSTED
    if any(w in low for w in ("no parseable", "parser", "layout", "could not parse",
                              "unexpected", "keyerror", "json")):
        return PARSER_BROKEN
    if any(w in low for w in ("no priced", "no results", "no data", "empty", "not found")):
        return NO_DATA
    return DEGRADED


@dataclass
class SourceHealth:
    id: str
    name: str
    enabled: bool
    credential_present: bool
    auth_ok: bool | None = None
    request_ok: bool | None = None
    http_status: int | None = None
    records_returned: int | None = None
    records_parsed: int | None = None
    candidates_generated: int | None = None
    observations_stored: int | None = None
    last_success_ts: float | None = None
    last_failure_ts: float | None = None
    error: str = ""
    rate_limit_remaining: int | None = None      # None = source doesn't report it
    daily_allowance: str = "unknown"
    avg_latency_ms: float | None = None
    cost_per_request: str = "free"
    data_freshness: str = "unknown"
    status: str = DISABLED
    note: str = ""

    def dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------- probing

def _timed(fn):
    t0 = time.time()
    try:
        out = fn()
        return out, None, round((time.time() - t0) * 1000, 1)
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:300], round((time.time() - t0) * 1000, 1)


def probe_sources(cfg, store, adapters: dict | None = None) -> list[dict]:
    """Real, safe test call to every source. Returns a list of SourceHealth dicts."""

    from .market.adapters import build_adapters
    ad = adapters if adapters is not None else build_adapters(cfg)
    out: list[SourceHealth] = []

    out.append(_probe_ebay(cfg, ad.get("ebay_us")))
    out.append(_probe_reddit(cfg, ad.get("reddit")))
    out.append(_probe_serper(cfg, ad.get("serper")))
    out.append(_probe_news(cfg, ad.get("news")))
    out.append(_probe_fx(cfg, ad.get("fx")))
    out.append(_probe_shopee(cfg, ad.get("shopee_th")))
    out.append(_probe_anthropic(cfg))
    out.append(_probe_telegram(cfg))

    # Fold in stored observation + candidate counts so a source that probes
    # healthy but has never produced anything is visibly distinguished from one
    # that is feeding the pipeline.
    counts = _observation_counts(store)
    cands = _candidate_counts(store)
    for h in out:
        h.observations_stored = counts.get(h.id, 0)
        h.candidates_generated = cands.get(h.id, 0)
    return [h.dict() for h in out]


def _candidate_counts(store) -> dict[str, int]:
    """Discovery candidates each source has contributed (all time)."""

    out: dict[str, int] = {}
    try:
        for row in store.list_discovered(active_only=False, limit=1000):
            src = row.get("source") or "?"
            # discovery source ids map onto probe ids where they share a backend
            src = {"ebay_discovery": "ebay_us", "reddit_discovery": "reddit"}.get(src, src)
            out[src] = out.get(src, 0) + 1
    except Exception:  # noqa: BLE001
        pass
    return out


def _probe_ebay(cfg, adapter) -> SourceHealth:
    h = SourceHealth(id="ebay_us", name="eBay Browse API", enabled=True,
                     credential_present=bool(cfg.ebay_client_id and cfg.ebay_client_secret),
                     daily_allowance="~5,000 calls/day (free tier)", cost_per_request="free")
    if not h.credential_present:
        h.status, h.note = DISABLED, "no EBAY_CLIENT_ID/SECRET — the volume backbone is dark"
        return h
    snap, err, ms = _timed(lambda: adapter.product_snapshot("nintendo switch console"))
    h.avg_latency_ms = ms
    if snap is None:
        h.request_ok = False
        h.error = err or getattr(adapter, "last_error", "")
        h.auth_ok = "oauth" not in h.error.lower()
        h.status = _classify_error(h.error)
        h.last_failure_ts = time.time()
        return h
    h.auth_ok = h.request_ok = True
    h.http_status = 200
    h.records_returned = int(snap.get("stock", 0))
    h.records_parsed = len(snap.get("sample", []))
    h.last_success_ts = time.time()
    h.data_freshness = "live (this call)"
    h.status = HEALTHY if h.records_parsed else NO_DATA
    h.note = f"median ${snap.get('price')}, {h.records_parsed} listings parsed"
    return h


def _probe_reddit(cfg, adapter) -> SourceHealth:
    h = SourceHealth(id="reddit", name="Reddit", enabled=True,
                     credential_present=bool(cfg.reddit_client_id and cfg.reddit_client_secret),
                     daily_allowance="~1000 req/10min (OAuth)", cost_per_request="free")
    if not h.credential_present:
        h.status, h.note = DISABLED, ("no REDDIT_CLIENT_ID/SECRET — public JSON is blocked from "
                                      "server IPs; demand signal comes from Serper instead")
        return h
    n, err, ms = _timed(lambda: adapter.mentions_24h("nintendo switch"))
    h.avg_latency_ms = ms
    if n is None:
        h.request_ok, h.error = False, (err or getattr(adapter, "last_error", ""))
        h.auth_ok = "oauth" not in h.error.lower()
        h.status, h.last_failure_ts = _classify_error(h.error), time.time()
        return h
    h.auth_ok = h.request_ok = True
    h.http_status, h.records_returned = 200, int(n)
    h.records_parsed, h.last_success_ts = int(n), time.time()
    h.data_freshness = "live (past 24h)"
    h.status = HEALTHY if n > 0 else NO_DATA
    h.note = f"{n} posts/24h for probe query"
    return h


def _probe_serper(cfg, adapter) -> SourceHealth:
    h = SourceHealth(id="serper", name="Serper (Google search)", enabled=True,
                     credential_present=bool(cfg.serper_api_key),
                     daily_allowance="2,500 free credits (one-time)", cost_per_request="1 credit")
    if not h.credential_present:
        h.status, h.note = DISABLED, "no SERPER_API_KEY — niche demand falls back to bootstrap"
        return h
    n, err, ms = _timed(lambda: adapter.reddit_posts_7d("nintendo switch"))
    h.avg_latency_ms = ms
    if n is None:
        h.request_ok, h.error = False, (err or getattr(adapter, "last_error", ""))
        h.auth_ok = "403" not in h.error and "401" not in h.error
        h.status, h.last_failure_ts = _classify_error(h.error), time.time()
        return h
    h.auth_ok = h.request_ok = True
    h.http_status, h.records_returned = 200, int(n)
    h.records_parsed, h.last_success_ts = int(n), time.time()
    h.data_freshness = "live (past week)"
    h.status = HEALTHY if n > 0 else NO_DATA
    h.note = f"{n} reddit results this week for probe query"
    return h


def _probe_news(cfg, adapter) -> SourceHealth:
    h = SourceHealth(id="news", name="Google News RSS", enabled=True,
                     credential_present=True, daily_allowance="keyless", cost_per_request="free")
    got, err, ms = _timed(lambda: adapter.headlines("thailand economy", 4))
    h.avg_latency_ms = ms
    if got is None:
        h.request_ok, h.error = False, (err or getattr(adapter, "last_error", ""))
        h.status, h.last_failure_ts = _classify_error(h.error), time.time()
        return h
    h.auth_ok = h.request_ok = True
    h.http_status, h.records_returned = 200, len(got)
    h.records_parsed, h.last_success_ts = len(got), time.time()
    h.data_freshness = "live (rolling)"
    h.status = HEALTHY if got else NO_DATA
    h.note = f"{len(got)} headlines for probe query"
    return h


def _probe_fx(cfg, adapter) -> SourceHealth:
    h = SourceHealth(id="fx", name="FX rates", enabled=True, credential_present=True,
                     daily_allowance="keyless", cost_per_request="free")
    r, err, ms = _timed(lambda: adapter.rates())
    h.avg_latency_ms = ms
    if not r:
        h.request_ok, h.error = False, (err or getattr(adapter, "last_error", ""))
        h.status, h.last_failure_ts = _classify_error(h.error), time.time()
        return h
    h.auth_ok = h.request_ok = True
    h.http_status, h.records_returned, h.records_parsed = 200, len(r), len(r)
    h.last_success_ts, h.data_freshness = time.time(), "live (daily)"
    h.status, h.note = HEALTHY, f"USD/THB {r.get('THB')}, USD/JPY {r.get('JPY')}"
    return h


def _probe_shopee(cfg, adapter) -> SourceHealth:
    h = SourceHealth(id="shopee_th", name="Shopee TH (via ScrapingDog)", enabled=True,
                     credential_present=bool(cfg.scrapingdog_api_key),
                     daily_allowance="depends on ScrapingDog plan",
                     cost_per_request="~1 ScrapingDog credit (paid)")
    if not h.credential_present:
        h.status, h.note = DISABLED, "no SCRAPINGDOG_API_KEY — Shopee prices come from manual quotes"
        return h
    snap, err, ms = _timed(lambda: adapter.product_snapshot("nintendo switch"))
    h.avg_latency_ms = ms
    if snap is None:
        h.request_ok, h.error = False, (err or getattr(adapter, "last_error", ""))
        h.auth_ok = "api_key" not in h.error.lower() and "401" not in h.error
        h.status, h.last_failure_ts = _classify_error(h.error), time.time()
        return h
    h.auth_ok = h.request_ok = True
    h.http_status = 200
    h.records_returned = int(snap.get("stock", 0))
    h.records_parsed = len(snap.get("sample", []))
    h.last_success_ts, h.data_freshness = time.time(), "live (scrape)"
    h.status = HEALTHY if h.records_parsed else PARSER_BROKEN
    h.note = "EXPERIMENTAL scrape — parser can break when Shopee changes layout"
    return h


def _probe_anthropic(cfg) -> SourceHealth:
    h = SourceHealth(id="anthropic", name="Claude (AI investigator)", enabled=bool(cfg.ai_enabled),
                     credential_present=bool(cfg.anthropic_api_key),
                     daily_allowance="account credit", cost_per_request="~cents (tiny probe)")
    if not h.credential_present:
        h.status, h.note = DISABLED, "no ANTHROPIC_API_KEY — discovery uses keyword heuristics"
        return h
    from .ai import AIClassifier
    (ok, note), err, ms = _timed(lambda: AIClassifier(cfg).check()) or ((False, ""), None, 0)
    h.avg_latency_ms = ms
    if err:
        h.request_ok, h.error = False, err
        h.status = _classify_error(err)
        return h
    h.auth_ok = h.request_ok = ok
    h.status = HEALTHY if ok else _classify_error(note)
    h.note = note
    if ok:
        h.last_success_ts = time.time()
    return h


def _probe_telegram(cfg) -> SourceHealth:
    """Validates the bot token via getMe — never sends a message (no spam)."""

    h = SourceHealth(id="telegram", name="Telegram alerts", enabled=True,
                     credential_present=bool(cfg.telegram_bot_token and cfg.telegram_chat_id),
                     daily_allowance="n/a", cost_per_request="free")
    if not h.credential_present:
        h.status, h.note = DISABLED, "no TELEGRAM_BOT_TOKEN/CHAT_ID — phone alerts off"
        return h
    import httpx

    def _getme():
        r = httpx.get(f"https://api.telegram.org/bot{cfg.telegram_bot_token}/getMe",
                      timeout=cfg.http_timeout)
        return r.status_code, r.json()

    res, err, ms = _timed(_getme)
    h.avg_latency_ms = ms
    if res is None:
        h.request_ok, h.error, h.status = False, err, _classify_error(err or "")
        return h
    code, body = res
    h.http_status = code
    if code == 200 and body.get("ok"):
        h.auth_ok = h.request_ok = True
        h.last_success_ts, h.status = time.time(), HEALTHY
        h.note = f"bot @{body.get('result', {}).get('username', '?')} reachable (token valid)"
    else:
        h.auth_ok, h.status = False, AUTH_FAILED
        h.error = str(body)[:200]
    return h


# --------------------------------------------------------------- funnels

def _observation_counts(store) -> dict[str, int]:
    """How many observations each source has actually stored (all time)."""

    out: dict[str, int] = {}
    try:
        with store._lock:
            for venue, n in store._conn.execute(
                    "SELECT venue, COUNT(*) FROM live_snapshots GROUP BY venue").fetchall():
                out[venue] = out.get(venue, 0) + n
            for src, n in store._conn.execute(
                    "SELECT source, COUNT(*) FROM live_mentions GROUP BY source").fetchall():
                out[src] = out.get(src, 0) + n
    except Exception:  # noqa: BLE001
        pass
    return out


def source_funnel(store) -> list[dict]:
    """Per-source record trace: observations stored → published opportunities.

    Attribution uses what is truthfully recorded: observation rows carry their
    venue/source, and each opportunity records its `sources`. Stages the schema
    cannot yet attribute per-source (anomaly/candidate) are marked None rather
    than guessed — that gap is itself the Phase 2 finding."""

    obs = _observation_counts(store)
    pubs_by_source: dict[str, int] = {}
    active = store.list_opportunities(limit=1000)
    for o in active:
        for s in o.get("sources", []) or []:
            pubs_by_source[s] = pubs_by_source.get(s, 0) + 1
    ids = sorted(set(obs) | set(pubs_by_source))
    rows = []
    for sid in ids:
        rows.append({
            "source": sid,
            "observations_stored": obs.get(sid, 0),
            "published_opportunities": pubs_by_source.get(sid, 0),
        })
    rows.sort(key=lambda r: r["published_opportunities"], reverse=True)
    return rows


def type_funnel(store) -> dict:
    """Opportunity-type breakdown + a duplication audit: how many of the
    'verified' opportunities are independent theses vs the same edge repeated.

    A 'thesis' is (opportunity type + route + entity family), so twenty
    listings of the same underpriced model collapse to one thesis. Also breaks
    down by verification LEVEL (Phase 7), so single-source/partial work is not
    conflated with multi-source-verified work."""

    active = store.list_opportunities(status="active", limit=1000)
    by_type: dict[str, int] = {}
    by_level: dict[str, int] = {}
    theses: set[str] = set()
    products: set[str] = set()
    single_source = 0
    for o in active:
        route = o.get("route", {}) or {}
        kind = route.get("kind") or o.get("type")
        by_type[kind] = by_type.get(kind, 0) + 1
        level = o.get("verification_level", "discovered")
        by_level[level] = by_level.get(level, 0) + 1
        if o.get("single_source"):
            single_source += 1
        products.add(o.get("entity_id", ""))
        thesis = f"{o.get('type')}|{route.get('buy_venue')}|{route.get('sell_venue')}|{o.get('entity_id')}"
        theses.add(thesis)
    return {
        "verified_opportunities": len(active),
        "unique_products": len(products),
        "unique_theses": len(theses),
        "single_source": single_source,
        "by_type": dict(sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)),
        "by_verification_level": dict(sorted(by_level.items(), key=lambda kv: kv[1], reverse=True)),
        "note": "unique_theses < verified_opportunities means the same edge is "
                "counted more than once; single_source counts opportunities whose "
                "evidence all comes from one marketplace (never 'execution ready').",
    }


def rejection_funnel(store, cycles: int = 30) -> dict:
    """Why candidates die — the taxonomy breakdown across recent cycles, overall
    and per opportunity type. Turns 'lots rejected' into an actionable map."""

    by_category: dict[str, int] = {}
    by_type_category: dict[str, dict[str, int]] = {}
    total = 0
    examples: dict[str, str] = {}
    for c in store.recent_cycles(cycles):
        rep = c["report"]
        for rj in rep.get("rejected", []):
            cat = rj.get("category") or "other"
            typ = rj.get("type", "?")
            by_category[cat] = by_category.get(cat, 0) + 1
            by_type_category.setdefault(typ, {})
            by_type_category[typ][cat] = by_type_category[typ].get(cat, 0) + 1
            examples.setdefault(cat, rj.get("reason", "")[:120])
            total += 1
    return {
        "total_rejected": total,
        "by_category": dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)),
        "by_type": by_type_category,
        "example_reason": examples,
    }
