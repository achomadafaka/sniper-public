#!/usr/bin/env python3
"""Did the source actually BUY in the transaction a fast-lane fill copied? Read from the chain.

Alpha, 2026-09-28 (urgent, after the gate audit's "2 sells judged as buys"): if the on-chain lane can
read a source SELL as a BUY it can open a funded position on a sell. For every onchain_watch and
slot_lane fill since --since, and for the named refused attempts, this reads the source transaction
and takes the SOURCE WALLET's own deltas: token delta on the mint (+ = received), lamports (net of fee
and created-account rent when it signed), and USDC. It never uses dump_forensics.trade_of's "largest
mover" trader: in a router transaction that is often a different party, which is how the audit's
first read labelled two genuine purchases as sells.

Verdicts:
  buy        wallet received the token and paid SOL or USDC
  sell       wallet's token balance fell
  no_payment wallet received the token and paid nothing we can see (transfer, handout, or paid by
             another signer: router-funded) -- reported, not called a sell
  absent     wallet not in the transaction's balances

Research key only, flat funded Solana book before every read (slot_minout_grading.wait_flat).

    python3 scripts/research/source_direction_check.py --since 2026-09-21
    python3 scripts/research/source_direction_check.py --selftest
"""
import argparse
import contextlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
USDC = "EPjFWdd5AufqSSqeM2qN1xYaGNcuyVxv8m1QvbnvhbjB"
RENT = 2039280


def owner_deltas(tx, owner, mint):
    """(token_delta_atomic, usdc_delta_atomic, lamport_delta_net) for `owner`; lamports exclude the fee
    when the owner signed first, and the rent of token accounts it created."""
    meta, msg = tx["meta"], tx["transaction"]["message"]
    def tok(side, m):
        return sum(int(b["uiTokenAmount"]["amount"]) for b in meta.get(side) or []
                   if b.get("owner") == owner and b.get("mint") == m)
    token = tok("postTokenBalances", mint) - tok("preTokenBalances", mint)
    usdc = tok("postTokenBalances", USDC) - tok("preTokenBalances", USDC)
    keys = list(msg.get("accountKeys") or [])
    loaded = meta.get("loadedAddresses") or {}
    keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    lam = None
    if owner in keys:
        i = keys.index(owner)
        lam = meta["postBalances"][i] - meta["preBalances"][i]
        if i == 0:
            lam += meta.get("fee") or 0
        created = (sum(1 for b in meta.get("postTokenBalances") or [] if b.get("owner") == owner)
                   - sum(1 for b in meta.get("preTokenBalances") or [] if b.get("owner") == owner))
        lam += max(0, created) * RENT
    return token, usdc, lam


def verdict(token, usdc, lam):
    if token < 0:
        return "sell"
    if token == 0:
        return "absent"
    paid = (usdc < 0) or (lam is not None and lam < -5000)
    return "buy" if paid else "no_payment"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-09-21")
    ap.add_argument("--attempts", nargs="*", default=["22ebc798", "4111c2bb"])
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        assert verdict(-5, 0, 0) == "sell" and verdict(0, 0, 0) == "absent"
        assert verdict(10, -1000, 0) == "buy" and verdict(10, 0, -10_000_000) == "buy"
        assert verdict(10, 0, 0) == "no_payment" and verdict(10, 0, None) == "no_payment"
        print("selftest ok")
        return 0
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    from sniper.persistence.db_core import get_readonly_connection
    from slot_minout_grading import wait_flat
    import dump_forensics as dfx
    import funded_exit_truth as fet
    fet.SPACING_S = 0.5
    conn_fn = lambda: get_readonly_connection(str(REPO / "sniper_tracker.db"))
    with contextlib.closing(conn_fn()) as c:
        items = [dict(r) for r in c.execute(
            "SELECT p.id AS pos, p.signal_source, p.mint, p.source_user_id, p.source_handle, p.realized_pnl, "
            "p.is_open, a.attempt_id, a.origin_event_id FROM memecoin_positions p JOIN memecoin_entry_attempts a "
            "ON a.attempt_id=p.attempt_id WHERE p.execution_mode='real' AND p.network_id='solana' AND "
            "p.signal_source IN ('onchain_watch','slot_lane') AND p.opened_at>=? ORDER BY p.opened_at", (a.since,))]
        for ref in a.attempts:
            r = c.execute("SELECT attempt_id, signal_source, mint, source_user_id, source_handle, origin_event_id "
                          "FROM memecoin_entry_attempts WHERE attempt_id LIKE ?", (ref + "%",)).fetchone()
            if r:
                items.append(dict(r, pos=None, realized_pnl=None, is_open=0))
        for it in items:
            sig = str(it["origin_event_id"]).split(":", 1)[1]
            det = c.execute("SELECT address FROM onchain_wallet_detections WHERE signature=?", (sig,)).fetchone()
            it["sig"] = sig
            it["owners"] = ([det["address"]] if det else []) + [r["address"] for r in c.execute(
                "SELECT address FROM memecoin_address_registry WHERE network_id='solana' AND "
                "source_user_id=? AND status='verified'", (it["source_user_id"],))]
    counts = {}
    for it in items:
        wait_flat(conn_fn)
        tx = dfx.get_tx(it["sig"])
        v, detail = "tx_not_found", ""
        if tx:
            best = None
            for owner in dict.fromkeys(it["owners"]):
                d = owner_deltas(tx, owner, it["mint"])
                if d[0] != 0:
                    best = (owner, d)
                    break
            if best:
                v = verdict(*best[1])
                detail = "owner %s token %+d usdc %+d lamports %s" % (best[0][:6], best[1][0], best[1][1], best[1][2])
            else:
                v = "absent"
        counts[(it["signal_source"], "fill" if it["pos"] else "refused", v)] = counts.get(
            (it["signal_source"], "fill" if it["pos"] else "refused", v), 0) + 1
        if v != "buy" or not it["pos"]:
            print("  %-13s %s %s %-16s %-10s pnl %s %s" % (it["signal_source"], it["pos"] or it["attempt_id"][:8],
                  it["sig"][:10], (it["source_handle"] or "?")[:16], v, it["realized_pnl"], detail))
    print("\ndirection by lane (fills since %s, plus the named refusals):" % a.since)
    for k, n in sorted(counts.items()):
        print("  %s %s %s: %d" % (k[0], k[1], k[2], n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
