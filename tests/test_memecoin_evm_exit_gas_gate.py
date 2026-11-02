"""Exit affordability gate (PRC 49252, 2026-09-03).

A node validates gas*price+value against COMMITTED native balance at ADMISSION -- before the tx
ever reaches a mempool -- so a signed exit that cannot clear that check is refused by every
endpoint, unmineable, and its nonce can never move. PRC 49252 sat that way for 27 minutes and
went from +23.6% to -6.2% before a human rescued it by hand. This is the prevention half of a
two-part fix; sniper-dc's eth_receipt_state (tests/test_evm_receipt_state_not_found.py) is the
repair/recovery half.

Entries are never touched here: `_sign` at its default `gas_price_override=None` is byte-for-byte
its original self, and every entry call site still calls it that way.
"""
import json
import sqlite3

import pytest
import rlp
from eth_account import Account

from sniper.execution import evm_real_seams as S
from sniper.persistence import db_memecoin as m

WETH_4663 = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"   # CHAIN_QUOTE["4663"]["token"]


def _decode_legacy_gas_price(raw_hex):
    decoded = rlp.decode(bytes.fromhex(raw_hex[2:]))
    return int.from_bytes(decoded[1], "big")


# Native that affords the gate's own unwrap (UNWRAP_GAS_LIMIT x 1.5e10) but not the exit (3e15).
from sniper.services.memecoin_evm_gas_topup import UNWRAP_GAS_LIMIT as _UNWRAP_GAS_LIMIT
_BETWEEN = (_UNWRAP_GAS_LIMIT * 15 * 10 ** 9 + 3 * 10 ** 15) // 2
assert _UNWRAP_GAS_LIMIT * 15 * 10 ** 9 < _BETWEEN < 3 * 10 ** 15


class _Rpc:
    """A minimal, configurable JSON-RPC fake keyed by method. `native_wei` is fixed for the whole
    test -- the gate never re-reads it after acting, by design (it trusts its own unwrap/reduce
    decision rather than re-verifying against a fake chain that cannot actually mutate state)."""

    def __init__(self, *, native_wei, weth_wei, gas_price_wei, nonce=1,
                native_readable=True, receipt_confirms=True):
        self.native_wei = native_wei
        self.weth_wei = weth_wei
        self.gas_price_wei = gas_price_wei
        self.nonce = nonce
        self.native_readable = native_readable
        self.receipt_confirms = receipt_confirms
        self.sent_raw = []

    def __call__(self, _method, _url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        method = req["method"]
        if method == "eth_getTransactionCount":
            return 200, {"result": hex(self.nonce)}, None
        if method == "eth_gasPrice":
            return 200, {"result": hex(self.gas_price_wei)}, None
        if method == "eth_getBalance":
            if not self.native_readable:
                return 200, {"result": None}, None
            return 200, {"result": hex(self.native_wei)}, None
        if method == "eth_call":
            return 200, {"result": "0x" + format(self.weth_wei, "064x")}, None
        if method == "eth_sendRawTransaction":
            self.sent_raw.append(req["params"][0])
            self.nonce += 1
            return 200, {"result": "0x" + "11" * 32}, None
        if method == "eth_getTransactionReceipt":
            if not self.receipt_confirms:
                return 200, {"result": None}, None
            return 200, {"result": {"status": "0x1", "blockNumber": "0x1",
                                    "transactionHash": req["params"][0]}}, None
        raise AssertionError(f"unexpected method {method}")


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_position(conn, pid=1, mint="0x" + "aa" * 20):
    conn.execute(
        "INSERT INTO memecoin_positions (id, mint, network_id, is_open, execution_mode, "
        "entry_price, opened_at) VALUES (?,?,?,1,'real',1.0,'2026-09-03T00:00:00+00:00')",
        (pid, mint, "4663"))
    conn.commit()
    return pid


def _executor(rpc):
    return S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc", http=rpc,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0)


TX = {"to": "0x" + "bb" * 20, "data": "0x", "value": 0, "gas": 200_000}


def test_signs_normally_when_native_covers_gas():
    rpc = _Rpc(native_wei=10 ** 18, weth_wei=0, gas_price_wei=10 ** 10)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    signed = ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX))

    assert signed is not None
    assert signed[0].startswith("0x")
    assert rpc.sent_raw == []          # no unwrap: the gate never broadcasts on the happy path
    assert not dict(m.get_position(conn, pid)).get("stuck_bag")


def test_unwraps_weth_to_cover_a_native_shortfall_then_signs():
    # gp = raw(1e10) * default gasprice_buffer(1.5) = 1.5e10; need = 200_000 * 1.5e10 = 3e15 wei.
    # unwrap_need = UNWRAP_GAS_LIMIT (150_000 since 2026-09-28) * 1.5e10 = 2.25e15 wei. native sits
    # between the two: can afford the unwrap, cannot afford the exit itself.
    rpc = _Rpc(native_wei=_BETWEEN, weth_wei=10 ** 18, gas_price_wei=10 ** 10)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    signed = ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX))

    assert signed is not None
    assert len(rpc.sent_raw) == 1                       # exactly one broadcast: the unwrap
    assert not dict(m.get_position(conn, pid)).get("stuck_bag")


def test_unwrap_targets_the_quote_token_with_a_withdraw_call():
    """The unwrap transaction itself, not just its effect."""
    from sniper.services.memecoin_evm_gas_topup import WITHDRAW_SELECTOR
    rpc = _Rpc(native_wei=_BETWEEN, weth_wei=10 ** 18, gas_price_wei=10 ** 10)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX))

    assert len(rpc.sent_raw) == 1
    decoded = rlp.decode(bytes.fromhex(rpc.sent_raw[0][2:]))
    to = "0x" + decoded[3].hex()
    data = decoded[5]
    assert to.lower() == WETH_4663.lower()
    assert data[:4].hex() == WITHDRAW_SELECTOR[2:]


@pytest.mark.parametrize("chain", ["5042", "1"])
def test_usdc_quote_never_uses_wrapped_native_withdraw(chain):
    """A USD quote balance cannot supply gas through a WETH withdraw call."""
    rpc = _Rpc(native_wei=260_000, weth_wei=100_000_000, gas_price_wei=1)
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id=chain, rpc_url="https://rpc", http=rpc,
        gasprice_buffer=2.0, usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0)
    conn = _conn()
    pid = _open_position(conn)
    conn.execute("UPDATE memecoin_positions SET network_id=? WHERE id=?", (chain, pid))

    signed = ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX))

    assert signed is not None
    assert rpc.sent_raw == []
    assert _decode_legacy_gas_price(signed[0]) == 1


def test_the_derisk_path_takes_its_own_owner_kind_not_close_claim():
    """derisk_fn passes its OWN owner_kind ("derisk_claim") through _make_exit_sign_fn, so the
    unwrap it triggers is tagged the same way. With no submission_transport configured (as here)
    _evm_send_for's owner_kind routing is a no-op -- this only proves the derisk call shape works
    end to end; the owner_kind pass-through itself is a one-line binding at each call site
    (evm_real_seams.py: exit_fn passes "close_claim", derisk_fn passes "derisk_claim") verified by
    direct reading, not by this test."""
    rpc = _Rpc(native_wei=_BETWEEN, weth_wei=10 ** 18, gas_price_wei=10 ** 10)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    signed = ex["_make_exit_sign_fn"](conn, pid, "derisk_claim")(dict(TX))

    assert signed is not None
    assert len(rpc.sent_raw) == 1


def test_reduces_gas_price_when_weth_is_insufficient_but_a_lower_fee_still_clears_the_node():
    # raw node price R = 1e10. gp (buffered 1.5x) = 1.5e10 -> need = 200_000*1.5e10 = 3e15.
    # native/gas_limit = 1.3e10, strictly between 1.1*R and 1.5*R -- admissible reduction.
    gas_limit = 200_000
    native = 1.3 * 10 ** 10 * gas_limit
    rpc = _Rpc(native_wei=int(native), weth_wei=0, gas_price_wei=10 ** 10)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    signed = ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX))

    assert signed is not None
    assert rpc.sent_raw == []          # reduced-fee path signs directly, no unwrap broadcast
    got_gas_price = _decode_legacy_gas_price(signed[0])
    assert got_gas_price == pytest.approx(int(native // gas_limit), abs=1)
    assert got_gas_price < int(1.5 * 10 ** 10)          # strictly below the buffered live price
    assert got_gas_price >= int(1.1 * 10 ** 10)         # still clears the admissibility floor
    assert not dict(m.get_position(conn, pid)).get("stuck_bag")


def test_refuses_to_sign_and_marks_stuck_bag_when_nothing_is_affordable():
    rpc = _Rpc(native_wei=100, weth_wei=0, gas_price_wei=10 ** 10)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    signed = ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX))

    assert signed is None
    assert rpc.sent_raw == []
    assert dict(m.get_position(conn, pid)).get("stuck_bag") == 1


def test_refuses_to_sign_when_native_balance_is_unreadable():
    """Unknown is not enough. An RPC failure must never read as affordability."""
    rpc = _Rpc(native_wei=10 ** 18, weth_wei=10 ** 18, gas_price_wei=10 ** 10,
              native_readable=False)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    signed = ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX))

    assert signed is None
    assert rpc.sent_raw == []
    assert dict(m.get_position(conn, pid)).get("stuck_bag") == 1


def test_falls_through_to_refusal_when_the_unwrap_never_confirms():
    """A dropped/unmined unwrap must not silently sign the underfunded exit anyway.

    The native must afford the gate's unwrap (UNWRAP_GAS_LIMIT x 1.5e10 = 2.25e15) but NOT the
    reduced-fee fallback ((native - value) // gas must stay under 1.1 x the node's 1e10). With the
    150k unwrap limit (2026-09-28) that window needs a larger exit: 300k gas, native 2.5e15
    (2.5e15 // 300_000 = 8.3e9 < 1.1e10)."""
    native = 25 * 10 ** 14
    assert _UNWRAP_GAS_LIMIT * 15 * 10 ** 9 <= native < 11 * 10 ** 9 * 300_000
    rpc = _Rpc(native_wei=native, weth_wei=10 ** 18, gas_price_wei=10 ** 10,
              receipt_confirms=False)
    ex = _executor(rpc)
    conn = _conn()
    pid = _open_position(conn)

    signed = ex["_make_exit_sign_fn"](conn, pid, "close_claim")(dict(TX, gas=300_000))

    assert signed is None
    assert len(rpc.sent_raw) == 1          # the unwrap WAS attempted, just never confirmed
    assert dict(m.get_position(conn, pid)).get("stuck_bag") == 1


def test_entries_are_unaffected_sign_keeps_its_original_contract():
    """`_sign` at its default gas_price_override=None must be identical to before this change."""
    rpc = _Rpc(native_wei=10 ** 18, weth_wei=10 ** 18, gas_price_wei=10 ** 10)
    ex = _executor(rpc)

    signed = ex["_sign"](dict(TX))

    assert signed is not None
    got_gas_price = _decode_legacy_gas_price(signed[0])
    # unbuffered raw(1e10) * default gasprice_buffer(1.5) -- the ORIGINAL, unmodified computation.
    assert got_gas_price == int(10 ** 10 * 1.5)
