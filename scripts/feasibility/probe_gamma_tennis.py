"""Paginate Polymarket Gamma /events?closed=true&tag=tennis, dump all tennis events.

Writes JSONL to stdout. Each line is one tennis-tagged event (full JSON).

Usage:
  python3 scripts/feasibility/probe_gamma_tennis.py > out.jsonl 2> log.txt
"""
import json
import sys
import time

import requests

GAMMA = "https://gamma-api.polymarket.com/events"
PAGE_LIMIT = 500


def is_tennis(event):
    tags = [t.get("slug", "") if isinstance(t, dict) else str(t)
            for t in (event.get("tags") or [])]
    return "tennis" in tags


def main():
    offset = 0
    total = 0
    tennis_count = 0
    while True:
        try:
            r = requests.get(
                GAMMA,
                params={
                    "closed": "true",
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "false",
                },
                timeout=30,
            )
        except Exception as e:
            print(f"[probe] offset={offset} REQUEST ERROR {e} — retry in 3s", file=sys.stderr)
            time.sleep(3)
            continue
        if not r.ok:
            print(f"[probe] offset={offset} HTTP {r.status_code} — stop", file=sys.stderr)
            break
        page = r.json()
        if not page:
            print(f"[probe] offset={offset} empty page — stop", file=sys.stderr)
            break
        in_page_tennis = 0
        for e in page:
            total += 1
            if is_tennis(e):
                tennis_count += 1
                in_page_tennis += 1
                print(json.dumps(e))
        print(f"[probe] offset={offset} page_size={len(page)} "
              f"tennis_in_page={in_page_tennis} total_events_seen={total} tennis_total={tennis_count}",
              file=sys.stderr)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(0.25)
    print(f"[probe] DONE total_events={total} tennis={tennis_count}", file=sys.stderr)


if __name__ == "__main__":
    main()
