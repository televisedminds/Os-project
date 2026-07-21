"""REST API + dashboard host.

The product is the feed, so the API is the product: verified opportunities
with full evidence, the agent fleet's status, the learning state, and a
morning briefing. Plans gate volume and playbooks (free = 3 opportunities,
no playbooks), which is how the platform sells intelligence rather than chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import date as _date

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from statistics import fmean

from . import __version__, economics, links, thailand
from .config import PLANS, WEB_DIR, Config
from .db import Store
from .models import OppStatus
from .pipeline import Orchestrator, briefing

# Rough door-to-door sourcing lead times by origin country (days).
LEAD_DAYS = {"TH": 1, "JP": 6, "CN": 14, "US": 10}


def _pct_delta(now: float, base: float) -> str | None:
    if base <= 0:
        return None
    d = (now - base) / base * 100
    if abs(d) < 1:
        return "±0%"
    return f"{'+' if d > 0 else ''}{d:.0f}%"


def _forecast(window_days: float) -> dict:
    """Survival estimate for the edge: modelled half-life = window/2."""

    half = max(2.0, float(window_days)) / 2
    probs = {d: round(100 * 0.5 ** (d / half)) for d in (1, 3, 7, 14)}
    if window_days <= 4:
        rec = "Start today — this window is already closing."
    elif window_days <= 8:
        rec = "Start within 1–2 days; waiting a week roughly halves your odds."
    else:
        rec = "Window is comfortable, but earlier entry captures the best prices."
    return {"probabilities": probs, "recommendation": rec,
            "note": "Model estimate from the current decay window — not a promise."}


def _money_timeline(o: dict) -> list[dict]:
    """Google-Maps-for-money: the expected cash journey, day by day."""

    e = o["economics"]
    if o["type"] == "product_arbitrage":
        buy_c = o["route"].get("buy_country", "TH")
        lead = LEAD_DAYS.get(buy_c, 7)
        sell_days = max(2.0, min(float(o["window_days"]), 14.0))
        frac = min(0.95, e["base"]["total_cost_usd"] / max(1.0, e["base"]["revenue_usd"]))
        return [
            {"day": 0, "label": "Buy inventory", "amount_usd": -round(e["capital_usd"], 2)},
            {"day": lead, "label": "Inventory arrives — listings go live", "amount_usd": None},
            {"day": lead + 1, "label": "First sale expected", "amount_usd": None},
            {"day": round(lead + sell_days * frac), "label": "Break even", "amount_usd": 0},
            {"day": round(lead + sell_days), "label": "Sold through — profit banked",
             "amount_usd": round(e["total_net_usd"], 2)},
            {"day": round(lead + sell_days) + 1, "label": "Reinvestable capital",
             "amount_usd": round(e["capital_usd"] + e["total_net_usd"], 2)},
        ]
    payback = round(e["capital_usd"] / max(1.0, e["total_net_usd"]) * 30)
    return [
        {"day": 0, "label": "Startup cost", "amount_usd": -round(e["capital_usd"], 2)},
        {"day": round(o.get("playbook", {}).get("timeline_days", 14) if o.get("playbook") else 14),
         "label": "Launched", "amount_usd": None},
        {"day": payback, "label": "Break even", "amount_usd": 0},
        {"day": max(payback + 1, 30), "label": "Expected monthly net from here",
         "amount_usd": round(e["total_net_usd"], 2)},
    ]


def _discovery(o: dict, world) -> dict | None:
    """The detective report: what actually moved, with deltas."""

    try:
        rows = []
        eid = o["entity_id"]
        mentions = []
        for src in world.social_sources():
            h = world.mentions(eid, src)
            if h:
                mentions = [a + b for a, b in zip(mentions, h)] if mentions else list(h)
        if len(mentions) >= 5:
            base = fmean(mentions[-8:-1])
            rows.append({"label": "Social mentions (24h)", "value": f"{mentions[-1]}/day",
                         "delta": _pct_delta(mentions[-1], base)})

        if o["type"] == "product_arbitrage":
            bv, sv = o["route"]["buy_venue"], o["route"]["sell_venue"]
            sh = world.product_history(eid, sv)
            if len(sh) >= 5:
                prices = [x["price"] for x in sh]
                stocks = [x["stock"] for x in sh]
                rows.append({"label": f"Sell price — {economics.VENUES[sv]['name']}",
                             "value": f"${prices[-1]:.2f}",
                             "delta": _pct_delta(prices[-1], fmean(prices[-8:-1]))})
                rows.append({"label": f"Visible stock — {economics.VENUES[sv]['name']}",
                             "value": f"{stocks[-1]} units",
                             "delta": _pct_delta(stocks[-1], fmean(stocks[-8:-1]))})
                rows.append({"label": "Active sellers", "value": f"{sh[-1]['sellers']}", "delta": None})
                rows.append({"label": "Sell-through", "value": f"{sh[-1]['sold_7d'] / 7:.1f}/day", "delta": None})
            bl = world.listing(eid, bv)
            if bl:
                rows.append({"label": f"Source supply — {economics.VENUES[bv]['name']}",
                             "value": f"{bl['stock']} units @ ${bl['price']:.2f}",
                             "delta": "still deep" if bl["stock"] >= 20 else "limited"})
            # Detection provenance: when WE first flagged it, and what the
            # market has done since — the provable head start.
            age_ticks = world.tick_no - o.get("tick_created", world.tick_no)
            if 0 < age_ticks < len(sh) and o.get("created_ts"):
                p0 = sh[-(age_ticks + 1)]["price"]
                hours = (time.time() - o["created_ts"]) / 3600
                age_label = f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f} days ago"
                rows.insert(0, {"label": "⚡ First flagged by the fleet",
                                "value": f"{age_label} @ ${p0:.2f}",
                                "delta": _pct_delta(sh[-1]["price"], p0)})
        else:
            nh = world.niche_history(eid)
            if len(nh) >= 5:
                vols = [x["volume"] for x in nh]
                rows.append({"label": "Demand volume", "value": f"{vols[-1]:,.0f}/mo",
                             "delta": _pct_delta(vols[-1], fmean(vols[-8:-1]))})
                m = nh[-1]
                rows.append({"label": "Demand posts", "value": f"{m['demand_posts']:.0f}/mo", "delta": None})
                supply = m.get("solution_count") if o["type"] in ("digital_product", "info_product") else m.get("providers")
                rows.append({"label": "Competing supply", "value": f"{supply}", "delta": None})

        rows.append({"label": "Profit window", "value": f"~{o['window_days']:.0f} days", "delta": None})
        return {"rows": rows} if rows else None
    except Exception:
        return None


class OutcomeIn(BaseModel):
    result: str                                  # "success" | "failure"
    realized_profit_usd: float | None = None
    days_taken: float | None = None
    failure_reason: str | None = None            # competition|shipping|demand|fees|customs|price_moved|capital
    notes: str | None = None


class ProgressIn(BaseModel):
    step: int
    done: bool = True


def _action_card(o: dict, operator: dict | None = None, resolve=None) -> dict | None:
    """A do-this-deal summary: where to buy, where to sell, at which prices,
    with clickable links — everything needed to act, in one block.

    `resolve(venue) -> (exact_url, search_url)` maps a venue to the most
    specific link we can offer (a manual watchlist URL or a live listing id,
    falling back to the venue's search). Defaults to search-only."""

    operator = operator or {}
    registered = set(operator.get("registered_venues", []))

    def _links(venue: str) -> tuple[str | None, str | None]:
        if resolve:
            return resolve(venue)
        s = links.search_url(venue, o["title"])
        return s, s

    try:
        e = o["economics"]
        fx = e.get("fx", {}).get("USD_THB", 36.4)
        thb = lambda x: round(float(x) * fx)  # noqa: E731
        pb = o.get("playbook") or {}
        steps = [s["title"] for s in pb.get("steps", [])]

        if o["type"] == "product_arbitrage":
            route = o["route"]
            bv, sv = route["buy_venue"], route["sell_venue"]
            buy_usd = e["base"]["lines"][0]["amount_usd"]
            listing = pb.get("listing") or {}
            sell_usd = float(listing.get("price_usd") or e["base"]["revenue_usd"])
            buy_url, buy_search = _links(bv)
            sell_url, sell_search = _links(sv)
            dislocation = route.get("kind") == "dislocation"
            if dislocation and route.get("buy_url"):
                # The exact underpriced listing — the whole point of a
                # dislocation is that we can hand over the precise URL to buy.
                buy_url = route["buy_url"]
            return {
                "type": "flip",
                "dislocation": dislocation,
                "buy": {"venue": economics.VENUES[bv]["name"],
                        "price_usd": round(buy_usd, 2), "price_thb": thb(buy_usd),
                        "max_price_usd": round(buy_usd * 1.08, 2),
                        "qty": e["qty"],
                        "url": buy_url, "search_url": buy_search,
                        "exact": bool(buy_url and buy_url != buy_search),
                        "how": thailand.VENUE_ACCESS.get(bv, {}).get("buy_note", "")},
                "sell": {"venue": economics.VENUES[sv]["name"],
                         "price_usd": round(sell_usd, 2), "price_thb": thb(sell_usd),
                         "url": sell_url, "search_url": sell_search,
                         "exact": bool(sell_url and sell_url != sell_search),
                         "signup_url": links.signup_url(sv),
                         "registered": sv in registered,
                         "how": ("✓ You're already registered here."
                                 if sv in registered else
                                 thailand.VENUE_ACCESS.get(sv, {}).get("sell_note", ""))},
                "invest_usd": e["capital_usd"], "invest_thb": thb(e["capital_usd"]),
                "profit_usd": e["total_net_usd"], "profit_thb": thb(e["total_net_usd"]),
                "profit_unit_usd": e["base"]["net_usd"],
                "pessimistic_unit_usd": e["pessimistic"]["net_usd"],
                "margin_pct": e["base"]["margin_pct"],
                "timeline_days": pb.get("timeline_days") or o["window_days"],
                "first_steps": steps[:4],
                "money_timeline": _money_timeline(o),
                "forecast": _forecast(o["window_days"]),
            }
        monthly = e["total_net_usd"]
        return {
            "type": "venture",
            "what": o["subtitle"],
            "invest_usd": e["capital_usd"], "invest_thb": thb(e["capital_usd"]),
            "monthly_usd": monthly, "monthly_thb": thb(monthly),
            "pessimistic_monthly_usd": e["pessimistic"]["net_usd"],
            "payback_months": round(e["capital_usd"] / max(1.0, monthly), 1),
            "geo": o.get("route", {}).get("geo", "global"),
            "timeline_days": pb.get("timeline_days") or o["window_days"],
            "first_steps": steps[:4],
            "money_timeline": _money_timeline(o),
            "forecast": _forecast(o["window_days"]),
        }
    except Exception:
        return None


def _row(o: dict, affinity: int = 0) -> dict:
    """Feed-row projection of a stored opportunity."""

    route = o.get("route", {})
    if o["type"] == "product_arbitrage":
        if route.get("kind") == "dislocation":
            route_label = f"{economics.VENUES.get(route.get('buy_venue', ''), {}).get('name', '?')} dislocation"
        else:
            route_label = f"{route.get('buy_country', '?')} → {route.get('sell_country', '?')}"
    else:
        route_label = route.get("geo", "global")
    return {
        "id": o["id"], "title": o["title"], "subtitle": o["subtitle"],
        "type": o["type"], "category": o["category"], "status": o["status"],
        "score": o["score"]["overall"], "confidence": o["confidence"],
        "net_usd": o["economics"]["total_net_usd"],
        "net_unit_usd": o["economics"]["base"]["net_usd"],
        "margin_pct": o["economics"]["base"]["margin_pct"],
        "capital_usd": o["economics"]["capital_usd"],
        "net_thb": o["economics"]["thb"].get("total_net_thb") or o["economics"]["thb"].get("net_per_month_thb"),
        "qty": o["economics"]["qty"],
        "econ_kind": o["economics"]["kind"],
        "window_days": o["window_days"], "route": route_label,
        "tick_created": o["tick_created"], "tick_updated": o["tick_updated"],
        "updated_ts": o.get("updated_ts"),
        "sources": o.get("sources", []),
        "verification_level": o.get("verification_level", "discovered"),
        "single_source": o.get("single_source", True),
        "invalidation_reason": o.get("invalidation_reason", ""),
        "personal": ({"boost": affinity,
                      "note": (f"Prioritized — you've profited in {o['category'].replace('_', ' ')} before"
                               if affinity > 0 else
                               f"Downranked — past losses in {o['category'].replace('_', ' ')}")}
                     if affinity else None),
    }


def create_app(config: Config | None = None, auto_cycle_seconds: int | None = None,
               seed_cycles: int = 2) -> FastAPI:
    cfg = config or Config()
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store)
    auto = cfg.auto_cycle_seconds if auto_cycle_seconds is None else auto_cycle_seconds

    def _push_new_verified(report: dict) -> None:
        """Instant Telegram alert for opportunities that verified for the
        FIRST time this cycle (refreshes stay quiet). Live mode only; any
        failure is swallowed — alerting must never break the research loop."""

        if cfg.mode != "live" or not (cfg.telegram_bot_token and cfg.telegram_chat_id):
            return
        new = [p for p in report.get("published", []) if p.get("new")]
        if not new:
            return
        try:
            from .notify import alert_text, send_telegram
            send_telegram(cfg, alert_text(new))
        except Exception:  # noqa: BLE001
            pass

    async def _auto_loop():
        while True:
            await asyncio.sleep(auto)
            report = await asyncio.to_thread(orch.run_cycle)
            _push_new_verified(report)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if not store.recent_cycles(1):
            if cfg.mode == "demo" and seed_cycles:
                for _ in range(seed_cycles):
                    orch.run_cycle()
            elif cfg.mode == "live":
                try:
                    orch.run_cycle()          # first observation pass (baselines)
                except Exception:
                    pass                       # adapters degrade; don't block startup
        task = asyncio.create_task(_auto_loop()) if auto and auto > 0 else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        store.close()

    app = FastAPI(title="Opportunity OS", version=__version__, lifespan=lifespan)
    from .ai import AIClassifier
    brain = AIClassifier(cfg)                    # selling kits + on-demand judgment
    app.state.orchestrator = orch
    app.state.store = store
    app.state.config = cfg
    app.state.brain = brain

    from .security import AdminGuard, SecretBox
    guard = AdminGuard(cfg)                       # gates settings + mutating endpoints
    app.state.guard = guard

    def plan_of(name: str | None) -> dict:
        return cfg.plan(name)

    # ------------------------------------------------------------------- api

    @app.get("/api/health")
    def health():
        out = {"ok": True, "version": __version__, "mode": cfg.mode,
               "tick": store.meta_get("tick", 0) if cfg.mode == "demo" else store.meta_get("live_tick", 0),
               "demo_mode": cfg.mode == "demo"}
        if cfg.mode == "demo":
            out["note"] = ("Demo mode runs on a deterministic simulated market; run with --live "
                           "and a watchlist for real data.")
        else:
            out["adapters"] = orch.world.status() if hasattr(orch.world, "status") else []
            out["source_errors_last_cycle"] = getattr(orch.world, "errors", [])
            out["fx"] = store.meta_get("live_fx")
            disc = getattr(orch.world, "discovery", None)
            if disc is not None:
                out["discovery"] = {"enabled": True, "sources": disc.status(),
                                    "counts": store.discovered_counts(),
                                    "last_run": getattr(orch.world, "discovery_report", {})}
            else:
                out["discovery"] = {"enabled": False}
        return out

    @app.get("/api/briefing")
    def get_briefing(plan: str | None = None):
        return briefing(store, cfg, plan)

    @app.get("/api/opportunities")
    def list_opportunities(status: str | None = None, category: str | None = None,
                           min_score: float = 0.0, q: str | None = None,
                           plan: str | None = None, cluster: bool = True,
                           limit: int = Query(default=100, le=500)):
        p = plan_of(plan)
        from . import clustering
        raw = store.list_opportunities(status, category, min_score, q, limit)
        metrics = clustering.cluster_metrics(len(raw), [o for o in raw if o["status"] == "active"])

        if cluster:
            # Phase 6: collapse duplicate theses (e.g. 20 underpriced listings of
            # one model) into ONE row that carries the depth, instead of 20 rows.
            clusters = {c["representative_id"]: c
                        for c in clustering.cluster([o for o in raw if o["status"] == "active"])}
            kept = [o for o in raw if o["status"] != "active" or o["id"] in clusters]
            rows = []
            for o in kept:
                r = _row(o, orch.learning.category_affinity(o["category"]))
                c = clusters.get(o["id"])
                if c and c["qualifying_listings"] > 1:
                    r["cluster"] = c
                rows.append(r)
        else:
            rows = [_row(o, orch.learning.category_affinity(o["category"])) for o in raw]

        rows.sort(key=lambda r: (r["status"] != "active",
                                 -(r["score"] + 2 * ((r.get("personal") or {}).get("boost", 0)))))
        locked = 0
        if p["max_opportunities"] is not None:
            keep = [r for r in rows if r["status"] == "active"][:p["max_opportunities"]]
            keep_ids = {r["id"] for r in keep}
            locked = len([r for r in rows if r["status"] == "active"]) - len(keep)
            rows = [r for r in rows if r["id"] in keep_ids or r["status"] != "active"]
        return {"plan": p["label"], "count": len(rows), "locked": max(0, locked),
                "metrics": metrics, "opportunities": rows}

    @app.get("/api/opportunities/{opp_id}")
    def get_opportunity(opp_id: str, plan: str | None = None):
        o = store.get_opportunity(opp_id)
        if not o:
            raise HTTPException(404, "unknown opportunity id")
        # Attach observable history for the detail charts (not persisted).
        try:
            if o["type"] == "product_arbitrage":
                sv = o["route"]["sell_venue"]
                hist = orch.world.product_history(o["entity_id"], sv)[-30:]
                o["history"] = {"label": f"Sell price — {economics.VENUES[sv]['name']}",
                                "unit": "$", "points": [h["price"] for h in hist]}
            else:
                hist = orch.world.niche_history(o["entity_id"])[-30:]
                o["history"] = {"label": "Monthly demand volume", "unit": "",
                                "points": [h["volume"] for h in hist]}
        except Exception:
            o["history"] = None
        p = plan_of(plan)
        o["kit"] = store.get_kit(opp_id)
        o["kit_available"] = brain.configured()
        if not p["playbooks"]:
            o = dict(o)
            o["playbook"] = None
            o["automation"] = None
            o["kit"] = None
            o["locked"] = {"playbooks": "Execution playbooks and automation plans are a Pro feature.",
                           "upgrade": PLANS["pro"]["blurb"]}
        def resolve_link(venue: str) -> tuple[str | None, str | None]:
            """Most specific link we can build for this venue, plus the search fallback."""

            search = links.search_url(venue, o["title"])
            # 1) a URL the operator pinned on a manual watchlist quote (their exact source)
            watch = getattr(orch.world, "watch", None)
            if watch is not None:
                wp = watch.product(o["entity_id"])
                if wp:
                    manual = wp.manual_listings.get(venue) or {}
                    if manual.get("url"):
                        return manual["url"], search
            # 2) an exact live listing id captured by the adapter (e.g. eBay Browse)
            try:
                rows = store.live_snapshot_series(o["entity_id"], venue, 1)
                ids = rows[-1]["extra"].get("item_ids") if rows else None
                exact = links.item_url(venue, ids[0]) if ids else None
                if exact:
                    return exact, search
            except Exception:
                pass
            return search, search

        o["action"] = _action_card(o, orch.operator, resolve_link)
        o["discovery"] = _discovery(o, orch.world)
        done = store.get_progress(opp_id)
        n_steps = len((o.get("playbook") or {}).get("steps", []) or [])
        o["progress"] = {"done_steps": done,
                         "pct": round(100 * len(done) / n_steps) if n_steps else 0}
        return o

    @app.post("/api/opportunities/{opp_id}/outcome")
    def record_outcome(opp_id: str, body: OutcomeIn):
        o = store.get_opportunity(opp_id)
        if not o:
            raise HTTPException(404, "unknown opportunity id")
        if body.result not in ("success", "failure"):
            raise HTTPException(422, "result must be 'success' or 'failure'")
        store.add_outcome(opp_id, body.result, body.realized_profit_usd, body.days_taken,
                          body.failure_reason, body.notes)
        update = orch.learning.record_outcome(o, body.result, body.realized_profit_usd,
                                              body.failure_reason)
        o["status"] = OppStatus.EXECUTED.value
        store.upsert_opportunity(o)
        return {"recorded": True, "learning": update}

    @app.post("/api/opportunities/{opp_id}/kit")
    def make_kit(opp_id: str, force: bool = False, plan: str | None = None):
        """Generate (or return the cached) AI selling kit: ready-to-paste
        bilingual listings for flips, a launch kit for ventures."""

        o = store.get_opportunity(opp_id)
        if not o:
            raise HTTPException(404, "unknown opportunity id")
        if not plan_of(plan)["playbooks"]:
            raise HTTPException(403, "Selling kits are a Pro feature — switch the plan picker.")
        if not force:
            cached = store.get_kit(opp_id)
            if cached:
                return {**cached, "cached": True}
        kit, err = brain.generate_kit(o)
        if kit is None:
            raise HTTPException(503, err)
        store.save_kit(opp_id, cfg.ai_model, kit)
        return {"kit": kit, "model": cfg.ai_model, "generated_at": time.time(), "cached": False}

    @app.post("/api/opportunities/{opp_id}/progress")
    def set_progress(opp_id: str, body: ProgressIn):
        if not store.get_opportunity(opp_id):
            raise HTTPException(404, "unknown opportunity id")
        store.set_progress(opp_id, body.step, body.done)
        return {"done_steps": store.get_progress(opp_id)}

    @app.get("/api/activity")
    def activity(limit: int = Query(default=40, le=100)):
        """The fleet's recent work as a human-readable live feed."""

        items: list[dict] = []
        latest_by_agent: dict[str, dict] = {}
        for s in store.recent_signals(200):
            if s["agent"] not in latest_by_agent:
                latest_by_agent[s["agent"]] = {"ts": s["ts"], "tick": s["tick"], "n": 0}
            if s["tick"] == latest_by_agent[s["agent"]]["tick"]:
                latest_by_agent[s["agent"]]["n"] += 1
        names = {a["agent"]: a["name"] for a in store.list_agents()}
        for agent, v in latest_by_agent.items():
            items.append({"ts": v["ts"], "kind": "scan", "actor": names.get(agent, agent),
                          "text": f"scanned — {v['n']} signals captured (pass {v['tick']})"})
        for a in store.recent_anomalies(12):
            items.append({"ts": a["ts"], "kind": "anomaly", "actor": "Anomaly detector",
                          "text": a["summary"]})
        for c in store.recent_cycles(2):
            rep = c["report"]
            for p in rep.get("published", []):
                items.append({"ts": c["ts"], "kind": "publish", "actor": "Verification council",
                              "text": f"VERIFIED · {p['title']} — score {p['score']}, "
                                      f"confidence {p['confidence']:.0%}"})
            # Rejections are the most numerous events (the fee/verification
            # gate is strict on purpose); cap them per cycle so they don't
            # crowd scans and publications out of the live feed.
            for r_ in rep.get("rejected", [])[:6]:
                items.append({"ts": c["ts"], "kind": "reject", "actor": "Verification council",
                              "text": f"REJECTED · {r_['title']} — {r_['reason']}"})
            for iv in rep.get("invalidated", []):
                items.append({"ts": c["ts"], "kind": "invalidate", "actor": "Re-verification",
                              "text": f"KILLED · {iv['title']} — {iv['reason']}"})
        items.sort(key=lambda x: x["ts"], reverse=True)
        top = items[:limit]
        # Cycle events are written after signals, so a busy cycle can bury the
        # "fleet scanned" lines. Guarantee they stay visible — the live feed
        # exists to show the fleet working, not only its conclusions.
        if not any(i["kind"] == "scan" for i in top):
            scans = [i for i in items if i["kind"] == "scan"][:3]
            if scans:
                top = (scans + [i for i in top if i["kind"] != "scan"])[:limit]
                top.sort(key=lambda x: x["ts"], reverse=True)
        return {"items": top}

    @app.get("/api/goal")
    def goal():
        """The compounding view: wallet → goal, with today's best mission."""

        op = orch.operator or {}
        actives = store.active_opportunities()
        realized = sum((r["realized_profit_usd"] or 0) for r in store.recent_outcomes(200)
                       if r["result"] == "success")
        realized -= sum(abs(r["realized_profit_usd"] or 0) for r in store.recent_outcomes(200)
                        if r["result"] == "failure")
        capital_start = op.get("capital_usd") or op.get("budget_usd") or cfg.capital_cap_usd
        wallet = round(capital_start + realized, 2)
        deployed = round(sum(o["economics"]["capital_usd"] for o in actives
                             if 1 in store.get_progress(o["id"])), 2)
        goal_usd = op.get("goal_usd")

        mission, best_roi = None, 0.0
        for o in actives:
            cap = o["economics"]["capital_usd"]
            if 0 < cap <= wallet:
                roi = o["economics"]["total_net_usd"] / cap
                if roi > best_roi:
                    best_roi = roi
                    mission = {"id": o["id"], "title": o["title"],
                               "expected_usd": o["economics"]["total_net_usd"],
                               "capital_usd": cap, "roi_pct": round(roi * 100),
                               "window_days": o["window_days"]}
        # Conservative projection: assume you execute ~1/3 of what's verified.
        projected_monthly = round(sum(o["economics"]["total_net_usd"] / max(2.0, o["window_days"]) * 30
                                      for o in actives) / 3, 2)
        out = {
            "enabled": bool(goal_usd or op.get("capital_usd")),
            "wallet_usd": wallet, "capital_start_usd": capital_start,
            "realized_usd": round(realized, 2), "deployed_usd": deployed,
            "cash_usd": round(wallet - deployed, 2),
            "goal_usd": goal_usd,
            "progress_pct": round(100 * wallet / goal_usd, 1) if goal_usd else None,
            "projected_monthly_usd": projected_monthly,
            "eta_months": (round(max(0.0, goal_usd - wallet) / projected_monthly, 1)
                           if goal_usd and projected_monthly > 0 else None),
            "mission": mission,
            "recommendation": (f"Act: '{mission['title']}' is the best use of your cash today "
                               f"({mission['roi_pct']}% ROI)." if mission else
                               "Hold cash — nothing verified clears the bar for your wallet today. "
                               "That is the system protecting you, not failing you."),
            "note": "Projection assumes you execute about a third of verified opportunities; "
                    "recorded outcomes replace assumptions over time.",
        }
        return out

    @app.get("/api/funnel")
    def funnel(hours: float = Query(default=24, le=168)):
        """The research funnel: how much work produced today's shortlist."""

        cutoff = time.time() - hours * 3600
        agg = {"observations": 0, "anomalies": 0, "investigations": 0,
               "verified": 0, "rechecked": 0, "rejected": 0, "killed": 0, "cycles": 0}
        for c in store.recent_cycles(50):
            if c["ts"] < cutoff:
                continue
            rep = c["report"]
            pubs = rep.get("published", [])
            agg["cycles"] += 1
            agg["observations"] += rep.get("signals", 0)
            agg["anomalies"] += rep.get("anomalies", 0)
            agg["investigations"] += rep.get("candidates", 0)
            # honesty: "verified" = verified for the FIRST time; a deal that
            # re-verifies every cycle is a re-check, not 48 new wins a day.
            agg["verified"] += len([p for p in pubs if p.get("new")])
            agg["rechecked"] += rep.get("reverified", 0) + len([p for p in pubs if not p.get("new")])
            agg["rejected"] += len(rep.get("rejected", []))
            agg["killed"] += len(rep.get("invalidated", []))
        agg["recommended_now"] = store.stats()["opportunities_active"]
        agg["hours"] = hours
        return agg

    @app.get("/api/radar")
    def radar():
        """Known future catalysts (you feed them; the AI computes prep windows)."""

        try:
            from .market import watchlist as wl
            items = wl.load(cfg.watchlist_path).radar if cfg.watchlist_path.exists() else []
        except Exception:
            items = []
        out = []
        today = _date.today()
        for c in items:
            try:
                d = _date.fromisoformat(c.date)
            except ValueError:
                continue
            days = (d - today).days
            if days < -7:
                continue
            out.append({"date": c.date, "label": c.label, "note": c.note,
                        "related": c.related, "days_until": days,
                        "prep_days": c.prep_days,
                        "prep_opens_in_days": max(0, days - c.prep_days),
                        "status": ("prep window OPEN — act now" if 0 <= days <= c.prep_days
                                   else "passed" if days < 0
                                   else f"prep opens in {days - c.prep_days} days")})
        out.sort(key=lambda x: x["days_until"])
        return {"items": out}

    @app.post("/api/cycle")
    def run_cycle(_: None = Depends(guard)):
        """Trigger a research cycle. Gated: it spends API budget (paid credits)
        and mutates state, so it is not open to anonymous callers."""

        return orch.run_cycle()

    @app.get("/api/diagnostics/funnel")
    def diagnostics_funnel():
        """Source + opportunity-type + rejection funnels from stored data (no
        network). Answers 'is 62 verified really 62 independent opportunities?'
        and 'why do candidates die?', per source and per type."""

        from . import diagnostics
        return {"by_source": diagnostics.source_funnel(store),
                "by_type": diagnostics.type_funnel(store),
                "rejections": diagnostics.rejection_funnel(store)}

    @app.post("/api/diagnostics/sources")
    def diagnostics_sources(_: None = Depends(guard)):
        """Honest per-source health via real test calls. Gated + POST because a
        probe spends live budget (a Serper/ScrapingDog credit, a tiny Claude
        call) and hits the network."""

        from . import diagnostics
        world = orch.world
        adapters = getattr(world, "adapters", None)
        return {"sources": diagnostics.probe_sources(cfg, store, adapters)}

    @app.get("/api/agents")
    def agents():
        rows = store.list_agents()
        for r in rows:
            r["reliability"] = orch.learning.source_reliability(r["source"])
        return {"count": len(rows), "agents": rows}

    @app.get("/api/signals")
    def signals(limit: int = Query(default=40, le=200)):
        return {"signals": store.recent_signals(limit)}

    @app.get("/api/anomalies")
    def anomalies(limit: int = Query(default=30, le=200)):
        return {"anomalies": store.recent_anomalies(limit)}

    @app.get("/api/discovery")
    def discovery(limit: int = Query(default=60, le=200), active_only: bool = True):
        """What the discovery engine has auto-found and is watching."""

        disc = getattr(orch.world, "discovery", None)
        return {
            "enabled": disc is not None,
            "counts": store.discovered_counts(),
            "sources": disc.status() if disc is not None else [],
            "last_run": getattr(orch.world, "discovery_report", {}),
            "found": store.list_discovered(active_only=active_only, limit=limit),
        }

    @app.get("/api/learning")
    def learning():
        st = orch.learning.state
        return {"weights": st["weights"], "calibration": st["calibration"],
                "source_reliability": st["source_reliability"],
                "verifier_reliability": st["verifier_reliability"],
                "outcomes": st["outcomes"], "adjustments": st["adjustments"][-10:],
                "recent_outcomes": store.recent_outcomes(10)}

    @app.get("/api/stats")
    def stats():
        s = store.stats()
        s["tick"] = store.meta_get("tick", 0)
        cycles = store.recent_cycles(5)
        s["last_cycles"] = [{"tick": c["tick"], "duration_ms": c["duration_ms"],
                             "published": len(c["report"].get("published", [])),
                             "rejected": len(c["report"].get("rejected", [])),
                             "invalidated": len(c["report"].get("invalidated", [])),
                             "signals": c["report"].get("signals", 0)} for c in cycles]
        s["categories"] = sorted({o["category"] for o in store.list_opportunities(limit=500)})
        s["last_report"] = cycles[0]["report"] if cycles else None
        actives = store.active_opportunities()
        s["capital_needed_usd"] = round(sum(o["economics"]["capital_usd"] for o in actives), 2)
        rois = [(o["economics"]["total_net_usd"] / o["economics"]["capital_usd"] * 100)
                for o in actives if o["economics"]["capital_usd"] > 0]
        s["best_roi_pct"] = round(max(rois), 0) if rois else 0
        try:
            n_products = len(orch.world.product_ids())
            n_niches = len(orch.world.niches())
            s["watching"] = {"products": n_products, "niches": n_niches,
                             "total": n_products + n_niches,
                             "discovered": store.discovered_counts()["active"]
                             if cfg.mode == "live" else 0}
        except Exception:  # noqa: BLE001
            s["watching"] = None
        return s

    @app.get("/api/thailand")
    def thailand_profile():
        prof = thailand.profile()
        prof["venue_names"] = {vid: v["name"] for vid, v in economics.VENUES.items()}
        return prof

    @app.get("/api/plans")
    def plans():
        return {"default": cfg.default_plan, "plans": PLANS}

    # ------------------------------------------------------------- settings

    from . import settings as app_settings

    def _reset_key_clients():
        """Drop cached tokens/clients so freshly saved keys take effect now."""

        brain._client = None
        try:
            ai = getattr(getattr(orch.world, "discovery", None), "ai", None)
            if ai is not None:
                ai._client = None
            adapters = getattr(orch.world, "adapters", {}) or {}
            for ad in adapters.values():
                if hasattr(ad, "_token"):
                    ad._token = ""
        except Exception:  # noqa: BLE001
            pass

    @app.get("/api/settings")
    def get_settings(_: None = Depends(guard)):
        """Masked status of every managed key — raw values are never returned.
        Auth-gated: unauthenticated callers cannot even learn which keys exist."""

        return {"keys": app_settings.status(cfg, store),
                "security": _security_status(),
                "note": "Keys are encrypted at rest and applied immediately — no restart. "
                        "Values are never sent back to the browser."}

    @app.post("/api/settings")
    def save_settings(updates: dict[str, str], _: None = Depends(guard)):
        try:
            changed = app_settings.save(cfg, store, updates)
        except ValueError as e:
            raise HTTPException(422, str(e))
        _reset_key_clients()
        return {"changed": changed, "keys": app_settings.status(cfg, store)}

    @app.post("/api/settings/test")
    def test_settings(_: None = Depends(guard)):
        """Live-check every configured service (network); unset ones are skipped."""

        return {"results": app_settings.run_checks(cfg)}

    def _security_status() -> dict:
        box = SecretBox(cfg)
        insecure = []
        if not guard.configured():
            insecure.append("OOS_DASHBOARD_TOKEN is not set — admin endpoints are "
                            "localhost-only; set a token to use them remotely.")
        if not box.active:
            insecure.append("credential encryption is INACTIVE — install cryptography "
                            "and set OOS_SECRET_KEY.")
        return {"admin_token_set": guard.configured(),
                "encryption_active": box.active, "warnings": insecure}

    # ------------------------------------------------------------- dashboard

    if WEB_DIR.exists():
        # No-cache headers on /static so browsers always re-check the file, and
        # a mtime stamp on the asset URLs so a changed file always looks "new".
        # Together these mean an update shows up on the next reload — no more
        # stale dashboards after `git pull`.
        class NoCacheStatic(StaticFiles):
            def is_not_modified(self, *a, **k) -> bool:  # force revalidation
                return False

            async def get_response(self, path, scope):
                resp = await super().get_response(path, scope)
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
                return resp

        app.mount("/static", NoCacheStatic(directory=str(WEB_DIR)), name="static")

        from fastapi.responses import HTMLResponse

        @app.get("/", include_in_schema=False)
        def index():
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            for asset in ("app.js", "style.css"):
                try:
                    v = int((WEB_DIR / asset).stat().st_mtime)
                except OSError:
                    v = 0
                html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
            return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})

    return app
