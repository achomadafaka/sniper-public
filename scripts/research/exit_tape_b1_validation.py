#!/usr/bin/env python3
"""Exit price tape stage 3a: does a pool-state read (B1) price our own Solana exits? Read-only research.

Spec docs/superpowers/specs/2026-09-30-memecoin-exit-price-tape-design.md §4 (B1) and §4b. For every
funded Solana full exit whose coin leg is ONE PumpSwap, Raydium CP or pump.fun-curve swap, the state
our own exit tx saw just before it ran is the state a B1 read at that slot would return. From it, B1's
model predicts the sale of our exact bag:
  * PumpSwap: quote = vault quote + the Pool's virtual quote V (read now; V is fixed per pool), base =
    vault base; gross = quote * coin / (base + coin); net = gross less the event's lp+protocol+creator bps.
  * Raydium CP: the swap event's EFFECTIVE vault_before (vault minus accrued fees), less its own
    trade fee and creator fee; the RAW vault prediction is reported beside it, as the bias a naive read has.
  * pump curve: the event's virtual reserves rolled back to before our trade, less its fee bps. The
    reserves come from the event itself, so this row checks the fee arithmetic only.
Two checks per exit, each against a stated tolerance (2 points on >= 90% per venue, spec §4):
  math: predicted first-hop output vs the program's own output (exact where the model is exact);
  e2e:  predicted first-hop output in USD (SOL at the Binance 1-minute close) vs our booked proceeds.
Transactions come from the clip-ceiling study's cache; a miss is read on the research Helius key,
flat book, with a credit counter that stops at --max-credits (Alpha: 1,000).
"""
from __future__ import annotations

import argparse
import base64
import os
import collections
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
STATE = Path(os.environ.get("SNIPER_STATE_ROOT") or REPO)      # the ledger and the study's tx cache
CACHE_DIR = STATE / "output" / "research" / "solana-clip-ceiling-20260927"
TOLERANCE, PASS_SHARE, MIN_N = 0.02, 0.90, 30
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CP_SWAP_EVENT = bytes.fromhex("40c6cde8260871e2")
EXIT_KINDS = ("bracket_exit", "manual_close", "mirror_exit")


def cp_event(tx, pool_hint=None):
    """The Raydium CP SwapEvent (170 bytes) of this tx, or None."""
    import solana_clip_ceiling as S
    from sniper.research.memecoin_opportunity.decoders import base58_encode
    for line in (tx.get("meta") or {}).get("logMessages") or []:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line.split(": ", 1)[1])
        except ValueError:
            continue
        if raw[:8] != CP_SWAP_EVENT or len(raw) < 170:
            continue
        u = lambda o: int.from_bytes(raw[o:o + 8], "little")          # noqa: E731
        return {"pool": base58_encode(raw[8:40]), "in_before": u(40), "out_before": u(48),
                "in_amount": u(56), "out_amount": u(64), "in_transfer_fee": u(72),
                "out_transfer_fee": u(80), "base_input": bool(raw[88]),
                "in_mint": base58_encode(raw[89:121]), "out_mint": base58_encode(raw[121:153]),
                "trade_fee": u(153), "creator_fee": u(161), "creator_fee_on_input": bool(raw[169])}
    return None


def cp_out(in_before, out_before, ev):
    """Raydium CP exact-input output from the given reserves and the event's own fees. `in_amount` is
    already net of a Token-2022 transfer fee: subtracting `in_transfer_fee` again ran 1% low (131 exits)."""
    net = ev["in_amount"] - ev["trade_fee"]
    if ev["creator_fee_on_input"]:
        net -= ev["creator_fee"]
    out = out_before * net // (in_before + net)
    if not ev["creator_fee_on_input"]:
        out -= ev["creator_fee"]
    return out


def predict(tx, mint, v_by_pool):
    """{venue, pred, actual, qmint, ...} for our exit's one coin leg, or {'censor': reason}."""
    import solana_clip_ceiling as S
    d = S.decode_leg(tx, mint, "sell")
    if d.get("censor"):
        return {"censor": d["censor"]}
    if len(d["legs"]) != 1:
        return {"censor": "multi_leg"}
    leg = d["legs"][0]
    venue = leg["venue"]
    if venue == "pumpswap":
        user = S._keys(tx)[0]
        (e,) = [x for x in S._amm_events(tx, user) if x["pool"] == leg["pool"]][:1] or [None]
        v = v_by_pool.get(leg["pool"])
        if e is None or v is None:
            return {"censor": "pumpswap_v_unknown" if e else "pumpswap_event_missing"}
        x, y, coin = e["pool_quote"] + v, e["pool_base"], e["base"]
        gross = x * coin // (y + coin)
        fee_bps = e["lp_bps"] + e["proto_bps"] + e["creator_bps"]
        return {"venue": venue, "pred": gross - gross * fee_bps // 10_000, "actual": e["user_quote_amount"],
                "pred_gross": gross, "actual_gross": e["quote"], "qmint": v_by_pool.get("quote_mint:" + leg["pool"]),
                "fee_bps": fee_bps}
    if venue == "raydium_cp":
        ev = cp_event(tx)
        if ev is None or ev["in_mint"] != mint:
            return {"censor": "cp_event_missing"}
        return {"venue": venue, "pred": cp_out(ev["in_before"], ev["out_before"], ev), "actual": ev["out_amount"],
                "pred_raw": cp_out(leg["y"], leg["x"], ev), "qmint": ev["out_mint"],
                "accrued_quote": leg["x"] - ev["out_before"], "accrued_coin": leg["y"] - ev["in_before"]}
    if venue == "pump_curve":
        e = S._curve_events(tx, mint, S._keys(tx)[0])[0]
        vs, vt = e["v_sol"] + e["sol_amount"], e["v_tok"] - e["token_amount"]   # before our sell
        gross = vs * e["token_amount"] // (vt + e["token_amount"])
        return {"venue": venue, "pred": gross - gross * e["fee_bps"] // 10_000, "actual": None,
                "pred_gross": gross, "actual_gross": e["sol_amount"], "qmint": WSOL, "fee_bps": e["fee_bps"]}
    return {"censor": "venue_" + venue}


_PX = {}


def sol_usd(t):
    m = int(t // 60) * 60
    if m not in _PX:
        url = "https://api.binance.com/api/v3/klines?symbol=SOLUSDT&interval=1m&startTime=%d&limit=1" % (m * 1000)
        with urllib.request.urlopen(url, timeout=10) as r:
            _PX[m] = float(json.loads(r.read())[0][4])
    return _PX[m]


def verdict(errs):
    errs = [e for e in errs if e is not None]
    within = sum(1 for e in errs if abs(e) <= TOLERANCE)
    q = sorted(errs)
    pct = lambda p: q[int(p * (len(q) - 1))] if q else None              # noqa: E731
    return {"n": len(errs), "within_2pts": within, "share": round(within / len(errs), 3) if errs else None,
            "p10": pct(0.1), "p50": pct(0.5), "p90": pct(0.9),
            "pass": bool(errs) and len(errs) >= MIN_N and within / len(errs) >= PASS_SHARE}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01")
    ap.add_argument("--max-credits", type=int, default=1000)
    ap.add_argument("--fetch-missing", action="store_true", help="read uncached exit txs on the research key")
    ap.add_argument("--out", default=str(STATE / "logs" / "acceptance" / "exit_tape_b1_validation.jsonl"))
    args = ap.parse_args()
    import solana_clip_ceiling as S
    S.OUT, S.TX_DIR = CACHE_DIR, CACHE_DIR / "tx"
    from sniper.util.envfiles import load_env_files
    load_env_files(str(STATE))
    v_by_pool = S._v_by_pool()
    led = sqlite3.connect("file:%s?mode=ro" % (STATE / "sniper_tracker.db"), uri=True)
    rows = led.execute(
        "SELECT f.id, f.tx_sig, p.mint, f.kind, f.notional, f.created_at, p.id FROM memecoin_fills f"
        " JOIN memecoin_positions p ON p.id = f.position_id WHERE p.execution_mode = 'real'"
        " AND p.network_id = 'solana' AND f.kind IN (?, ?, ?) AND f.tx_sig IS NOT NULL"
        " AND f.created_at >= ? ORDER BY f.id", (*EXIT_KINDS, args.since)).fetchall()
    credits = 0
    out = []
    for fid, sig, mint, kind, notional, at, pid in rows:
        tx = S.load_tx(sig)
        if tx is None and args.fetch_missing:
            if credits + 1 > args.max_credits:
                print("STOP: credit budget %d reached" % args.max_credits, flush=True)
                break
            import funded_exit_truth as fet
            from slot_minout_grading import wait_flat
            from sniper.persistence.db_core import get_readonly_connection
            wait_flat(lambda: get_readonly_connection(str(STATE / "sniper_tracker.db")))
            tx = fet.rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                                  "commitment": "confirmed"}])
            credits += 1
            if tx:
                S.tx_path(sig).write_text(json.dumps(tx))
        row = {"fill_id": fid, "position_id": pid, "sig": sig, "kind": kind, "booked_usd": notional, "at": at}
        if tx is None:
            out.append(dict(row, censor="tx_unread")); continue
        p = predict(tx, mint, v_by_pool)
        row.update(p)
        if not p.get("censor"):
            q = p.get("qmint")
            usd = 1.0 if q == USDC else (sol_usd(tx["blockTime"]) if q == WSOL else None)
            dec = 6 if q == USDC else 9
            if p.get("actual") is not None:
                row["math_err"] = p["pred"] / p["actual"] - 1 if p["actual"] else None
            if p.get("actual_gross") is not None and p.get("pred_gross") is not None and p["actual_gross"]:
                row["math_gross_err"] = p["pred_gross"] / p["actual_gross"] - 1
            if p.get("pred_raw") is not None and p["actual"]:
                row["raw_vault_err"] = p["pred_raw"] / p["actual"] - 1
            if usd is not None and notional:
                row["b1_usd"] = p["pred"] / 10 ** dec * usd
                row["e2e_err"] = row["b1_usd"] / notional - 1
            else:
                row["censor_e2e"] = "quote_mint_%s" % (q or "unknown")
        out.append(row)
    Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in out))
    by = collections.defaultdict(list)
    for r in out:
        by[r.get("venue") or ("censor:" + str(r.get("censor")))].append(r)
    report = {"credits": credits, "rows": len(out)}
    for venue, rs in sorted(by.items()):
        if venue.startswith("censor:"):
            report[venue] = len(rs); continue
        report[venue] = {"e2e": verdict([r.get("e2e_err") for r in rs]),
                         "math": verdict([r.get("math_err") for r in rs]),
                         "math_gross": verdict([r.get("math_gross_err") for r in rs]),
                         "raw_vault": verdict([r.get("raw_vault_err") for r in rs])}
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
