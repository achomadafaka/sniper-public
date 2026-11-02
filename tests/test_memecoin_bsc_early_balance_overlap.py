"""BAI cd3b9c72: the first live balances must overlap the existing early-quote join.

The quote's accepted USD frame still prices the raw custody balance and receipt booking.
Only the external RPC/venue answers are scripted; wallet lock, signer and ledger stay real.
"""
import threading
from types import SimpleNamespace

import pytest
from eth_account import Account

from sniper.execution import evm_real_seams as seams, exec_lock
from sniper.persistence import db_memecoin as m
from test_memecoin_evm_balance_beside_ladder import _c
from test_memecoin_evm_receipt_booking import Chain, MINT, POOL, _weth


def _entry(monkeypatch, *, chain="56", injected=False, envelope_change=None, listed=True,
           no_early=False, raw_failure=False, retry=False, take_failure=False,
           unknown_price=False):
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", chain if listed else "")
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "")
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", "")
    monkeypatch.setattr(seams, "quote_usd_price", lambda *a, **kw:
                        None if unknown_price else 999.0)
    from sniper.execution import evm_flap_quote_exit
    monkeypatch.setattr(evm_flap_quote_exit, "prewarm_after_fill", lambda *a: None)
    conn = _c()
    pid = m.reserve_pending(conn, mint=MINT, source_user_id="trader", strategy="alpha_b",
                            notional=10, reserved_at="2026-10-13T00:00:00Z", network_id=chain)
    acct = Account.create()
    key = exec_lock.evm_exec_lock_key(chain, acct.address)
    started = {kind: threading.Event() for kind in ("quote", "token")}
    done = {kind: threading.Event() for kind in started}
    calls, observed = {kind: 0 for kind in started}, {}
    selected_price = 999.0 if envelope_change or no_early else 2.0
    paid = int(round(10 / selected_price * 10**18))
    net = Chain(chain, acct.address, lambda owner: [
        (_weth(chain), owner, POOL, paid), (MINT, POOL, owner, 4 * 10**18)])

    def read(kind, value):
        if net.hash is None:
            with exec_lock.wallet_exec_lock(key, blocking=False) as held:
                assert not held, "a funded balance read must start under the wallet lock"
        calls[kind] += 1
        started[kind].set()
        try:
            if raw_failure and kind == "quote":
                raise RuntimeError("balance provider timeout")
            return value
        finally:
            done[kind].set()

    monkeypatch.setattr(seams, "erc20_balance_raw_checked",
                        lambda *a, **kw: read("quote", 100 * 10**18))
    quote = {"sellAmount": str(paid), "buyAmount": str(4 * 10**18),
             "issues": {"allowance": None}, "route": {"protocol": "pcs_v2"},
             "transaction": {"to": POOL, "data": "0x", "value": "0",
                             "gas": "500000", "gasPrice": "1000000000"}}

    def venue_quote(sell, buy, amount, *, slippage_bps=None):
        return dict(quote, sellAmount=str(amount), buyAmount=str(
            4 * 10**18 if str(buy).lower() == MINT.lower() else int(paid * .97)))

    def preflight(_quote, **kw):
        observed.setdefault("preflight_reads", []).append(dict(calls))
        return ((False, "entry_simulation_min_out: exact route minimum output reverted")
                if retry and len(observed["preflight_reads"]) == 1 else (True, "ok"))
    monkeypatch.setattr(seams, "preflight_weth_entry_quote", preflight)
    envelope = {"chain": chain, "taker": acct.address.lower(), "mint": MINT.lower(),
                "usd": 10.0, "quote_token_usd": 2.0, "quote": dict(quote),
                "atomic": paid, "slippage_bps": 2500,
                "observed_at": "2026-10-13T00:00:01Z", "rungs": []}
    from sniper.settings import memecoin as cfg
    envelope["slippage_bps"] = cfg.evm_entry_slippage_bps_for(chain)
    if envelope_change:
        envelope.update(envelope_change)
    if unknown_price:
        envelope["quote_token_usd"] = None

    def take(*a, **kw):
        observed["before_take"] = {kind: event.wait(0.2) for kind, event in started.items()}
        if take_failure:
            raise RuntimeError("early quote failed")
        return envelope

    # Observe the real executor's frame below the price binding; no custody seam is replaced.
    from sniper.execution import evm_swap_executor
    original = evm_swap_executor.execute_entry_evm

    def observe_execution(*a, **kw):
        observed["price"] = kw["sell_token_usd"]
        fn = kw["usdc_fn"]

        def balance():
            value = fn()
            observed.setdefault("balances", []).append(value)
            return value
        if hasattr(fn, "last_raw"):
            balance.last_raw = fn.last_raw
        kw["usdc_fn"] = balance
        first = kw.get("first_balances_fn")
        if first is not None:
            def observe_first():
                values = first()
                observed.setdefault("balances", []).append(values[0])
                return values
            kw["first_balances_fn"] = observe_first
        out = original(*a, **kw)
        observed["last_raw"] = getattr(fn, "last_raw", lambda: "injected")()
        return out
    monkeypatch.setattr(evm_swap_executor, "execute_entry_evm", observe_execution)
    usdc_fn = (lambda: read("quote", 123.0)) if injected else None
    if injected == "falsey":
        class FalseyBalance:
            def __bool__(self):
                return False

            def __call__(self):
                return read("quote", 123.0)
        usdc_fn = FalseyBalance()
    ex = seams.build_real_evm_executor(
        acct=acct, chain_id=chain, rpc_url="https://fixture.invalid", http=net.http,
        venue_quote_fn=venue_quote,
        usdc_fn=usdc_fn,
        token_fn=lambda mint: read("token", (4.0 if net.hash else 0.0, 18)))
    execute = exec_lock.serialize_executor(ex["execute_fn"], key)
    observed["run"] = lambda: execute(
        conn, pid, MINT, 10.0, "2026-10-13T00:00:01Z",
        early_quote=None if no_early else SimpleNamespace(take=take))
    return conn, pid, observed, calls, done


@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_raw_balances_start_before_early_quote_join_and_use_accepted_price(monkeypatch, chain):
    conn, pid, got, calls, _done = _entry(monkeypatch, chain=chain)
    result = got["run"]()
    assert got["before_take"] == {"quote": True, "token": True}
    assert result["status"] == "filled", result
    assert got["price"] == 2.0
    assert got["last_raw"] == 100 * 10**18
    assert got["balances"] == [200.0]
    assert m.pending_row(conn, pid)["evm_quote_asset_usd"] == 2.0
    assert m.get_position(conn, result["position_id"])["entry_notional"] == pytest.approx(10.0)
    assert got["preflight_reads"] == [{"quote": 1, "token": 1}]
    assert calls["quote"] == 1


@pytest.mark.parametrize("injected", [True, "falsey"])
@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_injected_usd_balance_keeps_its_original_units(monkeypatch, chain, injected):
    _conn, _pid, got, calls, _done = _entry(monkeypatch, chain=chain, injected=injected)
    result = got["run"]()
    assert got["before_take"] == {"quote": True, "token": True}
    assert result["status"] == "filled", result
    assert got["balances"] == [123.0]
    assert got["preflight_reads"] == [{"quote": 1, "token": 1}]
    assert calls["quote"] == 1


@pytest.mark.parametrize("change", [{"mint": "0x" + "88" * 20}, {"usd": 11.0},
                                    {"taker": "0x" + "99" * 20}, {"chain": "1"}])
@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_discarded_early_quote_rebinds_raw_balances_to_fresh_frame(monkeypatch, chain, change):
    _conn, _pid, got, _calls, _done = _entry(monkeypatch, chain=chain, envelope_change=change)
    result = got["run"]()
    assert got["before_take"] == {"quote": True, "token": True}
    assert result["status"] == "filled", result
    assert got["price"] == 999.0
    assert got["balances"] == [99900.0]


@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_min_out_retry_repeats_both_live_reads(monkeypatch, chain):
    _conn, _pid, got, calls, _done = _entry(monkeypatch, chain=chain, retry=True)
    result = got["run"]()
    assert got["before_take"] == {"quote": True, "token": True}
    assert result["status"] == "filled", result
    assert got["preflight_reads"] == [{"quote": 1, "token": 1}, {"quote": 2, "token": 2}]
    assert calls["quote"] == 2


@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_raw_provider_failure_stays_zero_with_unknown_raw_word(monkeypatch, chain):
    _conn, _pid, got, _calls, _done = _entry(monkeypatch, chain=chain, raw_failure=True)
    result = got["run"]()
    assert got["before_take"] == {"quote": True, "token": True}
    assert result["status"] == "filled", result
    assert got["last_raw"] is None
    assert got["balances"] == [0.0]


@pytest.mark.parametrize("option", ["listed", "no_early"])
@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_other_paths_keep_the_existing_read_sequence(monkeypatch, chain, option):
    options = {"listed": False} if option == "listed" else {"no_early": True}
    _conn, _pid, got, calls, _done = _entry(monkeypatch, chain=chain, **options)
    result = got["run"]()
    assert result["status"] == "filled", result
    if option == "listed":
        assert got["before_take"] == {"quote": False, "token": False}
    assert got["preflight_reads"] == [{"quote": 1, "token": 1}]
    assert calls["quote"] == 1


@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_failed_early_quote_leaves_only_bounded_readers(monkeypatch, chain):
    _conn, _pid, got, calls, done = _entry(monkeypatch, chain=chain, take_failure=True)
    with pytest.raises(RuntimeError, match="early quote failed"):
        got["run"]()
    assert got["before_take"] == {"quote": True, "token": True}
    assert all(event.wait(1.0) for event in done.values())
    assert calls == {"quote": 1, "token": 1}


@pytest.mark.parametrize("chain", ["56", "8453", "4663"])
def test_unknown_quote_price_aborts_after_retiring_the_read_pair(monkeypatch, chain):
    conn, pid, got, calls, done = _entry(monkeypatch, chain=chain, unknown_price=True)
    result = got["run"]()
    assert got["before_take"] == {"quote": True, "token": True}
    assert result == {"status": "aborted", "reason": "quote-token price unavailable"}
    assert m.pending_row(conn, pid)["status"] == "failed"
    assert all(event.wait(1.0) for event in done.values())
    assert calls == {"quote": 1, "token": 1}
