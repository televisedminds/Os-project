"""REST API + dashboard host.

The product is the feed, so the API is the product: verified opportunities
with full evidence, the agent fleet's status, the learning state, and a
morning briefing. Plans gate volume and playbooks (free = 3 opportunities,
no playbooks), which is how the platform sells intelligence rather than chat.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI, HTTPException, Query
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


def _action_card(o: dict, operator: dict | None = None) -> dict | None:
    """A do-this-deal summary: where to buy, where to sell, at which prices,
    with clickable links — everything needed to act, in one block."""

    operator = operator or {}
    registered = set(operator.get("registered_venues", []))
    try:
        e = o["economics"]
        fx = e.get("fx", {}).get("USD_THB", 36.4)
        thb = lambda x: round(float(x) * fx)  # noqa: E731
        pb = o.get("playbook") or {}
        steps = [s["title"] for s in pb.get("steps", [])]

        if o["type"] == "product_arbitrage":
            bv, sv = o["route"]["buy_venue"], o["route"]["sell_venue"]
            buy_usd = e["base"]["lines"][0]["amount_usd"]
            listing = pb.get("listing") or {}
            sell_usd = float(listing.get("price_usd") or e["base"]["revenue_usd"])
            return {
                "type": "flip",
                "buy": {"venue": economics.VENUES[bv]["name"],
                        "price_usd": round(buy_usd, 2), "price_thb": thb(buy_usd),
                        "max_price_usd": round(buy_usd * 1.08, 2),
                        "qty": e["qty"],
                        "url": links.search_url(bv, o["title"]),
                        "how": thailand.VENUE_ACCESS.get(bv, {}).get("buy_note", "")},
                "sell": {"venue": economics.VENUES[sv]["name"],
                         "price_usd": round(sell_usd, 2), "price_thb": thb(sell_usd),
                         "url": links.search_url(sv, o["title"]),
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


def _row(o: dict) -> dict:
    """Feed-row projection of a stored opportunity."""

    route = o.get("route", {})
    if o["type"] == "product_arbitrage":
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
        "invalidation_reason": o.get("invalidation_reason", ""),
    }


def create_app(config: Config | None = None, auto_cycle_seconds: int | None = None,
               seed_cycles: int = 2) -> FastAPI:
    cfg = config or Config()
    store = Store(cfg.db_path)
    orch = Orchestrator(cfg, store)
    auto = cfg.auto_cycle_seconds if auto_cycle_seconds is None else auto_cycle_seconds

    async def _auto_loop():
        while True:
            await asyncio.sleep(auto)
            await asyncio.to_thread(orch.run_cycle)

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
    app.state.orchestrator = orch
    app.state.store = store
    app.state.config = cfg

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
        return out

    @app.get("/api/briefing")
    def get_briefing(plan: str | None = None):
        return briefing(store, cfg, plan)

    @app.get("/api/opportunities")
    def list_opportunities(status: str | None = None, category: str | None = None,
                           min_score: float = 0.0, q: str | None = None,
                           plan: str | None = None,
                           limit: int = Query(default=100, le=500)):
        p = plan_of(plan)
        rows = [_row(o) for o in store.list_opportunities(status, category, min_score, q, limit)]
        locked = 0
        if p["max_opportunities"] is not None:
            keep = [r for r in rows if r["status"] == "active"][:p["max_opportunities"]]
            keep_ids = {r["id"] for r in keep}
            locked = len([r for r in rows if r["status"] == "active"]) - len(keep)
            rows = [r for r in rows if r["id"] in keep_ids or r["status"] != "active"]
        return {"plan": p["label"], "count": len(rows), "locked": max(0, locked), "opportunities": rows}

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
        if not p["playbooks"]:
            o = dict(o)
            o["playbook"] = None
            o["automation"] = None
            o["locked"] = {"playbooks": "Execution playbooks and automation plans are a Pro feature.",
                           "upgrade": PLANS["pro"]["blurb"]}
        o["action"] = _action_card(o, orch.operator)
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
            for r_ in rep.get("rejected", []):
                items.append({"ts": c["ts"], "kind": "reject", "actor": "Verification council",
                              "text": f"REJECTED · {r_['title']} — {r_['reason']}"})
            for iv in rep.get("invalidated", []):
                items.append({"ts": c["ts"], "kind": "invalidate", "actor": "Re-verification",
                              "text": f"KILLED · {iv['title']} — {iv['reason']}"})
        items.sort(key=lambda x: x["ts"], reverse=True)
        return {"items": items[:limit]}

    @app.post("/api/cycle")
    def run_cycle():
        return orch.run_cycle()

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
        return s

    @app.get("/api/thailand")
    def thailand_profile():
        prof = thailand.profile()
        prof["venue_names"] = {vid: v["name"] for vid, v in economics.VENUES.items()}
        return prof

    @app.get("/api/plans")
    def plans():
        return {"default": cfg.default_plan, "plans": PLANS}

    # ------------------------------------------------------------- dashboard

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIR / "index.html")

    return app
