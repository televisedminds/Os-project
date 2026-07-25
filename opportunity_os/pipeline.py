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
from . import economics, evidence, executability, research, thailand


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
        from .graph import KnowledgeGraph
        self.graph = KnowledgeGraph(store)         # shared product/opportunity memory
        from .ai import AIClassifier
        self.ai = AIClassifier(config)             # advisory risk desk (needs key)

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
        # A DB with no mode stamp that already holds opportunities is almost
        # certainly a pre-guard *demo* database. Refuse to run it as live —
        # otherwise its simulated opportunities would show under a LIVE badge.
        if not stored and self.cfg.mode == "live":
            try:
                n = len(self.db.active_opportunities())
            except Exception:  # noqa: BLE001
                n = 0
            if n:
                raise SystemExit(
                    f"OOS_MODE=live but {self.cfg.db_path} already holds {n} opportunities and has no "
                    f"mode stamp — it looks like a demo database. Simulated opportunities must not show "
                    f"under a LIVE badge. Point OOS_DB at a fresh file (e.g. data/live.db) so live mode "
                    f"starts clean.")
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
        candidates = self.investigator.build_candidates(
            self.world, anomalies, store=self.db, graph=self.graph)

        council = VerificationCouncil(self.cfg, self.learning.verifier_reliability)
        scorer = ScoringEngine(self.learning.weights, self.cfg.capital_cap_usd)

        published, rejected, updated_ids = [], [], set()
        published_new: list[dict] = []
        for cand in candidates:
            opp, reason = self._assess(cand, council, scorer, tick)
            is_venture = cand.get("kind") == "venture"
            if opp:
                self.db.upsert_opportunity(opp.to_dict())
                updated_ids.add(opp.id)
                stored = self.db.get_opportunity(opp.id) or {}
                is_new = stored.get("tick_created") == tick
                published.append({"id": opp.id, "title": opp.title, "score": opp.score.overall,
                                  "confidence": opp.confidence,
                                  "net_usd": opp.economics.total_net_usd,
                                  "window_days": opp.window_days,
                                  # first time this opportunity ever verified (vs a refresh)
                                  "new": is_new})
                if is_new:
                    published_new.append({
                        "entity_id": opp.entity_id, "title": opp.title,
                        "is_product": opp.type in (OppType.PRODUCT_ARBITRAGE, OppType.REFURBISHMENT)})
                if is_venture:
                    self._record_venture_verdict(cand, tick, "verified", None)
            else:
                route = cand.get("route", {}) or {}
                category = evidence.classify_rejection(reason)
                rejected.append({"title": cand["title"],
                                 "type": route.get("kind") or cand["opp_type"].value,
                                 "reason": reason,
                                 "category": category,
                                 # Milestone 2: how this candidate entered evaluation
                                 "entry_path": cand.get("entry_path")})
                if is_venture:
                    self._record_venture_verdict(cand, tick, "rejected", category)

        self._ai_risk_pass(published)
        self._record_yield(tick, anomalies, candidates, published_new)
        self._spawn_hypotheses(tick, published_new)

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
                if fail_status == OppStatus.INVALIDATED:
                    stored["verification_level"] = evidence.VerificationLevel.INVALIDATED.value
                self.db.upsert_opportunity(stored)
                invalidated.append({"id": stored["id"], "title": stored["title"], "reason": reason,
                                    "category": evidence.classify_rejection(reason)})

        from collections import Counter
        report = {
            "tick": tick,
            "signals": n_signals,
            "agents": len(self.fleet),
            "anomalies": len(anomalies),
            "candidates": len(candidates),
            # Per-type candidate counts — the observability layer's raw
            # material for "which generator produced work this cycle".
            "candidates_by_type": dict(Counter(c["opp_type"].value for c in candidates)),
            "published": published,
            "rejected": rejected,
            "reverified": reverified,
            "invalidated": invalidated,
            "discovered": getattr(self.world, "discovery_report", {}) or {},
            # Milestone 2: venture entry-path funnel for THIS cycle (anomaly vs
            # steady-state entrants, dedup, verdicts) — read back from the ledger.
            "venture_entry": self._venture_entry_summary(),
        }
        self.db.add_cycle(tick, round((time.time() - t0) * 1000, 1), report)
        self.db.meta_set("tick", tick)
        return report

    def _record_venture_verdict(self, cand: dict, tick: int,
                                verdict: str, category: str | None) -> None:
        """Fill in the council verdict on this cycle's venture-eval ledger row."""
        if not hasattr(self.db, "set_venture_verdict"):
            return
        try:
            self.db.set_venture_verdict(cand.get("entity_id", ""), tick, verdict, category)
        except Exception:  # noqa: BLE001 - telemetry never blocks a cycle
            pass

    def _venture_entry_summary(self) -> dict:
        if not hasattr(self.db, "venture_eval_summary"):
            return {}
        try:
            return self.db.venture_eval_summary(1)   # just this cycle's tick
        except Exception:  # noqa: BLE001
            return {}

    def _record_yield(self, tick: int, anomalies: list, candidates: list[dict],
                      published_new: list[dict]) -> None:
        """Feed the EV allocator: every scan, anomaly, candidate and publication
        is credited to its entity, so scan budget flows toward what produces.
        And propagate — a publication raises the priority of its graph
        neighbors, so finding one edge pulls the fleet toward adjacent ones."""

        if not hasattr(self.db, "yield_bump"):
            return
        try:
            from collections import Counter
            for pid in getattr(self.world, "scanned_this_tick", set()) or set():
                self.db.yield_bump(pid, tick, scans=1)
            for eid, c in Counter(a.entity_id for a in anomalies).items():
                self.db.yield_bump(eid, tick, anomalies=c)
            for eid, c in Counter(c["entity_id"] for c in candidates).items():
                self.db.yield_bump(eid, tick, candidates=c)
            for p in published_new:
                self.db.yield_bump(p["entity_id"], tick, published=1)
                neighbors = [n["dst"] for n in self.db.graph_neighbors(p["entity_id"], 8)]
                if neighbors:
                    self.db.bump_discovered_score(neighbors, 0.25)
        except Exception:  # noqa: BLE001 - telemetry must never break a cycle
            pass

    def _spawn_hypotheses(self, tick: int, published_new: list[dict]) -> None:
        """Phase 5 in action: a verified opportunity marks its product
        profitable in the knowledge graph, seeds the graph with the related
        products its own listings are co-listed with, then a BOUNDED traversal
        turns that one win into related hypotheses (variants, accessories,
        adjacent products) — promoted into discovery so the fleet investigates
        the cluster next, not one isolated listing. Never fans out unboundedly;
        never a network call here (it reads the sample already captured)."""

        try:
            from dataclasses import asdict
            from . import research
            from .graph import N_PRODUCT, E_CO_LISTED
            from .discovery import Candidate, clean_query, infer_category, slug
            for p in published_new:
                if not p.get("is_product"):
                    continue
                pid = p["entity_id"]
                self.graph.mark_outcome(pid, p["title"], profitable=True, tick=tick)
                # Seed co-listing edges from this product's own captured page —
                # the sellers already wrote the adjacency into their titles.
                if hasattr(self.world, "listing_sample"):
                    product = self.world.product_public(pid)
                    for venue in product.get("venues", []):
                        sample = self.world.listing_sample(pid, venue)
                        if not sample:
                            continue
                        for g in research.mine_related(sample, product["name"]):
                            nid = slug(g["phrase"], "disc_p")
                            self.graph.link(pid, nid, E_CO_LISTED, src_type=N_PRODUCT,
                                            dst_type=N_PRODUCT, src_name=product["name"],
                                            dst_name=g["phrase"].title(), weight=g["support"], tick=tick)
                        break                        # one venue's page is enough to seed
                hyps = self.graph.related_hypotheses(pid, budget=6, max_hops=2)
                for h in hyps:
                    if h.ntype not in ("product", "model", "accessory", "part", "unknown"):
                        continue
                    cid = h.node_id if h.node_id.startswith("disc_") else f"disc_p_{h.node_id}"
                    cand = Candidate(
                        kind="product", id=cid, name=h.name[:70], source="graph_expansion",
                        score=round(min(1.8, 0.4 + h.ev_score / 3), 3),
                        category=infer_category(h.name),
                        reason=f"{h.relation.replace('_', ' ')} of a verified opportunity "
                               f"('{p['title'][:40]}') — related hypothesis worth checking.",
                        queries={"ebay_us": clean_query(h.name)}, reddit_query=clean_query(h.name))
                    if hasattr(self.db, "upsert_discovered"):
                        self.db.upsert_discovered(asdict(cand))
        except Exception:  # noqa: BLE001 - enrichment must never break a cycle
            pass

    def _ai_risk_pass(self, published: list[dict]) -> None:
        """Advisory AI risk review for FIRST-TIME publications: names the edge
        (why the mispricing exists) and the concrete risks, appended to the
        opportunity's investigation timeline. Never blocks, never crashes."""

        new_ids = [p["id"] for p in published if p.get("new")]
        if not new_ids or not self.ai.configured():
            return
        try:
            opps = [o for oid in new_ids if (o := self.db.get_opportunity(oid))]
            reviews = self.ai.risk_review(opps)
            labels = {"proceed": "PROCEED", "proceed_with_caution": "CAUTION",
                      "high_risk": "HIGH RISK"}
            for o in opps:
                r = reviews.get(o["id"])
                if not r:
                    continue
                missing = r.get("missing_evidence", []) or []
                o["why_chain"].append({
                    "question": "AI risk review — why does this edge exist, and what could go wrong?",
                    "finding": f"[{labels.get(r['verdict'], r['verdict'])}] {r['edge']} "
                               f"Risks: {' · '.join(r['risks'])}"
                               + (f" · Missing: {', '.join(missing)}" if missing else ""),
                    "data": {"verdict": r["verdict"]},
                })
                # The AI's judgement is recorded as evidence of KIND
                # ai_interpretation — explicitly NOT a source of fact, and its
                # named gaps are surfaced so the operator sees what's unproven.
                o.setdefault("evidence", []).append({
                    "field": "ai_risk_review", "value": f"{r['verdict']}: {r['edge']}",
                    "kind": evidence.AI_INTERPRETATION, "source": "claude",
                    "independent": False, "ref": "", "ts": 0.0, "freshness": "n/a",
                    "is_sold_comp": False})
                for gap in missing:
                    o["evidence"].append({
                        "field": "missing_evidence", "value": gap, "kind": evidence.UNKNOWN,
                        "source": "claude", "independent": False, "ref": "", "ts": 0.0,
                        "freshness": "n/a", "is_sold_comp": False})
                self.db.upsert_opportunity(o)
        except Exception:  # noqa: BLE001 - advisory only
            pass

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
        # Evidence ledger + the verification level it earns (Phase 7/9): the
        # grade reflects the QUALITY of evidence, not just that it passed —
        # single-source, asking-price-only work is honestly capped.
        ledger = evidence.build_ledger(cand, verification, self.cfg.mode, self._latest_ts(cand))
        exec_report = executability.assess(self._exec_input(cand))
        level = evidence.compute_level(ledger, verification, cand["feasibility"],
                                       passed_gates=True, executable=exec_report["execution_ready"])
        single = evidence.is_single_source(ledger)
        # A dislocation is identified by its specific listing, so two under-
        # priced listings of the same product get distinct, stable identities.
        buy_key = cand["buy_venue"]
        if cand.get("dislocation"):
            buy_key = f"{cand['buy_venue']}:{cand['dislocation']['item_id']}"
        opp = Opportunity(
            id=opportunity_id(cand["opp_type"].value, cand["entity_id"],
                              buy_key, cand["sell_venue"]),
            type=cand["opp_type"], status=OppStatus.ACTIVE,
            category=cand["category"], title=cand["title"], subtitle=cand["subtitle"],
            entity_id=cand["entity_id"], route=cand["route"],
            tick_created=tick, tick_updated=tick,
            window_days=cand["window_days"], confidence=confidence,
            economics=cand["economics"], verification=verification, score=score,
            feasibility=cand["feasibility"], why_chain=cand["why"],
            playbook=playbook, automation=automation, sources=cand["sources"],
            evidence=ledger, verification_level=level.value, single_source=single,
            executability=exec_report,
        )
        return opp, ""

    def _exec_input(self, cand: dict) -> dict:
        """Normalize a candidate into the plain dict the executability engine
        reads (its economics/feasibility may be dataclasses at this stage)."""

        return {
            "type": cand["opp_type"].value, "kind": cand["kind"],
            "category": cand["category"], "route": cand.get("route", {}),
            "economics": asdict(cand["economics"]) if not isinstance(cand["economics"], dict)
            else cand["economics"],
            "feasibility": asdict(cand["feasibility"]) if not isinstance(cand["feasibility"], dict)
            else cand["feasibility"],
        }

    def _latest_ts(self, cand: dict) -> float:
        """Timestamp of the freshest observation behind a candidate (live mode);
        0 in demo, where freshness is a deterministic replay."""

        if self.cfg.mode != "live":
            return 0.0
        try:
            pid = cand["entity_id"]
            venue = cand.get("sell_venue") or cand.get("buy_venue")
            rows = self.db.live_snapshot_series(pid, venue, 1) if venue else []
            return rows[-1]["ts"] if rows else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    # --------------------------------------------------------------- reverify

    def _revalidate_economics(self, stored: dict, cand: dict) -> None:
        """Swap a candidate's ESTIMATED venture economics for economics re-priced
        from recorded cash, when such cash exists (Backlog #22). Mutates `cand` in
        place so the council, gates and scorer all see the observed numbers. A
        no-op without evidence — it never invents an observation."""

        from . import validation as val
        try:
            if cand.get("kind") == "flip" or not val.needs_revalidation(stored):
                return
            observed = val.observed_monthly_revenue(self.db, stored["id"])
            if not observed:
                return
            result = val.revalidate(stored, observed)
            if not result:
                return
            new_econ, audit = result
            from .models import Economics, Scenario, CostLine

            def _scn(d: dict) -> Scenario:
                s = Scenario(name=d["name"], revenue_usd=d["revenue_usd"],
                             lines=[CostLine(**l) for l in d["lines"]])
                return s.finalize()
            econ = Economics(**{**new_econ,
                                "base": _scn(new_econ["base"]),
                                "pessimistic": _scn(new_econ["pessimistic"])})
            cand["economics"] = econ
            self.db.add_economics_audit(audit)
        except Exception:  # noqa: BLE001 — never let revalidation break a cycle
            return

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

        route_kind = stored.get("route", {}).get("kind")
        # A seasonal opportunity has a HARD dated expiry: once the event passes,
        # the premium is gone regardless of any live price re-check.
        if stored["type"] == OppType.SEASONAL.value or route_kind == "seasonal":
            exp = stored.get("route", {}).get("expiry_date", "")
            if exp:
                from datetime import date as _date
                try:
                    if _date.today() > _date.fromisoformat(exp):
                        return False, f"seasonal event passed ({exp}) — premium gone", OppStatus.EXPIRED
                except ValueError:
                    pass

        if stored["type"] == OppType.PRODUCT_ARBITRAGE.value and route_kind == "dislocation":
            cand = self._fresh_dislocation(stored)
        elif stored["type"] == OppType.REFURBISHMENT.value or route_kind == "refurbish":
            cand = self._fresh_dislocation(stored)
        elif stored["type"] == OppType.WHOLESALE.value or route_kind == "wholesale":
            cand = self._fresh_wholesale(stored)
        elif stored["type"] == OppType.LEAD_GENERATION.value or route_kind == "lead_generation":
            cand = self._fresh_leadgen(stored)
        elif stored["type"] == OppType.SEASONAL.value or route_kind == "seasonal":
            cand = self._fresh_seasonal(stored)
        elif stored["type"] in (OppType.PRODUCT_ARBITRAGE.value, OppType.IMPORT_EXPORT.value):
            cand = self._fresh_flip(stored)
        else:
            cand = self._fresh_venture(stored)
        if cand is None:
            return False, "source inventory exhausted or listing no longer observable", OppStatus.INVALIDATED

        # Backlog #22 — observed-economics validation. A venture priced on an
        # ESTIMATED demand input is re-priced from recorded cash the moment such
        # cash exists, so the council judges real numbers instead of leaving it
        # validation_required forever. Re-checked every cycle so it self-heals even
        # if the outcome was recorded while this opportunity wasn't being re-verified.
        self._revalidate_economics(stored, cand)

        verification = (council.verify_flip(self.world, cand) if cand["kind"] == "flip"
                        else council.verify_venture(self.world, cand))
        confidence = round(min(0.99, verification.consensus * self.learning.calibration), 3)
        reason = self._gates(verification, confidence, cand["economics"])
        if reason:
            return False, reason, OppStatus.INVALIDATED

        automation = build_automation(cand)
        score = scorer.score(cand, automation.coverage_pct)
        ledger = evidence.build_ledger(cand, verification, self.cfg.mode, self._latest_ts(cand))
        exec_report = executability.assess(self._exec_input(cand))
        stored["economics"] = asdict(cand["economics"])
        stored["verification"] = asdict(verification)
        stored["score"] = asdict(score)
        stored["confidence"] = confidence
        stored["window_days"] = cand["window_days"]
        stored["subtitle"] = cand["subtitle"]
        stored["tick_updated"] = tick
        stored["status"] = OppStatus.ACTIVE.value
        stored["evidence"] = ledger
        stored["executability"] = exec_report
        stored["verification_level"] = evidence.compute_level(
            ledger, verification, cand["feasibility"], passed_gates=True,
            executable=exec_report["execution_ready"]).value
        stored["single_source"] = evidence.is_single_source(ledger)
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
        # Preserve the stored type (a product_arbitrage flip and an import_export
        # route both re-verify through here; the type must not silently change).
        opp_type = OppType(stored["type"]) if stored["type"] in (
            OppType.PRODUCT_ARBITRAGE.value, OppType.IMPORT_EXPORT.value) else OppType.PRODUCT_ARBITRAGE
        return {
            "kind": "flip", "opp_type": opp_type, "entity_id": pid,
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

    def _fresh_dislocation(self, stored: dict) -> dict | None:
        pid = stored["entity_id"]
        venue = stored["route"]["buy_venue"]
        item_id = stored["route"].get("item_id", "")
        if not hasattr(self.world, "listing_sample"):
            return None
        sample = self.world.listing_sample(pid, venue)
        row = next((s for s in sample if s.get("item_id") == item_id), None)
        if not row:
            return None                    # the underpriced listing is gone — edge taken
        refurbish = stored.get("route", {}).get("kind") == "refurbish"
        # For refurb, fair value comes from WORKING comps only (this row is a
        # parts unit and must not drag the comp down).
        if refurbish:
            from .generators_extra import _is_parts
            working = [s for s in sample if not _is_parts(s.get("title", "")) and float(s.get("price", 0)) > 0]
            stats = research.market_stats(working or sample)
        else:
            stats = research.market_stats(sample)
        product = self.world.product_public(pid)
        ask = float(row["price"])
        fair = stats["fair_usd"] or float(stored["route"].get("fair_usd", ask * 1.4))
        repair = float(stored.get("route", {}).get("repair_cost", 0.0)) if refurbish else 0.0
        econ = economics.compute_flip(product, venue, venue, ask, fair, qty=1,
                                      extra_cost_usd=repair,
                                      extra_note="refurbishment: parts + labour" if repair else "")
        agg = self.world.listing(pid, venue) or {}
        velocity = max(agg.get("sold_7d", 0) / 7.0, 0.2)
        vname = economics.VENUES[venue]["name"]
        if refurbish:
            subtitle = f"Buy a for-parts unit ${ask:.2f} on {vname}, repair (~${repair:.0f}), resell ${fair:.2f}"
            opp_type = OppType.REFURBISHMENT
        else:
            edge = (1 - ask / fair) * 100 if fair else 0
            subtitle = f"Buy one {vname} listing ${ask:.2f} → resell ${fair:.2f} ({edge:.0f}% under fair)"
            opp_type = OppType.PRODUCT_ARBITRAGE
        return {
            "kind": "flip", "opp_type": opp_type, "entity_id": pid,
            "title": stored["title"], "subtitle": subtitle,
            "category": stored["category"], "item": product,
            "buy_venue": venue, "sell_venue": venue, "buy_usd": ask, "sell_usd": fair,
            "qty": 1, "buy_stock": 1, "velocity": velocity,
            "sellers": stats["sellers"],
            "window_days": round(min(21.0 if refurbish else 10.0, max(2.0, stats["n"] / max(0.5, velocity))), 1),
            "economics": econ,
            "feasibility": thailand.feasibility(OppType.PRODUCT_ARBITRAGE.value,
                                                stored["category"], venue, venue),
            "dislocation": {"item_id": item_id, "url": stored["route"].get("buy_url", ""),
                            "ask_usd": ask, "fair_usd": fair,
                            "min_edge": self.cfg.dislocation_min_edge,
                            "refurbish": refurbish, "repair_cost": repair},
            "route": stored["route"],
        }

    def _fresh_wholesale(self, stored: dict) -> dict | None:
        pid = stored["entity_id"]
        venue = stored["route"]["buy_venue"]
        item_id = stored["route"].get("item_id", "")
        if not hasattr(self.world, "listing_sample"):
            return None
        sample = self.world.listing_sample(pid, venue)
        row = next((s for s in sample if s.get("item_id") == item_id), None)
        if not row:
            return None                    # the lot is gone — edge taken or pulled
        n = research.parse_lot_size(row.get("title", "")) or int(stored["route"].get("lot_size", 1))
        per_unit = float(row["price"]) / max(1, n)
        singles = [s for s in sample
                   if research.parse_lot_size(s.get("title", "")) is None
                   and float(s.get("price", 0)) > 0
                   and not research.looks_junk(s.get("title", ""), s.get("condition", ""))]
        stats = research.market_stats(singles)
        product = self.world.product_public(pid)
        fair = stats["fair_usd"] or float(stored.get("wholesale", {}).get("single_fair_usd", per_unit * 1.4))
        econ = economics.compute_flip(product, venue, venue, per_unit, fair, qty=n)
        agg = self.world.listing(pid, venue) or {}
        velocity = max(agg.get("sold_7d", 0) / 7.0, 0.2)
        vname = economics.VENUES[venue]["name"]
        return {
            "kind": "flip", "opp_type": OppType.WHOLESALE, "entity_id": pid,
            "title": stored["title"],
            "subtitle": f"Lot of {n} @ ${per_unit:.2f}/unit on {vname} → resell singles ${fair:.2f}",
            "category": stored["category"], "item": product,
            "buy_venue": venue, "sell_venue": venue, "buy_usd": per_unit, "sell_usd": fair,
            "qty": n, "buy_stock": n, "velocity": velocity, "sellers": stats["sellers"],
            "window_days": round(min(30.0, max(5.0, n / max(0.5, velocity))), 1),
            "economics": econ,
            "feasibility": thailand.feasibility("wholesale", stored["category"], venue, venue),
            "wholesale": {"item_id": item_id, "url": stored["route"].get("buy_url", ""),
                          "lot_size": n, "per_unit_usd": per_unit, "single_fair_usd": fair,
                          "min_edge": stored.get("wholesale", {}).get("min_edge", 0.25)},
            "route": stored["route"],
        }

    def _fresh_seasonal(self, stored: dict) -> dict | None:
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
            "kind": "flip", "opp_type": OppType.SEASONAL, "entity_id": pid,
            "title": stored["title"], "subtitle": stored["subtitle"],
            "category": stored["category"], "item": product,
            "buy_venue": bv, "sell_venue": sv, "buy_usd": buy["price"], "sell_usd": sell["price"],
            "qty": qty, "buy_stock": buy["stock"], "velocity": velocity, "sellers": sell["sellers"],
            "window_days": stored["window_days"],
            "economics": econ,
            "feasibility": thailand.feasibility("seasonal", stored["category"], bv, sv),
            "route": stored["route"],
        }

    def _fresh_leadgen(self, stored: dict) -> dict | None:
        nid = stored["entity_id"]
        niche = next((n for n in self.world.niches() if n["id"] == nid), None)
        if not niche:
            return None
        per_lead = float(stored.get("route", {}).get("per_lead_usd", 0)) or 2.0
        volume = niche["metrics"].get("volume", 0)
        econ = economics.compute_venture({"kind": "leadgen", "price_point_usd": per_lead,
                                          "metrics": {"volume": volume}})
        providers = int(niche["metrics"].get("providers", niche["metrics"].get("solution_count", 0)))
        return {
            "kind": "venture", "opp_type": OppType.LEAD_GENERATION, "entity_id": nid,
            "title": stored["title"], "subtitle": stored["subtitle"],
            "category": "lead_generation", "niche": niche,
            "buy_venue": None, "sell_venue": None,
            "qty": 1, "velocity": volume / 30.0, "sellers": providers,
            "window_days": stored["window_days"], "economics": econ,
            "feasibility": thailand.feasibility("lead_generation", "b2b_services", None, None),
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


def _operator_profile(store: Store, cfg: Config) -> dict:
    """The operator's execution preferences (risk tolerance, caps, reserve),
    read from the watchlist if present."""

    try:
        from dataclasses import asdict
        from .market import watchlist as wl
        if cfg.watchlist_path.exists():
            return asdict(wl.load(cfg.watchlist_path).operator)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _operator_capital(store: Store, cfg: Config) -> float:
    """The operator's deployable capital: their watchlist budget if set,
    otherwise the configured capital cap."""

    op = _operator_profile(store, cfg)
    for key in ("capital_usd", "budget_usd"):
        if op.get(key):
            return float(op[key])
    return float(cfg.capital_cap_usd)


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

    # Capital allocation: don't just rank opportunities — solve for the best
    # use of finite capital, deduplicated by thesis, respecting the operator's
    # budget, risk tolerance, per-opportunity cap and liquidity reserve.
    capital = float(_operator_capital(store, cfg))
    op = _operator_profile(store, cfg)
    capital_plan = research.allocate_capital(
        actives, capital,
        risk_tolerance=op.get("risk_tolerance", "balanced"),
        max_per_opportunity=op.get("max_per_opportunity") or None,
        liquidity_reserve_pct=float(op.get("liquidity_reserve_pct", 0.10)),
        max_holding_days=op.get("max_holding_days") or None)

    return {
        "generated_at": now.isoformat(),
        "timezone": cfg.home_timezone,
        "tick": tick,
        "plan": plan["label"],
        "headline": lines[0],
        "notes": lines[1:],
        "capital_plan": capital_plan,
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
