import json
import sqlite3
import threading
import time
import tempfile
from contextlib import closing

from eth_account import Account
import pytest

from sniper.execution import evm_real_seams as seams
from sniper.execution import evm_swap_executor, evm_wallet
from sniper.execution import evm_native_float
from sniper.persistence import db_memecoin as m
from sniper.persistence import db_core
from sniper.services import memecoin_evm_risk as risk
from sniper.settings import memecoin as cfg

AMOUNT = 67_670_902_865_186_024
NATIVE_BEFORE = 102_547_358_800_000_000


def probe_for_chain(monkeypatch, chain, read, gas_read=lambda: 60_000_000,
                    reserve_read=lambda: .00255, spendable="", ledger_path=None,
                    gas_inputs=(.00005, .00002, .0006)):
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "1,56,4663,8453")
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", spendable)
    monkeypatch.setattr(cfg, "max_concurrent_for", lambda _: 2)
    monkeypatch.setattr(cfg, "gas_entry_native_for", lambda _: gas_inputs[0])
    monkeypatch.setattr(cfg, "gas_approval_native_for", lambda _: gas_inputs[1])
    monkeypatch.setattr(cfg, "gas_exit_native_for", lambda _: gas_inputs[2])
    monkeypatch.setattr(cfg, "gas_reserve_min_native_for", lambda _: 0)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_LOW_WEI", 2_000_000_000_000_000)
    monkeypatch.setattr(evm_wallet, "native_balance_raw_or_none", lambda *a, **k: read())
    if reserve_read is not None:
        monkeypatch.setattr(risk, "required_entry_native", lambda *a, **kwargs: reserve_read())
    monkeypatch.setattr(evm_swap_executor, "execute_entry_evm",
                        lambda *a, **k: {"status": "aborted", "probe": k["native_float_fn"]({})})

    def http(*args, body=None, **kwargs):
        method = json.loads(body).get("method") if body else None
        return 200, {"result": hex(gas_read()) if method == "eth_gasPrice" else "0x1"}, None

    test_dir = tempfile.TemporaryDirectory()
    conn = sqlite3.connect(ledger_path or test_dir.name + "/book.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint="0x" + "ab" * 20, source_user_id="source",
                            strategy="alpha_b", notional=50, network_id=chain,
                            reserved_at="2026-10-09T15:28:24Z")
    executor = seams.build_real_evm_executor(
        acct=Account.create(), chain_id=chain, rpc_url="rpc", http=http,
        usdc_fn=lambda: 100, token_fn=lambda _: (0, 18), price_fn=lambda: 750)
    try:
        probe = executor["execute_fn"](conn, pid, "0x" + "ab" * 20, 50,
                                        "2026-10-09T15:28:24Z")["probe"]
        if probe is None:
            test_dir.cleanup()
        else:
            probe._test_db_dir = test_dir
        return probe
    finally:
        conn.close()


def transactions(chain):
    quote_token = evm_wallet.CHAIN_QUOTE[chain]["token"]
    return ({"to": quote_token, "data": "0x2e1a7d4d%064x" % AMOUNT, "value": 0},
            {"value": AMOUNT, "gas": 1_200_000})


@pytest.mark.parametrize("chain", ["56", "4663", "8453", "1"])
def test_bsc_stalled_float_read_keeps_unwrap_without_waiting(monkeypatch, chain):
    started, release = threading.Event(), threading.Event()

    def read():
        started.set()
        release.wait(5)
        return NATIVE_BEFORE

    probe = probe_for_chain(monkeypatch, chain, read)
    try:
        assert started.wait(1)
        before = time.perf_counter()
        assert probe.covers(*transactions(chain)) is False
        elapsed = time.perf_counter() - before
        if chain in {"56", "8453"}:
            assert elapsed < .1, "Entry waited for an optional native balance read"
        else:
            assert elapsed >= .8, "Other chains must retain their existing read wait"
    finally:
        release.set()
        probe._thread.join(1)


@pytest.mark.parametrize("native, covered", [(NATIVE_BEFORE, True), (None, False),
    (0, False), (AMOUNT, False), (AMOUNT + 2_400_000_000_000_000, False),
    (AMOUNT + 2_550_000_000_000_000, True)])
@pytest.mark.parametrize("chain", ["56", "8453"])
def test_bsc_consumes_only_ready_native_balance_with_gas_cover(monkeypatch, native, covered, chain):
    probe = probe_for_chain(monkeypatch, chain, lambda: native)
    probe._thread.join(1)
    assert probe.covers(*transactions(chain)) is covered


def test_bsc_failed_background_read_keeps_unwrap(monkeypatch):
    def unavailable():
        raise TimeoutError("balance read unavailable")

    probe = probe_for_chain(monkeypatch, "56", unavailable)
    probe._thread.join(1)
    assert probe.covers(*transactions("56")) is False


@pytest.mark.parametrize("blocked", ["gas", "reserve"])
def test_bsc_unfinished_gas_or_book_read_keeps_unwrap_without_waiting(monkeypatch, blocked):
    started, release = threading.Event(), threading.Event()

    def stalled():
        started.set()
        release.wait(5)
        return 60_000_000 if blocked == "gas" else .00255

    kwargs = {"gas_read" if blocked == "gas" else "reserve_read": stalled}
    probe = probe_for_chain(monkeypatch, "56", lambda: NATIVE_BEFORE, **kwargs)
    try:
        before = time.perf_counter()
        assert probe.covers(*transactions("56")) is False
        assert time.perf_counter() - before < .1
        assert started.wait(1), "Optional evidence must be gathered on the worker"
    finally:
        release.set()
        probe._thread.join(1)


@pytest.mark.parametrize("signal_source", ["manual", "onchain_watch"])
def test_bsc_retains_actual_book_reserve_beyond_automatic_slots(monkeypatch, tmp_path,
                                                              signal_source):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_FREE_AFTER_S", 60)
    path = tmp_path / "book.db"
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    for i in range(5):
        conn.execute("INSERT INTO memecoin_positions "
                     "(mint,source_user_id,strategy,opened_at,execution_mode,is_open,"
                     "network_id,signal_source,entry_notional) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("0x%040x" % i, "source", "alpha_b",
                      "2026-09-01T00:00:00Z", "real", 1, "56", signal_source, 50))
    conn.commit()
    assert m.automatic_funded_count(conn, "56") == 0
    conn.close()

    other = tmp_path / "unrelated.db"
    with closing(sqlite3.connect(other)) as empty:
        m.ensure_memecoin_tables(empty)
    monkeypatch.setenv("DB_PATH", str(other))

    probe = probe_for_chain(monkeypatch, "56", lambda: AMOUNT + 2_550_000_000_000_000,
                            reserve_read=None, ledger_path=str(path))
    probe._thread.join(1)
    assert probe.covers(*transactions("56")) is False
    # Five open liabilities plus this candidate once, not its reservation plus another candidate.
    assert probe._floor == 7_510_000_000_000_000


def test_bsc_failed_book_read_keeps_unwrap(monkeypatch):
    def failed():
        raise sqlite3.OperationalError("book unavailable")

    probe = probe_for_chain(monkeypatch, "56", lambda: NATIVE_BEFORE, reserve_read=failed)
    probe._thread.join(1)
    assert probe.covers(*transactions("56")) is False


def test_bsc_unknown_ledger_binding_keeps_unwrap(monkeypatch):
    monkeypatch.setattr(db_core, "_connection_db_path", lambda conn: None)
    assert probe_for_chain(monkeypatch, "56", lambda: NATIVE_BEFORE) is None


def test_bsc_stale_completed_probe_keeps_unwrap(monkeypatch):
    probe = probe_for_chain(monkeypatch, "56", lambda: NATIVE_BEFORE)
    probe._thread.join(1)
    monkeypatch.setattr(evm_native_float.time, "monotonic", lambda: probe._read_at + 2)
    assert probe.covers(*transactions("56")) is False


def test_bsc_spendable_flag_still_uses_nonblocking_full_book_probe(monkeypatch):
    probe = probe_for_chain(monkeypatch, "56", lambda: AMOUNT + 2_550_000_000_000_000,
                            reserve_read=lambda: .00751, spendable="56")
    probe._thread.join(1)
    assert probe.covers(*transactions("56")) is False
    assert probe._floor >= 7_510_000_000_000_000
    assert probe._wait_for_read is False


@pytest.mark.parametrize("chain", ["56", "8453"])
def test_bsc_probe_consume_latency_for_ready_and_pending_reads(monkeypatch, chain):
    samples = {"ready": [], "pending": []}
    for state in samples:
        for _ in range(20):
            release, started = threading.Event(), threading.Event()

            def read():
                started.set()
                release.wait(5)
                return NATIVE_BEFORE

            if state == "ready":
                release.set()
            probe = probe_for_chain(monkeypatch, chain, read)
            assert started.wait(1)
            if state == "ready":
                probe._thread.join(1)
            try:
                before = time.perf_counter()
                covered = probe.covers(*transactions(chain))
                samples[state].append((time.perf_counter() - before) * 1000)
                assert covered is (state == "ready")
            finally:
                release.set()
                probe._thread.join(1)
    for state, values in samples.items():
        values.sort()
        print("%s %s probe consume p50=%.4f ms p90=%.4f ms" %
              (chain, state, values[len(values) // 2], values[int(len(values) * .9) - 1]))
        assert values[-1] < 100, "Optional balance read delayed the BSC entry"


def test_base_spendable_probe_reads_its_book_on_the_background_thread(monkeypatch):
    readers = []
    probe = probe_for_chain(monkeypatch, '8453', lambda: NATIVE_BEFORE, spendable='8453',
        reserve_read=lambda: readers.append(threading.get_ident()) or .00255)
    probe._thread.join(1)
    assert readers and threading.get_ident() not in readers
    assert probe._wait_for_read is False
    assert probe.covers(*transactions('8453')) is True


def test_chain1_actual_execution_closure_passes_own_unsigned_reservation_floor(monkeypatch):
    captured = []
    original = evm_native_float.NativeFloatProbe
    def probe(*args, **kwargs):
        captured.append(kwargs['reserve_floor_wei'])
        return original(*args, **kwargs)
    monkeypatch.setattr(evm_native_float, 'NativeFloatProbe', probe)
    got = probe_for_chain(monkeypatch, '1', lambda: 10 ** 17, reserve_read=None,
                          spendable='1', gas_inputs=(.001, .0001, .001))
    got._thread.join(1)
    assert captured == [3_300_000_000_000_000]
    assert got.covers(*transactions('1')) is True
    assert got.decision_facts['book_count'] == 0
    assert got.decision_facts['book_pending_count'] == 0
    assert got.decision_facts['book_floor_wei'] == 3_300_000_000_000_000
