#!/usr/bin/env python3
"""Project eligible Radar evidence, then optionally start the hot ledger fresh."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.research.memecoin_opportunity.insights import (
    connect_insights,
    project_matured_insights,
)
from sniper.research.memecoin_opportunity.retention import eligible_launch_ids
from sniper.research.memecoin_opportunity.store import connect
from sniper.util.clock import real_utc_now_iso


def prepare(
    *, hot_db: str | Path, insight_db: str | Path, now_iso: str,
    retention_s: int, fresh_hot: bool,
) -> dict[str, int | bool]:
    hot_path = Path(hot_db).expanduser().resolve()
    insight_path = Path(insight_db).expanduser().resolve()
    if not hot_path.is_file():
        raise FileNotFoundError(f"hot opportunity database does not exist: {hot_path}")
    if hot_path == insight_path:
        raise ValueError("hot and insight databases must be different files")
    hot = connect(hot_path)
    insights = connect_insights(insight_path)
    try:
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
        cutoff = (now - timedelta(seconds=retention_s)).isoformat().replace("+00:00", "Z")
        launch_ids = eligible_launch_ids(
            hot, cutoff_iso=cutoff, max_launches=1_000_000_000
        )
        projected = project_matured_insights(
            hot, insights, now_iso=now_iso, launch_ids=launch_ids
        ) if launch_ids else 0
        hot.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        insights.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        insights.close()
        hot.close()
    if fresh_hot:
        for candidate in (
            hot_path, Path(f"{hot_path}-wal"), Path(f"{hot_path}-shm")
        ):
            if candidate.exists():
                candidate.unlink()
    return {
        "eligible_launches": len(launch_ids),
        "projected_launches": projected,
        "fresh_hot": fresh_hot,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hot-db", required=True)
    parser.add_argument("--insight-db", required=True)
    parser.add_argument("--retention-s", type=int, default=1800)
    parser.add_argument("--fresh-hot", action="store_true")
    args = parser.parse_args(argv)
    if args.retention_s <= 0:
        parser.error("--retention-s must be > 0")
    result = prepare(
        hot_db=args.hot_db,
        insight_db=args.insight_db,
        now_iso=real_utc_now_iso(),
        retention_s=args.retention_s,
        fresh_hot=args.fresh_hot,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
