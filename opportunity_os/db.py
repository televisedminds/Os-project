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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
