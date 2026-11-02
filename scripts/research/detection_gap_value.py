#!/usr/bin/env python3
"""What the delay between a source's buy and ours costs: the ceiling on paying for faster detection.

Operator question 2026-09-27: "look into paid shred feeds, what would they cost". A shred feed only
moves detection earlier, so its value is bounded by the price the mint moves between the source's
trade and ours. Per funded Solana entry since --since that has a source signature
(copier_crowd_and_fees.fills resolves it): the mint's price just after the source's trade (the
pre-trade price of the next priced trade on the same venue) against the pre-trade price of our own
trade, over every transaction on the mint from the source's slot to ours (getTransactionsForAddress).
Research key only; pump curve and PumpSwap venues are priced, others counted as unpriced.

    python3 scripts/research/detection_gap_value.py --since 2026-09-24 --out gap.json
"""
import argparse
import contextlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402
import dump_forensics as dfx  # noqa: E402
from copier_crowd_and_fees import fills  # noqa: E402
from jito_evaluation import post_price, pre_price  # noqa: E402
from sniper.persistence.db_core import get_readonly_connection  # noqa: E402

fet.SPACING_S = 0.3
MAX_SLOTS = 60


def notionals(ids):
    with contextlib.closing(get_readonly_connection(str(REPO / "sniper_tracker.db"))) as c:
        q = ",".join("?" * len(ids))
        return {r[0]: r[1] for r in c.execute(
            f"SELECT position_id, notional FROM memecoin_fills WHERE kind='entry' AND position_id IN ({q})",
            list(ids))}


def gap_one(f):
    """The move from the source's own post-trade price to our pre-trade price on the SAME pool.

    Operator review 2026-09-27: the first version compared venue families and started from the
    first priced trade after the source, and it took heuristic sources. Now only an exact source
    counts; the reference is the source trade's own post-trade price, and our trade must be on
    the same curve or PumpSwap pool, or the row is `different_pool`. The result is an observed
    price move, not a recoverable amount: nothing here shows we could have traded earlier."""
    if f.get("source_basis") != "exact":
        return {**f, "status": "no_exact_source"}
    ours = dfx.get_tx(f["ours"])
    src = dfx.get_tx(f["source"])
    if not ours or not src:
        return {**f, "status": "unreadable"}
    s_src, s_us = src["slot"], ours["slot"]
    if s_us < s_src:
        return {**f, "status": "source_after_ours"}
    if s_us - s_src > MAX_SLOTS:
        return {**f, "status": "too_far", "slots_behind": s_us - s_src}
    p_src, key_src = post_price(src, f["mint"])
    p_us, key_us = pre_price(ours, f["mint"])
    if p_src is None or p_us is None:
        return {**f, "status": "unpriced", "slots_behind": s_us - s_src,
                "source_venue": key_src, "our_venue": key_us}
    if key_src != key_us:
        return {**f, "status": "different_pool", "slots_behind": s_us - s_src,
                "source_venue": key_src, "our_venue": key_us}
    return {**f, "status": "ok", "venue": key_us, "slots_behind": s_us - s_src,
            "gap_move": p_us / p_src - 1.0}


def selftest():
    assert MAX_SLOTS > 23, "the J7 lane lands up to ~23 slots behind"
    global post_price, pre_price
    real = (dfx.get_tx, post_price, pre_price)
    try:
        dfx.get_tx = lambda sig: {"slot": 10 if sig == "src" else 11}
        f = {"source": "src", "ours": "us", "mint": "M", "source_basis": "exact"}
        post_price = lambda tx, mint: (1.0, "pumpswap:A")
        pre_price = lambda tx, mint: (1.1, "pumpswap:A")
        r = gap_one(f)
        assert r["status"] == "ok" and abs(r["gap_move"] - 0.1) < 1e-12 and r["slots_behind"] == 1
        pre_price = lambda tx, mint: (1.1, "pumpswap:B")
        assert gap_one(f)["status"] == "different_pool", "another pool of the same mint is not a gap"
        assert gap_one(dict(f, source_basis="heuristic_window"))["status"] == "no_exact_source"
        dfx.get_tx = lambda sig: {"slot": 12 if sig == "src" else 11}
        assert gap_one(f)["status"] == "source_after_ours"
    finally:
        dfx.get_tx, post_price, pre_price = real
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-24")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    rows = []
    for f in fills(a.since):
        try:
            rows.append(gap_one(f))
        except fet.Stop as exc:
            print(f"STOP: {exc}", file=sys.stderr)
            break
    nots = notionals([r["position_id"] for r in rows])
    for r in rows:
        r["notional"] = nots.get(r["position_id"])
    Path(a.out or "/dev/stdout").write_text(json.dumps(rows, indent=1))
    print(f"rows {len(rows)} 429s {fet.RATE_429[0]}", file=sys.stderr)


if __name__ == "__main__":
    main()
