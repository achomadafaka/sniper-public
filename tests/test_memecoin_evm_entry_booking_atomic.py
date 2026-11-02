"""An EVM entry fill is booked in ONE transaction: position, entry fill, reservation confirmed.

Found 2026-09-29 by the Ink crash matrix (beta/ink-runner 8edad0d5e). The three writes were three
commits. A process that died after the position committed left a funded position with no entry
fill while its reservation still held the swap's hash. The restart then counted the exposure twice
(the open position and the reservation). On Ink, the receipt recovery tried to book the same swap
again, and the open-position unique index turned that into an orphaned row.

A death anywhere inside the booking (SystemExit, which no `except Exception` absorbs) must leave
all three or none. Chain 8453, the real executor, network scripted at the JSON-RPC seam.
"""
import json
import sqlite3

import pytest
from eth_account import Account

from sniper.execution import evm_real_seams as S
from sniper.persistence import db_memecoin as m


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]
        it["i"] += 1
        return v
    return f


def _enter(monkeypatch):
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="t", network_id="8453")
    swap_to = "0x" + "44" * 20
    seen = {}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        pair = (str(sell).lower(), str(buy).lower())
        if pair[::-1] in seen:
            out = str(int(seen[pair[::-1]] * 0.97))
        else:
            seen[pair] = int(amount)
            out = "1"
        return {"sellAmount": str(amount), "buyAmount": out, "issues": {"allowance": None},
                "transaction": {"to": swap_to, "data": "0x", "value": "0",
                                "gas": "500000", "gasPrice": "1000000000"}}

    from evm_receipt_fake import MISS, answer
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    acct = Account.create()

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_sendRawTransaction":
            return 200, {"result": "0xhash"}, None
        hit = answer(meth, req.get("params") or [], owner=acct.address, mint="0xtok",
                     quote=CHAIN_QUOTE["8453"]["token"], coin=5, paid=10 * 10 ** 18)
        if hit is not MISS:                         # the swap's own receipt books it
            return 200, {"result": hit}, None
        return 200, {"result": "0x0"}, None

    ex = S.build_real_evm_executor(acct=acct, chain_id="8453", rpc_url="https://rpc",
                                   http=http, venue_quote_fn=venue_quote,
                                   usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))

    def run():
        try:
            return ex["execute_fn"](c, pid, "0xtok", 10.0, "2026-07-09T00:00:01Z")
        except SystemExit:
            if c.in_transaction:           # a dead process keeps nothing uncommitted
                c.rollback()
            return None
    return c, pid, run


def _state(c, pid):
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    return {"positions": one("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real'"),
            "entry_fills": one("SELECT COUNT(*) FROM memecoin_fills WHERE kind='entry'"),
            "pending": one("SELECT status FROM memecoin_pending_entries WHERE id=?", pid)}


def test_a_fill_books_all_three(monkeypatch):
    c, pid, run = _enter(monkeypatch)
    assert run()["status"] == "filled"
    assert _state(c, pid) == {"positions": 1, "entry_fills": 1, "pending": "confirmed"}


@pytest.mark.parametrize("dies_in", ["insert_fill", "confirm_pending"])
def test_a_death_inside_the_booking_leaves_none_of_it(monkeypatch, dies_in):
    c, pid, run = _enter(monkeypatch)
    real = getattr(m, dies_in)

    def dying(*_a, **_k):
        raise SystemExit("process died while booking the fill")
    monkeypatch.setattr(m, dies_in, dying)
    assert run() is None
    monkeypatch.setattr(m, dies_in, real)
    got = _state(c, pid)
    assert got == {"positions": 0, "entry_fills": 0, "pending": "reserved"}, got
    tx_sig = m.pending_row(c, pid)["tx_sig"]
    assert tx_sig and len(tx_sig) == 66, "recovery still owns the swap's identity"


def test_no_network_read_runs_inside_the_booking_transaction(monkeypatch):
    """The executed-pool receipt read (up to the hedged 12 s budget) runs after the commit, so the
    write lock is never held across network I/O."""
    from sniper.services import memecoin_executed_pool as pool
    c, pid, run = _enter(monkeypatch)
    inside = []

    def watched(_network_id, _receipt, **_kw):
        inside.append(c.in_transaction)
        return None, None, None
    monkeypatch.setattr(pool, "executed_pool_from_receipt", watched)
    assert run()["status"] == "filled"
    assert inside == [False], "the receipt read ran inside the booking transaction"
