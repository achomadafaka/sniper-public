"""Sample /prices-history for historical tennis markets — audit depth and granularity.

Input: docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl
Output: JSONL to stdout, one line per sampled market with:
  - condition_id, token_id, end_date, title
  - fidelity_60 sample: n_points, first_ts, last_ts, gap_stats
  - fidelity_1 sample: n_points or "unsupported"
"""
import json
import random
import sys
import time
from datetime import datetime

import requests

CLOB = "https://clob.polymarket.com/prices-history"
INPUT = "docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl"


def iso_to_ts(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def fetch_history(token_id, start_ts, end_ts, fidelity):
    try:
        r = requests.get(
            CLOB,
            params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": fidelity,
            },
            timeout=20,
        )
        if not r.ok:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:200]}
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def pick_sample(path, n=10):
    """Pick random H2H tennis events with required fields present."""
    events = []
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            end_date = e.get("endDate")
            markets = e.get("markets") or []
            if not end_date or not markets:
                continue
            etitle = (e.get("title") or "").strip()
            if " vs " not in etitle.lower() and " vs. " not in etitle.lower():
                continue  # skip outright winners and non-H2H
            # find H2H market where market.question == event.title
            for m in markets:
                if (m.get("question") or "").strip() != etitle:
                    continue
                cid = m.get("conditionId")
                try:
                    tids = json.loads(m.get("clobTokenIds", "[]"))
                except Exception:
                    continue
                if cid and tids:
                    events.append({
                        "condition_id": cid,
                        "token_id": tids[0],
                        "end_date": end_date,
                        "title": etitle,
                    })
                break
    random.seed(42)
    random.shuffle(events)
    return events[:n]


def summarize(data):
    if not data or "error" in data:
        return {"error": data.get("error", "unknown") if isinstance(data, dict) else "unknown"}
    hist = data.get("history") or []
    if not hist:
        return {"n_points": 0}
    ts = [p.get("t") for p in hist if p.get("t")]
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    return {
        "n_points": len(hist),
        "first_ts": min(ts) if ts else None,
        "last_ts": max(ts) if ts else None,
        "median_gap_sec": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "max_gap_sec": max(gaps) if gaps else None,
    }


def main():
    samples = pick_sample(INPUT, n=10)
    print(f"[probe] sampled {len(samples)} markets", file=sys.stderr)
    for s in samples:
        end_ts = iso_to_ts(s["end_date"])
        if not end_ts:
            continue
        start_ts = end_ts - 86400 * 7  # 7 days before end

        f60 = fetch_history(s["token_id"], start_ts, end_ts, fidelity=60)
        time.sleep(0.2)
        f1 = fetch_history(s["token_id"], start_ts, end_ts, fidelity=1)
        time.sleep(0.2)

        print(json.dumps({
            "condition_id": s["condition_id"],
            "token_id": s["token_id"],
            "title": s["title"],
            "end_date": s["end_date"],
            "start_ts": start_ts,
            "end_ts": end_ts,
            "fidelity_60": summarize(f60),
            "fidelity_1": summarize(f1),
        }))


if __name__ == "__main__":
    main()
