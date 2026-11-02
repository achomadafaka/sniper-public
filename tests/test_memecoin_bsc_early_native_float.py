"""Prepare the existing BSC float read during the early quote's wait; consume it without waiting."""
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from eth_account import Account

from sniper.execution import evm_native_float as floats, evm_real_seams as seams
from sniper.execution import evm_swap_executor, evm_wallet, exec_lock
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from test_memecoin_bsc_native_float_speed import AMOUNT, NATIVE_BEFORE, transactions
from test_memecoin_evm_receipt_booking import Chain, MINT, POOL, _seq


def _entry(monkeypatch, tmp_path, *, chain="56", mode="float", early=True, attempt=1,
           missing=False, force_lifi=False, native=NATIVE_BEFORE, pending=False,
           stale=False, factory_error=False, high_book=False, retry=False, real=False,
           take_failure=False, mutation=None, lane_snapshot=None):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", chain if mode == "float" else "")
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", chain if mode == "spendable" else "")
    monkeypatch.setenv("MEMECOIN_EVM_BALANCE_BESIDE_LADDER_CHAINS", "")
    monkeypatch.setattr(cfg, "max_concurrent_for", lambda _: 2)
    for name, value in (("gas_entry_native_for", .00005), ("gas_approval_native_for", .00002),
                        ("gas_exit_native_for", .0006), ("gas_reserve_min_native_for", 0)):
        monkeypatch.setattr(cfg, name, lambda _, v=value: v)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_LOW_WEI", 2_000_000_000_000_000)
    price = 10 * 10**18 / AMOUNT
    monkeypatch.setattr(seams, "quote_usd_price", lambda *a, **kw: price)
    monkeypatch.setattr(seams, "preflight_weth_entry_quote", lambda *a, **kw: (True, "ok"))
    from sniper.execution import evm_flap_quote_exit
    monkeypatch.setattr(evm_flap_quote_exit, "prewarm_after_fill", lambda *a: None)
    conn = sqlite3.connect(tmp_path / "book.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    if high_book:
        for i in range(5):
            conn.execute("INSERT INTO memecoin_positions "
                "(mint,source_user_id,strategy,opened_at,execution_mode,is_open,network_id,signal_source,entry_notional) "
                "VALUES (?,?,?,?,?,?,?,?,?)", ("0x%040x" % i, "u", "alpha_b", "2026-09-01T00:00:00Z",
                                               "real", 1, chain, "manual", 50))
        conn.commit()
    pid = m.reserve_pending(conn, mint=MINT, source_user_id="trader", strategy="alpha_b",
                            notional=10, reserved_at="2026-10-19T10:00:00Z", network_id=chain)
    acct = Account.create()
    net = Chain(chain, acct.address, lambda owner: [(MINT, POOL, owner, 4 * 10**18)])
    got = {"construct": 0, "reads": 0, "probes": [], "consumed": [], "stages": [], "signed": []}
    release, started = threading.Event(), threading.Event()
    if not pending:
        release.set()

    def raw_native(*a, **kw):
        if threading.current_thread().name != "native-float-read":
            return NATIVE_BEFORE  # the existing unwrap visibility baseline, when needed
        got["reads"] += 1
        got["reader_thread"] = threading.get_ident()
        started.set()
        assert release.wait(2), "test did not release the optional provider read"
        if isinstance(native, Exception):
            raise native
        return native
    monkeypatch.setattr(evm_wallet, "native_balance_raw_or_none", raw_native)
    original_probe = floats.NativeFloatProbe

    def make_probe(*a, **kw):
        got["construct"] += 1
        if factory_error:
            raise RuntimeError("optional probe could not start")
        probe = original_probe(*a, **kw)
        got["probes"].append(probe)
        if pending:
            monkeypatch.setattr(probe._thread, "join", lambda *a, **kw:
                                pytest.fail("entry waited for the optional float read"))
        return probe
    monkeypatch.setattr(floats, "NativeFloatProbe", make_probe)
    original_http = net.http

    def http(*a, body=None, **kw):
        request = json.loads(body)
        if request["method"] in ("eth_gasPrice", "eth_getBalance"):
            net.calls.append(request["method"])
            return 200, {"result": hex(60_000_000 if request["method"] == "eth_gasPrice" else 2 * 10**18)}, None
        status, response, error = original_http(*a, body=body, **kw)
        if request["method"] == "eth_getTransactionByHash" and isinstance(response.get("result"), dict):
            response["result"]["value"] = hex(AMOUNT)
        return status, response, error

    pre, tx = transactions(chain)
    tx = dict(tx, to=POOL, data="0x", gasPrice="60000000")
    quote = {"sellAmount": str(AMOUNT), "buyAmount": str(4 * 10**18),
             "issues": {"allowance": None}, "route": {"protocol": "flap_curve"},
             "pre_transactions": [dict(pre, gas=100000)], "transaction": tx}

    def venue(sell, buy, amount, **kw):
        return dict(quote, sellAmount=str(amount), buyAmount=str(
            4 * 10**18 if str(buy).lower() == MINT.lower() else int(AMOUNT * .97)))

    def take(*a, **kw):
        got["start_to_take_ms"] = (time.perf_counter() - got["entry_started"]) * 1000
        got["before_take"] = got["construct"]
        if got["probes"]:
            assert started.wait(1)
            if not pending:
                got["probes"][0]._thread.join(1)
            if stale:
                got["probes"][0]._read_at -= 2
        if take_failure:
            assert got["construct"] == got["reads"] == 1
            raise RuntimeError("early quote failed")
        return {"chain": chain, "taker": acct.address.lower(), "mint": MINT.lower(), "usd": 10.,
                "quote_token_usd": price,
                "quote": dict(quote, pre_transactions=[]) if mutation else quote, "atomic": AMOUNT,
                "slippage_bps": cfg.evm_entry_slippage_bps_for(chain),
                "observed_at": "2026-10-19T10:00:01Z"}

    if not real:
        def execute(*a, **kw):
            got["before_consume"] = got["construct"]
            if mutation:
                first_quote = kw["quote_fn"](MINT, 10.)
                assert not first_quote.get("pre_transactions")
                if mutation == "sign":
                    kw["sign_fn"](first_quote["transaction"])
                elif mutation == "approve":
                    kw["approve_fn"](POOL, AMOUNT)
                else:
                    assert kw["quote_fn"](MINT, 10.).get("pre_transactions")
            for _ in range(2 if retry else 1):
                callback = kw["native_float_fn"]
                consumed_at = time.perf_counter()
                try:
                    probe = callback(quote) if callback else None
                except Exception:
                    probe = None
                got.setdefault("callback_ms", []).append((time.perf_counter() - consumed_at) * 1000)
                got["consumed"].append(probe)
                got.setdefault("covered", []).append(probe.covers(pre, tx) if probe else False)
            return {"status": "aborted", "reason": "fixture"}
        monkeypatch.setattr(evm_swap_executor, "execute_entry_evm", execute)
    original_sign = seams.sign_tx
    monkeypatch.setattr(seams, "sign_tx", lambda tx, *a, **kw:
        got["signed"].append(dict(tx)) or original_sign(tx, *a, **kw))
    ex = seams.build_real_evm_executor(acct=acct, chain_id=chain, rpc_url="https://fixture.invalid",
        http=http, venue_quote_fn=venue, usdc_fn=_seq([100., 90.]), token_fn=lambda _: (0., 18),
        force_lifi=force_lifi)
    key = exec_lock.evm_exec_lock_key(chain, acct.address)
    execute = exec_lock.serialize_executor(ex["execute_fn"], key)
    try:
        got["entry_started"] = time.perf_counter()
        token = floats.LANE_SNAPSHOT.set((chain, lane_snapshot)) if lane_snapshot else None
        got["result"] = execute(conn, pid + 1 if missing else pid, MINT, 10., "2026-10-19T10:00:01Z",
            early_quote=SimpleNamespace(take=take) if early else None, trace_attempt_base=attempt,
            trace=SimpleNamespace(mark=lambda *a, **kw: None,
                mark_substage=lambda name, **kw: got["stages"].append((name, kw)),
                stage_completed_at=lambda _: "2026-10-19T10:00:01Z"))
        if token is not None:
            floats.LANE_SNAPSHOT.reset(token)
        got["pending"] = m.pending_row(conn, pid)
        got["positions"] = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE mint=?", (MINT,)).fetchone()[0]
        return got
    finally:
        release.set()
        for probe in got["probes"]:
            threading.Thread.join(probe._thread, 1)
        conn.close()


@pytest.mark.parametrize("mode", ["float", "spendable"])
def test_bsc_prepares_one_raw_probe_before_early_quote_wait_and_consumes_it_once(monkeypatch, tmp_path, mode):
    got = _entry(monkeypatch, tmp_path, mode=mode, retry=True)
    assert got["before_take"] == got["before_consume"] == 1
    assert got["construct"] == got["reads"] == 2
    assert got["consumed"][0] is got["probes"][0]
    assert got["consumed"][1] is got["probes"][1]
    assert got["covered"][0] is True
    assert got["reader_thread"] != threading.get_ident()


@pytest.mark.parametrize("option", ["pending", "stale", "none", "zero", "read_error", "high_book", "factory_error"])
def test_unknown_stale_or_short_prepared_float_keeps_full_unwrap_without_new_read(monkeypatch, tmp_path, option):
    options = {"none": {"native": None}, "zero": {"native": 0},
               "read_error": {"native": TimeoutError("provider timeout")},
               "high_book": {"high_book": True, "native": AMOUNT + 2_550_000_000_000_000}}
    got = _entry(monkeypatch, tmp_path, **options.get(option, {option: True}))
    assert got["before_take"] == got["construct"] == 1
    assert got["covered"] == [False]
    assert got["reads"] == (0 if option == "factory_error" else 1)
    if option == "high_book":
        assert got["probes"][0]._floor == 7_510_000_000_000_000


@pytest.mark.parametrize("options", [{"missing": True}, {"mode": "off"}, {"early": False},
    {"chain": "8453"}, {"chain": "4663"}, {"attempt": 2}, {"force_lifi": True}])
def test_unrelated_entry_paths_keep_demand_probe_timing(monkeypatch, tmp_path, options):
    got = _entry(monkeypatch, tmp_path, **options)
    assert got.get("before_take", 0) == got.get("before_consume", 0) == 0
    if options.get("missing") or options.get("mode") == "off":
        assert got["construct"] == 0


@pytest.mark.parametrize("mutation", ["sign", "approve", "preflight_retry"])
def test_native_retry_after_first_route_without_unwrap_gets_fresh_probe(monkeypatch, tmp_path, mutation):
    got = _entry(monkeypatch, tmp_path, mutation=mutation)
    assert got["before_take"] == 1
    assert got["construct"] == got["reads"] == 2
    assert got["consumed"] == [got["probes"][1]]


@pytest.mark.parametrize("pending", [False, True])
def test_real_bsc_executor_skips_only_ready_full_book_covered_unwrap(monkeypatch, tmp_path, pending):
    got = _entry(monkeypatch, tmp_path, real=True, pending=pending)
    assert got["before_take"] == 1
    assert got["result"]["status"] == "filled", got["result"]
    assert got["positions"] == 1 and got["pending"]["status"] == "confirmed"
    assert got["construct"] == got["reads"] == 1
    unwraps = [tx for tx in got["signed"] if tx.get("data", "").startswith("0x2e1a7d4d")]
    assert len(unwraps) == int(pending)
    assert any(name == "evm.native_float" and fields["outcome"] ==
               ("unwrap_kept" if pending else "unwrap_skipped") for name, fields in got["stages"])


def test_failed_early_quote_leaves_only_one_bounded_optional_reader(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="early quote failed"):
        _entry(monkeypatch, tmp_path, take_failure=True)
