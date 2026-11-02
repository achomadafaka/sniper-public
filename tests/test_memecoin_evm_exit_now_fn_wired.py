"""EVM mirror of tests/test_memecoin_solana_exit_now_fn_wired.py. Finishes 9ff18f3d
(2026-09-09, Alpha review): `execute_exit_evm` grew a `now_fn` parameter but the one LIVE call
site -- `exit_fn` inside `build_real_evm_executor`, `evm_real_seams.py` -- never passed it. This
drives the real seam exactly as the live caller does (`ex["exit_fn"](conn, position, exit_reason,
now_iso)`, see `sniper.services.memecoin_real_runner.make_venue_exit_fn`), not
`execute_exit_evm` with hand-made kwargs.

Chain "8453" is pinned to a USD-quoted shape (`kind: "usd"`, 1 quote-token == $1) by the autouse
fixture below, so the test needs no ETH/USD mark and no WETH plumbing. Production chain 1 quotes in
WETH since 2026-09-30 (eth-mainnet spec); the fixture keeps this test about the now_fn wiring.
"""
import json
import sqlite3

from eth_account import Account

from sniper.execution.evm_real_seams import build_real_evm_executor
from sniper.persistence import db_memecoin as m

import pytest

from sniper.execution import evm_wallet as W

DECISION_AT = "2026-09-09T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _usd_quoted_chain_1(monkeypatch):
    monkeypatch.setitem(W.CHAIN_QUOTE, "8453", {"token": W.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})
    # The byte guard of the chain-1 private branch (step 2) refuses a chain-1 send to any URL but
    # Flashbots. This file is about `now_fn`, on a USD-shaped stand-in, so the branch is off here.
    from sniper.execution import free_endpoint_registry
    monkeypatch.setattr(free_endpoint_registry, "PRIVATE_ONLY_SUBMIT", {})
MINT = "0x" + "22" * 20
SPENDER = "0x" + "55" * 20


def _open_position(conn):
    pid = m.open_position(
        conn, source_user_id="u1", source_handle="(real)", mint=MINT, ticker="",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at=DECISION_AT, strategy="alpha_b", execution_mode="real",
        network_id="8453")
    m.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at=DECISION_AT, tx_sig="E")
    return m.get_position(conn, pid)


def _http(method, url, headers=None, body=None, timeout=10):
    if method != "POST":
        return 200, {}, None
    meth = json.loads(body).get("method")
    if meth == "eth_getTransactionCount":
        return 200, {"result": "0x1"}, None
    if meth == "eth_gasPrice":
        return 200, {"result": "0x3b9aca00"}, None
    if meth == "eth_getBalance":
        return 200, {"result": hex(10 ** 18)}, None       # 1 ETH native -- covers any exit gas
    if meth == "eth_call":
        return 200, {"result": "0x" + "f" * 64}, None      # huge balance/allowance either way
    if meth == "eth_getTransactionReceipt":
        return 200, {"result": {"status": "0x1"}}, None
    return 200, {"result": None}, None


def _quote(sell_token, buy_token, amount, *, slippage_bps=None):
    return {"sellAmount": str(amount), "buyAmount": "15000000",
            "otherAmountThreshold": "14000000",
            "transaction": {"to": SPENDER, "data": "0x", "value": "0",
                            "gas": "300000", "gasPrice": "1000000000"},
            "issues": {"allowance": {"spender": SPENDER}}}


def _build_executor(usdc_values):
    usdc = iter(usdc_values)
    return build_real_evm_executor(
        acct=Account.create(), chain_id="8453", rpc_url="rpc", http=_http,
        venue_quote_fn=_quote, usdc_fn=lambda: next(usdc), token_fn=lambda mint: 10.0,
        backend_label="auto_free")


def test_the_live_seam_stamps_three_independent_instants_not_one_frozen_value():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    position = _open_position(conn)
    ex = _build_executor([0.0, 15.0])

    result = ex["exit_fn"](conn, position, "mirror_exit", DECISION_AT)

    assert result["status"] == "closed"
    p = m.get_position(conn, position["id"])
    assert p["close_submitted_at"] != DECISION_AT, "still stamping the frozen decision instant"
    assert p["closed_at"] != DECISION_AT, "still stamping the frozen decision instant"
    assert p["close_submitted_at"] <= p["closed_at"], "submit must not be recorded after close"


def test_the_live_seam_still_closes_when_the_real_clock_raises(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    position = _open_position(conn)
    # Built BEFORE the patch: the executor's OWN submission-audit clock (unrelated to this
    # change, bound once at build time) keeps working. Only `exit_fn`'s close_submitted_at/
    # closed_at clock is re-imported fresh on every call and sees the raise.
    ex = _build_executor([0.0, 15.0])

    def _raising_clock():
        raise RuntimeError("clock down")
    monkeypatch.setattr("sniper.util.clock.real_utc_now_iso", _raising_clock)

    result = ex["exit_fn"](conn, position, "mirror_exit", DECISION_AT)

    assert result["status"] == "closed", "a raising clock must never fail a real sell"
    p = m.get_position(conn, position["id"])
    assert p["close_submitted_at"] == DECISION_AT
    assert p["closed_at"] == DECISION_AT


@pytest.fixture(autouse=True)
def _generic_usd_quote_fixture(monkeypatch):
    """Generic exit plumbing uses a USD-shaped stand-in; ETH canonical accounting has fork coverage."""
    from sniper.execution import evm_wallet as wallet
    monkeypatch.setitem(wallet.CHAIN_QUOTE, "8453",
                        {"token": wallet.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})
