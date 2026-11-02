#!/usr/bin/env python3
"""Run the local X source-graph capture receiver (display-only, read-only lane).

Binds 127.0.0.1 only. Receives sanitized-on-arrival card batches from the browser userscript
(scripts/memecoin_x_capture.user.js) and persists through the parse -> normalize -> store
pipeline behind the clock-health gate. Isolated store; no trading imports or effects.

    python3 scripts/run_memecoin_x_receiver.py [--db data/memecoin_x.db] [--port 8787]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_x_receiver import create_app, initialize_store


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/memecoin_x.db")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--bind", choices=("127.0.0.1",), default="127.0.0.1")
    ap.add_argument(
        "--clock-offset-file",
        default=os.environ.get("SNIPER_CLOCK_OFFSET_FILE", str(ROOT / "data" / "clock_offset.json")),
    )
    args = ap.parse_args()
    initialize_store(args.db)
    app = create_app(db_path=args.db, offset_path=args.clock_offset_file)
    print(f"[mcx] receiver on http://{args.bind}:{args.port}  db={args.db}  (display-only)")
    app.run(host=args.bind, port=args.port)


if __name__ == "__main__":
    main()
