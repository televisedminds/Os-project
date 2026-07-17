"""The orchestrator: one research cycle, end to end.

    tick the market → scanners observe → anomalies detected → investigations
    → verification council → economics gates → publish → and, crucially,
    RE-VERIFY every already-published opportunity against live data.

Re-verification is the answer to the hardest problem in this product:
opportunities decay. A restock, a competitor pile-in, or a price convergence
kills a published opportunity — the platform notices on the next cycle and
marks it INVALIDATED with the reason, instead of leaving stale advice up.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .agents import (AnomalyDetector, Investigator, LearningEngine, ScoringEngine,
                     VerificationCouncil, build_automation, build_fleet, build_playbook)
from .config import Config
from .db import Store
from .market import SimulatedMarket
from .models import Opportunity, OppStatus, OppType, opportunity_id
from . import economics, thailand


class Orchestrator:
    def __init__(self, config: Config, store: Store, world=None):
        self.cfg = config
        self.db = store
        from . import settings as app_settings
        app_settings.load_into(config, store)      # keys saved in the dashboard
        self._guard_mode()
        self.world = world if world is not None else self._build_world()
        self.operator = self._load_operator()
        if self.operator.get("budget_usd"):
            self.cfg.capital_cap_usd = float(self.operator["budget_usd"])
        self.fleet = build_fleet(self.world)
        self.detector = AnomalyDetector(config)
        self.investigator = Investigator(config)
        self.learning = LearningEngine(store)

    def _load_operator(self) -> dict:
        """Operator profile from the watchlist (works in both modes)."""

        watch = getattr(self.world, "watch", None)
        if watch is not None:
            from dataclasses import asdict as _asdict
            return _asdict(watch.operator)
        try:
            from .market import watchlist as wl
            if self.cfg.watchlist_path.exists():
                from dataclasses import asdict as _asdict
                return _asdict(wl.load(self.cfg.watchlist_path).operator)
        except Exception:
            pass
        return {}

    def _guard_mode(self) -> None:
        """A database belongs to one mode; mixing sim and live history would
        poison every baseline."""

        stored = self.db.meta_get("mode")
        if stored and stored != self.cfg.mode:
            raise SystemExit(
                f"This database ({self.cfg.db_path}) was created in '{stored}' mode but OOS_MODE is "
                f"'{self.cfg.mode}'. Point OOS_DB at a different file (e.g. data/live.db) or delete it.")
        self.db.meta_set("mode", self.cfg.mode)

    def _build_world(self):
        if self.cfg.mode == "live":
            from .market.live import LiveMarket
            return LiveMarket(self.cfg, self.db)
        # demo: deterministic replay — rebuild from seed, re-run stored ticks,
        # so restarts resume exactly where they left off.
        w = SimulatedMarket(seed=self.cfg.world_seed, warmup=self.cfg.warmup_ticks)
        stored_tick = self.db.meta_get("tick", self.cfg.warmup_ticks)
        w.fast_forward(max(0, stored_tick - self.cfg.warmup_ticks))
        return w

    # ------------------------------------------------------------------ cycle

    def run_cycle(self) -> dict:
        t0 = time.time()
        self.world.tick()
        tick = self.world.tick_no

        n_signals = 0
        for agent in self.fleet:
            sigs = agent.scan(self.world)
            self.db.add_signals(sigs, tick)
            self.db.agent_run(agent, tick, len(sigs))
            n_signals += len(sigs)

        anomalies = self.detector.detect(self.world)
        self.db.add_anomalies(anomalies)
        candidates = self.investigator.build_candidates(self.world, anomalies)

        council = VerificationCouncil(self.cfg, self.learning.verifier_reliability)
        scorer = ScoringEngine(self.learning.weights, self.cfg.capital_cap_usd)

        published, rejected, updated_ids = [], [], set()
        for cand in candidates:
            opp, reason = self._assess(cand, council, scorer, tick)
            if opp:
                self.db.upsert_opportunity(opp.to_dict())
                updated_ids.add(opp.id)
                stored = self.db.get_opportunity(opp.id) or {}
                published.append({"id": opp.id, "title": opp.title, "score": opp.score.overall,
                                  "confidence": opp.confidence,
                                  "net_usd": opp.economics.total_net_usd,
                                  "window_days": opp.window_days,
                                  # first time this opportunity ever verified (vs a refresh)
                                  "new": stored.get("tick_created") == tick})
            else:
                rejected.append({"title": cand["title"], "type": cand["opp_type"].value, "reason": reason})

        invalidated, reverified = [], 0
        for stored in self.db.active_opportunities():
            if stored["id"] in updated_ids:
                continue
            ok, reason, fail_status = self._reverify(stored, council, scorer, tick)
            if ok:
                reverified += 1
            else:
                stored["status"] = fail_status.value
                stored["invalidation_reason"] = reason
                stored["tick_updated"] = tick
                self.db.upsert_opportunity(stored)
                invalidated.append({"id": stored["id"], "title": stored["title"], "reason": reason})

        report = {
            "tick": tick,
            "signals": n_signals,
            "agents": len(self.fleet),
            "anomalies": len(anomalies),
            "candidates": len(candidates),
            "published": published,
            "rejected": rejected,
            "reverified": reverified,
            "invalidated": invalidated,
            "discovered": getattr(self.world, "discovery_report", {}) or {},
        }
        self.db.add_cycle(tick, round((time.time() - t0) * 1000, 1), report)
        self.db.meta_set("tick", tick)
        return report

    # ----------------------------------------------------------------- assess

    def _gates(self, verification, confidence: float, econ) -> str | None:
        failed = [c for c in verification.checks if c.critical and not c.passed]
        if failed:
            return "; ".join(f"{c.name} failed — {c.evidence}" for c in failed)
        if confidence < self.cfg.min_consensus_confidence:
            return (f"consensus confidence {confidence:.2f} below the "
                    f"{self.cfg.min_consensus_confidence:.2f} publication bar")
        if econ.kind == "flip" and econ.base.margin_pct < self.cfg.min_margin_pct:
            return f"base margin {econ.base.margin_pct:.0f}% below the {self.cfg.min_margin_pct:.0f}% floor"
        if self.cfg.require_pessimistic_profit and econ.pessimistic.net_usd <= 0:
            return f"pessimistic scenario loses ${-econ.pessimistic.net_usd:.2f}"
        return None

    def _assess(self, cand: dict, council: VerificationCouncil, scorer: ScoringEngine,
                tick: int) -> tuple[Opportunity | None, str]:
        if cand["opp_type"].value in self.operator.get("avoid_types", []):
            return None, (f"excluded by your operator profile — '{cand['opp_type'].value}' "
                          f"is on your avoid list")
        verification = (council.verify_flip(self.world, cand) if cand["kind"] == "flip"
                        else council.verify_venture(self.world, cand))
        confidence = round(min(0.99, verification.consensus * self.learning.calibration), 3)
        reason = self._gates(verification, confidence, cand["economics"])
        if reason:
            return None, reason

        automation = build_automation(cand)
        playbook = build_playbook(cand)
        score = scorer.score(cand, automation.coverage_pct)
        opp = Opportunity(
            id=opportunity_id(cand["opp_type"].value, cand["entity_id"],
                              cand["buy_venue"], cand["sell_venue"]),
            type=cand["opp_type"], status=OppStatus.ACTIVE,
            category=cand["category"], title=cand["title"], subtitle=cand["subtitle"],
            entity_id=cand["entity_id"], route=cand["route"],
            tick_created=tick, tick_updated=tick,
            window_days=cand["window_days"], confidence=confidence,
            economics=cand["economics"], verification=verification, score=score,
            feasibility=cand["feasibility"], why_chain=cand["why"],
            playbook=playbook, automation=automation, sources=cand["sources"],
        )
        return opp, ""

    # --------------------------------------------------------------- reverify

    def _reverify(self, stored: dict, council: VerificationCouncil, scorer: ScoringEngine,
                  tick: int) -> tuple[bool, str, OppStatus | None]:
        """Returns (still_valid, reason, failure_status). A window that ran out
        is EXPIRED (natural end of life); a failed re-check is INVALIDATED
        (the market turned) — the dashboard filters distinguish the two."""

        # In demo mode one tick == one simulated day; in live mode ticks are
        # observation passes, so expiry runs on wall-clock age instead.
        if self.cfg.mode == "live":
            age_days = (time.time() - stored.get("created_ts", time.time())) / 86400
        else:
            age_days = tick - stored["tick_created"]
        if age_days > stored["window_days"] * 2 + 4:
            return False, "window elapsed — original edge has fully played out", OppStatus.EXPIRED

        if stored["type"] == OppType.PRODUCT_ARBITRAGE.value:
            cand = self._fresh_flip(stored)
        else:
            cand = self._fresh_venture(stored)
        if cand is None:
            return False, "source inventory exhausted or listing no longer observable", OppStatus.INVALIDATED

        verification = (council.verify_flip(self.world, cand) if cand["kind"] == "flip"
                        else council.verify_venture(self.world, cand))
        confidence = round(min(0.99, verification.consensus * self.learning.calibration), 3)
        reason = self._gates(verification, confidence, cand["economics"])
        if reason:
            return False, reason, OppStatus.INVALIDATED

        automation = build_automation(cand)
        score = scorer.score(cand, automation.coverage_pct)
        stored["economics"] = asdict(cand["economics"])
        stored["verification"] = asdict(verification)
        stored["score"] = asdict(score)
        stored["confidence"] = confidence
        stored["window_days"] = cand["window_days"]
        stored["subtitle"] = cand["subtitle"]
        stored["tick_updated"] = tick
        stored["status"] = OppStatus.ACTIVE.value
        self.db.upsert_opportunity(stored)
        return True, "", None

    def _fresh_flip(self, stored: dict) -> dict | None:
        pid = stored["entity_id"]
        bv, sv = stored["route"]["buy_venue"], stored["route"]["sell_venue"]
        buy, sell = self.world.listing(pid, bv), self.world.listing(pid, sv)
        if not buy or not sell or buy["stock"] == 0:
            return None
        product = self.world.product_public(pid)
        qty = max(1, min(stored["economics"]["qty"], buy["stock"]))
        econ = economics.compute_flip(product, bv, sv, buy["price"], sell["price"], qty=qty)
        velocity = max(sell["sold_7d"] / 7.0, 0.1)
        return {
            "kind": "flip", "opp_type": OppType.PRODUCT_ARBITRAGE, "entity_id": pid,
            "title": stored["title"],
            "subtitle": f"Buy {economics.VENUES[bv]['name']} ${buy['price']:.2f} → "
                        f"sell {economics.VENUES[sv]['name']} ${sell['price']:.2f}",
            "category": stored["category"], "item": product,
            "buy_venue": bv, "sell_venue": sv,
            "buy_usd": buy["price"], "sell_usd": sell["price"],
            "qty": qty, "buy_stock": buy["stock"], "velocity": velocity,
            "sellers": sell["sellers"],
            "window_days": round(min(14.0, max(2.0, (sell["stock"] + qty) / max(0.5, velocity))), 1),
            "economics": econ,
            "feasibility": thailand.feasibility(OppType.PRODUCT_ARBITRAGE.value, stored["category"], bv, sv),
            "route": stored["route"],
        }

    def _fresh_venture(self, stored: dict) -> dict | None:
        nid = stored["entity_id"]
        niche = next((n for n in self.world.niches() if n["id"] == nid), None)
        if not niche:
            return None
        econ = economics.compute_venture(niche)
        m = niche["metrics"]
        supply_label = "credible solutions" if niche["kind"] in ("digital", "info") else "active providers"
        supply_n = m["solution_count"] if niche["kind"] in ("digital", "info") else m["providers"]
        return {
            "kind": "venture", "opp_type": OppType(stored["type"]), "entity_id": nid,
            "title": stored["title"],
            "subtitle": f"{m['volume']:,.0f} demand events/mo, {m['growth_pct']:.0f}%/mo growth, "
                        f"{supply_n:.0f} {supply_label}",
            "category": stored["category"], "niche": niche,
            "buy_venue": None, "sell_venue": None,
            "qty": 1, "velocity": m["volume"] / 30.0, "sellers": int(supply_n),
            "window_days": stored["window_days"],
            "economics": econ,
            "feasibility": thailand.feasibility(stored["type"], stored["category"], None, None),
            "route": stored["route"],
        }


# --------------------------------------------------------------------- brief

def _key_gaps(store: Store, cfg: Config) -> list[str]:
    """Loud, specific notes about work that is BLOCKED on a missing key.
    A silent bottleneck reads as 'the app is broken'; a named one is a
    15-minute fix. Live mode only — demo needs no keys."""

    if cfg.mode != "live":
        return []
    watch = None
    try:
        from .market import watchlist as wl
        if cfg.watchlist_path.exists():
            watch = wl.load(cfg.watchlist_path)
    except Exception:  # noqa: BLE001
        pass
    discovered = store.list_discovered(active_only=True, limit=100)
    gaps: list[str] = []

    if not cfg.ebay_client_id:
        dark = sum(1 for p in (watch.products if watch else []) if "ebay_us" in p.queries)
        dark += sum(1 for d in discovered if d.get("kind") == "product")
        if dark:
            gaps.append(f"🔑 {dark} product{'s are' if dark != 1 else ' is'} WAITING on your free "
                        f"eBay key — the fleet cannot see US prices without it, so these can never "
                        f"verify. Fix: ⚙ Keys tab (≈15 min, developer.ebay.com).")
    if not cfg.reddit_client_id and not cfg.serper_api_key:
        idle = len(watch.niches) if watch else 0
        idle += sum(1 for d in discovered if d.get("kind") == "niche")
        if idle:
            gaps.append(f"🔑 {idle} niche{'s are' if idle != 1 else ' is'} WAITING on a demand "
                        f"signal — add your free Reddit key (reddit.com/prefs/apps), or the "
                        f"instant stand-in: a free Serper key (serper.dev, 2,500 searches, "
                        f"2-minute signup). Either one in ⚙ Keys.")
    return gaps


def briefing(store: Store, cfg: Config, plan_name: str | None = None) -> dict:
    plan = cfg.plan(plan_name)
    actives = store.list_opportunities(status="active", limit=200)
    tick = store.meta_get("tick", 0)
    new_today = [o for o in actives if o["tick_created"] == tick]
    cycles = store.recent_cycles(1)
    last = cycles[0]["report"] if cycles else {}
    now = datetime.now(ZoneInfo(cfg.home_timezone))
    hour = now.hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")

    closing_soon = [o for o in actives if o["window_days"] <= 3]
    expired = store.list_opportunities(status="expired", limit=200)
    limit = plan["max_opportunities"] or len(actives)
    top = actives[:limit]
    lines = [f"{greeting}. I found {len(actives)} opportunities worth your attention today"
             + (f" — {len(new_today)} new since the last cycle." if new_today else ".")]
    if closing_soon:
        lines.append(f"{len(closing_soon)} of them are closing within ~3 days — act on those first.")
    if last.get("invalidated"):
        lines.append(f"{len(last['invalidated'])} previously published "
                     f"opportunit{'y was' if len(last['invalidated']) == 1 else 'ies were'} "
                     f"invalidated as market conditions changed.")
    if last.get("rejected"):
        lines.append(f"{len(last['rejected'])} candidates were investigated and rejected before "
                     f"reaching you — they didn't survive fee/tax/verification checks.")
    disc = last.get("discovered") or {}
    if disc.get("promoted"):
        lines.append(f"Discovery added {disc['promoted']} new candidate"
                     f"{'s' if disc['promoted'] != 1 else ''} to the watch fleet"
                     + (f" ({disc['ai']})." if disc.get("ai") else "."))
    in_progress = [o for o in actives if store.get_progress(o["id"])]
    if in_progress:
        lines.append(f"{len(in_progress)} deal{'s' if len(in_progress) != 1 else ''} in progress — "
                     f"when one finishes, record the outcome so the scoring learns from YOUR results.")
    lines += _key_gaps(store, cfg)

    return {
        "generated_at": now.isoformat(),
        "timezone": cfg.home_timezone,
        "tick": tick,
        "plan": plan["label"],
        "headline": lines[0],
        "notes": lines[1:],
        "counts": {"active": len(actives), "new_today": len(new_today),
                   "closing_soon": len(closing_soon), "expired_total": len(expired),
                   "reverified_last_cycle": last.get("reverified", 0),
                   "invalidated_last_cycle": len(last.get("invalidated", [])),
                   "rejected_last_cycle": len(last.get("rejected", []))},
        "top": [{"id": o["id"], "title": o["title"], "subtitle": o["subtitle"],
                 "score": o["score"]["overall"], "confidence": o["confidence"],
                 "net_usd": o["economics"]["total_net_usd"],
                 "window_days": o["window_days"], "type": o["type"]} for o in top],
        "locked": max(0, len(actives) - limit) if plan["max_opportunities"] else 0,
    }
