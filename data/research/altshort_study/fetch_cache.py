"""Cache HL 1h candles + funding history for the alt-short standalone-signal study.
Resume-safe: skips any coin whose cache file already exists.
Read-only public HL endpoints. Scoped to data/research/altshort_study/cache/.
"""
from __future__ import annotations
import json, os, sys, time, datetime as dt
sys.path.insert(0, "/home/bigpapi/Desktop/Sniper")
from sniper.markets.hyperliquid_api import fetch_marks, fetch_candles, fetch_funding_history

CACHE = "/home/bigpapi/Desktop/Sniper/data/research/altshort_study/cache"
os.makedirs(CACHE, exist_ok=True)

STUDY_START = int(dt.datetime(2026, 1, 27, tzinfo=dt.timezone.utc).timestamp() * 1000)
STUDY_END = int(dt.datetime(2026, 7, 26, 15, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _with_backoff(fn, *a, **kw):
    """429-aware retry: exponential backoff, up to 6 tries. Raises on persistent failure
    (caller must NOT cache a file on exception, so a retry run picks it back up)."""
    delay = 1.0
    for attempt in range(6):
        try:
            return fn(*a, **kw)
        except Exception as e:
            if "429" in str(e) and attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            raise
    raise RuntimeError("unreachable")


def fetch_all_candles():
    marks = fetch_marks()
    universe = sorted(marks.keys())
    print(f"universe n={len(universe)}", flush=True)
    with open(f"{CACHE}/_universe.json", "w") as f:
        json.dump(universe, f)
    for i, coin in enumerate(universe):
        fp = f"{CACHE}/candles_{coin}.json"
        if os.path.exists(fp):
            continue
        try:
            candles = _with_backoff(fetch_candles, coin, "1h", STUDY_START, STUDY_END)
        except Exception as e:
            print(f"  candles FAIL {coin}: {e} (will retry next run)", flush=True)
            time.sleep(0.3)
            continue
        with open(fp, "w") as f:
            json.dump(candles, f)
        if i % 20 == 0:
            print(f"candles {i}/{len(universe)} {coin} n={len(candles)}", flush=True)
        time.sleep(0.25)


def fetch_all_funding():
    universe = json.load(open(f"{CACHE}/_universe.json"))
    for i, coin in enumerate(universe):
        fp = f"{CACHE}/funding_{coin}.json"
        if os.path.exists(fp):
            continue
        rows = []
        start = STUDY_START
        ok = True
        for _ in range(30):  # hard cap pages, safety
            try:
                page = _with_backoff(fetch_funding_history, coin, start, STUDY_END)
            except Exception as e:
                print(f"  funding FAIL {coin}: {e} (will retry next run)", flush=True)
                ok = False
                break
            if not page:
                break
            rows.extend(page)
            if len(page) < 500:
                break
            start = page[-1]["time"] + 1
            time.sleep(0.25)
        if not ok:
            time.sleep(0.5)
            continue
        with open(fp, "w") as f:
            json.dump(rows, f)
        if i % 20 == 0:
            print(f"funding {i}/{len(universe)} {coin} n={len(rows)}", flush=True)
        time.sleep(0.25)


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("candles", "all"):
        fetch_all_candles()
    if stage in ("funding", "all"):
        fetch_all_funding()
    print("DONE", flush=True)
