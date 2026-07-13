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

from . import __version__, economics, thailand
from .config import PLANS, WEB_DIR, Config
from .db import Store
from .models import OppStatus
from .pipeline import Orchestrator, briefing


class OutcomeIn(BaseModel):
    result: str                                  # "success" | "failure"
    realized_profit_usd: float | None = None
    days_taken: float | None = None
    failure_reason: str | None = None            # competition|shipping|demand|fees|customs|price_moved|capital
    notes: str | None = None


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
        if seed_cycles and not store.recent_cycles(1):
            for _ in range(seed_cycles):
                orch.run_cycle()
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
        return {"ok": True, "version": __version__, "tick": store.meta_get("tick", 0),
                "demo_mode": True,
                "note": "Demo mode runs on a deterministic simulated market; wire live "
                        "connectors in opportunity_os/market/live.py for production."}

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
