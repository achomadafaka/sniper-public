#!/usr/bin/env python3
"""fwlm-v0 evaluator — operator-gated (spec §15 step 8: verdict only at the milestone).
Generates missing controls, computes the §12/§13 verdict, persists it as a report row.
No cron installs this; an operator runs it when the pre-registered milestone is reached."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import far_whale_eval as E    # noqa: E402
from sniper.hyperlive import far_whale_store as S   # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fwlm-v0 controls + verdict (operator-gated)")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args(argv)
    conn = S.connect(args.db)
    S.ensure_schema(conn)
    now_ms = int(time.time() * 1000)
    counts = E.generate_controls(conn)
    verdict = E.evaluate(conn, now_ms=now_ms)
    S.save_report(conn, kind="verdict", payload=verdict, ts_ms=now_ms)
    print(f"controls: {counts}")
    print(json.dumps(verdict, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
