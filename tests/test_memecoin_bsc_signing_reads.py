"""BSC signing overlaps independent reads; it never guesses a nonce or gas price."""
import json
import threading

import pytest
from eth_account import Account

from sniper.execution import evm_real_seams as seams

TX = {"to": "0x" + "bb" * 20, "data": "0x", "value": 0, "gas": 200_000}


def executor(http, chain="56"):
    return seams.build_real_evm_executor(
        acct=Account.create(), chain_id=chain, rpc_url="https://fixture.invalid",
        http=http, usdc_fn=lambda: 100.0, token_fn=lambda _mint: 0.0)


def test_bsc_gas_read_starts_before_nonce_finishes(monkeypatch):
    gas_started = threading.Event()
    calls = []

    def http(_method, _url, *, body=None, **_kwargs):
        request = json.loads(body)
        method = request["method"]
        calls.append(method)
        if method == "eth_getTransactionCount":
            assert request["params"][1] == "pending"
            # A serial gas read cannot satisfy this handshake. No speed threshold.
            if not gas_started.wait(0.5):
                return 200, {"result": None}, None
            return 200, {"result": "0x0"}, None
        assert method == "eth_gasPrice"
        gas_started.set()
        return 200, {"result": "0xa"}, None

    signed_txs = []
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct:
                        (signed_txs.append(tx), ("0xraw", "0xhash"))[1])
    signed = executor(http)["_sign"](dict(TX, gasPrice="0x14"))
    assert signed == ("0xraw", "0xhash", 0)
    assert sorted(calls) == ["eth_gasPrice", "eth_getTransactionCount"]
    assert signed_txs == [dict(TX, nonce=0, gasPrice=20, chainId=56)]


@pytest.mark.parametrize("failed_method,answer", [
    ("eth_getTransactionCount", {"result": None}),
    ("eth_getTransactionCount", {"result": "wrong"}),
    ("eth_getTransactionCount", {"error": {"code": -32005}}),
    ("eth_gasPrice", {"result": None}),
    ("eth_gasPrice", {"result": "wrong"}),
    ("eth_gasPrice", {"error": {"code": -32005}}),
])
def test_unknown_reads_do_not_sign(monkeypatch, failed_method, answer):
    def http(_method, _url, *, body=None, **_kwargs):
        request = json.loads(body)
        result = answer if request["method"] == failed_method else {"result": "0xa"}
        return 200, result, None

    signed = []
    monkeypatch.setattr(seams, "sign_tx", lambda *args: signed.append(args))
    assert executor(http)["_sign"](TX) is None
    assert signed == []


@pytest.mark.parametrize("chain", ["8453", "4663"])
def test_non_bsc_read_order_and_exit_override_are_preserved(monkeypatch, chain):
    calls = []

    def http(_method, _url, *, body=None, **_kwargs):
        calls.append(json.loads(body)["method"])
        return 200, {"result": "0xa"}, None

    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct: ("0xraw", "0xhash"))
    assert executor(http, chain)["_sign"](TX) is not None
    assert calls == ["eth_getTransactionCount", "eth_gasPrice"]
    calls.clear()
    assert executor(http, chain)["_approve"](
        TX["to"], 123, send_fn=lambda _raw: "accepted", wait_for_receipt=False)
    assert calls == ["eth_getTransactionCount", "eth_gasPrice"]
    calls.clear()
    assert executor(http)["_sign"](TX, gas_price_override=7) is not None
    assert calls == ["eth_getTransactionCount"]


def test_thread_unavailable_falls_back_to_original_reads_once(monkeypatch):
    calls = []

    def no_thread(_thread):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", no_thread)
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_k: (calls.append("nonce"), 2)[1])
    monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_k: (calls.append("gas"), 10)[1])
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct: ("0xraw", "0xhash"))
    assert executor(None)["_sign"](TX) == ("0xraw", "0xhash", 2)
    assert calls == ["nonce", "gas"]


def test_bad_quoted_gas_returns_unsigned_from_worker_exception(monkeypatch):
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_k: 2)
    signed = []
    monkeypatch.setattr(seams, "sign_tx", lambda *args: signed.append(args))
    assert executor(None)["_sign"](dict(TX, gasPrice="wrong")) is None
    assert signed == []


@pytest.fixture
def entry_seams(monkeypatch):
    """Capture the real entry closures; read/claim/fee/sign behavior stays in the seam."""
    from sniper.execution import evm_swap_executor
    from sniper.persistence import db_memecoin as m
    from test_memecoin_evm_balance_beside_ladder import _c

    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663")
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "8453")
    monkeypatch.setenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS", "")
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", "")
    monkeypatch.setattr(seams, "quote_usd_price", lambda *_a, **_kw: 1.0)
    monkeypatch.setattr(seams, "eth_confirm", lambda *_a, **_kw: "confirmed")
    monkeypatch.setattr(seams, "eth_send_raw", lambda *_a, **_kw: "accepted")

    def build(chain):
        captured = {}

        def execute(_conn, **kwargs):
            captured.update(kwargs)
            return {"status": "aborted"}

        monkeypatch.setattr(evm_swap_executor, "execute_entry_evm", execute)
        ex = executor(None, chain)
        conn = _c()
        pid = m.reserve_pending(conn, mint=TX["to"], source_user_id="trader", strategy="alpha_b",
                                notional=10, reserved_at="t", network_id=chain)
        ex["execute_fn"](conn, pid, TX["to"], 10, "t")
        return ex, captured
    return build


def _entry_read(captured, operation, tx=TX):
    return (captured["sign_fn"](tx) if operation == "sign"
            else captured["approve_fn"](TX["to"], 123))


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("operation", ["sign", "approval"])
def test_entry_gas_starts_before_nonce_finishes(monkeypatch, entry_seams, chain, operation):
    gas_started, nonce_started = threading.Event(), threading.Event()
    caller = threading.get_ident()
    calls, signed = [], []

    def nonce(*_a, **_kw):
        assert threading.get_ident() == caller, "the caller owns nonce allocation"
        calls.append("nonce")
        nonce_started.set()
        assert gas_started.wait(0.5), "gas still waits behind nonce"
        return 5

    def gas(*_a, **_kw):
        calls.append("gas")
        gas_started.set()
        assert nonce_started.wait(0.5)
        return 100

    monkeypatch.setattr(seams, "eth_nonce", nonce)
    monkeypatch.setattr(seams, "eth_gas_price", gas)
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct, **_kw:
                        (signed.append(tx), ("raw", "hash"))[1])
    _ex, captured = entry_seams(chain)
    assert _entry_read(captured, operation, dict(TX, gasPrice="0xc8"))
    assert sorted(calls) == ["gas", "nonce"]
    assert signed[0]["nonce"] == 5
    assert signed[0]["gasPrice"] == (200 if operation == "sign" else 150)


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("operation", ["sign", "approval"])
@pytest.mark.parametrize("refresh_raw", [300, None])
def test_entry_refreshes_gas_expired_during_nonce_read(monkeypatch, entry_seams, chain, operation,
                                                     refresh_raw):
    now, reads, signed = [100.0], [], []
    _ex, captured = entry_seams(chain)
    observed = threading.Event()
    caller = threading.get_ident()

    def clock():
        stamp = now[0]
        if threading.get_ident() != caller and reads:
            observed.set()  # the memo owner is publishing this actual response timestamp
        return stamp

    def gas(*_a, **_kw):
        reads.append(now[0])
        return 100 if len(reads) == 1 else refresh_raw

    def nonce(*_a, **_kw):
        assert observed.wait(0.5), "cold gas did not finish beside nonce"
        now[0] = 101.1
        return 5

    monkeypatch.setattr(seams._time, "monotonic", clock)
    monkeypatch.setattr(seams, "eth_gas_price", gas)
    monkeypatch.setattr(seams, "eth_nonce", nonce)
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct, **_kw:
                        (signed.append(tx), ("raw", "hash"))[1])
    result = _entry_read(captured, operation, dict(TX, gasPrice="0xc8"))
    assert reads == [100.0, 101.1]
    if refresh_raw is None and operation == "approval":
        assert not result and not signed
    else:
        assert result
        assert signed[0]["gasPrice"] == (200 if refresh_raw is None else 450)


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("operation", ["sign", "approval"])
def test_failed_nonce_does_not_refresh_expired_gas(monkeypatch, entry_seams, chain, operation):
    now, reads, signed = [100.0], [], []
    _ex, captured = entry_seams(chain)
    observed = threading.Event()
    caller = threading.get_ident()

    def clock():
        stamp = now[0]
        if threading.get_ident() != caller and reads:
            observed.set()
        return stamp

    def gas(*_a, **_kw):
        reads.append(now[0])
        return 100

    def nonce(*_a, **_kw):
        assert observed.wait(0.5)
        now[0] = 101.1
        return None

    monkeypatch.setattr(seams._time, "monotonic", clock)
    monkeypatch.setattr(seams, "eth_gas_price", gas)
    monkeypatch.setattr(seams, "eth_nonce", nonce)
    monkeypatch.setattr(seams, "sign_tx", lambda *a, **kw: signed.append(a))
    assert not _entry_read(captured, operation)
    assert reads == [100.0]
    assert signed == []


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("operation", ["sign", "approval"])
def test_entry_reuses_fresh_gas_memo_without_extra_rpc(monkeypatch, entry_seams, chain, operation):
    reads, signed = [], []
    monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_kw: reads.append("gas") or 100)
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_kw: 5)
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct, **_kw:
                        (signed.append(tx), ("raw", "hash"))[1])
    ex, captured = entry_seams(chain)
    assert ex["_node_gas_price"]() == 150
    assert _entry_read(captured, operation)
    assert reads == ["gas"]
    assert signed[0]["gasPrice"] == 150


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("operation", ["sign", "approval"])
@pytest.mark.parametrize("failed_read", ["gas", "nonce"])
def test_entry_failed_reads_remain_unsigned(monkeypatch, entry_seams, chain, operation, failed_read):
    signed, gas_calls = [], []
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_kw: None if failed_read == "nonce" else 5)
    monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_kw:
                        gas_calls.append("gas") or (None if failed_read == "gas" else 100))
    monkeypatch.setattr(seams, "sign_tx", lambda *a, **kw: signed.append(a))
    _ex, captured = entry_seams(chain)
    assert not _entry_read(captured, operation)
    assert signed == []
    assert gas_calls == ["gas"]


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("operation", ["sign", "approval"])
def test_entry_thread_failure_keeps_serial_fallback(monkeypatch, entry_seams, chain, operation):
    calls = []

    def no_thread(_thread):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", no_thread)
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_kw: calls.append("nonce") or 5)
    monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_kw: calls.append("gas") or 100)
    monkeypatch.setattr(seams, "sign_tx", lambda *_a, **_kw: ("raw", "hash"))
    _ex, captured = entry_seams(chain)
    assert _entry_read(captured, operation)
    assert calls == ["nonce", "gas"]


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("stale_memo", [False, True])
def test_failed_node_gas_keeps_valid_quote_without_retry(monkeypatch, entry_seams, chain, stale_memo):
    reads, signed = [], []
    now = [100.0]
    monkeypatch.setattr(seams._time, "monotonic", lambda: now[0])
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_kw: 5)
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct, **_kw:
                        (signed.append(tx), ("raw", "hash"))[1])
    ex, captured = entry_seams(chain)
    if stale_memo:
        monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_kw: reads.append("gas") or 100)
        assert ex["_node_gas_price"]() == 150
        now[0] = 102.0
    monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_kw: reads.append("gas"))
    assert captured["sign_fn"](dict(TX, gasPrice="0xc8")) == ("raw", "hash", 5)
    assert reads == ["gas"] * (2 if stale_memo else 1)
    assert signed[0]["gasPrice"] == 200


@pytest.mark.parametrize("chain", ["8453", "4663"])
@pytest.mark.parametrize("operation", ["sign", "approval"])
def test_entry_uses_concurrent_memo_refresh_before_nonce_join(monkeypatch, entry_seams, chain,
                                                            operation):
    now, reads, signed = [100.0], [], []
    ex, captured = entry_seams(chain)
    observed = threading.Event()
    caller = threading.get_ident()

    def clock():
        stamp = now[0]
        if threading.get_ident() != caller and reads:
            observed.set()
        return stamp

    def gas(*_a, **_kw):
        reads.append(now[0])
        return 100 if len(reads) == 1 else 300

    def nonce(*_a, **_kw):
        assert observed.wait(0.5)
        now[0] = 101.1
        assert ex["_node_gas_price"]() == 450  # another reader refreshed the shared memo
        now[0] = 101.2
        return 5

    monkeypatch.setattr(seams._time, "monotonic", clock)
    monkeypatch.setattr(seams, "eth_gas_price", gas)
    monkeypatch.setattr(seams, "eth_nonce", nonce)
    monkeypatch.setattr(seams, "sign_tx", lambda tx, _acct, **_kw:
                        (signed.append(tx), ("raw", "hash"))[1])
    assert _entry_read(captured, operation, dict(TX, gasPrice="0xc8"))
    assert reads == [100.0, 101.1]
    assert signed[0]["gasPrice"] == 450


@pytest.mark.parametrize("acceptance,want_nonce,want_reads", [
    ("accepted", 6, 1), (False, 5, 2), (None, 6, 2),
])
def test_entry_approval_handoff_and_rejection_keep_nonce_ownership(monkeypatch, entry_seams,
                                                                 acceptance, want_nonce,
                                                                 want_reads):
    reads, gas_reads = [], []
    monkeypatch.setattr(seams, "eth_nonce", lambda *_a, **_kw: reads.append("nonce") or 5)
    monkeypatch.setattr(seams, "eth_gas_price", lambda *_a, **_kw: gas_reads.append("gas") or 100)
    monkeypatch.setattr(seams, "sign_tx", lambda *_a, **_kw: ("raw", "hash"))
    _ex, captured = entry_seams("4663")
    monkeypatch.setattr(seams, "eth_send_raw", lambda *_a, **_kw: acceptance)
    approved = captured["approve_fn"](TX["to"], 123)
    assert bool(approved) == (acceptance == "accepted")
    # An ambiguous approval retains its allocation but never hands a cached nonce to the swap.
    assert captured["sign_fn"](TX) == ("raw", "hash", want_nonce)
    assert reads == ["nonce"] * want_reads
    assert gas_reads == ["gas"]
