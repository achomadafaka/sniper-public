"""Which transaction shapes are behind J7's 4663 fills, and how many would the watcher decode?

Alpha, 2026-09-28: before building a decoder for the Relay envelopes the watcher refuses as
`unsupported_relayer_swap` (2 of the 15 misses since the watcher went live), count how many J7 4663
fills since 09-21 have that shape. Per fill, the SOURCE transaction (the J7 origin
`swap:raybot:<tx>:<trader>:<mint>`) is read once and classified from its input alone:

  direct                     tx.to is not the Relay approval proxy (a router the direct decoder reads)
  relay:adapter              Relay Permit2 envelope whose swap call targets the supported adapter
  relay:other:<target>:<sel> Relay envelope, swap call to another target (the gap to size)
  relay:shape:<why>          Relay envelope the decoder rejects before the swap target
  no_tx_hash                 the origin carries no transaction hash

Read-only ledger. Chain reads: robinhood-rpc.publicnode.com only, eth_getTransactionByHash, one
call every SPACING_S (publicnode answers a bare 403 after about 30 research calls in 5 minutes, and
the EVM watcher reads 4663 there too).

    python3 scripts/research/evm_relay_shape_census.py [--since 2026-09-21T00:00:00]
    python3 scripts/research/evm_relay_shape_census.py --selftest
"""
import argparse
import collections
import contextlib
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

from evm_watch_miss_replay import Chain  # noqa: E402


def classify_input(tx, proxy, adapter, types):
    """One label from the transaction alone."""
    from eth_abi import decode
    if str(tx.get("to") or "").lower() != proxy:
        return "direct"
    raw = str(tx.get("input") or "")
    if not raw.startswith("0x0a2b8f36"):
        return "relay:shape:selector_%s" % raw[:10]
    try:
        _payer, _permit, calls, *_rest = decode(types, bytes.fromhex(raw[10:]))
    except Exception:
        return "relay:shape:undecodable"
    if len(calls) != 3:
        return "relay:shape:calls_%d" % len(calls)
    target, selector = str(calls[1][0]).lower(), calls[1][3][:4].hex()
    if target == adapter:
        return "relay:adapter"
    return "relay:other:%s:%s" % (target[:10], selector)


def fills(conn, since):
    rows = conn.execute(
        "SELECT a.decision_started_at, a.source_handle, a.origin_event_id FROM memecoin_entry_attempts a "
        "LEFT JOIN memecoin_positions p ON p.attempt_id=a.attempt_id AND p.execution_mode='real' "
        "WHERE a.network_id='4663' AND a.signal_source='follow_activity' AND a.decision_started_at>=? "
        "AND (a.terminal_status='filled' OR p.id IS NOT NULL) ORDER BY a.decision_started_at",
        (since,)).fetchall()
    out = []
    for at, handle, origin in rows:
        parts = str(origin or "").split(":")
        tx, trader = (parts[2].lower(), parts[3].lower()) if parts[:2] == ["swap", "raybot"] and len(parts) >= 5 else (None, None)
        out.append({"at": at, "handle": handle, "tx": tx, "trader": trader})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-21T00:00:00")
    a = ap.parse_args()
    import onchain_evm_wallet_watch as w
    import sniper.services.memecoin_evm_onchain_watch as ow
    from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY
    from sniper.persistence.db_core import get_readonly_connection
    w._configure("4663")
    with contextlib.closing(get_readonly_connection(str(w.DB))) as c:
        watched = w._verified_addresses(c)
        items = fills(c, a.since)
    chain = Chain()
    counts, watched_counts = collections.Counter(), collections.Counter()
    for it in items:
        label = "no_tx_hash"
        if it["tx"]:
            tx = chain("eth_getTransactionByHash", [it["tx"]])
            label = classify_input(tx or {}, RELAY_APPROVAL_PROXY.lower(), ow.RELAY_ADAPTER_4663,
                                   ow.PERMIT2_TYPES) if tx else "tx_null"
        counts[label] += 1
        if it["trader"] in watched:
            watched_counts[label] += 1
        print("%s %-16s %s %s%s" % (it["at"][:19], (it["handle"] or "")[:16], (it["tx"] or "-")[:12],
                                    label, " watched" if it["trader"] in watched else ""), flush=True)
    print("4663 J7 fills since %s: %d; chain calls %d" % (a.since, len(items), chain.calls))
    for label, n in counts.most_common():
        print("  %-44s %3d  (watched trader %d)" % (label, n, watched_counts[label]))


def selftest():
    from eth_abi import encode
    import sniper.services.memecoin_evm_onchain_watch as ow
    proxy, adapter = "0x" + "cc" * 20, ow.RELAY_ADAPTER_4663
    types = ow.PERMIT2_TYPES

    def envelope(target):
        calls = [("0x" + "01" * 20, False, 0, b"\x09\x5e\xa7\xb3"),
                 (target, False, 0, bytes.fromhex("998b5942")),
                 ("0x" + "02" * 20, False, 0, b"\x9b\xb4\x37\x18")]
        values = ["0x" + "03" * 20, ([("0x" + "04" * 20, 1)], 0, 1), calls,
                  "0x" + "05" * 20, "0x" + "05" * 20, b"", b"\x00" * 65]
        return "0x0a2b8f36" + encode(types, values).hex()
    assert classify_input({"to": "0x" + "ee" * 20}, proxy, adapter, types) == "direct"
    assert classify_input({"to": proxy, "input": envelope(adapter)}, proxy, adapter, types) == "relay:adapter"
    other = classify_input({"to": proxy, "input": envelope("0x" + "0d" * 20)}, proxy, adapter, types)
    assert other == "relay:other:0x0d0d0d0d:998b5942", other
    assert classify_input({"to": proxy, "input": "0xdeadbeef"}, proxy, adapter, types).startswith("relay:shape:")
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    else:
        main()
