#!/usr/bin/env python3
"""Prove the Base fast lane's buy templates on Base itself, read-only (step 1 plan 2026-10-14,
part D). Run once by Alpha before MEMECOIN_BASE_FAST_LANE=runner_shadow.

For each route kind (v4_native, v4_weth), the lane's own builder (memecoin_rh_fast_lane.lane_buy_tx,
chain 8453) builds a $1 buy of a real coin from the step-0 decisions, at that coin's own pool key.
Two eth_calls on the lane's host (MEMECOIN_BASE_FAST_LANE_RPC_URL), from the funded address with the
clip as value:
- with a minimum output of 1 atom, the call must succeed (the template executes on Base);
- with an impossible minimum output, the same call must revert (the swap really ran and its
  slippage check is wired).
A kind passes only when both hold. eth_call spends nothing and signs nothing; 4 calls in all,
spaced 1 s, stopping at the first transport error. Exit 0 when every kind asked for passes.

    python3 scripts/research/base_fast_template_proof.py --taker 0x898b... [--kinds v4_native,v4_weth]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CLIP_USD = 1.0
IMPOSSIBLE_OUT = 1 << 127          # more atoms than any coin's supply, inside V4's uint128 minimum


def latest_decision(db_path, kind):
    """The newest step-0 would_send of `kind`, or None: (record dict)."""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
    try:
        row = conn.execute("SELECT record_json FROM base_fast_decisions WHERE reason='would_send' "
                           "AND route_kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def build(rec, taker, *, min_out_one):
    """(the unsigned buy, clip wei) by the lane's own builder, at a $1 clip and the record's key."""
    from fractions import Fraction
    from sniper.services.memecoin_rh_fast_lane import lane_buy_tx
    clip_wei = int(Fraction(str(CLIP_USD)) / Fraction(str(rec["eth_usd"])) * 10 ** 18)
    key = rec["key"]
    # A cap of exactly clip wei per coin atom makes min_out 1; the other cap makes it IMPOSSIBLE_OUT.
    cap_num, cap_den = (clip_wei, 1) if min_out_one else (1, IMPOSSIBLE_OUT // clip_wei)
    payload = {"route_kind": rec["route_kind"], "key": key, "mint": rec["mint"],
               "cap_num": cap_num, "cap_den": cap_den,
               "in_is_c0": str(key["currency0"]).lower() == str(rec.get("x") or "").lower()}
    tx, min_out = lane_buy_tx(payload, clip_wei, int(time.time()) + 600, taker, chain="8453")
    return tx, clip_wei, min_out


def eth_call(url, taker, tx, value, http):
    """(succeeded, detail): an answered call or an answered revert; a transport failure raises."""
    from sniper.execution.evm_wallet import _rpc
    answer = _rpc(url, "eth_call", [{"from": taker, "to": tx["to"], "data": tx["data"],
                                     "value": hex(int(value)), "gas": hex(int(tx["gas"]))}, "latest"],
                  http=http)
    if not isinstance(answer, dict):
        raise RuntimeError("no answer")
    if "result" in answer and answer.get("error") is None:
        return True, "ok"
    error = answer.get("error") or {}
    message = str(error.get("message") if isinstance(error, dict) else error)
    if "revert" not in message.lower() and not (isinstance(error, dict) and error.get("data")):
        raise RuntimeError("not a revert: %s" % message[:120])
    return False, message[:160]


def main(argv=None):
    from sniper.util.state_paths import state_path
    from sniper.settings import memecoin as cfg
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--taker", required=True, help="the funded 8453 address (never a key)")
    p.add_argument("--kinds", default="v4_native,v4_weth")
    p.add_argument("--url", default=None, help="default: MEMECOIN_BASE_FAST_LANE_RPC_URL")
    p.add_argument("--decisions", default=str(state_path("data", "base_fast_decisions.db")))
    args = p.parse_args(argv)
    url = args.url or cfg.memecoin_base_fast_lane_rpc_url()
    if not url.startswith("https://"):
        print(json.dumps({"verdict": "no_lane_host"}))
        return 2
    from sniper.execution.evm_wallet import _http
    taker = args.taker.lower()
    out, ok_all = {}, True
    for kind in [k.strip() for k in args.kinds.split(",") if k.strip()]:
        rec = latest_decision(args.decisions, kind)
        if rec is None:
            out[kind] = {"verdict": "no_step0_coin"}
            ok_all = False
            continue
        row = {"mint": rec["mint"], "pool": rec["pool"], "decided_at": rec.get("decided_at")}
        try:
            tx, clip, min_out = build(rec, taker, min_out_one=True)
            row.update(to=tx["to"], clip_wei=str(clip), min_out=str(min_out))
            row["min_out_one"] = eth_call(url, taker, tx, clip, _http)
            time.sleep(1.0)
            tx, clip, min_out = build(rec, taker, min_out_one=False)
            row["min_out_impossible"] = eth_call(url, taker, tx, clip, _http)
            time.sleep(1.0)
        except Exception as exc:                        # transport: stop, never a verdict
            row["verdict"] = "transport_error:%s" % type(exc).__name__
            out[kind] = row
            ok_all = False
            break
        passed = row["min_out_one"][0] is True and row["min_out_impossible"][0] is False
        row["verdict"] = "pass" if passed else "fail"
        ok_all = ok_all and passed
        out[kind] = row
    print(json.dumps(out, indent=1, default=str))
    return 0 if ok_all else 1


if __name__ == "__main__":
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))                # the operator's .env, never in a library call
    sys.exit(main())
