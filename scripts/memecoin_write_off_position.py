#!/usr/bin/env python3
"""Write off a stuck funded bag: close it in the ledger at $0 proceeds. Nothing happens on chain.

WHY (operator, 2026-09-29, via Gamma and Alpha). A bag nobody can sell (AICAT 66881: every sell
reverts on chain) stays open forever. It holds a slot and exposure, and it blocks every deploy,
because a deploy waits for a flat book. There was no way to say "this is lost".

WHAT IT DOES. `write_off_real_position` closes the position with exit_reason 'written_off' and
realized = de-risk proceeds already booked - entry cost. No quote, no signature, no fill row. The
tokens stay in the wallet; once no open position owns the mint, the runner's Other Holdings scan
lists them, and a sale from there books to Other Holdings, not to this position.

WHAT IT REFUSES. A position that is not open, not funded (execution_mode='real'), has no entry
cost, or has an exit in flight: a close or de-risk claim, a signature, or a submitted or unknown
sell intent. A write-off beside an ambiguous sell would book the bag twice. The ledger update
repeats every refusal as its predicate, so a claim that lands mid-run makes it refuse.

THE BALANCE. The audit row records the token balance at write-off, read with eth_call on a
non-custody host (never the funded RPC pool) for the wallet given with --address. No address, no
host for the chain, or a failed read records "unknown", never 0.

    python3 scripts/memecoin_write_off_position.py 66881 --reason "sells revert on chain"
    python3 scripts/memecoin_write_off_position.py 66881 --reason "..." --address 0x... --confirm
The operator runs --confirm; nobody else.
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Non-custody read hosts (the research-read rule). Direct urllib, never the funded pool: a pool
# member list can route a "research" read to a custody endpoint.
READ_HOSTS = {"4663": "https://robinhood-rpc.publicnode.com",
              "56": "https://bsc-dataseed1.defibit.io"}
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
SEL_BALANCE_OF = "0x70a08231"
SEL_DECIMALS = "0x313ce567"


def _eth_call(url, to, data, *, timeout=8.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                             "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    if "result" not in out:
        raise RuntimeError("eth_call error: %s" % out.get("error"))
    return out["result"]


def balance_at_write_off(pos, *, address):
    """(text, source host or None). Every failure is 'unknown: <why>', never a zero."""
    chain = str(pos.get("network_id") or "")
    host = READ_HOSTS.get(chain)
    if host is None:
        return "unknown: no non-custody read host for chain %s" % chain, None
    if not address or not _ADDRESS.match(str(address)):
        return "unknown: no valid wallet address given (--address)", None
    try:
        raw = int(_eth_call(host, pos["mint"],
                            SEL_BALANCE_OF + str(address).lower()[2:].rjust(64, "0")), 16)
        decimals = int(_eth_call(host, pos["mint"], SEL_DECIMALS), 16)
    except Exception as exc:                 # noqa: BLE001 - recorded, not raised
        return "unknown: read failed (%s)" % exc, host
    return "raw=%d decimals=%d address=%s" % (raw, decimals, address), host


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("position_id", type=int)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--actor", default="operator")
    ap.add_argument("--address", help="the funded wallet, for the balance read (public address)")
    ap.add_argument("--db", help="ledger path (default: the funded ledger)")
    ap.add_argument("--confirm", action="store_true", help="write it off (operator only)")
    args = ap.parse_args(argv)
    if not args.reason.strip():
        ap.error("--reason must not be empty")

    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_connection, get_readonly_connection

    reader = get_readonly_connection(args.db)
    try:
        pos, why = m.write_off_refusal(reader, args.position_id)
        derisk = m.derisk_proceeds(reader, args.position_id) if pos else None
    finally:
        reader.close()
    if pos is not None:
        print("position %s  %s %s  mode=%s open=%s entry_notional=%s paused=%s stuck=%s" % (
            pos["id"], pos.get("network_id"), pos.get("ticker") or pos.get("mint"),
            pos.get("execution_mode"), pos.get("is_open"), pos.get("entry_notional"),
            pos.get("tracking_paused_at"), pos.get("stuck_bag")))
    if why is not None:
        print("REFUSED: %s" % why)
        return 2
    if derisk is None:
        print("would book realized_pnl UNKNOWN (a de-risk partial is unpriced), exit_reason written_off")
    else:
        realized = derisk - float(pos["entry_notional"])
        print("would book realized_pnl %.2f (de-risk proceeds %.2f - entry %.2f), exit_reason "
              "written_off" % (realized, derisk, float(pos["entry_notional"])))
    if not args.confirm:
        print("DRY RUN: nothing written. Add --confirm to write it off (operator only).")
        return 0

    balance, source = balance_at_write_off(pos, address=args.address)
    print("balance at write-off: %s%s" % (balance, " via %s" % source if source else ""))
    writer = get_connection(args.db)
    try:
        result = m.write_off_real_position(writer, args.position_id, actor=args.actor,
                                           reason=args.reason, balance_text=balance,
                                           balance_source=source)
    finally:
        writer.close()
    print(json.dumps(result, default=str))
    return 0 if result["status"] in ("written_off", "already_written_off") else 2


if __name__ == "__main__":
    sys.exit(main())
