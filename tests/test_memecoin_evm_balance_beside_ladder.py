"""C3 (spec 2026-10-01-memecoin-evm-entry-speed-design.md): on a chain listed in
MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS the executor starts its pre-trade balance reads at entry,
beside the quote ladder, and joins them where it reads them today: before the price gates and
before the first wallet mutation. Default off: an unlisted chain reads after the ladder, as before."""
import sqlite3
import threading
import time

import pytest

from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

_QUOTE = {"sellAmount": "10000000", "transaction": {"to": "0xrouter", "data": "0xdead"},
          "issues": {"allowance": {"spender": "0xspender"}}}


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _reserve(c, network_id="8453"):
    return m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                             notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id=network_id)


def _run(c, pid, network_id="8453", **seams):
    base = dict(approve_fn=lambda spender, amount: True, sign_fn=lambda tx: ("0xraw", "0xhash", 7),
                send_fn=lambda raw: None, confirm_fn=lambda h: "confirmed")
    base.update(seams)
    return execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id=network_id, usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", **base)


def _balances(order, first_read_started=None, first_read_done=None, delay_s=0.0):
    n = {"usdc": 0, "token": 0}

    def usdc_fn():
        n["usdc"] += 1
        if n["usdc"] == 1:
            order.append("usdc_pre_start")
            if first_read_started is not None:
                first_read_started.set()
            time.sleep(delay_s)
            if first_read_done is not None:
                first_read_done.set()
            return 100.0
        return 90.0

    def token_fn(_mint):
        n["token"] += 1
        return 0.0 if n["token"] == 1 else 5.0
    return usdc_fn, token_fn


@pytest.mark.parametrize("chain", ["8453", "56"])
def test_listed_chain_reads_balances_beside_the_ladder(monkeypatch, chain):
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", chain)
    c = _c(); pid = _reserve(c, chain)
    order, started = [], threading.Event()
    usdc_fn, token_fn = _balances(order, first_read_started=started)

    def quote_fn(_mint, _usd):
        # The read must already be running while the ladder is still out.
        order.append("ladder_waiting" if started.wait(2.0) else "ladder_timed_out")
        return dict(_QUOTE)

    result = _run(c, pid, network_id=chain, quote_fn=quote_fn, usdc_fn=usdc_fn, token_fn=token_fn)
    assert result["status"] == "filled"
    assert order[:2] == ["usdc_pre_start", "ladder_waiting"], order


def test_unlisted_chain_reads_after_the_ladder_as_before(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", "4663")
    c = _c(); pid = _reserve(c)
    order = []
    usdc_fn, token_fn = _balances(order)

    def quote_fn(_mint, _usd):
        order.append("ladder")
        return dict(_QUOTE)

    result = _run(c, pid, quote_fn=quote_fn, usdc_fn=usdc_fn, token_fn=token_fn)
    assert result["status"] == "filled"
    assert order[:2] == ["ladder", "usdc_pre_start"], order


@pytest.mark.parametrize("chain", ["8453", "56"])
def test_early_reads_are_joined_before_the_first_wallet_mutation(monkeypatch, chain):
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", chain)
    c = _c(); pid = _reserve(c, chain)
    order, done = [], threading.Event()
    usdc_fn, token_fn = _balances(order, first_read_done=done, delay_s=0.3)
    seen = {}

    def approve_fn(spender, amount):
        seen["read_done_at_approve"] = done.is_set()
        return True

    result = _run(c, pid, network_id=chain, quote_fn=lambda _m, _u: dict(_QUOTE), usdc_fn=usdc_fn,
                  token_fn=token_fn, approve_fn=approve_fn)
    assert result["status"] == "filled"
    assert seen == {"read_done_at_approve": True}


def test_a_failed_early_read_fails_exactly_like_today(monkeypatch):
    def boom():
        raise RuntimeError("balance read failed")

    outcomes = []
    for chains in ("", "8453"):
        monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", chains)
        c = _c(); pid = _reserve(c)
        with pytest.raises(RuntimeError, match="balance read failed"):
            _run(c, pid, quote_fn=lambda _m, _u: dict(_QUOTE), usdc_fn=boom,
                 token_fn=lambda _m: 0.0)
        outcomes.append(m.pending_row(c, pid) is not None)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("raw,want", [
    ("", frozenset()), (" 4663 , 56 ", frozenset({"4663", "56"})), ("4663,base", frozenset()),
])
def test_chain_list_parses_strictly(monkeypatch, raw, want):
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", raw)
    assert cfg.memecoin_evm_balance_beside_ladder_chains() == want


def test_a_min_out_retry_reads_both_balances_again(monkeypatch):
    # Only the first read site takes the early reads; the retry's pass must read fresh, or a
    # confirmed WETH unwrap between the passes would feed a stale raw to the unwrap guard (Gamma LOW).
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", "8453")
    c = _c(); pid = _reserve(c)
    calls = {"usdc": 0, "token": 0}

    def usdc_fn():
        calls["usdc"] += 1
        return 100.0 if calls["usdc"] <= 2 else 90.0

    def token_fn(_mint):
        calls["token"] += 1
        return 0.0 if calls["token"] <= 2 else 5.0

    sims = {"n": 0}

    def preflight(_quote):
        sims["n"] += 1
        if sims["n"] == 2:
            sims["reads_at_retry"] = (calls["usdc"], calls["token"])
        return ((False, "entry_simulation_min_out: exact route minimum output reverted")
                if sims["n"] == 1 else (True, "ok"))

    result = _run(c, pid, quote_fn=lambda _m, _u, slippage_bps=None: dict(_QUOTE),
                  usdc_fn=usdc_fn, token_fn=token_fn, entry_preflight_fn=preflight)
    assert result["status"] == "filled"
    # By the retry's simulation both balances were read twice: the early read, then the retry's own.
    assert sims["reads_at_retry"] == (2, 2)
