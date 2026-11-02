#!/usr/bin/env python3
"""Backfill a funded de-risk partial the ledger never booked, or give a quantity-less one its quantity.

WHY. Before Codex N14 (merged 7684866ba), a confirmed late partial was cleared without a fill:
pos 31099 (Momota, Solana, 2026-08-07) sold 25% of its bag at 19:34Z and closed at 09:23Z on
08-11 with realized $79.61, short by that partial's proceeds. MOONDENG 36517 (2026-08-12) has a
de-risk intent stuck at submitted_unknown (claim 5beb9017, tx 4EoVEArx...E3jG). Since N14 a partial
whose intent cannot say its quantity is booked with qty NULL and is never priced (Gamma LOW-2).

WHAT IT DOES. For each de-risk intent of the position with no fill for its transaction (or for the
quantity-less fill given with --fill), it reads that ONE transaction on the Solana research host and
prints the receipt: landed, failed or not found; for a landed sale, the tokens our wallet sold and
the USDC it received (receipt_exit_proceeds, the funded parser). It prints realized before and after.
With --confirm (the operator only):
  landed      -> book the partial (unpriced, receipt quantity), then price it from the receipt
                 with reconcile_unpriced_derisk: an open position adds it at its final close; a
                 closed one with realized booked gets realized += proceeds.
  failed      -> the intent's status becomes 'failed'.
  not found   -> nothing: the node has no record, which is not chain failure (review C08, 2026-10-08).
                 A later full exit that sold the whole entry balance is printed as a diagnostic only:
                 final = entry - partial + inflow, so it proves partial == inflow, never partial == 0.
--fill F    -> set the fill's quantity from the receipt, then price it the same way.

WHAT IT REFUSES. Any chain but Solana; any read while a funded Solana position is open (the research
key's rule); a position that is not funded; a transaction it cannot read.

    python3 scripts/memecoin_backfill_derisk.py --position 31099 --position 36517
    python3 scripts/memecoin_backfill_derisk.py --position 31099 --confirm
    python3 scripts/memecoin_backfill_derisk.py --fill 123456 --confirm
The operator runs --confirm; nobody else.
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read_transaction(url, sig, *, timeout=20.0):
    """The jsonParsed transaction, or None when the node has no record of it. Raises on a failed
    read: an unreadable answer is never "not found"."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [
        sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
              "commitment": "confirmed"}]}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                             "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    if "error" in out or "result" not in out:
        raise RuntimeError("getTransaction error: %s" % str(out.get("error"))[:200])
    return out["result"]


def _owner_mint_atomic(entries, owner, mint):
    total, decimals = 0, None
    for entry in entries or ():
        if entry.get("owner") != owner or entry.get("mint") != mint:
            continue
        value = entry.get("uiTokenAmount") or {}
        total += int(value["amount"])
        decimals = int(value["decimals"])
    return total, decimals


def receipt_outcome(tx, *, sig, owner, mint):
    """{'state': landed | failed | not_found | unreadable, ...}. A landed sale carries the tokens
    the owner sold (UI units), the USDC it received and the block time."""
    from sniper.execution.solana_swap_executor import receipt_exit_proceeds
    if tx is None:
        return {"state": "not_found"}
    try:
        if not sig or tx["transaction"]["signatures"][0] != sig:
            return {"state": "unreadable"}
        meta = tx["meta"]
        if meta.get("err") is not None:
            return {"state": "failed", "err": meta["err"]}
        pre, decimals_pre = _owner_mint_atomic(meta.get("preTokenBalances"), owner, mint)
        post, decimals_post = _owner_mint_atomic(meta.get("postTokenBalances"), owner, mint)
        decimals = decimals_pre if decimals_pre is not None else decimals_post
        proceeds = receipt_exit_proceeds(tx, signature=sig, owner=owner)
        at = datetime.fromtimestamp(int(tx["blockTime"]), timezone.utc).isoformat()
    except (KeyError, IndexError, TypeError, ValueError):
        return {"state": "unreadable"}
    if decimals is None or pre <= post or proceeds is None:
        return {"state": "unreadable"}
    return {"state": "landed", "sold_qty": (pre - post) / 10 ** decimals, "proceeds": proceeds,
            "at": at}


def _solana_owner():
    from sniper.execution.solana_wallet import load_keypair
    from sniper.settings import memecoin as cfg
    return str(load_keypair(cfg.MEMECOIN_SOLANA_KEYPAIR).pubkey())


def main(argv=None, *, fetch=read_transaction, owner=None, research_url=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--position", type=int, action="append", default=[])
    ap.add_argument("--fill", type=int, action="append", default=[])
    ap.add_argument("--db", help="ledger path (default: the funded ledger)")
    ap.add_argument("--confirm", action="store_true", help="write it (operator only)")
    args = ap.parse_args(argv)
    if not args.position and not args.fill:
        ap.error("give --position and/or --fill")

    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    import os
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_connection, get_readonly_connection
    from sniper.settings import memecoin as cfg
    if cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == 'shyft':
        from sniper.services.memecoin_solana_provider import provider_urls
        env = dict(os.environ)
        if research_url is not None:
            env['MEMECOIN_SOLANA_READ_RPC_URL'] = research_url
        url = provider_urls('read', env)[0]
    else:
        url = research_url or os.getenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', '').strip()
    now = now or datetime.now(timezone.utc).isoformat()

    reader = get_readonly_connection(args.db)
    try:
        if m.list_open_positions(reader, mode="real", chain="solana", include_paused=True):
            print("REFUSED: a funded Solana position is open; the research key reads only on a "
                  "flat Solana book")
            return 2
        if not url:
            print("REFUSED: MEMECOIN_RESEARCH_HELIUS_RPC_URL is not set")
            return 2
        work = []
        for pid in args.position:
            pos = m.get_position(reader, pid)
            if not pos or pos.get("execution_mode") != "real" or pos.get("network_id") != "solana":
                print("position %s: REFUSED (not a funded Solana position)" % pid)
                continue
            intents = m.unbooked_derisk_intents(reader, pid)
            if not intents:
                print("position %s: no de-risk intent without a fill" % pid)
            for intent in intents:
                work.append(("intent", pos, intent))
        for fill_id in args.fill:
            row = m.unpriced_null_qty_derisk_fill(reader, fill_id)
            if row is None or m.normalized_coin_network(row.get("network_id"), row.get("mint")) != "solana":
                print("fill %s: REFUSED (not an unpriced quantity-less Solana partial)" % fill_id)
                continue
            work.append(("fill", row, {"sig": row["derisk_tx"], "fill_id": fill_id,
                                       "fraction": row.get("fill_fraction")}))
        owner = owner or _solana_owner()
        plans = []
        for kind, pos, item in work:
            try:
                outcome = receipt_outcome(fetch(url, item["sig"]), sig=item["sig"], owner=owner,
                                          mint=pos["mint"])
            except Exception as exc:
                outcome = {"state": "unreadable", "why": "%s: %s" % (type(exc).__name__, exc)}
            equal_qty = (outcome["state"] == "not_found" and kind == "intent"
                         and m.full_exit_qty_equals_entry_qty(reader, pos["id"]))
            plans.append((kind, pos, item, outcome))
            before = pos.get("realized_pnl")
            head = "%s %s pos %s %s tx %s" % (kind, item.get("claim_id") or item.get("fill_id"),
                                              pos["id"], pos.get("ticker"), item["sig"])
            if outcome["state"] == "landed":
                after = (before + outcome["proceeds"] if (not pos["is_open"] and before is not None)
                         else before)
                print("%s: LANDED %s; sold %.6f tokens for %.6f USDC; realized %s -> %s%s" % (
                    head, outcome["at"], outcome["sold_qty"], outcome["proceeds"], before, after,
                    "" if not pos["is_open"] else " (open: added at its final close)"))
            elif outcome["state"] == "failed":
                print("%s: FAILED on chain (%s); intent -> failed; realized unchanged %s" % (
                    head, outcome.get("err"), before))
            elif outcome["state"] == "not_found":
                print("%s: NOT FOUND on chain; no change, the intent stays unresolved%s; realized "
                      "unchanged %s" % (
                          head, " (diagnostic: the last full exit sold exactly the entry balance, "
                          "consistent with no partial but not proof: an inflow can match it)"
                          if equal_qty else "", before))
            else:
                print("%s: UNREADABLE (%s): no change" % (head, outcome.get("why", "receipt")))
    finally:
        reader.close()
    if not args.confirm:
        print("DRY RUN: nothing written. Add --confirm to write it (operator only).")
        return 0

    writer = get_connection(args.db)
    try:
        for kind, pos, item, outcome in plans:
            if outcome["state"] == "landed":
                if kind == "intent":
                    fill_id = m.backfill_unbooked_derisk(
                        writer, position_id=pos["id"], sig=item["sig"], qty=outcome["sold_qty"],
                        fraction=item.get("fraction"), at=outcome["at"])
                else:
                    fill_id = item["fill_id"] if m.set_unpriced_derisk_qty(
                        writer, fill_id=item["fill_id"], position_id=pos["id"], sig=item["sig"],
                        qty=outcome["sold_qty"]) else None
                priced = fill_id is not None and m.reconcile_unpriced_derisk(
                    writer, fill_id=fill_id, position_id=pos["id"], sig=item["sig"],
                    notional=outcome["proceeds"], fill_source="chain_backfill_receipt")
                after = m.get_position(writer, pos["id"]).get("realized_pnl")
                print("pos %s tx %s: %s; realized now %s" % (
                    pos["id"], item["sig"], "booked and priced" if priced else "NOT written "
                    "(already booked, or changed since the read)", after))
            elif kind == "intent" and outcome["state"] == "failed":
                ok = m.set_exit_intent_status(writer, item["claim_id"], "failed", now)
                print("pos %s claim %s: intent %s" % (pos["id"], item["claim_id"],
                                                      "-> failed" if ok else "NOT updated"))
    finally:
        writer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
