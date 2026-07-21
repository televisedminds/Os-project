"""SQLite persistence. One file, no server, safe for a single-process app
(API thread + background cycle task share a lock)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tick INTEGER, agent TEXT, source TEXT,
    kind TEXT, entity_id TEXT, venue TEXT, strength REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tick INTEGER, kind TEXT, entity_id TEXT,
    severity REAL, summary TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY, created_ts REAL, updated_ts REAL, tick_created INTEGER, tick_updated INTEGER,
    type TEXT, status TEXT, category TEXT, title TEXT, score REAL, confidence REAL,
    net_usd REAL, margin_pct REAL, window_days REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, opportunity_id TEXT, result TEXT,
    realized_profit_usd REAL, days_taken REAL, failure_reason TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS learning (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS agent_runs (
    agent TEXT PRIMARY KEY, name TEXT, source TEXT, description TEXT,
    last_ts REAL, last_tick INTEGER, signals_last INTEGER, signals_total INTEGER);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, tick INTEGER, duration_ms REAL, report TEXT);
CREATE INDEX IF NOT EXISTS idx_signals_tick ON signals(tick);
CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunities(status);
CREATE TABLE IF NOT EXISTS live_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT, venue TEXT, tick INTEGER, ts REAL,
    price REAL, stock INTEGER, sellers INTEGER, sold_7d INTEGER, extra TEXT);
CREATE TABLE IF NOT EXISTS live_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT, source TEXT, tick INTEGER, ts REAL, count INTEGER);
CREATE TABLE IF NOT EXISTS live_niches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, niche_id TEXT, tick INTEGER, ts REAL, metrics TEXT);
CREATE TABLE IF NOT EXISTS live_headlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT, tick INTEGER, ts REAL, text TEXT, etype TEXT);
CREATE INDEX IF NOT EXISTS idx_live_snap ON live_snapshots(entity_id, venue, tick);
CREATE INDEX IF NOT EXISTS idx_live_mention ON live_mentions(entity_id, source, tick);
CREATE TABLE IF NOT EXISTS mission_progress (
    opportunity_id TEXT, step_order INTEGER, done INTEGER, ts REAL,
    PRIMARY KEY (opportunity_id, step_order));
CREATE TABLE IF NOT EXISTS discovered (
    id TEXT PRIMARY KEY, kind TEXT, name TEXT, source TEXT, score REAL,
    status TEXT, first_ts REAL, last_ts REAL, payload TEXT);
CREATE INDEX IF NOT EXISTS idx_discovered_status ON discovered(status, score);
CREATE TABLE IF NOT EXISTS kits (
    opportunity_id TEXT PRIMARY KEY, ts REAL, model TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS listing_samples (
    entity_id TEXT, venue TEXT, tick INTEGER, ts REAL, payload TEXT,
    PRIMARY KEY (entity_id, venue));
CREATE TABLE IF NOT EXISTS research_yield (
    entity_id TEXT PRIMARY KEY, scans INTEGER DEFAULT 0, anomalies INTEGER DEFAULT 0,
    candidates INTEGER DEFAULT 0, published INTEGER DEFAULT 0, prior REAL DEFAULT 0,
    updated_tick INTEGER DEFAULT 0, ts REAL);
CREATE TABLE IF NOT EXISTS graph_edges (
    src TEXT, dst TEXT, kind TEXT, weight REAL DEFAULT 1, tick INTEGER, ts REAL,
    PRIMARY KEY (src, dst, kind));
CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id TEXT PRIMARY KEY, ntype TEXT, name TEXT, attrs TEXT,
    first_ts REAL, last_ts REAL);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(ntype);
-- Per-opportunity execution chat (Phase 13). Everything is keyed by
-- opportunity_id so one opportunity's workspace can never contaminate another.
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opportunity_id TEXT, ts REAL,
    role TEXT, content TEXT, evidence TEXT, tool TEXT);
CREATE INDEX IF NOT EXISTS idx_chat_msg ON chat_messages(opportunity_id, ts);
CREATE TABLE IF NOT EXISTS chat_state (
    opportunity_id TEXT PRIMARY KEY, state TEXT, next_action TEXT,
    updated_ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS chat_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opportunity_id TEXT, ts REAL,
    kind TEXT, amount_usd REAL, qty INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS idx_chat_ledger ON chat_ledger(opportunity_id, ts);
CREATE TABLE IF NOT EXISTS chat_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opportunity_id TEXT, ts REAL,
    url TEXT, alive INTEGER, price_usd REAL, note TEXT);
CREATE TABLE IF NOT EXISTS chat_checklist (
    opportunity_id TEXT, step TEXT, done INTEGER, ts REAL,
    PRIMARY KEY (opportunity_id, step));
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ meta

    def meta_get(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def meta_set(self, key: str, value) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value)))

    # --------------------------------------------------------------- signals

    def add_signals(self, signals: list, tick: int) -> None:
        now = time.time()
        rows = [(now, tick, s.agent, s.source, s.kind, s.entity_id, s.venue, s.strength,
                 json.dumps(s.payload, default=str)) for s in signals]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO signals(ts,tick,agent,source,kind,entity_id,venue,strength,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?)", rows)
            self._conn.execute("DELETE FROM signals WHERE id NOT IN "
                               "(SELECT id FROM signals ORDER BY id DESC LIMIT 5000)")

    def recent_signals(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def add_anomalies(self, anomalies: list) -> None:
        now = time.time()
        rows = [(now, a.tick, a.kind.value, a.entity_id, a.severity, a.summary,
                 json.dumps(a.evidence, default=str)) for a in anomalies]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO anomalies(ts,tick,kind,entity_id,severity,summary,evidence) "
                "VALUES(?,?,?,?,?,?,?)", rows)
            self._conn.execute("DELETE FROM anomalies WHERE id NOT IN "
                               "(SELECT id FROM anomalies ORDER BY id DESC LIMIT 1000)")

    def recent_anomalies(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM anomalies ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r, evidence=json.loads(r["evidence"])) for r in rows]

    # --------------------------------------------------------- opportunities

    def upsert_opportunity(self, opp_dict: dict) -> None:
        now = time.time()
        with self._lock, self._conn:
            existing = self._conn.execute("SELECT created_ts, tick_created FROM opportunities WHERE id=?",
                                          (opp_dict["id"],)).fetchone()
            created_ts = existing["created_ts"] if existing else now
            if existing:
                opp_dict["tick_created"] = existing["tick_created"]
            self._conn.execute(
                "INSERT INTO opportunities(id,created_ts,updated_ts,tick_created,tick_updated,type,status,"
                "category,title,score,confidence,net_usd,margin_pct,window_days,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET updated_ts=excluded.updated_ts, "
                "tick_updated=excluded.tick_updated, status=excluded.status, score=excluded.score, "
                "confidence=excluded.confidence, net_usd=excluded.net_usd, margin_pct=excluded.margin_pct, "
                "window_days=excluded.window_days, payload=excluded.payload",
                (opp_dict["id"], created_ts, now, opp_dict["tick_created"], opp_dict["tick_updated"],
                 opp_dict["type"], opp_dict["status"], opp_dict["category"], opp_dict["title"],
                 opp_dict["score"]["overall"], opp_dict["confidence"],
                 opp_dict["economics"]["total_net_usd"], opp_dict["economics"]["base"]["margin_pct"],
                 opp_dict["window_days"], json.dumps(opp_dict, default=str)))

    def get_opportunity(self, opp_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload, created_ts, updated_ts FROM opportunities WHERE id=?",
                                     (opp_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["created_ts"], payload["updated_ts"] = row["created_ts"], row["updated_ts"]
        return payload

    def list_opportunities(self, status: str | None = None, category: str | None = None,
                           min_score: float = 0.0, q: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT payload, created_ts, updated_ts FROM opportunities WHERE score>=?", [min_score]
        if status:
            sql += " AND status=?"; args.append(status)
        if category:
            sql += " AND category=?"; args.append(category)
        if q:
            sql += " AND title LIKE ?"; args.append(f"%{q}%")
        sql += " ORDER BY status='active' DESC, score DESC LIMIT ?"; args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            p["created_ts"], p["updated_ts"] = r["created_ts"], r["updated_ts"]
            out.append(p)
        return out

    def active_opportunities(self) -> list[dict]:
        return self.list_opportunities(status="active", limit=500)

    # ---------------------------------------------------------------- others

    def add_outcome(self, opportunity_id: str, result: str, realized_profit_usd: float | None,
                    days_taken: float | None, failure_reason: str | None, notes: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO outcomes(ts,opportunity_id,result,realized_profit_usd,days_taken,"
                "failure_reason,notes) VALUES(?,?,?,?,?,?,?)",
                (time.time(), opportunity_id, result, realized_profit_usd, days_taken, failure_reason, notes))

    def recent_outcomes(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM outcomes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def learning_get(self):
        return self.meta_learning_get()

    def meta_learning_get(self):
        with self._lock:
            row = self._conn.execute("SELECT value FROM learning WHERE key='state'").fetchone()
        return json.loads(row["value"]) if row else None

    def learning_set(self, state: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO learning(key,value) VALUES('state',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (json.dumps(state, default=str),))

    def agent_run(self, agent, tick: int, n_signals: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO agent_runs(agent,name,source,description,last_ts,last_tick,signals_last,signals_total) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(agent) DO UPDATE SET last_ts=excluded.last_ts, "
                "last_tick=excluded.last_tick, signals_last=excluded.signals_last, "
                "signals_total=signals_total+excluded.signals_last",
                (agent.id, agent.name, agent.source, agent.description, time.time(), tick, n_signals, n_signals))

    def list_agents(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM agent_runs ORDER BY agent").fetchall()
        return [dict(r) for r in rows]

    def add_cycle(self, tick: int, duration_ms: float, report: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO cycles(ts,tick,duration_ms,report) VALUES(?,?,?,?)",
                               (time.time(), tick, duration_ms, json.dumps(report, default=str)))

    def recent_cycles(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM cycles ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r, report=json.loads(r["report"])) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            def one(sql, *args):
                return self._conn.execute(sql, args).fetchone()[0]
            return {
                "opportunities_active": one("SELECT COUNT(*) FROM opportunities WHERE status='active'"),
                "opportunities_total": one("SELECT COUNT(*) FROM opportunities"),
                "invalidated": one("SELECT COUNT(*) FROM opportunities WHERE status='invalidated'"),
                "signals_total": one("SELECT COUNT(*) FROM signals"),
                "anomalies_total": one("SELECT COUNT(*) FROM anomalies"),
                "outcomes": one("SELECT COUNT(*) FROM outcomes"),
                "avg_confidence": one("SELECT COALESCE(AVG(confidence),0) FROM opportunities WHERE status='active'"),
                "profit_pool_usd": one("SELECT COALESCE(SUM(net_usd),0) FROM opportunities WHERE status='active'"),
            }

    # ------------------------------------------------------ mission progress

    def set_progress(self, opportunity_id: str, step_order: int, done: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO mission_progress(opportunity_id,step_order,done,ts) VALUES(?,?,?,?) "
                "ON CONFLICT(opportunity_id,step_order) DO UPDATE SET done=excluded.done, ts=excluded.ts",
                (opportunity_id, step_order, int(done), time.time()))

    def get_progress(self, opportunity_id: str) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT step_order FROM mission_progress WHERE opportunity_id=? AND done=1",
                (opportunity_id,)).fetchall()
        return sorted(r["step_order"] for r in rows)

    # ---------------------------------------------------- live observations

    def add_live_snapshot(self, entity_id: str, venue: str, tick: int, snap: dict,
                          extra: dict | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO live_snapshots(entity_id,venue,tick,ts,price,stock,sellers,sold_7d,extra) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (entity_id, venue, tick, time.time(), snap["price"], snap["stock"],
                 snap["sellers"], snap["sold_7d"], json.dumps(extra or {}, default=str)))

    def live_snapshot_series(self, entity_id: str, venue: str, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, tick, price, stock, sellers, sold_7d, extra FROM live_snapshots "
                "WHERE entity_id=? AND venue=? ORDER BY id DESC LIMIT ?",
                (entity_id, venue, limit)).fetchall()
        out = [dict(r, extra=json.loads(r["extra"])) for r in rows]
        return list(reversed(out))

    def add_live_mention(self, entity_id: str, source: str, tick: int, count: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO live_mentions(entity_id,source,tick,ts,count) VALUES(?,?,?,?,?)",
                               (entity_id, source, tick, time.time(), count))

    def live_mention_series(self, entity_id: str, source: str, limit: int = 60) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT count FROM live_mentions WHERE entity_id=? AND source=? ORDER BY id DESC LIMIT ?",
                (entity_id, source, limit)).fetchall()
        return [r["count"] for r in reversed(rows)]

    def add_live_niche(self, niche_id: str, tick: int, metrics: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO live_niches(niche_id,tick,ts,metrics) VALUES(?,?,?,?)",
                               (niche_id, tick, time.time(), json.dumps(metrics, default=str)))

    def live_niche_series(self, niche_id: str, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT metrics FROM live_niches WHERE niche_id=? ORDER BY id DESC LIMIT ?",
                (niche_id, limit)).fetchall()
        return [json.loads(r["metrics"]) for r in reversed(rows)]

    def add_live_headlines(self, items: list[dict], tick: int) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO live_headlines(entity_id,tick,ts,text,etype) VALUES(?,?,?,?,?)",
                [(h["entity_id"], tick, time.time(), h["text"], h.get("etype", "news")) for h in items])

    def live_headlines_since(self, since_tick: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entity_id, tick, text, etype FROM live_headlines WHERE tick>? ORDER BY id",
                (since_tick,)).fetchall()
        return [dict(r) for r in rows]

    def live_headlines_for(self, entity_id: str, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entity_id, tick, text, etype FROM live_headlines WHERE entity_id=? "
                "ORDER BY id DESC LIMIT ?", (entity_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # -------------------------------------------------------- discovery store

    def upsert_discovered(self, cand: dict) -> bool:
        """Persist a discovered candidate. Returns True if it is newly promoted
        (first time seen), False if it was already known (score refreshed)."""

        now = time.time()
        with self._lock, self._conn:
            existing = self._conn.execute("SELECT id FROM discovered WHERE id=?",
                                          (cand["id"],)).fetchone()
            self._conn.execute(
                "INSERT INTO discovered(id,kind,name,source,score,status,first_ts,last_ts,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "score=MAX(discovered.score,excluded.score), last_ts=excluded.last_ts, "
                "name=excluded.name, source=excluded.source, payload=excluded.payload, "
                "status='active'",
                (cand["id"], cand["kind"], cand["name"], cand["source"], float(cand["score"]),
                 "active", now, now, json.dumps(cand, default=str)))
        return existing is None

    def list_discovered(self, active_only: bool = False, limit: int = 100) -> list[dict]:
        sql = "SELECT payload, status, first_ts, last_ts, score FROM discovered"
        if active_only:
            sql += " WHERE status='active'"
        sql += " ORDER BY score DESC, last_ts DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (limit,)).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            p.update(status=r["status"], first_ts=r["first_ts"], last_ts=r["last_ts"],
                     score=r["score"])
            out.append(p)
        return out

    def expire_discovered(self, ttl_days: float, max_active: int) -> int:
        """Retire stale (past TTL) and overflow (beyond the active cap, lowest
        score first) discoveries. Returns how many were retired."""

        cutoff = time.time() - ttl_days * 86400
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE discovered SET status='expired' WHERE status='active' AND last_ts < ?",
                (cutoff,))
            retired = cur.rowcount or 0
            keep = [r["id"] for r in self._conn.execute(
                "SELECT id FROM discovered WHERE status='active' ORDER BY score DESC, last_ts DESC "
                "LIMIT ?", (max_active,)).fetchall()]
            if keep:
                placeholders = ",".join("?" * len(keep))
                cur = self._conn.execute(
                    f"UPDATE discovered SET status='expired' WHERE status='active' "
                    f"AND id NOT IN ({placeholders})", keep)
                retired += cur.rowcount or 0
        return retired

    # ----------------------------------------------------------- selling kits

    def save_kit(self, opportunity_id: str, model: str, kit: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kits(opportunity_id,ts,model,payload) VALUES(?,?,?,?) "
                "ON CONFLICT(opportunity_id) DO UPDATE SET ts=excluded.ts, "
                "model=excluded.model, payload=excluded.payload",
                (opportunity_id, time.time(), model, json.dumps(kit, ensure_ascii=False)))

    def get_kit(self, opportunity_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, model, payload FROM kits WHERE opportunity_id=?",
                (opportunity_id,)).fetchone()
        if not row:
            return None
        return {"kit": json.loads(row["payload"]), "generated_at": row["ts"], "model": row["model"]}

    def discovered_counts(self) -> dict:
        with self._lock:
            active = self._conn.execute(
                "SELECT COUNT(*) FROM discovered WHERE status='active'").fetchone()[0]
            total = self._conn.execute("SELECT COUNT(*) FROM discovered").fetchone()[0]
        return {"active": active, "total": total}

    def bump_discovered_score(self, ids: list[str], delta: float) -> int:
        """Opportunity propagation: raise the priority of graph neighbors when
        a related entity produces a verified opportunity."""

        if not ids:
            return 0
        with self._lock, self._conn:
            placeholders = ",".join("?" * len(ids))
            cur = self._conn.execute(
                f"UPDATE discovered SET score = score + ?, last_ts = ? "
                f"WHERE id IN ({placeholders}) AND status='active'",
                [float(delta), time.time(), *ids])
        return cur.rowcount or 0

    # ---------------------------------------------- listing samples (research)

    def save_listing_sample(self, entity_id: str, venue: str, tick: int,
                            sample: list[dict]) -> None:
        """Latest full page of listings per (entity, venue) — one row, replaced
        each scan, so the research layer always sees fresh microstructure
        without the history tables growing by 50 listings per snapshot."""

        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO listing_samples(entity_id,venue,tick,ts,payload) VALUES(?,?,?,?,?) "
                "ON CONFLICT(entity_id,venue) DO UPDATE SET tick=excluded.tick, "
                "ts=excluded.ts, payload=excluded.payload",
                (entity_id, venue, tick, time.time(), json.dumps(sample)))

    def get_listing_sample(self, entity_id: str, venue: str) -> tuple[list[dict], int]:
        """-> (sample, tick) — empty list if never captured."""

        with self._lock:
            row = self._conn.execute(
                "SELECT payload, tick FROM listing_samples WHERE entity_id=? AND venue=?",
                (entity_id, venue)).fetchone()
        if not row:
            return [], 0
        return json.loads(row["payload"]), int(row["tick"])

    # ------------------------------------------------- research yield (bandit)

    def yield_bump(self, entity_id: str, tick: int, *, scans: int = 0, anomalies: int = 0,
                   candidates: int = 0, published: int = 0, prior: float = 0.0) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO research_yield(entity_id,scans,anomalies,candidates,published,"
                "prior,updated_tick,ts) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET "
                "scans=scans+excluded.scans, anomalies=anomalies+excluded.anomalies, "
                "candidates=candidates+excluded.candidates, published=published+excluded.published, "
                "prior=prior+excluded.prior, updated_tick=excluded.updated_tick, ts=excluded.ts",
                (entity_id, scans, anomalies, candidates, published, prior, tick, time.time()))

    def yield_rows(self, entity_ids: list[str] | None = None) -> list[dict]:
        with self._lock:
            if entity_ids:
                placeholders = ",".join("?" * len(entity_ids))
                rows = self._conn.execute(
                    f"SELECT * FROM research_yield WHERE entity_id IN ({placeholders})",
                    entity_ids).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM research_yield").fetchall()
        return [dict(r) for r in rows]

    def yield_totals(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(scans),0) s, COALESCE(SUM(anomalies),0) a, "
                "COALESCE(SUM(candidates),0) c, COALESCE(SUM(published),0) p "
                "FROM research_yield").fetchone()
        return {"scans": row["s"], "anomalies": row["a"],
                "candidates": row["c"], "published": row["p"]}

    # -------------------------------------------------- knowledge graph edges

    def add_graph_edges(self, src: str, edges: list[dict], tick: int) -> None:
        """edges: [{dst, kind, weight}] — weight accumulates on re-observation."""

        if not edges:
            return
        now = time.time()
        with self._lock, self._conn:
            for e in edges:
                self._conn.execute(
                    "INSERT INTO graph_edges(src,dst,kind,weight,tick,ts) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(src,dst,kind) DO UPDATE SET "
                    "weight=weight+excluded.weight, tick=excluded.tick, ts=excluded.ts",
                    (src, e["dst"], e.get("kind", "related"), float(e.get("weight", 1.0)),
                     tick, now))

    def graph_neighbors(self, src: str, limit: int = 12) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dst, kind, weight FROM graph_edges WHERE src=? "
                "ORDER BY weight DESC LIMIT ?", (src, limit)).fetchall()
        return [dict(r) for r in rows]

    def graph_edge_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]

    def upsert_graph_node(self, node_id: str, ntype: str, name: str, attrs: dict | None = None) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO graph_nodes(node_id,ntype,name,attrs,first_ts,last_ts) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET "
                "ntype=excluded.ntype, name=excluded.name, "
                "attrs=COALESCE(excluded.attrs, graph_nodes.attrs), last_ts=excluded.last_ts",
                (node_id, ntype, name, json.dumps(attrs or {}), now, now))

    def get_graph_node(self, node_id: str) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT node_id, ntype, name, attrs, first_ts, last_ts "
                "FROM graph_nodes WHERE node_id=?", (node_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["attrs"] = json.loads(d["attrs"] or "{}")
        return d

    def graph_nodes_by_type(self, ntype: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_id, ntype, name, attrs, last_ts FROM graph_nodes "
                "WHERE ntype=? ORDER BY last_ts DESC LIMIT ?", (ntype, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["attrs"] = json.loads(d["attrs"] or "{}")
            out.append(d)
        return out

    def graph_node_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]

    def graph_edges_from(self, src: str, kinds: list[str] | None = None,
                         limit: int = 50) -> list[dict]:
        sql = "SELECT src, dst, kind, weight FROM graph_edges WHERE src=?"
        args: list = [src]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        sql += " ORDER BY weight DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------- per-opportunity chat (P13)

    def add_chat_message(self, opp_id: str, role: str, content: str,
                         evidence: list | None = None, tool: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO chat_messages(opportunity_id,ts,role,content,evidence,tool) "
                "VALUES(?,?,?,?,?,?)",
                (opp_id, time.time(), role, content, json.dumps(evidence or []), tool))

    def chat_history(self, opp_id: str, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, role, content, evidence, tool FROM chat_messages "
                "WHERE opportunity_id=? ORDER BY ts ASC, id ASC LIMIT ?",
                (opp_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"] or "[]")
            out.append(d)
        return out

    def get_chat_state(self, opp_id: str) -> dict:
        with self._lock:
            r = self._conn.execute(
                "SELECT state, next_action, updated_ts, data FROM chat_state "
                "WHERE opportunity_id=?", (opp_id,)).fetchone()
        if not r:
            return {"state": "not_started", "next_action": None, "data": {}}
        d = dict(r)
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def set_chat_state(self, opp_id: str, state: str, next_action: str | None,
                       data: dict | None = None) -> None:
        with self._lock, self._conn:
            existing = self.get_chat_state(opp_id)
            merged = {**existing.get("data", {}), **(data or {})}
            self._conn.execute(
                "INSERT INTO chat_state(opportunity_id,state,next_action,updated_ts,data) "
                "VALUES(?,?,?,?,?) ON CONFLICT(opportunity_id) DO UPDATE SET "
                "state=excluded.state, next_action=excluded.next_action, "
                "updated_ts=excluded.updated_ts, data=excluded.data",
                (opp_id, state, next_action, time.time(), json.dumps(merged)))

    def add_chat_ledger(self, opp_id: str, kind: str, amount_usd: float,
                        qty: int = 0, note: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO chat_ledger(opportunity_id,ts,kind,amount_usd,qty,note) "
                "VALUES(?,?,?,?,?,?)", (opp_id, time.time(), kind, float(amount_usd), int(qty), note))

    def chat_ledger(self, opp_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, kind, amount_usd, qty, note FROM chat_ledger "
                "WHERE opportunity_id=? ORDER BY ts ASC", (opp_id,)).fetchall()
        return [dict(r) for r in rows]

    def realized_pnl(self, opp_id: str) -> dict:
        """Net realised from the ledger: sales + refunds-in minus purchases/expenses."""

        inflow = outflow = 0.0
        for e in self.chat_ledger(opp_id):
            if e["kind"] in ("sale", "refund_received"):
                inflow += e["amount_usd"]
            elif e["kind"] in ("purchase", "expense", "refund_issued"):
                outflow += e["amount_usd"]
        return {"inflow_usd": round(inflow, 2), "outflow_usd": round(outflow, 2),
                "net_usd": round(inflow - outflow, 2)}

    def add_chat_link(self, opp_id: str, url: str, alive: bool,
                      price_usd: float | None = None, note: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO chat_links(opportunity_id,ts,url,alive,price_usd,note) "
                "VALUES(?,?,?,?,?,?)",
                (opp_id, time.time(), url, 1 if alive else 0, price_usd, note))

    def chat_links(self, opp_id: str, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, url, alive, price_usd, note FROM chat_links "
                "WHERE opportunity_id=? ORDER BY ts DESC LIMIT ?", (opp_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_checklist_item(self, opp_id: str, step: str, done: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO chat_checklist(opportunity_id,step,done,ts) VALUES(?,?,?,?) "
                "ON CONFLICT(opportunity_id,step) DO UPDATE SET done=excluded.done, ts=excluded.ts",
                (opp_id, step, 1 if done else 0, time.time()))

    def chat_checklist(self, opp_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT step, done, ts FROM chat_checklist WHERE opportunity_id=? ORDER BY ts ASC",
                (opp_id,)).fetchall()
        return [{"step": r["step"], "done": bool(r["done"]), "ts": r["ts"]} for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
