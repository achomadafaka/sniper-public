"""Phase 2 Task 9 — standalone exit-state worker: startup, gate, one-pass wiring, header enrichment.

The worker is DEFAULT OFF and must (a) actually START under the watchdog's exact command (real
subprocess, no import error), (b) open nothing while off, (c) run one materialize pass when on, and
(d) enrich block-time headers NEWEST-first so unavailable ancient blocks never starve the current
window. The live-RPC seam is driven with a MOCK get_block_fn — no network.
"""
import os
import subprocess
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

import scripts.memecoin_exit_state_worker as w
from sniper.persistence import db_ticks as t
from sniper.analysis import tick_features as tf

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(w.__file__)))


def _iso(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()


def test_worker_launches_as_subprocess_without_import_error():
    """The watchdog runs `python3 scripts/memecoin_exit_state_worker.py` from the repo root — the
    script dir, not the root, is on sys.path, so a naive `import sniper` ModuleNotFerrors. OFF -> exits 0."""
    env = dict(os.environ, MEMECOIN_EXIT_STATE_ENABLED="0")
    r = subprocess.run([sys.executable, "scripts/memecoin_exit_state_worker.py"],
                       cwd=_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "ModuleNotFoundError" not in r.stderr and "No module named" not in r.stderr


def test_enabled_reads_live_env(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EXIT_STATE_ENABLED", "1")
    assert w._enabled() is True
    monkeypatch.setenv("MEMECOIN_EXIT_STATE_ENABLED", "0")
    assert w._enabled() is False


def test_unset_env_falls_back_to_the_settings_default_not_a_second_one(monkeypatch):
    """Regression: the worker used to carry its own hardcoded 'unset' default, so flipping the
    settings default silently did nothing. With the var unset, the worker must agree with settings."""
    import sniper.settings.memecoin as cfg
    monkeypatch.delenv("MEMECOIN_EXIT_STATE_ENABLED", raising=False)
    assert w._enabled() is bool(cfg.MEMECOIN_EXIT_STATE_ENABLED)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_STATE_ENABLED", False, raising=False)
    assert w._enabled() is False
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_STATE_ENABLED", True, raising=False)
    assert w._enabled() is True


def test_worker_off_by_default_opens_nothing(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EXIT_STATE_ENABLED", "0")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    opened, called = [], []
    monkeypatch.setattr(w, "_connect_ticks", lambda: opened.append("ticks"))
    monkeypatch.setattr(w, "_connect_exit_state", lambda: opened.append("exit_state"))
    monkeypatch.setattr(w, "_connect_ledger", lambda: opened.append("ledger"))
    monkeypatch.setattr(w, "materialize_open_positions", lambda *a, **k: called.append(1))
    assert w.main(iterations=1) == 0
    assert opened == [] and called == []          # gated off: no connections, no materialize


def test_worker_enabled_runs_one_pass(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EXIT_STATE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(w, "_connect_ticks", lambda: "TC")
    monkeypatch.setattr(w, "_connect_exit_state", lambda: "XC")   # own-DB split (P0-3, 2026-07-24)
    monkeypatch.setattr(w, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(w, "_connect_sol_tape_readonly", lambda: "STC")
    monkeypatch.setattr(w, "_rpc_get_block_fn", lambda: None)   # no live RPC in the test
    calls = []
    monkeypatch.setattr(w, "materialize_open_positions",
                        lambda mconn, tc, now_ts, sol_tc=None, xc=None: calls.append((mconn, tc, sol_tc, xc)))
    assert w.main(iterations=1) == 1
    # exactly one pass, wired to the ticks/ledger/exit-state connections AND the sol tape's OWN connection
    assert calls == [("MC", "TC", "STC", "XC")]


def test_worker_materializes_when_sol_tape_file_is_absent(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EXIT_STATE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(w, "_connect_ticks", lambda: "TC")
    monkeypatch.setattr(w, "_connect_exit_state", lambda: "XC")
    monkeypatch.setattr(w, "_connect_ledger", lambda: "MC")
    opened = []
    monkeypatch.setattr(w, "_connect_sol_tape_readonly",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(w, "_rpc_get_block_fn", lambda: None)
    calls = []
    monkeypatch.setattr(w, "materialize_open_positions",
                        lambda mconn, tc, now_ts, sol_tc=None, xc=None: calls.append((mconn, tc, sol_tc, xc)))
    assert w.main(iterations=1) == 1
    assert opened == []                            # never opened: no Solana seam, no second connection
    assert calls == [("MC", "TC", None, "XC")]


def test_exit_state_worker_has_no_solana_rpc_or_tape_pass_surface():
    import inspect
    source = inspect.getsource(w)
    assert "solana_tape_pass" not in source
    assert "get_signatures_fn" not in source
    assert "get_transaction_fn" not in source


def test_connect_ticks_is_read_only(tmp_path, monkeypatch):
    """P0-3: the worker's OWN ticks handle must be structurally unable to write — a second writer on
    the shared ticks DB already caused a WAL-lock incident once. Attempting a write must raise."""
    import sqlite3
    path = tmp_path / "ticks.db"
    seed = sqlite3.connect(str(path)); t.ensure_tick_tables(seed); t.ensure_block_times(seed)
    seed.close()
    monkeypatch.setenv("MEMECOIN_TICKS_DB", str(path))
    c = w._connect_ticks()
    try:
        assert c.isolation_level is None
        with pytest.raises(sqlite3.OperationalError):
            c.execute("INSERT INTO tick_cursors (chain_id, last_block, updated_at) VALUES ('x', 1, 'y')")
    finally:
        c.close()


def test_exit_state_db_is_a_distinct_file_never_the_ticks_db(tmp_path, monkeypatch):
    """The exit-state connection owns its own file, distinct from the shared ticks DB, and carries
    the exit-state tables plus its own block_times copy."""
    from sniper.persistence import db_exit_state as xs
    path = tmp_path / "exit_state.db"
    monkeypatch.setenv("MEMECOIN_EXIT_STATE_DB", str(path))
    assert xs.exit_state_db_path() == str(path)
    c = w._connect_exit_state()
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"memecoin_exit_market_state", "memecoin_exit_candidate_decisions",
               "block_times"} <= tables
    finally:
        c.close()
    assert path.exists()


def test_exit_state_db_is_in_the_wal_checkpoint_list():
    import inspect
    from scripts import wal_checkpoint as wc
    src = inspect.getsource(wc.main)
    assert "exit_state_db_path" in src   # resolver, not a literal path (P2: state-root/env aware)


def _ticks_with_pool(chain_id, pool, blocks, base):
    tc = sqlite3.connect(":memory:"); tc.row_factory = sqlite3.Row
    t.ensure_tick_tables(tc); t.ensure_block_times(tc); tf.ensure_feature_tables(tc)
    t.insert_ticks(tc, [{"chain_id": chain_id, "pool": pool, "kind": "v2_swap", "block_no": b,
                         "tx_sig": f"0x{b:064x}", "log_index": 0, "maker": "0xr",
                         "amount0": "1", "amount1": "-1", "observed_at": _iso(base + b)} for b in blocks])
    return tc


def _ledger_one_pos(chain_id, pool, tc=None, mint="0xbase"):
    """One open position. The pool is reached by joining `mint` to pool_meta's BASE side — never by
    quote_route_target, which is the router (P1, Codex 2026-07-24)."""
    mconn = sqlite3.connect(":memory:"); mconn.row_factory = sqlite3.Row
    mconn.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, is_open INTEGER, "
                  "execution_mode TEXT, network_id TEXT, mint TEXT, quote_route_target TEXT)")
    mconn.execute("INSERT INTO memecoin_positions VALUES (1,1,'real',?,?,?)",
                  (chain_id, mint, "ROUTER-IGNORED"))
    mconn.commit()
    if tc is not None:
        tc.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, "
                   "token0_decimals, token1_decimals, resolved_at) VALUES (?,?,?,?,?,?,?,?)",
                   (chain_id, pool, "0xquote", mint, 0, 18, 18, "2026-01-01T00:00:00Z"))
        tc.commit()
    return mconn


def _xc():
    """A bare exit-state-shaped connection — just the block_times table this worker writes into
    (own-DB split, P0-3 2026-07-24). Never `tc`, the shared read-only ticks connection."""
    xc = sqlite3.connect(":memory:"); xc.row_factory = sqlite3.Row
    t.ensure_block_times(xc)
    return xc


def test_block_time_enrichment_is_bounded():
    base = 1_700_000_000
    tc = _ticks_with_pool("4663", "0xp", range(1, 6), base)
    mconn = _ledger_one_pos("4663", "0xp", tc)
    xc = _xc()
    headers = {5: base + 5, 4: base + 4, 3: base + 3, 2: base + 2, 1: base + 1}
    n = w.enrich_block_times(mconn, tc, xc, lambda chain, b: headers.get(b), _iso(base), cap=2)
    assert n == 2                                        # capped at 2 despite 5 missing
    assert t.block_ts_map(tc, "4663", [1, 2, 3, 4, 5]) == {}   # never written to the shared ticks DB


def test_enrichment_newest_first_not_starved_by_unavailable_old_blocks():
    """Old blocks 1,2,3 have no header (RPC returns None); newer 4,5 do. A newest-first scan must
    reach 4,5 — the oldest-first scan retried the unavailable [1,2] forever and never got there."""
    base = 1_700_000_000
    tc = _ticks_with_pool("4663", "0xp", range(1, 6), base)
    mconn = _ledger_one_pos("4663", "0xp", tc)
    xc = _xc()
    available = {4: base + 4, 5: base + 5}              # 1,2,3 permanently unavailable
    n = w.enrich_block_times(mconn, tc, xc, lambda chain, b: available.get(b), _iso(base), cap=2)
    assert n == 2
    m = t.block_ts_map(xc, "4663", [1, 2, 3, 4, 5])     # written to the worker's OWN store
    assert set(m) == {4, 5}                             # newest AVAILABLE filled; old unavailable did not starve it


def test_enrichment_skips_blocks_the_shared_ticks_db_already_has_free():
    """The funded runner's own getLogs blockTimestamp capture (in `tc`) needs no RPC spend here —
    `_missing_blocks` must consult both stores before asking the RPC seam for anything."""
    base = 1_700_000_000
    tc = _ticks_with_pool("4663", "0xp", range(1, 4), base)
    t.backfill_block_times(tc, "4663", [1, 2], lambda b: base + b, _iso(base))   # already free-captured
    mconn = _ledger_one_pos("4663", "0xp", tc)
    xc = _xc()
    asked = []

    def get_block(chain, b):
        asked.append(b)
        return base + b

    n = w.enrich_block_times(mconn, tc, xc, get_block, _iso(base), cap=5)
    assert n == 1                    # only block 3 was actually missing from both stores
    assert asked == [3]


def test_worker_uses_the_corrected_clock_not_the_host_clock(monkeypatch):
    """now_ts is compared against CHAIN block_ts (real UTC). This host runs ~+7 h fast with NTP
    inactive, so the raw host clock made every feature look 7 h stale -> fail-closed -> the engine
    could never act, and no snapshot was ever written."""
    monkeypatch.setenv("MEMECOIN_EXIT_STATE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(w, "_connect_ticks", lambda: "TC")
    monkeypatch.setattr(w, "_connect_exit_state", lambda: "XC")
    monkeypatch.setattr(w, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(w, "_rpc_get_block_fn", lambda: None)
    monkeypatch.setattr("sniper.services.memecoin_solana_tape.get_signatures_fn", lambda: None)
    monkeypatch.setattr("sniper.services.memecoin_solana_amounts.get_transaction_fn", lambda: None)
    monkeypatch.setattr(w, "real_utc_now", lambda: 1_700_000_000.0)
    seen = []
    monkeypatch.setattr(w, "materialize_open_positions",
                        lambda mconn, tc, now_ts, sol_tc=None, xc=None: seen.append(now_ts))
    assert w.main(iterations=1) == 1
    assert seen == [1_700_000_000.0]          # the corrected clock, not time.time()
