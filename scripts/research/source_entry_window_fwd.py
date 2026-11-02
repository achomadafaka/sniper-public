#!/usr/bin/env python3
"""Forward pass for the source-entry study: for rows that pass the Amendment-2 support filter (curve incomplete at tc, mcap <= 40k USD at tc,
age = tc - t0 <= 1800 s) add swaps_fwd = swaps in (floor(tc), floor(tc)+1900] from the curve PDA, newest-first. Rows on UTC 10-21..10-24 take the
forward path from our own tape (source=tape, no calls). fwd_complete is true only if paging reached a swap at or before floor(tc) (or the PDA's start)
without hitting the page cap; tape rows are complete only if the tape covers tc+1900. Others are censored, never filled.
    python3 scripts/research/source_entry_window_fwd.py IN.jsonl OUT.jsonl"""
import json
import sqlite3
import sys
from pathlib import Path

import base58
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_entry_window as W  # noqa: E402

HORIZON_S, FX0, MCAP_MAX, AGE_MAX = 1900, 104.329875, 40000.0, 1800.0
TAPE_DB = "/home/bigpapi/Desktop/Sniper/data/desk/swaps/2026-10-%02d.db"
TAPE_DAYS = {21: None, 22: None, 23: None, 24: 1792845278}      # day -> last tape second (10-24 partial: host down 12:34Z), None = full UTC day
COMPACT = ("slot", "idx", "bt", "buy", "qa", "ta", "actor", "vq", "vb", "rt", "fee_bps", "cfee_bps", "sig")


def in_support(r):
    ls = r.get("last_state")
    if r.get("reason") not in (None, "no_swaps_in_window") or not ls or r.get("t0") is None or ls["rt"] <= 0:
        return None
    mcap, age = ls["vq"] / ls["vb"] * 1e6 * FX0, r["tc"] - r["t0"]
    return (mcap <= MCAP_MAX and age <= AGE_MAX), mcap, age


def tape_forward(mint, tc):
    day = int(tc // 86400)
    d0 = int(datetime.fromtimestamp(tc, timezone.utc).strftime("%d"))
    lo, hi = int(tc), int(tc) + HORIZON_S
    out, end_ok = [], True
    for d in (d0, d0 + 1):
        if d not in TAPE_DAYS:
            if d == d0 + 1 and hi >= (day + 1) * 86400:
                end_ok = False                                     # window runs into a day we do not have a tape for
            continue
        db = sqlite3.connect("file:%s?mode=ro" % (TAPE_DB % d), uri=True)
        cols = {c[1] for c in db.execute("pragma table_info(swaps)")}
        txi = "tx_index" if "tx_index" in cols else "NULL"             # the 10-21 db predates tx_index: absent is NULL (unknown), never 0
        for r in db.execute("select slot, " + txi + ", coalesce(block_time, cast(received_at as integer)), is_buy, quote_atomic, token_atomic, actor, "
                            "virtual_quote, virtual_token, real_token, fee_bps, creator_fee_bps, signature, received_at from swaps where mint=? "
                            "order by received_at, slot, ordinal", (mint,)):
            if lo < r[2] <= hi:
                out.append(dict(zip(COMPACT, r[:13])))
    last = TAPE_DAYS.get(d0)
    if last is not None and hi > last:
        end_ok = False
    return out, end_ok


def chain_forward(rpc, mint, tc):
    mint32, pda, lo = base58.b58decode(mint), W.curve_address(mint), int(tc)
    hi, got, tok, pages, complete = lo + HORIZON_S, [], None, 0, False
    while pages < 60:
        o = {"sortOrder": "desc", "limit": 100, "filters": {"blockTime": {"lte": hi}, "status": "succeeded"}}
        if tok:
            o["paginationToken"] = tok
        page = rpc.tx_page(pda, o)
        data = page.get("data", [])
        pages += 1
        for tx in data:
            got.extend(W.events(tx, mint32))
        tok = page.get("paginationToken")
        if not data or not tok or data[-1]["blockTime"] <= lo:
            complete = True                                        # reached the fill second or the start of the PDA history
            break
    got = sorted((e for e in got if e["bt"] > lo), key=lambda e: (e["slot"], e["idx"]))
    return [{k: (v.hex() if isinstance(v, bytes) else v) for k, v in e.items() if k != "mint"} for e in got], complete


def main(src, out):
    rpc, done = W.Rpc(), set()
    if Path(out).exists():
        done = {json.loads(line)["mint"] for line in open(out) if line.strip()}
    n = 0
    with open(out, "a") as f:
        for line in open(src):
            r = json.loads(line)
            if r["mint"] in done:
                continue
            sup = in_support(r)
            r["in_support"] = bool(sup and sup[0])
            if sup:
                r["mcap_usd_tc"], r["age_s_tc"], r["curve_complete_at_tc"] = sup[1], sup[2], False
            elif r.get("last_state"):
                r["curve_complete_at_tc"] = True
            if r["in_support"]:
                day = int(datetime.fromtimestamp(r["tc"], timezone.utc).strftime("%d"))
                month = datetime.fromtimestamp(r["tc"], timezone.utc).strftime("%Y-%m")
                if month == "2026-10" and day in TAPE_DAYS:
                    r["swaps_fwd"], r["fwd_complete"] = tape_forward(r["mint"], r["tc"]); r["fwd_source"] = "tape"
                else:
                    try:
                        r["swaps_fwd"], r["fwd_complete"] = chain_forward(rpc, r["mint"], r["tc"]); r["fwd_source"] = "chain"
                    except Exception as ex:
                        r["swaps_fwd"], r["fwd_complete"], r["fwd_source"], r["fwd_error"] = [], False, "chain", str(ex)[:100]
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
            f.flush()
            n += 1
            if n % 200 == 0:
                print("rows", n, "calls", rpc.calls, "MB", round(rpc.bytes / 1e6), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:3])
