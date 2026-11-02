#!/usr/bin/env python3
"""Compare Jupiter Ultra and Swap quote responses on coins we refuse.

WHY THIS EXISTS. On 2026-09-24 DegenCapitalLLC's RUNE buy was refused by the round-trip cap at
19.40%. Their transaction, decoded on the research key, ran DFlow and bought 29.1M RUNE from a
market maker's WALLET rather than from any pool, paying in GP -- RUNE's quote asset is itself a
memecoin, so every AMM route we can build is two hops through a $20,634 pool. They filled at
8.8465e-05 per token on $2,500 while our quote at the same instant was 1.19574e-04 on $165.43:
26% cheaper on fifteen times the size, which is backwards for the same path and is what proves it
was not the same path. FIREMAKING was the same shape through the same GP intermediate.

Whether Ultra reaches maker liquidity is an empirical question, and the one sample taken by hand says no: on RUNE
it returned router `metis` with an AMM route, no better price, and took 274 ms against swap/v1's
61 ms. RFQ buys price and costs entry latency; a single sample cannot price that trade.

WHAT THIS IS NOT. It never runs on the entry path, writes no ledger row, creates no table and
decides nothing. It reads refusals that have already happened and asks both endpoints what they
would have offered, at the same instant, for the same mint and size.

READING IT LATER. The denominator is refusals WE produced, so a share here describes our own gate
and our own router, never the market. An endpoint that errors is `unknown`, never "did not win" --
collapsing those two is how a failure becomes evidence for the thing that failed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sniper.util.clock import real_utc_now

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# THE KEYED PORTAL, not `lite-api`. We already hold three Jupiter keys and the runner reports
# `keyed: true` in its heartbeat, so the keyless 0.5 RPS / 30 RPM per-IP bucket was never the
# constraint. Runtime key equality is checked before sampling; names alone do not prove quota
# separation. Every row records paper-key sharing only when the loaded keys actually match.
SWAP_URL = "https://api.jup.ag/swap/v1/quote"
ULTRA_URL = "https://api.jup.ag/ultra/v1/order"
KEY_VAR = "MEMECOIN_CAPTURE_JUPITER_API_KEY"
SHARES_KEY_WITH = "MEMECOIN_PAPER_JUPITER_API_KEY"
# publicnode refuses Python's default User-Agent with a 403 and the Jupiter hosts throttle it;
# every research call in this repo sends a browser-shaped agent for the same reason.
BASE_HEADERS = {"User-Agent": "curl/8.5.0", "Accept": "application/json"}


def _headers(api_key):
    head = dict(BASE_HEADERS)
    if api_key:
        head["x-api-key"] = api_key
    return head


def _get(url, api_key, timeout=15.0):
    """(payload, error, latency_ms). An error is TYPED and never returns a payload."""
    started = time.perf_counter()
    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers=_headers(api_key)), timeout=timeout).read()
        return json.loads(raw), None, (time.perf_counter() - started) * 1000.0
    except urllib.error.HTTPError as exc:
        return None, "http_%d" % exc.code, (time.perf_counter() - started) * 1000.0
    except urllib.error.URLError as exc:
        return None, "transport:%s" % type(exc.reason).__name__, (time.perf_counter() - started) * 1000.0
    except Exception as exc:
        return None, "error:%s" % type(exc).__name__, (time.perf_counter() - started) * 1000.0


def _route_labels(payload):
    return [((leg or {}).get("swapInfo") or {}).get("label")
            for leg in (payload.get("routePlan") or [])]


def _valid_out(payload, mint, amount_atomic):
    if not isinstance(payload, dict) or payload.get("inputMint") != USDC \
            or payload.get("outputMint") != mint:
        return None
    plan = payload.get("routePlan")
    if plan is not None and (not isinstance(plan, list) or any(
            not isinstance(leg, dict) or not isinstance(leg.get("swapInfo"), dict)
            for leg in plan)):
        return None
    if payload.get("platformFee") is not None and not isinstance(payload["platformFee"], dict):
        return None
    try:
        raw_in = payload.get("inAmount")
        if isinstance(raw_in, bool) or not str(raw_in).isdigit() \
                or int(raw_in) != amount_atomic:
            return None
        out = payload.get("outAmount")
        if isinstance(out, bool) or not str(out).isdigit():
            return None
        value = int(out)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def quote_swap(mint, amount_atomic, slippage_bps, api_key):
    url = "%s?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d" % (
        SWAP_URL, USDC, mint, amount_atomic, slippage_bps)
    payload, err, ms = _get(url, api_key)
    if err:
        return {"endpoint": "swap/v1", "error": err, "latency_ms": ms}
    out = _valid_out(payload, mint, amount_atomic)
    if out is None:
        return {"endpoint": "swap/v1", "error": "invalid_quote", "latency_ms": ms}
    return {"endpoint": "swap/v1", "out_atomic": out, "latency_ms": ms,
            "router": "metis", "legs": _route_labels(payload)}


def quote_ultra(mint, amount_atomic, slippage_bps, api_key):
    # `ultra/v1/order` takes no slippage argument -- it owns that decision. The parameter is
    # accepted here so both callers have one shape, and it is recorded, never sent.
    url = "%s?inputMint=%s&outputMint=%s&amount=%d" % (ULTRA_URL, USDC, mint, amount_atomic)
    payload, err, ms = _get(url, api_key)
    if err:
        return {"endpoint": "ultra/v1", "error": err, "latency_ms": ms}
    out = _valid_out(payload, mint, amount_atomic)
    if out is None:
        return {"endpoint": "ultra/v1", "error": "invalid_quote", "latency_ms": ms}
    router = payload.get("router") or payload.get("swapType")
    legs = _route_labels(payload)
    return {"endpoint": "ultra/v1", "out_atomic": out, "latency_ms": ms,
            "router": router, "legs": legs, "route_type": ultra_route_type(router, legs),
            "fee_bps": ultra_fee_bps(payload),
            "raw_fee_bps": payload.get("feeBps"),
            "raw_platform_fee_bps": ((payload.get("platformFee") or {}) or {}).get("feeBps"),
            "ultra_slippage_bps": payload.get("slippageBps")}


def ultra_fee_bps(payload):
    """Largest declared fee field; no claim about how it relates to outAmount."""
    values = [payload.get("feeBps"), ((payload.get("platformFee") or {}) or {}).get("feeBps")]
    numeric = []
    for value in values:
        try:
            fee = float(value) if not isinstance(value, bool) else float("nan")
            if math.isfinite(fee) and fee >= 0:
                numeric.append(fee)
        except (TypeError, ValueError):
            pass
    return max(numeric) if numeric else None


def ultra_route_type(router, legs):
    """An explicit AMM route, or unknown. Router names do not prove a maker fill.
    """
    if legs:
        return "amm_fallback"
    return "unknown"


def compare(mint, amount_atomic, slippage_bps, swap_first, api_key):
    """Both endpoints, back to back, with the order ALTERNATED by the caller.

    Whichever runs first sees an older book, so a fixed order would hand one side a systematic
    edge on a coin that is moving -- which is every coin this samples. The order and the gap
    between the two calls are recorded so a reader can bound that bias instead of assuming it away.
    """
    first, second = (quote_swap, quote_ultra) if swap_first else (quote_ultra, quote_swap)
    t0 = time.perf_counter()
    a = first(mint, amount_atomic, slippage_bps, api_key)
    gap_ms = (time.perf_counter() - t0) * 1000.0
    b = second(mint, amount_atomic, slippage_bps, api_key)
    by = {a["endpoint"]: a, b["endpoint"]: b}
    swap, ultra = by["swap/v1"], by["ultra/v1"]

    # DECIMAL-FREE ON PURPOSE. Both sides return the same token's atomic amount for the same
    # input, so their RATIO needs no decimals. Fee fields are recorded, but these responses do not
    # establish whether outAmount is already net of fees; no net winner is assigned.
    verdict, edge, net_edge = verdict_with_fee(swap, ultra)
    return {"first_called": "swap/v1" if swap_first else "ultra/v1",
            "call_gap_ms": gap_ms, "swap": swap, "ultra": ultra,
            "ultra_edge_pct": edge, "ultra_net_edge_pct": net_edge, "verdict": verdict,
            "slippage_bps_swap_only": slippage_bps}


class PairBudget:
    """Refuses to exceed a stated pairs-per-minute rate. Two requests make one pair."""

    def __init__(self, pairs_per_min, clock=time.monotonic):
        self.min_interval_s = 60.0 / float(pairs_per_min) if pairs_per_min > 0 else float("inf")
        self.clock = clock
        self.last = None

    def wait_s(self):
        if self.last is None:
            return 0.0
        return max(0.0, self.min_interval_s - (self.clock() - self.last))

    def spend(self):
        self.last = self.clock()


def recent_refusals(db_conn, since_iso, limit):
    """Solana refusals with a clip we can re-quote. `aborted` counts: some refusals carry it."""
    rows = db_conn.execute(
        "SELECT attempt_id, mint, ticker, terminal_reason, decision_started_at,"
        "       funded_contract_json"
        "  FROM memecoin_entry_attempts"
        " WHERE network_id='solana' AND decision_started_at >= ?"
        "   AND terminal_status IN ('refused','aborted') AND mint IS NOT NULL"
        " ORDER BY decision_started_at DESC LIMIT ?", (since_iso, limit)).fetchall()
    out = []
    for row in rows:
        clip = None
        try:
            sizing = (json.loads(row["funded_contract_json"]) or {}).get("sizing") or {}
            clip = sizing.get("approved_clip_usd")
        except Exception:
            clip = None
        out.append({"attempt_id": row["attempt_id"], "mint": row["mint"],
                    "ticker": row["ticker"], "reason": row["terminal_reason"],
                    "refused_at": row["decision_started_at"], "clip_usd": clip})
    return out


def selftest():
    """Asserts on the functions `compare` actually calls, never on a local restatement of them.

    The previous version carried its own `verdict()` helper implementing the retired fee rule and
    asserted on THAT, so those assertions proved a fixture while `compare` had moved on (6e68de1a).
    """
    ok = {"endpoint": "swap/v1", "out_atomic": 1000, "latency_ms": 1.0}
    better = {"endpoint": "ultra/v1", "out_atomic": 1100, "latency_ms": 1.0}
    dead = {"endpoint": "ultra/v1", "error": "http_429", "latency_ms": 1.0}

    # A 429 from EITHER side is unknown, never "the other tier won".
    assert verdict_with_fee(ok, dead) == ("unknown", None, None)
    assert verdict_with_fee(dead, ok) == ("unknown", None, None)
    # Raw edge only: no winner is declared until the fee's accounting basis is verified.
    v, raw, net = verdict_with_fee(ok, better)
    assert v == "unknown" and abs(raw - 10.0) < 1e-9 and net is None, (v, raw, net)

    # IDENTITY BEFORE ARITHMETIC. A quote for a different mint, a different input, a different
    # input amount, or no positive output is not an answer to the question that was asked, and
    # comparing it would produce a plausible wrong ratio.
    mint, amount = "MINT111", 1_000_000
    good = {"inputMint": USDC, "outputMint": mint, "inAmount": str(amount),
            "outAmount": "5000", "routePlan": []}
    assert _valid_out(good, mint, amount) == 5000
    assert _valid_out({**good, "outputMint": "OTHERMINT"}, mint, amount) is None, "wrong mint"
    assert _valid_out({**good, "inputMint": "SOL"}, mint, amount) is None, "wrong input"
    assert _valid_out({**good, "inAmount": str(amount - 1)}, mint, amount) is None, "wrong amount"
    assert _valid_out({**good, "outAmount": "0"}, mint, amount) is None, "no output"
    assert _valid_out({**good, "outAmount": True}, mint, amount) is None, "bool is not a count"
    assert _valid_out({**good, "routePlan": ["leg"]}, mint, amount) is None, "malformed plan"

    assert ultra_route_type("metis", ["Meteora DLMM"]) == "amm_fallback"
    assert ultra_route_type("metis", []) == "unknown"
    assert ultra_fee_bps({"feeBps": 10, "platformFee": {"feeBps": 10}}) == 10, "MAX, never the sum"
    assert ultra_fee_bps({}) is None
    print("selftest ok")


def verdict_with_fee(swap, ultra):
    """Raw quote ratio only; fee-adjusted edge needs a verified accounting basis."""
    if swap.get("error") or ultra.get("error"):
        return "unknown", None, None
    raw = 100.0 * (ultra["out_atomic"] / swap["out_atomic"] - 1.0)
    return "unknown", raw, None


# Below this many gradable rows the report lists values instead of quoting percentiles.
MIN_PCTL = 10
_NUM = re.compile(r"[\d][\d,.]*")


def _family(reason):
    """The refusal FAMILY, with its numbers removed.

    Every mcap refusal carries its own market cap, so the raw string groups into buckets of one --
    "mcap $1,548,771 over ceiling $250,000" and "mcap $1,617,786 over ceiling $250,000" are the
    same gate and were counted as two families in the first run.
    """
    return _NUM.sub("#", str(reason or "?")).split(" (")[0][:46]


def report(path):
    """Distribution over whatever has been sampled. Every share names its denominator."""
    rows = []
    lines = Path(path).read_text(encoding="utf-8").splitlines() if Path(path).exists() else []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    pairs = [r for r in rows if r.get("verdict") and "swap" in r]
    stops = [r for r in rows if r.get("event") == "rate_limit_stop"]
    print("pairs %d   rate-limit stops %d   file %s" % (len(pairs), len(stops), path))
    if not pairs:
        return 0
    first = min(r["sampled_at"] for r in pairs)
    last = max(r["sampled_at"] for r in pairs)
    print("window %s .. %s" % (first[:19], last[:19]))
    graded = [r for r in pairs if r.get("ultra_edge_pct") is not None]
    print("comparable raw quotes %d of %d; net fee accounting unverified"
          % (len(graded), len(pairs)))
    if not graded:
        return 0
    raw_win = sum(1 for r in graded if (r.get("ultra_edge_pct") or 0.0) > 0)
    print("ultra higher outAmount %d of %d (%.0f%%)" % (raw_win, len(graded),
                                                         100.0 * raw_win / len(graded)))
    print("RFQ fill identity unavailable from quote router fields")
    # PERCENTILES ONLY ONCE THEY MEAN SOMETHING. At n=2 the p90 index lands below the p50 index
    # and the report printed a p90 SMALLER than its p50 -- a number that reads as a broken
    # instrument. Under MIN_PCTL rows the values are listed instead.
    for name, key in (("raw edge %", "ultra_edge_pct"),):
        vals = sorted(r[key] for r in graded if r.get(key) is not None)
        if not vals:
            continue
        if len(vals) < MIN_PCTL:
            print("  %-10s n=%d: %s" % (name, len(vals), ", ".join("%+.3f" % v for v in vals)))
        else:
            print("  %-10s p10 %+.3f  p50 %+.3f  p90 %+.3f"
                  % (name, vals[int(0.1 * (len(vals) - 1))], vals[len(vals) // 2],
                     vals[int(0.9 * (len(vals) - 1))]))
    for side in ("swap", "ultra"):
        lat = sorted(r[side]["latency_ms"] for r in graded if r[side].get("latency_ms"))
        if not lat:
            continue
        if len(lat) < MIN_PCTL:
            print("  %-6s latency n=%d: %s" % (side, len(lat), ", ".join("%.0f" % v for v in lat)))
        else:
            print("  %-6s latency p50 %.0f ms  p90 %.0f ms"
                  % (side, lat[len(lat) // 2], lat[int(0.9 * (len(lat) - 1))]))
    fams = {}
    for r in graded:
        fam = _family(r.get("refusal_reason"))
        cell = fams.setdefault(fam, [0, 0])
        cell[0] += 1
        cell[1] += 1 if (r.get("ultra_edge_pct") or 0) > 0 else 0
    print("by refusal family (n, ultra higher raw outAmount):")
    for fam, (n, win) in sorted(fams.items(), key=lambda kv: -kv[1][0]):
        print("  %-48s n %3d  raw higher %d" % (fam, n, win))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="data/ultra_vs_swap_shadow.jsonl",
                    help="append-only JSONL; this script never writes to the ledger")
    ap.add_argument("--lookback-min", type=float, default=30.0)
    ap.add_argument("--pairs-per-min", type=float, default=30.0,
                    help="hard ceiling; two requests make one pair")
    ap.add_argument("--max-pairs", type=int, default=200, help="stop after this many pairs")
    ap.add_argument("--max-hours", type=float, default=24.0, help="stop after this long")
    ap.add_argument("--poll-s", type=float, default=60.0, help="between ledger looks")
    ap.add_argument("--rate-limit-stop-s", type=float, default=60.0)
    ap.add_argument("--slippage-bps", type=int, default=300)
    ap.add_argument("--report", action="store_true", help="summarise the JSONL and exit")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.report:
        return report(args.out)

    from sniper.persistence.db_core import get_readonly_connection

    # LOAD .env HERE, never from the caller's shell. A settings read without it silently returns
    # the CODE DEFAULT, and passing the key on a command line would publish it in `ps`.
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO / ".env")
    except Exception:
        pass
    api_key = (os.getenv(KEY_VAR) or "").strip()
    if not api_key:
        print("%s is empty in this process -- load .env before running, or the request goes out "
              "KEYLESS on the bucket the funded lane shares" % KEY_VAR, file=sys.stderr)
        return 2
    funded_key = (os.getenv("MEMECOIN_JUPITER_API_KEY") or "").strip()
    if not funded_key or api_key == funded_key:
        print("Research/funded Jupiter key separation is unverified; refusing to sample",
              file=sys.stderr)
        return 2
    paper_key = (os.getenv(SHARES_KEY_WITH) or "").strip()
    shared_key_var = SHARES_KEY_WITH if paper_key and api_key == paper_key else None

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    pairs = 0
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("attempt_id"):
                seen.add(row["attempt_id"])
                if row.get("verdict") and "swap" in row:
                    pairs += 1

    budget = PairBudget(args.pairs_per_min)
    deadline = time.monotonic() + args.max_hours * 3600.0
    print("sampling on %s (paper key %s; funded key distinct), "
          "cap %.0f pairs/min, stop at %d pairs or %.1f h"
          % (KEY_VAR, "shared" if shared_key_var else "distinct or unavailable",
             args.pairs_per_min, args.max_pairs, args.max_hours),
          flush=True)

    def emit(row):
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    while pairs < args.max_pairs and time.monotonic() < deadline:
        since = (datetime.fromtimestamp(real_utc_now(), timezone.utc)
                 - timedelta(minutes=args.lookback_min)).isoformat()
        # closing(), because get_readonly_connection returns a BARE connection and `with` on a
        # sqlite3 connection commits without closing -- a reader held open across network I/O
        # pins the WAL read-mark on the funded ledger.
        with contextlib.closing(get_readonly_connection()) as conn:
            conn.row_factory = sqlite3.Row
            candidates = recent_refusals(conn, since, args.max_pairs)
        fresh = [c for c in candidates if c["attempt_id"] not in seen]
        for cand in fresh:
            if pairs >= args.max_pairs or time.monotonic() >= deadline:
                break
            wait = budget.wait_s()
            if wait > 0:
                time.sleep(wait)
            clip = cand["clip_usd"]
            try:
                amount_atomic = int(round(float(clip) * 1_000_000)) if clip is not None else 0
            except (TypeError, ValueError, OverflowError):
                amount_atomic = 0
            if amount_atomic <= 0:
                emit({"event": "unquotable_amount", "attempt_id": cand["attempt_id"],
                      "clip_usd": clip, "reason": "missing_or_nonpositive_approved_clip"})
                seen.add(cand["attempt_id"])
                continue
            row = {"sampled_at": datetime.fromtimestamp(real_utc_now(), timezone.utc).isoformat(),
                   "key_var": KEY_VAR, "key_shared_with": shared_key_var,
                   "attempt_id": cand["attempt_id"], "mint": cand["mint"],
                   "ticker": cand["ticker"], "refusal_reason": cand["reason"],
                   "refused_at": cand["refused_at"], "clip_usd": clip,
                   "amount_atomic": amount_atomic,
                   **compare(cand["mint"], amount_atomic, args.slippage_bps,
                             swap_first=(pairs % 2 == 0), api_key=api_key)}
            budget.spend()
            emit(row)
            seen.add(cand["attempt_id"])
            pairs += 1
            print("%s %-10s $%.2f %-7s raw %s net %s  swap %.0fms ultra %.0fms %s"
                  % (row["sampled_at"][11:19], cand["ticker"] or "?", clip, row["verdict"],
                     "n/a" if row["ultra_edge_pct"] is None else "%+.3f%%" % row["ultra_edge_pct"],
                     "n/a" if row["ultra_net_edge_pct"] is None
                     else "%+.3f%%" % row["ultra_net_edge_pct"],
                     row["swap"].get("latency_ms") or -1, row["ultra"].get("latency_ms") or -1,
                     row["ultra"].get("route_type") or row["ultra"].get("error") or ""),
                  flush=True)

            # A 429 FROM EITHER SIDE STOPS THE LANE, and the stop is a ROW. A pause that leaves no
            # trace reads afterwards as a quiet market rather than as us being throttled, and the
            # gap it makes in the sample is invisible.
            hit = [r.get("error") for r in (row["swap"], row["ultra"]) if r.get("error")]
            if any(str(e).startswith("http_429") for e in hit):
                emit({"sampled_at": datetime.fromtimestamp(real_utc_now(), timezone.utc).isoformat(),
                      "event": "rate_limit_stop", "stopped_s": args.rate_limit_stop_s,
                      "after_attempt_id": cand["attempt_id"], "errors": hit})
                print("429 -- stopping %.0fs" % args.rate_limit_stop_s, flush=True)
                time.sleep(args.rate_limit_stop_s)
        if pairs < args.max_pairs and time.monotonic() < deadline:
            time.sleep(args.poll_s)

    print("done: %d pairs" % pairs, flush=True)
    return report(args.out)


if __name__ == "__main__":
    sys.exit(main() or 0)
