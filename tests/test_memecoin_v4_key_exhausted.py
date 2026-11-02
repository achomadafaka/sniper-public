"""A V4 PoolKey walk that runs out of pages is remembered, and its id is resolved offline (Alpha GO
2026-10-07). On 4663 two bridge pools, 0x6444a8e0 and 0x8674c1c5 (Initialize at blocks 18.6M and
37.3M), were older than any walk reaches. Every page answered empty and the 0x0->latest read was
unavailable (NoEligibleReadEndpoint), so no clean miss was noted and every multihop quote walked
them again: 47 eth_getLogs since 10-01, ~1 s of COMPANY's 2.43 s quote."""
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from sniper.execution import evm_v4_quote as V
from sniper.persistence import db_memecoin as m

CHAIN = "4663"
HEAD = 50_000_000
KEY = {"currency0": "0x" + "00" * 20, "currency1": "0x" + "11" * 20, "fee": 3000, "tick_spacing": 60,
       "hooks": "0x" + "22" * 20}
POOL = "0x" + V.keccak(bytes.fromhex(V._encode_pool_key(KEY))).hex()      # a real id: the indexer hash-checks
INIT_LOG = {"topics": [V._INIT_TOPIC, POOL, "0x" + "00" * 32, "0x" + "00" * 12 + "11" * 20],
            "data": "0x" + format(3000, "064x") + format(60, "064x") + "00" * 12 + "22" * 20}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # THE STORE THREAD IS SHARED AND RESOLVES THE WANTED PATH WHEN A WRITE RUNS (deploy-gate flake,
    # 2026-10-08). A walk that ran out queues its wanted line; on a loaded host the one-worker thread
    # ran it after the NEXT test had patched wanted_pool_keys_path, so the line landed in that test's
    # tmp_path and test_a_clean_miss... saw a file it never wrote. Drain before patching (an earlier
    # test's writes) and at teardown while this test's patches still stand (its own).
    _flush_store_thread(timeout=30)
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda chain, primary: (primary,))
    monkeypatch.setattr(V, "_archive_is_chain", lambda *a, **k: True)
    monkeypatch.setattr(V, "wanted_pool_keys_path", lambda chain: tmp_path / ("wanted_%s.txt" % chain))
    monkeypatch.setenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_4663", "100000")
    for table in (V._scan_exhausted, V._scan_miss, V._scan_depth, V._scan_429, V._key_cache):
        table.clear()
    V.begin_quote()
    yield
    _flush_store_thread(timeout=30)
    V._scan_exhausted.clear()


def _flush_store_thread(timeout=5):
    V._offpath_executor("store").submit(lambda: None).result(timeout=timeout)


def _driver(monkeypatch, *, head=HEAD, full="no_member", found_from=None):
    """Scripted reads. Every page answers empty; the full range raises NoEligibleReadEndpoint
    ('no_member') or answers empty ('clean'); a page whose range reaches below `found_from`
    carries the Initialize log."""
    calls = []

    def rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(head)}
        if method == "eth_chainId":
            return {"result": hex(int(CHAIN))}
        page = params[0]
        calls.append((page["fromBlock"], page["toBlock"]))
        if page["toBlock"] == "latest":
            if full == "no_member":
                raise V.evm_rpc_pool.NoEligibleReadEndpoint("no eligible read member")
            return {"result": []}
        lo, hi = int(page["fromBlock"], 16), int(page["toBlock"], 16)
        if found_from is not None and lo <= found_from <= hi:
            return {"result": [INIT_LOG]}
        return {"result": []}
    monkeypatch.setattr(V, "_rpc", rpc)
    return calls


def test_a_walk_that_runs_out_without_the_full_range_is_remembered_and_wanted(monkeypatch, tmp_path):
    calls = _driver(monkeypatch)
    assert V._pool_key(CHAIN, POOL, "primary") is None
    assert V.last_provider_blind() == "poolkey_read_failed"
    assert len(calls) == 1 + V._V4_KEY_SCAN_MAX_PAGES + 1            # window, the walk, the full range
    _flush_store_thread()
    line = (tmp_path / "wanted_4663.txt").read_text().split()
    assert line[0] == POOL
    # The next quote reads ONE window and nothing else; the label is unchanged.
    V.begin_quote()
    calls.clear()
    assert V._pool_key(CHAIN, POOL, "primary") is None
    assert len(calls) == 1 and calls[0][1] == hex(HEAD)
    assert V.last_provider_blind() == "poolkey_read_failed"
    _flush_store_thread()
    assert len((tmp_path / "wanted_4663.txt").read_text().splitlines()) == 1   # wanted once per walk


def test_a_pool_that_appears_after_the_walk_is_still_found(monkeypatch):
    _driver(monkeypatch)
    V._pool_key(CHAIN, POOL, "primary")
    later = HEAD + 30_000                        # ~50 min of 4663 blocks: past the 8,192 first window
    calls = _driver(monkeypatch, head=later, found_from=HEAD + 12_000)
    V.begin_quote()
    assert V._pool_key(CHAIN, POOL, "primary") == KEY
    (lo, hi), = calls
    assert int(lo, 16) <= HEAD and int(hi, 16) == later            # every block since the walk's head


def test_a_stored_key_wins_over_the_memo(monkeypatch):
    _driver(monkeypatch)
    V._pool_key(CHAIN, POOL, "primary")
    assert V._exhausted_head((CHAIN, POOL)) == HEAD
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: dict(KEY))
    V._key_cache.clear()
    calls = _driver(monkeypatch)
    assert V._pool_key(CHAIN, POOL, "primary") == KEY and calls == []


def test_the_memo_expires_and_zero_turns_it_off(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(V, "_now", lambda: clock[0])
    calls = _driver(monkeypatch)
    V._pool_key(CHAIN, POOL, "primary")
    clock[0] += V._KEY_SCAN_EXHAUSTED_TTL_S + 1
    V.begin_quote()
    calls.clear()
    V._pool_key(CHAIN, POOL, "primary")
    assert len(calls) > 2                                             # walked again after the TTL
    V._scan_exhausted.clear()
    V._scan_depth.clear()
    monkeypatch.setattr(V, "_KEY_SCAN_EXHAUSTED_TTL_S", 0.0)
    V._pool_key(CHAIN, POOL, "primary")
    assert V._exhausted_head((CHAIN, POOL)) is None


def test_a_clean_miss_stays_a_clean_miss_and_wants_nothing(monkeypatch, tmp_path):
    _driver(monkeypatch, full="clean")
    assert V._pool_key(CHAIN, POOL, "primary") is None
    _flush_store_thread()
    # An empty full-range answer is a clean no-pool (it returns before any memo): not exhausted.
    assert V._exhausted_head((CHAIN, POOL)) is None
    assert not (tmp_path / "wanted_4663.txt").exists()


@pytest.mark.parametrize("fault", ["rate_limit", "timeout"])
def test_a_429_or_a_timeout_on_the_full_range_is_not_a_walk_that_ran_out(monkeypatch, tmp_path, fault):
    """Gamma LOW-1: those leave the full range open for its own retry (a 429 waits only the 60 s cool-
    down); a 30-minute memo and a wanted line would skip it on a fault, not on evidence."""
    from sniper.execution import evm_wallet as w
    calls = _driver(monkeypatch)
    inner = V._rpc

    def rpc(url, method, params, http=None):
        if method == "eth_getLogs" and params[0]["toBlock"] == "latest":
            calls.append(("0x0", "latest"))
            raise w._http_failure(429, url) if fault == "rate_limit" else TimeoutError("slow")
        return inner(url, method, params, http=http)
    monkeypatch.setattr(V, "_rpc", rpc)
    assert V._pool_key(CHAIN, POOL, "primary") is None
    assert ("0x0", "latest") in calls                       # the walk completed and reached the full range
    _flush_store_thread()
    assert V._exhausted_head((CHAIN, POOL)) is None
    assert not (tmp_path / "wanted_4663.txt").exists()


# --- the indexer resolves the wanted ids ---------------------------------------------------------

def _indexer(monkeypatch, tmp_path):
    """The indexer script, loaded where no `.env` can be found. It looks at SNIPER_STATE_ROOT,
    MEMECOIN_STATE_ROOT, the working directory and its own repo root (`_REPO`, from __file__), and
    the shared checkout has a real `.env` at the last two (Alpha 2026-10-08: 4 of 11 failed there,
    green in worktrees). So: both roots and the cwd point at an empty directory, and the script runs
    from a copy there, so `_REPO` is that directory too. The stub still fails the test on any load."""
    import shutil
    root = tmp_path / "isolated-root"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve().parent.parent / "scripts" / "memecoin_v4_poolkey_index.py"
    path = Path(shutil.copy(src, root / "scripts" / src.name))
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(root))
    monkeypatch.setenv("MEMECOIN_STATE_ROOT", str(root))
    monkeypatch.chdir(root)
    stub = types.ModuleType("dotenv")
    stub.load_dotenv = lambda *a, **k: pytest.fail("the test must never load a .env")
    monkeypatch.setitem(sys.modules, "dotenv", stub)
    spec = importlib.util.spec_from_file_location("v4_poolkey_index_under_test", path)
    ix = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ix)
    return ix


def _ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_the_indexer_walks_a_wanted_id_back_by_its_own_topic_and_stores_it(monkeypatch, tmp_path):
    ix = _indexer(monkeypatch, tmp_path)
    monkeypatch.setattr(ix, "state_path", lambda *parts: tmp_path.joinpath(*parts[1:]))
    (tmp_path / "wanted_4663.txt").write_text("%s 1\n%s 2\nnot-an-id 3\n" % (POOL, POOL))
    reads = []

    def rpc(url, method, params, timeout=60):
        page = params[0]
        assert page["topics"] == [V._INIT_TOPIC, POOL]
        lo, hi = int(page["fromBlock"], 16), int(page["toBlock"], 16)
        reads.append((lo, hi))
        if len(reads) == 1:            # a lookalike under this topic: its key does not hash to the id
            return [dict(INIT_LOG, data="0x" + format(500, "064x") + format(10, "064x") + "00" * 12 + "22" * 20,
                         blockNumber=hex(80_000_000))]
        return [dict(INIT_LOG, blockNumber=hex(18_594_337))] if lo <= 18_594_337 <= hi else []
    monkeypatch.setattr(ix, "_rpc", rpc)
    conn = _ledger()
    stored, calls, _refused = ix.resolve_wanted(conn, chain=CHAIN, url="archive", pool_manager=V.V4[CHAIN]["pool_manager"],
                                      head=82_857_471, window=10_000_000, max_calls=12, pace_s=0)
    assert stored == 1 and calls == len(reads) == 7
    assert reads[0] == (72_857_472, 82_857_471) and all(b - a + 1 == 10_000_000 for a, b in reads)
    assert m.get_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=POOL,
                                 pool_manager=V.V4[CHAIN]["pool_manager"]) == KEY
    assert json.loads((tmp_path / "v4_poolkey_wanted_4663.json").read_text()) == {}
    assert not (tmp_path / "wanted_4663.txt").exists()


def test_the_indexer_paces_its_reads_stops_on_a_429_and_defaults_to_four(monkeypatch, tmp_path):
    """Alpha on Gamma LOW-2: the 4663 host is shared with funded reads, so start at 4 paced reads a run
    and stop on the first 429."""
    import inspect
    ix = _indexer(monkeypatch, tmp_path)
    monkeypatch.setattr(ix, "state_path", lambda *parts: tmp_path.joinpath(*parts[1:]))
    assert inspect.signature(ix.run).parameters["wanted_calls"].default == 4
    (tmp_path / "wanted_4663.txt").write_text("%s 1\n" % POOL)
    sleeps, reads = [], []
    monkeypatch.setattr(ix.time, "sleep", sleeps.append)
    answers = iter([[], [], RuntimeError("rpc eth_getLogs: HTTP 429 Too Many Requests")])

    def rpc(url, method, params, timeout=60):
        reads.append(1)
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(ix, "_rpc", rpc)
    stored, calls, _refused = ix.resolve_wanted(_ledger(), chain=CHAIN, url="archive",
                                      pool_manager=V.V4[CHAIN]["pool_manager"], head=82_857_471,
                                      window=10_000_000, max_calls=4)
    assert stored == 0 and calls == len(reads) == 3                  # the 429 ended the run
    assert sleeps == [ix.WANTED_PACE_S] * 2 and ix.WANTED_PACE_S >= 1.0   # paced, never back-to-back


def test_a_refused_read_keeps_the_id_and_its_progress(monkeypatch, tmp_path):
    ix = _indexer(monkeypatch, tmp_path)
    monkeypatch.setattr(ix, "state_path", lambda *parts: tmp_path.joinpath(*parts[1:]))
    (tmp_path / "wanted_4663.txt").write_text("%s 1\n" % POOL)
    answers = iter([[], RuntimeError("rpc eth_getLogs: range too large")])

    def rpc(url, method, params, timeout=60):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(ix, "_rpc", rpc)
    conn = _ledger()
    stored, calls, _refused = ix.resolve_wanted(conn, chain=CHAIN, url="archive", pool_manager=V.V4[CHAIN]["pool_manager"],
                                      head=82_857_471, window=10_000_000, max_calls=12, pace_s=0)
    assert stored == 0 and calls == 2                      # the refusal ends this run's reads
    state = json.loads((tmp_path / "v4_poolkey_wanted_4663.json").read_text())
    assert state[POOL]["hi"] == 72_857_471                  # the answered window is not read again


def test_a_429_on_one_wanted_id_ends_the_whole_run(monkeypatch, tmp_path):
    """Gamma NIT on 753b69a16: with two ids wanted, a 429 on the first must not let the second's reads
    go out after it."""
    ix = _indexer(monkeypatch, tmp_path)
    monkeypatch.setattr(ix, "state_path", lambda *parts: tmp_path.joinpath(*parts[1:]))
    other = "0x" + "ef" * 32
    (tmp_path / "wanted_4663.txt").write_text("%s 1\n%s 2\n" % (POOL, other))
    reads = []

    def rpc(url, method, params, timeout=60):
        reads.append(params[0]["topics"][1])
        raise RuntimeError("rpc eth_getLogs: HTTP 429 Too Many Requests")
    monkeypatch.setattr(ix, "_rpc", rpc)
    stored, calls, _refused = ix.resolve_wanted(_ledger(), chain=CHAIN, url="archive",
                                      pool_manager=V.V4[CHAIN]["pool_manager"], head=82_857_471,
                                      window=10_000_000, max_calls=4, pace_s=0)
    assert stored == 0 and calls == len(reads) == 1
    state = json.loads((tmp_path / "v4_poolkey_wanted_4663.json").read_text())
    assert state[POOL]["hi"] is None and state[other]["hi"] is None     # neither id advanced
