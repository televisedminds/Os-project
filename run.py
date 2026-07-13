#!/usr/bin/env python3
"""Opportunity OS entry point.

    python run.py serve            # dashboard + API on :8000, auto-cycles every 180s
    python run.py cycle -n 3       # run three research cycles and print the reports
    python run.py brief            # print the morning briefing to the terminal
    python run.py reset            # wipe state, re-seed the demo world
"""

from __future__ import annotations

import argparse
import json

from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.pipeline import Orchestrator, briefing


def cmd_serve(args) -> None:
    import uvicorn
    from opportunity_os.api import create_app
    auto = args.auto_cycle if args.auto_cycle is not None else 180
    app = create_app(Config(), auto_cycle_seconds=auto)
    print(f"Opportunity OS → http://{args.host}:{args.port}   "
          f"(auto research cycle every {auto}s{'' if auto else ' — disabled'})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_cycle(args) -> None:
    cfg = Config()
    orch = Orchestrator(cfg, Store(cfg.db_path))
    for i in range(args.n):
        r = orch.run_cycle()
        print(f"cycle tick={r['tick']}  signals={r['signals']}  anomalies={r['anomalies']}  "
              f"candidates={r['candidates']}  published={len(r['published'])}  "
              f"rejected={len(r['rejected'])}  invalidated={len(r['invalidated'])}")
        for p in r["published"]:
            print(f"   + {p['title']}  (score {p['score']}, confidence {p['confidence']:.0%})")
        for rj in r["rejected"]:
            print(f"   - rejected: {rj['title']} — {rj['reason'][:110]}")
        for iv in r["invalidated"]:
            print(f"   x invalidated: {iv['title']} — {iv['reason'][:110]}")


def cmd_brief(args) -> None:
    cfg = Config()
    b = briefing(Store(cfg.db_path), cfg, args.plan)
    print(f"\n  OPPORTUNITY OS — {b['generated_at'][:16]} ({b['timezone']})\n")
    print(f"  {b['headline']}")
    for n in b["notes"]:
        print(f"  {n}")
    print()
    for i, t in enumerate(b["top"], 1):
        print(f"  {i:>2}. [{t['score']:>4.1f}] {t['title']}")
        print(f"      {t['subtitle']}")
        print(f"      est. ${t['net_usd']:,.2f} · confidence {t['confidence']:.0%} · "
              f"window ~{t['window_days']:.0f}d\n")
    if b["locked"]:
        print(f"  … plus {b['locked']} more on the Pro plan.")


def cmd_reset(args) -> None:
    cfg = Config()
    if cfg.db_path.exists():
        cfg.db_path.unlink()
        print(f"removed {cfg.db_path}")
    orch = Orchestrator(cfg, Store(cfg.db_path))
    for _ in range(args.warm):
        r = orch.run_cycle()
    print(f"re-seeded demo world → tick {r['tick']}, "
          f"{len(orch.db.active_opportunities())} active opportunities")


def main() -> None:
    ap = argparse.ArgumentParser(prog="opportunity-os", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the dashboard + API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--auto-cycle", type=int, default=None,
                   help="seconds between automatic research cycles (0 = off, default 180)")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("cycle", help="run research cycles now")
    c.add_argument("-n", type=int, default=1)
    c.set_defaults(fn=cmd_cycle)

    b = sub.add_parser("brief", help="print the morning briefing")
    b.add_argument("--plan", default=None)
    b.set_defaults(fn=cmd_brief)

    r = sub.add_parser("reset", help="wipe state and re-seed the demo world")
    r.add_argument("--warm", type=int, default=2, help="cycles to run after reset")
    r.set_defaults(fn=cmd_reset)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
