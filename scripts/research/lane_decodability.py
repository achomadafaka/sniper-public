#!/usr/bin/env python3
"""What share of each armed Solana wallet's buys can the slot lane decode? Replayed, exactly.

Question 2026-09-27 (Gamma/Lambda): the slot lane reads only pump events whose actor is the owner
(memecoin_slot_signal.decode_owner_buy), so a USDC-paid route into another pool leaves no signal.
For every wallet armed on solana with a verified owner, take its newest J7 raybot Solana receipts
(swap:raybot:<sig>:<trader>:<mint>), keep those where the owner's balance of the mint rose (a buy),
and push each transaction's logs through the lane's own decoder as a logsNotification frame:
  decoded   a SlotBuy with no refusal: the lane would act (its later venue gates still apply)
  refused   a SlotBuy carrying a refusal (multi_own_buy)
  unread    None: no pump event with the owner as actor (routes into Raydium/Meteora/Orca, or an
            aggregator's own authority as the event's user)
Research key only (funded_exit_truth.rpc), read-only.

    python3 scripts/research/lane_decodability.py --per-wallet 10
    python3 scripts/research/lane_decodability.py --selftest
"""
import argparse
import collections
import contextlib
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402
import dump_forensics as dfx  # noqa: E402

fet.SPACING_S = 0.2
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"


def frame_of(tx):
    """The logsNotification the lane would have received for this transaction."""
    return {"params": {"result": {"context": {"slot": tx["slot"]}, "value": {
        "signature": tx["transaction"]["signatures"][0], "err": tx["meta"]["err"],
        "logs": tx["meta"].get("logMessages") or []}}}}


def classify(tx, owner, mint):
    """('decoded'|'refused'|'unread', venue) for the owner's buy of `mint` in `tx`; None if not a buy."""
    from sniper.services.memecoin_slot_signal import _owner_bytes, decode_owner_buy
    pre, post = dfx.owner_bag(tx, owner, mint)
    if post <= pre:
        return None
    hit = decode_owner_buy(frame_of(tx), owner, _owner_bytes(owner), "uid", recv_monotonic=0.0,
                           recv_utc="2026-01-01T00:00:00+00:00")
    if hit is None:
        progs = [l.split()[1] for l in tx["meta"].get("logMessages") or [] if " invoke [1]" in l]
        return "unread", ",".join(sorted(set(p[:6] for p in progs)))
    if getattr(hit, "refusal", None):
        return "refused", hit.refusal
    return "decoded", getattr(hit, "venue", "?")


def main(per_wallet):
    from sniper.services.memecoin_address_registry import armed_verified_owners
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        owners = armed_verified_owners(c) or {}
        work = []
        for addr, (uid, handle) in owners.items():
            rows = c.execute(
                "SELECT fomo_event_id FROM memecoin_events WHERE source_user_id=? AND network_id='solana' "
                "AND fomo_event_id >= 'swap:raybot:' AND fomo_event_id < 'swap:raybot;' "
                "ORDER BY created_at DESC LIMIT ?", (uid, per_wallet * 3)).fetchall()
            work.append((handle, addr, [r[0] for r in rows]))
    total = collections.Counter()
    for handle, addr, fids in sorted(work):
        counts, venues, seen = collections.Counter(), collections.Counter(), set()
        for fid in fids:
            parts = fid.split(":")
            sig, trader, mint_lower = parts[2], parts[3], parts[4]
            if sig in seen or trader != addr.lower():
                continue
            seen.add(sig)
            tx = dfx.get_tx(sig)
            if not tx or tx["meta"]["err"] is not None:
                continue
            mint = next((b["mint"] for b in tx["meta"].get("postTokenBalances") or []
                         if b["mint"].lower() == mint_lower), None)
            res = classify(tx, addr, mint) if mint else None
            if res is None:
                continue
            counts[res[0]] += 1
            venues[res[1]] += 1
            if sum(counts.values()) >= per_wallet:
                break
        total.update(counts)
        n = sum(counts.values())
        print(json.dumps({"handle": handle, "buys": n, **counts,
                          "decoded_share": round(counts["decoded"] / n, 2) if n else None,
                          "top": venues.most_common(3)}), flush=True)
    n = sum(total.values())
    print(json.dumps({"ALL": n, **total, "decoded_share": round(total["decoded"] / n, 3) if n else None,
                      "calls_429": fet.RATE_429[0]}), flush=True)


def selftest():
    tx = {"slot": 5, "transaction": {"signatures": ["S"]}, "meta": {"err": None, "logMessages": ["a"],
          "preTokenBalances": [{"owner": "O", "mint": "M", "uiTokenAmount": {"amount": "0"}}],
          "postTokenBalances": [{"owner": "O", "mint": "M", "uiTokenAmount": {"amount": "5"}}]}}
    f = frame_of(tx)["params"]["result"]
    assert f["context"]["slot"] == 5 and f["value"]["signature"] == "S" and f["value"]["logs"] == ["a"]
    sell = {**tx, "meta": {**tx["meta"], "preTokenBalances": tx["meta"]["postTokenBalances"],
                           "postTokenBalances": tx["meta"]["preTokenBalances"]}}
    assert classify(sell, "O", "M") is None                      # a sell is not a buy
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-wallet", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else main(a.per_wallet)
