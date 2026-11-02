#!/usr/bin/env python3
"""Bounded feasibility check on the mobula OHLCV endpoint. NOT an integration.

fomo's own web app calls `fomo-api.mobula.io/api/2/token/ohlcv-history` at a median 147 ms. Our
tape is the reason the impulse screen cannot price an event at obs+3s: only 7% of buy events have
a clean mark within 5 s (DexScreener 45 s, fastlane_mid 17 s). This script asks whether that
endpoint could close the gap. It answers only that.

    python3 scripts/research/mobula_ohlcv_feasibility.py --freeze --events 40
    python3 scripts/research/mobula_ohlcv_feasibility.py --probe --max-requests 40

## Three things this check must not claim

1. OHLCV does NOT fix the measurement outright. A candle spanning [t0+3, t0+4) contains trades
   AFTER the baseline instant, so reading its close as "the price at t0+3" looks ahead by up to one
   period. Only a candle whose END is at or before the baseline is admissible. `--strict-baseline`
   is on by default and a candle that straddles the instant is REJECTED, not clamped.
2. It cannot restore a censored event unless the endpoint HAD data at that event's time. Present
   coverage is not past coverage, and a backfilled series is not evidence that the price was
   observable live. The report separates "endpoint returns candles" from "candles exist at t0".
3. "No WebSocket" was true for ONE page session on ONE token route. This script does not test that
   claim and does not repeat it.

## Gates, all enforced in code

1. No session token, no cookie, no Authorization header. Unauthenticated only. If the endpoint
   requires auth we learn that and stop; we do not borrow the operator's session.
2. The event sample is FROZEN to disk with a digest before the first request. A later probe run
   against a changed sample refuses to start, so the sample cannot be reshaped by what we see.
3. Every response records granularity, candle timestamps, pool identity, latency and revision
   behaviour (the same window is requested twice and diffed).
4. Candles that include data after the baseline instant are rejected.
5. Admissible candles are compared against our existing clean marks where both exist.
6. Any 401, 403 or 429 stops the run immediately.
7. Nothing here is importable by the funded entry or exit path. It writes only to its own report
   file and reads the tracker read-only.
"""
import argparse
import hashlib
import json
import pathlib
import sqlite3
import statistics as st
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ENDPOINT = "https://fomo-api.mobula.io/api/2/token/ohlcv-history"
SAMPLE_PATH = pathlib.Path("data/research/mobula_feasibility_sample.json")
REPORT_PATH = pathlib.Path("data/research/mobula_feasibility_report.json")
BASELINE_OFFSET_S = 3.0
INTER_REQUEST_S = 1.0
STOP_CODES = (401, 403, 429)


class Halt(RuntimeError):
    """A gate tripped. The run stops; whatever was collected is still reported."""


def _iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# --- gate 2: freeze the sample before any request ------------------------------------------------

def freeze_sample(db, count, since):
    """Pick the events ONCE and write them with a digest. Deterministic: oldest-first, no random."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = []
    for r in conn.execute(
        """SELECT fomo_event_id, mint, network_id, received_at_normalized, received_at, their_price
           FROM memecoin_events
           WHERE side='buy' AND network_id='solana' AND their_price > 0
             AND COALESCE(received_at_normalized, received_at) >= ?
           ORDER BY COALESCE(received_at_normalized, received_at)
           LIMIT ?""", (since, count)):
        rows.append({"event_id": r["fomo_event_id"], "mint": r["mint"],
                     "network_id": r["network_id"],
                     "observed_at": r["received_at_normalized"] or r["received_at"],
                     "their_price": r["their_price"]})
    conn.close()
    payload = {"frozen_at_note": "frozen before the first request; see gate 2",
               "since": since, "count": len(rows), "events": rows}
    body = json.dumps(payload, sort_keys=True, indent=1)
    payload["digest"] = hashlib.sha256(body.encode()).hexdigest()
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_PATH.write_text(json.dumps(payload, sort_keys=True, indent=1) + "\n")
    return payload


def load_sample():
    if not SAMPLE_PATH.exists():
        raise Halt(f"no frozen sample at {SAMPLE_PATH}. Run --freeze first (gate 2).")
    payload = json.loads(SAMPLE_PATH.read_text())
    stamped = payload.pop("digest", None)
    body = json.dumps(payload, sort_keys=True, indent=1)
    if hashlib.sha256(body.encode()).hexdigest() != stamped:
        raise Halt("the frozen sample changed after it was stamped (gate 2). Refusing to probe.")
    payload["digest"] = stamped
    return payload


# --- gate 1 + 6: unauthenticated request, stop on 401/403/429 -------------------------------------

# Every attempt that leaves this process, counted BEFORE the response decides anything. The
# first version incremented at the call site after `fetch` returned, so a request that raised
# (a 429, say) was never counted and the report read "requests made: 0" for a run that had in
# fact hit the endpoint. On a rate-limit-sensitive probe an undercounting counter is the one
# number you cannot afford to get wrong.
SENT = {"n": 0}


def fetch(params, timeout=15):
    """Unauthenticated GET. No cookie, no Authorization, no session anything (gate 1)."""
    SENT["n"] += 1
    query = "&".join(f"{k}={v}" for k, v in params.items())
    request = urllib.request.Request(
        f"{ENDPOINT}?{query}",
        headers={"Accept": "application/json", "User-Agent": "sniper-feasibility/1.0"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
            return {"ok": True, "status": response.status, "body": body,
                    "ms": (time.monotonic() - started) * 1000.0}
    except urllib.error.HTTPError as exc:
        if exc.code in STOP_CODES:
            raise Halt(f"HTTP {exc.code} from mobula — gate 6 stops the run immediately")
        return {"ok": False, "status": exc.code, "ms": (time.monotonic() - started) * 1000.0,
                "error": f"http {exc.code}"}
    except Exception as exc:                     # transport, timeout, malformed json
        return {"ok": False, "status": None, "ms": (time.monotonic() - started) * 1000.0,
                "error": type(exc).__name__}


# --- gate 3 + 4: describe the candles, and admit only those that end at or before the baseline ----

def parse_candles(body):
    """Normalise whatever shape comes back into [{open_ts, close, high, low, open, volume}]."""
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("time") or row.get("timestamp") or row.get("t")
        if ts is None:
            continue
        ts = float(ts)
        if ts > 1e11:                            # milliseconds
            ts /= 1000.0
        out.append({"open_ts": ts, "open": row.get("open"), "high": row.get("high"),
                    "low": row.get("low"), "close": row.get("close"),
                    "volume": row.get("volume")})
    out.sort(key=lambda c: c["open_ts"])
    return out


def infer_period_s(candles):
    """Gate 3: granularity is measured from the response, never assumed from the request."""
    if len(candles) < 3:
        return None
    gaps = [b["open_ts"] - a["open_ts"] for a, b in zip(candles, candles[1:])]
    gaps = [g for g in gaps if g > 0]
    return st.median(gaps) if gaps else None


def admissible(candles, baseline_epoch, period_s, strict=True):
    """Gate 4: a candle may price the baseline only if it ENDS at or before it.

    A candle spanning [t0+3, t0+4) contains trades after t0+3. Using its close as the price at
    t0+3 looks ahead by up to one period. With `strict` off the containing candle is allowed and
    the result is labelled LOOK-AHEAD so it can never be mistaken for a clean reading.
    """
    if period_s is None:
        return None, "unknown_period"
    ended = [c for c in candles if c["open_ts"] + period_s <= baseline_epoch + 1e-9]
    if ended:
        return ended[-1], "clean"
    if strict:
        return None, "rejected_straddles_baseline"
    containing = [c for c in candles if c["open_ts"] <= baseline_epoch]
    return (containing[-1] if containing else None), "LOOK_AHEAD"


# --- gate 5: overlap against our own clean marks --------------------------------------------------

def our_mark_at(conn, mint, at_epoch, tolerance_s=30.0):
    at = datetime.fromtimestamp(at_epoch, tz=timezone.utc)
    lo = (at - timedelta(seconds=tolerance_s)).isoformat()
    hi = (at + timedelta(seconds=tolerance_s)).isoformat()
    row = conn.execute(
        "SELECT sweep_ts, mid FROM memecoin_position_marks "
        "WHERE mint=? AND quality!='quarantined_spike' AND mid>0 AND sweep_ts BETWEEN ? AND ? "
        "ORDER BY ABS(JULIANDAY(sweep_ts) - JULIANDAY(?)) LIMIT 1",
        (mint, lo, hi, at.isoformat())).fetchone()
    return (row[1], row[0]) if row else (None, None)


def probe(args):
    sample = load_sample()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    SENT["n"] = 0
    results = []
    halted = None

    try:
        for event in sample["events"]:
            if SENT["n"] >= args.max_requests:
                break
            t0 = _iso(event["observed_at"])
            if t0 is None:
                continue
            baseline = t0.timestamp() + args.baseline_offset
            params = {"address": event["mint"], "chainId": "solana",
                      "from": int((t0.timestamp() - 120) * 1000),
                      "to": int((t0.timestamp() + 900) * 1000),
                      "period": args.period, "usd": "true"}

            first = fetch(params)
            time.sleep(INTER_REQUEST_S)
            record = {"event_id": event["event_id"], "mint": event["mint"],
                      "observed_at": event["observed_at"], "ms": round(first["ms"], 1),
                      "ok": first["ok"], "status": first["status"]}

            if not first["ok"]:
                record["error"] = first.get("error")
                results.append(record)
                continue

            candles = parse_candles(first["body"])
            period_s = infer_period_s(candles)
            record.update({
                "candles": len(candles),
                "granularity_s": period_s,                       # gate 3: measured, not assumed
                "first_candle_ts": candles[0]["open_ts"] if candles else None,
                "last_candle_ts": candles[-1]["open_ts"] if candles else None,
                # gate 3: pool identity, if the payload names one at all
                "pool_identity": (first["body"].get("pool") or first["body"].get("pair")
                                  or first["body"].get("poolAddress")
                                  if isinstance(first["body"], dict) else None),
                # the distinction correction 2 demands: an endpoint that answers is not the same
                # as an endpoint that HAD data at the event instant.
                "covers_t0": bool(candles) and candles[0]["open_ts"] <= t0.timestamp(),
            })

            chosen, verdict = admissible(candles, baseline, period_s, strict=args.strict_baseline)
            record["baseline_verdict"] = verdict                  # gate 4
            if chosen:
                record["baseline_price"] = chosen["close"]
                record["baseline_candle_end_s_before"] = round(
                    baseline - (chosen["open_ts"] + (period_s or 0)), 3)
                ours, ours_ts = our_mark_at(conn, event["mint"], baseline)  # gate 5
                if ours and chosen["close"]:
                    record["our_mark"] = ours
                    record["our_mark_ts"] = ours_ts
                    record["disagreement_pct"] = round(
                        (float(chosen["close"]) / float(ours) - 1.0) * 100.0, 2)

            if args.revision_check and SENT["n"] < args.max_requests:
                time.sleep(args.revision_delay_s)
                second = fetch(params)
                time.sleep(INTER_REQUEST_S)
                if second["ok"]:
                    again = parse_candles(second["body"])
                    record["revised"] = again != candles          # gate 3
                    record["revision_delta_candles"] = len(again) - len(candles)
            results.append(record)
    except Halt as stop:
        halted = str(stop)

    conn.close()
    return report(sample, results, SENT["n"], halted, args)


def report(sample, results, requests_sent, halted, args):
    ok = [r for r in results if r.get("ok")]
    clean = [r for r in ok if r.get("baseline_verdict") == "clean"]
    disagreements = [r["disagreement_pct"] for r in clean if "disagreement_pct" in r]
    grains = [r["granularity_s"] for r in ok if r.get("granularity_s")]
    payload = {
        "endpoint": ENDPOINT,
        "authenticated": False,                                   # gate 1
        "sample_digest": sample.get("digest"),                    # gate 2
        "requests_sent": requests_sent,
        "halted": halted,                                         # gate 6
        "strict_baseline": args.strict_baseline,                  # gate 4
        "events_probed": len(results),
        "responded": len(ok),
        "covers_event_instant": sum(1 for r in ok if r.get("covers_t0")),
        "baseline_clean": len(clean),
        "baseline_rejected_straddling": sum(
            1 for r in ok if r.get("baseline_verdict") == "rejected_straddles_baseline"),
        "granularity_s_median": st.median(grains) if grains else None,
        "latency_ms_median": st.median([r["ms"] for r in ok]) if ok else None,
        "revised_on_refetch": sum(1 for r in ok if r.get("revised")),
        "overlap_n": len(disagreements),
        "overlap_disagreement_pct_median": st.median(disagreements) if disagreements else None,
        "results": results,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")

    print(f"endpoint            : {ENDPOINT}  (unauthenticated)")
    print(f"frozen sample       : {sample.get('digest','')[:16]}…  {sample['count']} events")
    print(f"requests SENT       : {requests_sent} (cap {args.max_requests})")
    if halted:
        print(f"HALTED              : {halted}")
    print(f"responded           : {payload['responded']}/{payload['events_probed']}")
    print(f"had data at t0      : {payload['covers_event_instant']}   "
          "<- coverage NOW, which is not proof of coverage THEN")
    print(f"granularity (median): {payload['granularity_s_median']}s")
    print(f"latency ms (median) : {payload['latency_ms_median']}")
    print(f"baseline clean      : {payload['baseline_clean']}   "
          f"rejected as straddling: {payload['baseline_rejected_straddling']}")
    print(f"revised on refetch  : {payload['revised_on_refetch']}")
    print(f"overlap vs our marks: n={payload['overlap_n']} "
          f"median disagreement {payload['overlap_disagreement_pct_median']}%")
    print(f"\nfull report: {REPORT_PATH}")
    print("\nThis is a feasibility reading, not an integration. Nothing here is wired to the "
          "funded\nentry or exit path, and a clean result does not authorise wiring it.")
    return 0 if not halted else 2


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--freeze", action="store_true", help="write the frozen sample and exit")
    ap.add_argument("--probe", action="store_true", help="probe the frozen sample")
    ap.add_argument("--events", type=int, default=40)
    ap.add_argument("--since", default="2026-08-18")
    ap.add_argument("--period", default="1s")
    ap.add_argument("--baseline-offset", type=float, default=BASELINE_OFFSET_S)
    ap.add_argument("--max-requests", type=int, default=40)
    ap.add_argument("--revision-check", action="store_true", default=True)
    ap.add_argument("--revision-delay-s", type=float, default=5.0)
    ap.add_argument("--no-strict-baseline", dest="strict_baseline", action="store_false",
                    default=True, help="allow the straddling candle, labelled LOOK_AHEAD")
    args = ap.parse_args()

    if args.freeze:
        payload = freeze_sample(args.db, args.events, args.since)
        print(f"froze {payload['count']} events to {SAMPLE_PATH}")
        print(f"digest {payload['digest']}")
        print("Gate 2 satisfied. The sample cannot change before the probe.")
        return 0
    if not args.probe:
        raise SystemExit("pass --freeze first, then --probe")
    try:
        return probe(args)
    except Halt as stop:
        print(f"HALTED before any request: {stop}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
