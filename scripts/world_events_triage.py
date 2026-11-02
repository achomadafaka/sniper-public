#!/usr/bin/env python3
"""Dry-run triage for stale world_events.

Read-only. Classifies stale active events into buckets for human review
before any lifecycle mutation. Writes TSV + JSON to /tmp.

Buckets:
  orphaned_finance   — slug starts with 'fin:' (finance domain moved to markets/state
                       memory; these are orphaned in world_events)
  resolve_candidate  — slug contains a past month-day token (e.g. '-apr10', '-mar26')
                       AND the parsed date (year=2026) is >7 days behind now
  still_active       — everything else; refreshes automatically once touch-on-review
                       ships in intel_tool.py

Usage:
  python3 scripts/world_events_triage.py                    # stale >72h (A3 threshold)
  python3 scripts/world_events_triage.py --older-than 48    # custom age filter
  python3 scripts/world_events_triage.py --all-active       # every active event
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_TOKEN_RE = re.compile(
    r"-(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(\d{1,2})(?:-|$)",
    re.IGNORECASE,
)


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _sb_get(path: str) -> list[dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + path
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_ANON_KEY"]
    req = urllib.request.Request(
        url,
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_date_token(slug: str, assumed_year: int = 2026) -> datetime | None:
    """Extract first month-day token like '-apr10' → datetime(2026, 4, 10)."""
    m = _DATE_TOKEN_RE.search(slug)
    if not m:
        return None
    mon = _MONTH_MAP[m.group(1).lower()]
    day_str = m.group(2)
    try:
        day = int(day_str)
    except ValueError:
        return None
    # Guard: token '-apr26' is ambiguous — could be day-26 or year-'26. If
    # day > 31, treat as year-abbrev and don't parse as date.
    if day < 1 or day > 31:
        return None
    try:
        return datetime(assumed_year, mon, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def classify(event: dict, now_utc: datetime) -> tuple[str, str]:
    """Return (bucket, reason)."""
    slug = event.get("slug") or ""
    if slug.startswith("fin:"):
        return (
            "orphaned_finance",
            "finance domain now owned by markets/state memory, not world_events",
        )
    date_parsed = _parse_date_token(slug)
    if date_parsed is not None:
        age_days = (now_utc - date_parsed).total_seconds() / 86400.0
        if age_days > 7:
            return (
                "resolve_candidate",
                f"slug date-token resolves to {date_parsed.date().isoformat()}, {age_days:.1f}d in past",
            )
    return (
        "still_active",
        "no heuristic match; refreshes on next analyst review",
    )


def run(min_age_hours: float, include_all_active: bool) -> dict:
    _load_env()
    path = (
        "world_events?"
        "status=in.(active,escalating,developing)"
        "&select=slug,title,status,last_updated,created_at,source"
        "&order=last_updated.asc"
    )
    events = _sb_get(path)

    now_utc = datetime.now(timezone.utc)
    classified: list[dict] = []
    for ev in events:
        last_upd = _parse_ts(ev.get("last_updated"))
        age_h = ((now_utc - last_upd).total_seconds() / 3600.0) if last_upd else float("inf")
        if not include_all_active and age_h <= min_age_hours:
            continue
        bucket, reason = classify(ev, now_utc)
        classified.append({
            "slug": ev.get("slug"),
            "status": ev.get("status"),
            "age_hours": round(age_h, 1),
            "last_updated": ev.get("last_updated"),
            "created_at": ev.get("created_at"),
            "title": (ev.get("title") or "")[:80],
            "bucket": bucket,
            "reason": reason,
        })

    # Summary
    summary: dict = {"total_stale": len(classified), "by_bucket": {}}
    for row in classified:
        summary["by_bucket"][row["bucket"]] = summary["by_bucket"].get(row["bucket"], 0) + 1
    return {"generated_at": now_utc.isoformat(), "summary": summary, "rows": classified}


def write_outputs(report: dict, out_dir: Path) -> tuple[Path, Path]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"world_events_triage_{ts}.json"
    tsv_path = out_dir / f"world_events_triage_{ts}.tsv"

    json_path.write_text(json.dumps(report, indent=2))

    with tsv_path.open("w") as f:
        f.write("bucket\tage_h\tstatus\tslug\ttitle\treason\n")
        # Sort: orphaned_finance first, then resolve_candidate, then still_active; within, oldest first
        bucket_order = {"orphaned_finance": 0, "resolve_candidate": 1, "still_active": 2}
        rows_sorted = sorted(
            report["rows"],
            key=lambda r: (bucket_order.get(r["bucket"], 99), -r["age_hours"]),
        )
        for r in rows_sorted:
            f.write(
                f"{r['bucket']}\t{r['age_hours']}\t{r['status']}\t{r['slug']}\t"
                f"{r['title']}\t{r['reason']}\n"
            )

    return json_path, tsv_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--older-than", type=float, default=72.0, help="min age in hours (default 72, matches A3)")
    ap.add_argument("--all-active", action="store_true", help="include all active events regardless of age")
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp"))
    args = ap.parse_args()

    report = run(args.older_than, args.all_active)
    json_path, tsv_path = write_outputs(report, args.out_dir)

    print(f"generated_at: {report['generated_at']}")
    print(f"total_stale:  {report['summary']['total_stale']}")
    print("by_bucket:")
    for b, n in sorted(report["summary"]["by_bucket"].items(), key=lambda kv: -kv[1]):
        print(f"  {b:20s}  {n}")
    print(f"\njson: {json_path}")
    print(f"tsv:  {tsv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
