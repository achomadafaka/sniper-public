"""A wanted-key refusal stops the whole indexer run (2026-10-08 review EVM02).

`resolve_wanted` stopped its own reads on the first refusal (a 429 included), but `run()` never
saw that and went straight on to the tail and backfill reads on the same host, the one the 4663
money path also reads. Now a provider refusal ends the run before any further read; stored keys
and the wanted ids' progress are kept for the next pass. A malformed answer (not a list) is a
refusal too, never an empty range.
"""
import importlib.util
import json
import sqlite3

import pytest

from sniper.execution import evm_v4_quote as V
from sniper.persistence import db_core
from sniper.persistence import db_memecoin as m
from test_memecoin_v4_key_exhausted import CHAIN, INIT_LOG, POOL, _indexer

OTHER = "0x" + "ef" * 32
HEAD = 82_857_471


@pytest.fixture
def world(monkeypatch, tmp_path):
    ix = _indexer(monkeypatch, tmp_path)
    path = str(tmp_path / "ledger.db")

    def connect(*a, **k):
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db
    conn = connect()
    m.ensure_memecoin_tables(conn)
    conn.commit()
    monkeypatch.setattr(ix, "state_path", lambda *parts: tmp_path.joinpath(*parts[1:]))
    monkeypatch.setattr(V, "wanted_pool_keys_path", lambda chain: tmp_path / ("wanted_%s.txt" % chain))
    monkeypatch.setattr(ix, "_index_rpc_url", lambda *a: "https://archive.invalid")
    # Both the tail (high < head) and the backfill (low > floor) are due.
    monkeypatch.setattr(ix, "_load_state", lambda: {CHAIN: {"low": HEAD - 100, "high": HEAD - 100}})
    monkeypatch.setattr(ix, "_save_state", lambda *a: None)
    monkeypatch.setattr(ix.time, "sleep", lambda s: None)
    monkeypatch.setattr(db_core, "get_connection", connect)
    reads = []
    answers = {}

    def rpc(url, method, params, timeout=60):
        if method == "eth_blockNumber":
            return hex(HEAD)
        topics = params[0]["topics"]
        kind = topics[1] if len(topics) > 1 else "scan"
        reads.append(kind)
        queue = answers.get(kind) or [[]]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(ix, "_rpc", rpc)

    def run(wanted=(POOL,)):
        if wanted:
            (tmp_path / "wanted_4663.txt").write_text("".join("%s 1\n" % w for w in wanted))
        ix.run(CHAIN, days=1, page=10, max_pages=2, sleep_s=0, wanted_window=10_000_000,
               wanted_calls=4, wanted_pace_s=0)
        state_file = tmp_path / "v4_poolkey_wanted_4663.json"
        return json.loads(state_file.read_text()) if state_file.exists() else None
    return ix, conn, reads, answers, run


def test_a_wanted_429_is_the_last_read_of_the_run(world):
    _ix, _conn, reads, answers, run = world
    answers[POOL] = [RuntimeError("rpc eth_getLogs: HTTP 429 Too Many Requests")]
    state = run()
    assert reads == [POOL], reads
    assert state[POOL]["hi"] is None                # the refused window is not labelled empty


@pytest.mark.parametrize("refusal", [TimeoutError(), RuntimeError(), RuntimeError("HTTP 429")])
def test_a_refusal_with_any_message_stops_every_wanted_key(world, refusal):
    _ix, _conn, reads, answers, run = world
    answers[POOL] = [refusal]
    state = run(wanted=(POOL, OTHER))
    assert reads == [POOL]
    assert state[POOL]["hi"] is None and state[OTHER]["hi"] is None


def test_an_empty_http_transport_exception_stops_the_real_rpc_wrapper(world, monkeypatch):
    ix, _conn, _reads, _answers, run = world
    spec = importlib.util.spec_from_file_location("indexer_transport_under_test", ix.__file__)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    reads = []

    def http(method, url, body=None, **kwargs):
        request = json.loads(body)
        if request["method"] == "eth_blockNumber":
            return 200, {"result": hex(HEAD)}, {}
        pool = request["params"][0]["topics"][1]
        reads.append(pool)
        if pool == POOL:
            raise TimeoutError()
        return 200, {"result": []}, {}

    monkeypatch.setattr(ix.W, "_http", http)
    monkeypatch.setattr(ix, "_rpc", transport._rpc)
    state = run(wanted=(POOL, OTHER))
    assert reads == [POOL]
    assert state[POOL]["hi"] is None and state[OTHER]["hi"] is None


def test_a_wanted_range_refusal_also_ends_the_run_and_keeps_progress(world):
    _ix, _conn, reads, answers, run = world
    answers[POOL] = [[], RuntimeError("rpc eth_getLogs: range too large")]
    state = run()
    assert reads == [POOL, POOL], reads
    assert state[POOL]["hi"] == HEAD - 10_000_000   # the answered window is not read again


@pytest.mark.parametrize("malformed", [None, {}, "", False])
def test_a_malformed_wanted_answer_is_a_refusal_not_an_empty_range(world, malformed):
    _ix, _conn, reads, answers, run = world
    answers[POOL] = [malformed]
    state = run()
    assert reads == [POOL], reads
    assert state[POOL]["hi"] is None


def test_a_key_stored_before_the_refusal_stays_and_nothing_else_is_read(world):
    _ix, conn, reads, answers, run = world
    answers[POOL] = [[dict(INIT_LOG, blockNumber=hex(HEAD - 5))]]
    answers[OTHER] = [RuntimeError("HTTP 429")]
    state = run(wanted=(POOL, OTHER))
    assert reads == [POOL, OTHER], reads
    assert m.get_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=POOL,
                                 pool_manager=V.V4[CHAIN]["pool_manager"]) is not None
    assert POOL not in state and state[OTHER]["hi"] is None


def test_clean_empty_wanted_reads_let_the_scan_continue(world):
    _ix, _conn, reads, _answers, run = world
    run()
    assert reads[:4] == [POOL] * 4 and "scan" in reads[4:], reads


def test_no_wanted_ids_runs_the_scan_as_before(world):
    _ix, _conn, reads, _answers, run = world
    run(wanted=())
    assert reads and set(reads) == {"scan"}, reads


def test_a_storage_error_in_the_wanted_step_is_not_a_provider_stop(world, monkeypatch):
    ix, _conn, reads, answers, run = world
    answers[POOL] = [[dict(INIT_LOG, blockNumber=hex(HEAD - 5))]]

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(ix.M, "put_evm_v4_pool_key", broken)
    run()
    assert reads[0] == POOL and "scan" in reads[1:], reads


@pytest.mark.parametrize("fault", ["store", "decode"])
def test_a_storage_or_parse_error_keeps_the_taken_ids_and_their_progress(world, monkeypatch, tmp_path, fault):
    """Gamma on 0c403758a: _take_wanted unlinks the runner's file before the reads, so an exception
    escaping the loop must not skip the state write that carries the taken ids and their `hi`."""
    ix, _conn, reads, answers, run = world
    answers[POOL] = [[], [dict(INIT_LOG, blockNumber=hex(HEAD - 10_000_005))]]

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    if fault == "store":
        monkeypatch.setattr(ix.M, "put_evm_v4_pool_key", broken)
    else:
        monkeypatch.setattr(ix.V, "decode_initialize_log", broken)
    state = run()
    assert reads[:2] == [POOL, POOL], reads
    assert not (tmp_path / "wanted_4663.txt").exists()
    assert state is not None and state[POOL]["hi"] == HEAD - 10_000_000, state
