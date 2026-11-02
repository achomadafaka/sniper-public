#!/usr/bin/env python3
"""Who lands in the source's own slot S on a heavily copied wallet's buys, and what they pay (spec
2026-10-09 memecoin S-landing on top wallets, section 2).

For each slot-lane SEND of one wallet (memecoin_entry_attempts with a tx_sig) since --since: every
transaction on the mint in slots S..S+1 in block order (Helius getTransactionsForAddress, full), with
the source's own (slot, index), each buyer's signer, compute-unit price and limit, priority lamports,
tips to known tip accounts (Jito, Helius Sender, Nozomi, Astralane, NextBlock, GMGN) and every other
lamport transfer the signer paid, its top-level programs, and, for the buyers that landed in S after
the source, whether Jito's public explorer lists the transaction in a bundle. Plus our own send's fee
facts. Read-only; research key only (funded_exit_truth.rpc), held while the Solana book is not flat
(copier_crowd_and_fees.install_flat_guard); at most --max-calls RPC calls.

    python3 scripts/research/s_landing_census.py --uid aefe2ddd-c580-5245-a2f5-e4ed62f7ef10 \\
        --since 2026-09-26 --out data/research_s_landing_frank.json
"""
import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
import copier_crowd_and_fees as ccf  # noqa: E402
import copiers_ahead_repeat as car  # noqa: E402
import funded_exit_truth as fet  # noqa: E402
import jito_evaluation as jev  # noqa: E402

# Jito's eight tip accounts (copier_crowd_and_fees; its eighth had a dropped 'k' until 2026-10-09, Gamma
# confirmed against getTipAccounts). Every other transfer is listed by recipient.
JITO = ccf.JITO_TIP_ACCOUNTS
SENDER = frozenset({
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE", "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta", "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD", "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF", "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey", "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or"})


class Budget(Exception):
    pass


def tip_labels(row):
    out = collections.Counter()
    unknown = []
    for to, lamports in row.get("sol_out") or []:
        if to in JITO:
            out["jito"] += lamports
        elif to in SENDER:
            out["helius_sender"] += lamports
        else:
            label = car.TIP_EXACT.get(to) or next(
                (v for k, v in car.TIP_PREFIXES.items() if to.lower().startswith(k)), None)
            if label:
                out[label] += lamports
            else:
                unknown.append([to, lamports])
    return dict(out), unknown


def sends(ledger, uid, since):
    return ledger.execute(
        "SELECT attempt_id, origin_event_id, mint, tx_sig, terminal_status, terminal_reason, "
        "decision_started_at FROM memecoin_entry_attempts WHERE source_user_id=? AND network_id='solana' "
        "AND decision_started_at>=? AND delivery_lane LIKE 'slot_lane%' AND tx_sig IS NOT NULL "
        "ORDER BY decision_started_at", (uid, since)).fetchall()


def one(send, calls, max_calls):
    aid, origin, mint, ours, status, reason, at = send
    source = origin.split(":", 1)[1]

    def rpc(method, params):
        if calls[0] >= max_calls:
            raise Budget()
        calls[0] += 1
        return fet.rpc(method, params)
    src = rpc("getTransaction", [source, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                          "commitment": "confirmed"}])
    if not src:
        return dict(attempt_id=aid, status=status, error="source_unread")
    S = src["slot"]
    our_tx = rpc("getTransaction", [ours, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                           "commitment": "confirmed"}])
    our_slot = our_tx["slot"] if our_tx else None
    rows = []
    token = None
    seen_tokens = set()
    def censored(code):
        return dict(attempt_id=aid, at=at, status=status, mint=mint, source=source, slot=S,
                    censored=code)
    while True:
        opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 100, "encoding": "json",
                "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
                "filters": {"slot": {"gte": S, "lte": S + 1}}}
        if token:
            opts["paginationToken"] = token
        try:
            res = rpc("getTransactionsForAddress", [mint, opts])
        except Budget:
            raise
        except Exception:
            return censored("page_unreadable")
        if not isinstance(res, dict) or "error" in res or not isinstance(res.get("data"), list):
            return censored("page_unreadable")
        page = res["data"]
        for tx in page:
            transaction = tx.get("transaction") if isinstance(tx, dict) else None
            signatures = transaction.get("signatures") if isinstance(transaction, dict) else None
            if (not isinstance(signatures, list) or not signatures
                    or not isinstance(signatures[0], str) or not signatures[0]
                    or type(tx.get("slot")) is not int or not S <= tx["slot"] <= S + 1
                    or type(tx.get("transactionIndex")) is not int or tx["transactionIndex"] < 0):
                return censored("page_unreadable")
        rows.extend(page)
        token = res.get("paginationToken")
        if token is None or token == "":
            break
        if not isinstance(token, str) or token in seen_tokens or not page:
            return censored("page_unreadable")
        seen_tokens.add(token)
    source_index = next((r["transactionIndex"] for r in rows
                         if r["slot"] == S and r["transaction"]["signatures"][0] == source), None)
    if source_index is None:
        return censored("source_order_unknown")
    buyers = []
    for tx in rows:
        sig = tx["transaction"]["signatures"][0]
        if sig in (source, ours) or (tx["slot"] == S and tx["transactionIndex"] <= source_index):
            continue
        meta = tx.get("meta")
        if not isinstance(meta, dict) or "err" not in meta:
            return censored("page_unreadable")
        if meta["err"] is not None:
            continue
        msg = tx["transaction"].get("message")
        if (not isinstance(msg, dict) or not isinstance(msg.get("accountKeys"), list)
                or not msg["accountKeys"] or not isinstance(msg.get("instructions"), list)
                or not isinstance(msg.get("header"), dict)
                or type(msg["header"].get("numRequiredSignatures")) is not int
                or not 0 < msg["header"]["numRequiredSignatures"] <= len(msg["accountKeys"])
                or msg["header"]["numRequiredSignatures"] != len(tx["transaction"]["signatures"])
                or not isinstance(meta.get("preBalances"), list)
                or not isinstance(meta.get("postBalances"), list)
                or type(meta.get("fee")) is not int or meta["fee"] < 0):
            return censored("page_unreadable")
        for field in ("preTokenBalances", "postTokenBalances"):
            balances = meta.get(field)
            if not isinstance(balances, list) or any(
                    not isinstance(b, dict) or not b.get("owner") or not b.get("mint")
                    or not isinstance(b.get("uiTokenAmount"), dict) for b in balances):
                return censored("page_unreadable")
        try:
            trade = ccf._sol_trade(tx, mint)
        except (KeyError, TypeError, ValueError, IndexError, OverflowError, ZeroDivisionError):
            return censored("page_unreadable")
        if not trade or trade.get("side") != "buy":
            continue
        slot, index = tx["slot"], tx["transactionIndex"]
        try:
            facts = ccf.fee_facts(tx)
        except (KeyError, TypeError, ValueError, IndexError, OverflowError, ZeroDivisionError):
            return censored("page_unreadable")
        tips, unknown = tip_labels(facts)
        buyers.append(dict(slot_offset=slot - S, index=index, sig=sig, signer=facts["signer"],
                           sol=trade.get("sol"), cu_price=facts["cu_price"], cu_limit=facts["cu_limit"],
                           priority_lamports=facts["priority_lamports"], tips=tips, other_transfers=unknown,
                           programs=facts["programs"]))
    for b in buyers:
        if b["slot_offset"] == 0:
            det = jev.bundle_of(b["sig"])
            b["jito_bundle"] = bool(det)
            b["bundle_size"] = len(det.get("transactions") or []) if det else None
            b["bundle_tip_lamports"] = det.get("landedTipLamports") if det else None
    ours_facts = ccf.fee_facts(our_tx) if our_tx else None
    return dict(attempt_id=aid, at=at, status=status, reason=(reason or "")[:120], mint=mint, source=source,
                slot=S, source_index=source_index, our_slot=our_slot,
                our_offset=None if our_slot is None else our_slot - S,
                ours=None if ours_facts is None else dict(
                    priority_lamports=ours_facts["priority_lamports"], cu_price=ours_facts["cu_price"],
                    cu_limit=ours_facts["cu_limit"], tips=tip_labels(ours_facts)[0]),
                buyers=buyers)


def summary(results, sol_usd):
    def stats(rows):
        n = len(rows)
        if not n:
            return dict(n=0)
        tipped = [r for r in rows if r["tips"]]
        bundled = [r for r in rows if r.get("jito_bundle")]
        pay = sorted((r["priority_lamports"] or 0) + sum(r["tips"].values()) for r in rows)
        labels = collections.Counter(k for r in rows for k in r["tips"])
        return dict(n=n, tipped=len(tipped), tip_labels=dict(labels), jito_bundled=len(bundled),
                    signers=len({r["signer"] for r in rows}),
                    paid_lamports_p50=pay[n // 2], paid_lamports_p90=pay[min(n - 1, int(n * 0.9))],
                    paid_usd_p50=pay[n // 2] / 1e9 * sol_usd,
                    paid_usd_p90=pay[min(n - 1, int(n * 0.9))] / 1e9 * sol_usd)
    known = [r for r in results if "buyers" in r and not r.get("censored") and not r.get("error")]
    in_s = [b for r in known for b in r["buyers"] if b["slot_offset"] == 0]
    in_s1 = [b for r in known for b in r["buyers"] if b["slot_offset"] == 1]
    repeat = collections.Counter(b["signer"] for b in in_s)
    programs = collections.Counter(p for b in in_s for p in b["programs"])
    return dict(sends=len(results), census_known=len(known),
                censored=dict(collections.Counter(r.get("censored") or r.get("error")
                                                 for r in results if r.get("censored") or r.get("error"))),
                in_s=stats(in_s), in_s_plus_1=stats(in_s1),
                s_signers_repeating=sorted(((s, n) for s, n in repeat.items() if n > 1), key=lambda x: -x[1]),
                s_programs=programs.most_common(8),
                ours=[dict(status=r["status"], offset=r.get("our_offset"), ours=r.get("ours")) for r in results])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uid", required=True)
    p.add_argument("--since", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--max-calls", type=int, default=80)
    p.add_argument("--sol-usd", type=float, default=110.0)
    a = p.parse_args()
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    root = state_root()                          # SNIPER_STATE_ROOT: the live ledger and .env
    load_env_files(str(root))
    ccf.LEDGER = str(root / "sniper_tracker.db")
    ccf.install_flat_guard()
    ledger = get_readonly_connection(ccf.LEDGER)
    calls, results = [0], []
    for send in sends(ledger, a.uid, a.since):
        try:
            results.append(one(send, calls, a.max_calls))
        except Budget:
            print("call budget reached at %d" % calls[0], file=sys.stderr)
            break
    out = dict(uid=a.uid, since=a.since, calls=calls[0], sol_usd=a.sol_usd, summary=summary(results, a.sol_usd),
               results=results)
    Path(a.out).write_text(json.dumps(out, indent=1, default=str))
    json.dump(out["summary"], sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main()
