#!/usr/bin/env python3
"""Opportunity OS entry point.

Demo mode (default — simulated market, no keys needed):
    python run.py serve                dashboard + API on :8000, cycle every 180s
    python run.py cycle -n 3           run three research cycles now
    python run.py brief                print the morning briefing
    python run.py reset                wipe state, re-seed the demo world

Live mode (real connectors over your watchlist; add --live or OOS_MODE=live):
    python run.py live-check           validate keys, adapters, watchlist, FX
    python run.py serve --live         observe every 30 min, publish what verifies
    python run.py cycle --live         one observation/research pass now
    python run.py brief --live --push  send the briefing to your Telegram

Live mode keeps its own database (data/live.db) so sim and real history never mix.
"""

from __future__ import annotations

import argparse
import os

from opportunity_os.config import DATA_DIR, Config


def _cfg(args) -> Config:
    if getattr(args, "live", False):
        os.environ["OOS_MODE"] = "live"
        os.environ.setdefault("OOS_DB", str(DATA_DIR / "live.db"))
    cfg = Config()
    if cfg.mode == "live" and "OOS_DB" not in os.environ:
        cfg.db_path = DATA_DIR / "live.db"
    return cfg


def _store(cfg):
    from opportunity_os.db import Store
    return Store(cfg.db_path)


def cmd_serve(args) -> None:
    import uvicorn
    from opportunity_os.api import create_app
    cfg = _cfg(args)
    auto = args.auto_cycle if args.auto_cycle is not None else (1800 if cfg.mode == "live" else 180)
    app = create_app(cfg, auto_cycle_seconds=auto)
    print(f"Opportunity OS [{cfg.mode.upper()}] → http://{args.host}:{args.port}   "
          f"(research cycle every {auto}s)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_cycle(args) -> None:
    from opportunity_os.pipeline import Orchestrator
    cfg = _cfg(args)
    orch = Orchestrator(cfg, _store(cfg))
    for _ in range(args.n):
        r = orch.run_cycle()
        print(f"[{cfg.mode}] cycle tick={r['tick']}  signals={r['signals']}  anomalies={r['anomalies']}  "
              f"candidates={r['candidates']}  published={len(r['published'])}  "
              f"rejected={len(r['rejected'])}  invalidated={len(r['invalidated'])}")
        for p in r["published"]:
            print(f"   + {p['title']}  (score {p['score']}, confidence {p['confidence']:.0%})")
        for rj in r["rejected"]:
            print(f"   - rejected: {rj['title']} — {rj['reason'][:110]}")
        for iv in r["invalidated"]:
            print(f"   x invalidated: {iv['title']} — {iv['reason'][:110]}")
        d = r.get("discovered") or {}
        if d.get("promoted"):
            print(f"   ~ discovery: +{d['promoted']} new candidate(s) promoted "
                  f"({d.get('found', 0)} found this sweep across {len(d.get('sources', {}))} sources)")
        if cfg.mode == "live" and getattr(orch.world, "errors", None):
            for e in orch.world.errors:
                print(f"   ! source degraded: {e}")


def cmd_brief(args) -> None:
    from opportunity_os import settings as app_settings
    from opportunity_os.notify import briefing_text, send_telegram
    from opportunity_os.pipeline import briefing
    cfg = _cfg(args)
    store = _store(cfg)
    app_settings.load_into(cfg, store)             # keys saved in the dashboard
    b = briefing(store, cfg, args.plan)
    text = briefing_text(b, max_items=10)
    print("\n" + text + "\n")
    if args.push:
        ok, note = send_telegram(cfg, briefing_text(b))
        print(f"telegram push: {note}")


def cmd_reset(args) -> None:
    from opportunity_os.pipeline import Orchestrator
    cfg = _cfg(args)
    if cfg.db_path.exists():
        if cfg.mode == "live" and not args.yes:
            print(f"Refusing to wipe LIVE observation history at {cfg.db_path} without --yes "
                  f"(days of baselines live there).")
            return
        cfg.db_path.unlink()
        print(f"removed {cfg.db_path}")
    orch = Orchestrator(cfg, _store(cfg))
    warm = 0 if cfg.mode == "live" else args.warm
    for _ in range(warm):
        orch.run_cycle()
    print(f"re-initialised [{cfg.mode}] → tick {orch.db.meta_get('tick', 0) or orch.world.tick_no}, "
          f"{len(orch.db.active_opportunities())} active opportunities")


def cmd_live_check(args) -> None:
    args.live = True
    cfg = _cfg(args)
    from opportunity_os.market import watchlist as wl
    from opportunity_os.market.live import LiveMarket

    print(f"\n◆ OPPORTUNITY OS live-check   (db: {cfg.db_path})\n")
    try:
        watch = wl.load(cfg.watchlist_path)
        obs_venues = sorted({v for p in watch.products for v in p.queries})
        man_venues = sorted({v for p in watch.products for v in p.manual_listings})
        print(f"  watchlist  ✓ {cfg.watchlist_path}")
        print(f"             {len(watch.products)} products (observed: {', '.join(obs_venues) or '—'};"
              f" manual quotes: {', '.join(man_venues) or '—'}), {len(watch.niches)} niches")
    except Exception as e:  # noqa: BLE001
        print(f"  watchlist  ✗ {e}")
        raise SystemExit(1)

    from opportunity_os import settings as app_settings
    lc_store = _store(cfg)
    applied = app_settings.load_into(cfg, lc_store)    # keys saved in the dashboard
    if applied:
        print(f"  keys       ✓ {len(applied)} key(s) loaded from the dashboard settings")
    lm = LiveMarket(cfg, lc_store)
    all_ok = True
    for st in lm.healthcheck():
        mark = "✓" if st["ok"] else "✗"
        all_ok &= st["ok"] or st["id"] in ("reddit", "shopee_th")   # optional sources
        print(f"  {st['id']:<8} {mark} {st['name']}: {st['note']}")

    if lm.discovery is not None:
        print("\n  discovery engine (auto-finds new opportunities across the internet):")
        for st in lm.discovery.healthcheck():
            print(f"  {st['id']:<18} {'✓' if st['ok'] else '−'} {st['name']}: {st['note']}")
    else:
        print("\n  discovery − disabled (set OOS_DISCOVERY=1 to auto-find beyond your watchlist)")

    from opportunity_os.notify import send_telegram
    if cfg.telegram_bot_token:
        ok, note = send_telegram(cfg, "◆ Opportunity OS: live-check ping — notifications working.")
        print(f"  telegram {'✓' if ok else '✗'} {note}")
    else:
        print("  telegram − not configured (optional: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")

    print("\n  Next: python run.py cycle --live   (first pass records baselines;"
          "\n        verified opportunities appear once history accumulates — usually 1–3 days)\n")
    raise SystemExit(0 if all_ok else 1)


def cmd_diagnose(args) -> None:
    """Honest per-source health: a real, safe test call to each integration."""

    args.live = True
    cfg = _cfg(args)
    from opportunity_os import diagnostics, settings as app_settings
    store = _store(cfg)
    app_settings.load_into(cfg, store)

    print(f"\n◆ OPPORTUNITY OS  diagnose_sources   (db: {cfg.db_path})")
    print("  Making one real test call per source — a key being 'set' proves nothing.\n")
    rows = diagnostics.probe_sources(cfg, store)
    icon = {"HEALTHY": "✓", "DEGRADED": "~", "AUTH_FAILED": "✗", "QUOTA_EXHAUSTED": "$",
            "PARSER_BROKEN": "!", "NO_DATA": "·", "DISABLED": "−"}
    print(f"  {'SOURCE':<14}{'STATUS':<16}{'LAT':>7}  {'PARSED':>7}  {'STORED':>7}  NOTE")
    print(f"  {'-'*14}{'-'*16}{'-'*7}  {'-'*7}  {'-'*7}  {'-'*30}")
    for r in rows:
        lat = f"{r['avg_latency_ms']:.0f}ms" if r["avg_latency_ms"] is not None else "—"
        parsed = r["records_parsed"] if r["records_parsed"] is not None else "—"
        note = (r["note"] or r["error"] or "")[:44]
        print(f"  {r['id']:<14}{icon.get(r['status'],'?')} {r['status']:<14}{lat:>7}  "
              f"{str(parsed):>7}  {str(r['observations_stored']):>7}  {note}")

    print("\n  Source funnel (what actually reaches a published opportunity):")
    for f in diagnostics.source_funnel(store):
        print(f"    {f['source']:<14} obs {f['observations_stored']:>6}   "
              f"→ published {f['published_opportunities']}")
    tf = diagnostics.type_funnel(store)
    print(f"\n  Opportunity types: {tf['by_type']}")
    print(f"  {tf['verified_opportunities']} verified = {tf['unique_theses']} unique theses "
          f"across {tf['unique_products']} products")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(prog="opportunity-os", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def live_flag(p):
        p.add_argument("--live", action="store_true", help="run against real connectors (OOS_MODE=live)")

    s = sub.add_parser("serve", help="run the dashboard + API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--auto-cycle", type=int, default=None,
                   help="seconds between research cycles (default: 180 demo / 1800 live; 0 = off)")
    live_flag(s)
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("cycle", help="run research cycles now")
    c.add_argument("-n", type=int, default=1)
    live_flag(c)
    c.set_defaults(fn=cmd_cycle)

    b = sub.add_parser("brief", help="print (and optionally push) the briefing")
    b.add_argument("--plan", default=None)
    b.add_argument("--push", action="store_true", help="also send to Telegram")
    live_flag(b)
    b.set_defaults(fn=cmd_brief)

    r = sub.add_parser("reset", help="wipe state and re-initialise")
    r.add_argument("--warm", type=int, default=2, help="demo cycles to run after reset")
    r.add_argument("--yes", action="store_true", help="confirm wiping live observation history")
    live_flag(r)
    r.set_defaults(fn=cmd_reset)

    lc = sub.add_parser("live-check", help="validate live keys, adapters, watchlist and FX")
    lc.set_defaults(fn=cmd_live_check)

    dg = sub.add_parser("diagnose_sources",
                        help="real test call to every source; honest health + funnel report")
    dg.set_defaults(fn=cmd_diagnose)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
