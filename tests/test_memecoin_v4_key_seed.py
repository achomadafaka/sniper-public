"""Seed the ladder's V4 PoolKey cache from our own swap tape (2026-09-24).

THE GAP. A Pons coin graduates, its new NATIVE-paired V4 pool starts trading within seconds, and
the ladder cannot quote it until V4 discovery learns the pool key from a provider or an
`eth_getLogs`. Measured on the retained hour of 4663: of the 34 keys the ladder learned inside that
hour, `verified_at` minus the pool's FIRST tape tick is p10 85 s, p50 346 s, p90 417 s, max 577 s.
PENIS (06:46 attempts) got its key at 07:03:15Z; PENIS42069's landed at 06:55:02Z. In that window
the only rung that answers is relay, and on PENIS it quoted 2.90x the source price.

Our own volume writer already sees the `Initialize` event -- BEFORE the first swap -- decodes the
full PoolKey and verifies it (`v4_pool_id(key) != pool` raises in `register_pool`). It just did not
keep `fee`, `tick_spacing` and `hooks`, so nothing downstream could rebuild the key.

The seed is EXACT, never a heuristic: the key is rebuilt from the tape row and accepted only when
`v4_pool_id(key)` hashes back to the stored pool id.
"""
import json
import sqlite3

import pytest

from sniper.analysis.tick_features import ensure_feature_tables
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_v4_key_seed import seed_v4_pool_keys

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
COIN = "0x" + "cc" * 20


def _key(fee=10000, tick_spacing=200, hooks=NATIVE):
    return {"currency0": NATIVE, "currency1": COIN, "fee": fee,
            "tick_spacing": tick_spacing, "hooks": hooks}


def _pool_id(key):
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    return v4_pool_id(key)


def _tape(tmp_path, rows):
    path = str(tmp_path / "volume.db")
    c = sqlite3.connect(path)
    ensure_feature_tables(c)
    for col in ("fee", "tick_spacing", "hooks"):
        try:
            c.execute("ALTER TABLE pool_meta ADD COLUMN %s %s"
                      % (col, "TEXT" if col == "hooks" else "INTEGER"))
        except sqlite3.OperationalError:
            pass
    for pool, key, resolved_at in rows:
        c.execute("INSERT OR REPLACE INTO pool_meta(chain_id, pool, token0, token1, quote_side,"
                  " resolved_at, fee, tick_spacing, hooks) VALUES (?,?,?,?,?,?,?,?,?)",
                  (CHAIN, pool, key["currency0"], key["currency1"], 0, resolved_at,
                   key["fee"], key["tick_spacing"], key["hooks"]))
    c.commit(); c.close()
    return path


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_a_pool_the_tape_registered_is_seeded_exactly(tmp_path):
    key = _key()
    pool = _pool_id(key)
    tape = _tape(tmp_path, [(pool, key, "2026-09-24T07:00:00+00:00")])
    conn = _ledger()
    n = seed_v4_pool_keys(conn, chain="4663", tape_path=tape, since_iso="2026-09-24T06:00:00+00:00")
    assert n == 1
    row = conn.execute("select key_json, pool_manager from memecoin_evm_v4_pool_keys"
                       " where network_id=? and pool_id=?", (CHAIN, pool)).fetchone()
    assert row is not None, "the key never reached the ladder's cache"
    assert json.loads(row["key_json"]) == key


def test_a_row_whose_key_does_not_hash_back_is_refused(tmp_path):
    """The identity check is the whole safety argument: a corrupted or mis-attributed row must not
    enter the cache, because the ladder would then quote a pool that hashes to something else."""
    key = _key()
    tape = _tape(tmp_path, [("0x" + "ab" * 32, key, "2026-09-24T07:00:00+00:00")])
    conn = _ledger()
    assert seed_v4_pool_keys(conn, chain="4663", tape_path=tape,
                             since_iso="2026-09-24T06:00:00+00:00") == 0
    assert conn.execute("select count(*) from memecoin_evm_v4_pool_keys").fetchone()[0] == 0


def test_rows_written_before_the_columns_existed_are_skipped_not_guessed(tmp_path):
    """Every pool_meta row written before this change has no fee/tick_spacing/hooks. A guessed fee
    would hash to a different pool, so the row is simply not seeded."""
    key = _key()
    pool = _pool_id(key)
    tape = _tape(tmp_path, [(pool, key, "2026-09-24T07:00:00+00:00")])
    c = sqlite3.connect(tape)
    c.execute("UPDATE pool_meta SET fee=NULL, tick_spacing=NULL, hooks=NULL")
    c.commit(); c.close()
    conn = _ledger()
    assert seed_v4_pool_keys(conn, chain="4663", tape_path=tape,
                             since_iso="2026-09-24T06:00:00+00:00") == 0


def test_the_cursor_bounds_the_pass(tmp_path):
    """The pass reads rows newer than the cursor, so a steady state costs a handful of rows rather
    than the whole table."""
    key_old, key_new = _key(fee=3000), _key(fee=10000)
    tape = _tape(tmp_path, [(_pool_id(key_old), key_old, "2026-09-24T05:00:00+00:00"),
                            (_pool_id(key_new), key_new, "2026-09-24T07:00:00+00:00")])
    conn = _ledger()
    assert seed_v4_pool_keys(conn, chain="4663", tape_path=tape,
                             since_iso="2026-09-24T06:00:00+00:00") == 1
    assert conn.execute("select count(*) from memecoin_evm_v4_pool_keys").fetchone()[0] == 1


def test_a_missing_tape_is_an_absent_pass_not_an_error(tmp_path):
    conn = _ledger()
    assert seed_v4_pool_keys(conn, chain="4663", tape_path=str(tmp_path / "nope.db"),
                             since_iso="2026-09-24T06:00:00+00:00") == 0


def test_the_writer_keeps_the_three_fields_the_seed_needs(tmp_path):
    """The other half: `register_pool` must PERSIST fee, tick_spacing and hooks, and must NAME them
    in its INSERT OR REPLACE -- an unnamed column is erased by a later re-registration, which would
    silently un-seed a pool we had already learned."""
    from sniper.services.memecoin_volume_writer import VolumeTape

    key = _key(fee=8388608, tick_spacing=60, hooks="0x" + "ee" * 20)
    pool = _pool_id(key)
    from sniper.services import memecoin_volume_writer as W

    conn = W.connect(str(tmp_path / "tape.db"))   # the writer's own ensure path, not a bare conn
    conn.row_factory = sqlite3.Row
    tape = VolumeTape(conn, CHAIN)
    tape.register_pool(pool, key, 1_000)
    row = conn.execute("select fee, tick_spacing, hooks from pool_meta where pool=?",
                       (pool,)).fetchone()
    assert (row["fee"], row["tick_spacing"], row["hooks"]) == (
        key["fee"], key["tick_spacing"], key["hooks"]), dict(row)
    tape.register_pool(pool, key, 2_000)          # a re-registration must not blank them
    row = conn.execute("select fee, tick_spacing, hooks from pool_meta where pool=?",
                       (pool,)).fetchone()
    assert row["fee"] == key["fee"] and row["hooks"] == key["hooks"], dict(row)


def test_a_tape_that_predates_the_key_columns_is_not_an_empty_pass(tmp_path, caplog):
    """Two different facts must not collapse into the same 0: a missing column raises inside the
    SELECT, gets caught, and reads exactly like a quiet minute. The live 4663 tape was in this
    state when the seeder was written, so the distinction is not hypothetical."""
    import logging

    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    ensure_feature_tables(c)
    for col in ("fee", "tick_spacing", "hooks"):
        c.execute("ALTER TABLE pool_meta DROP COLUMN %s" % col)
    c.commit(); c.close()
    conn = _ledger()
    with caplog.at_level(logging.INFO, logger="sniper.services.memecoin_v4_key_seed"):
        assert seed_v4_pool_keys(conn, chain="4663", tape_path=path,
                                 since_iso="2026-09-24T06:00:00+00:00") == 0
    assert any("no fee" in r.message or "tick_spacing" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_progress_drains_equal_timestamp_pages_without_losing_overflow(tmp_path):
    keys = [_key(fee=fee) for fee in range(1, 6)]
    tape = _tape(tmp_path, [('0x' + '00' * 32, keys[0], '2026-09-24T07:00:00+00:00')] +
                 [(_pool_id(key), key, '2026-09-24T07:00:00+00:00') for key in keys])
    conn, progress = _ledger(), {}
    counts = [seed_v4_pool_keys(conn, chain=CHAIN, tape_path=tape,
              since_iso='2026-09-24T06:00:00+00:00', limit=2, progress=progress)
              for _ in range(4)]
    assert counts == [1, 2, 2, 0]
    assert conn.execute('SELECT count(*) FROM memecoin_evm_v4_pool_keys').fetchone()[0] == 5


def test_progress_retains_page_after_partial_write_and_tape_failure(tmp_path, monkeypatch):
    keys = [_key(fee=fee) for fee in (1, 2)]
    tape = _tape(tmp_path, [(_pool_id(key), key, '2026-09-24T07:00:00+00:00')
                            for key in keys])
    conn, progress = _ledger(), {}
    put, calls = m.put_evm_v4_pool_key, []
    def fail_second(*args, **kwargs):
        calls.append(kwargs['pool_id'])
        if len(calls) == 2:
            raise sqlite3.OperationalError('temporary write failure')
        return put(*args, **kwargs)
    monkeypatch.setattr(m, 'put_evm_v4_pool_key', fail_second)
    assert seed_v4_pool_keys(conn, chain=CHAIN, tape_path=tape,
        since_iso='2026-09-24T06:00:00+00:00', progress=progress) == 1
    assert progress == {}
    assert seed_v4_pool_keys(conn, chain=CHAIN, tape_path=tmp_path / 'missing',
        since_iso='2026-09-24T06:00:00+00:00', progress=progress) == 0
    assert progress == {}
    monkeypatch.setattr(m, 'put_evm_v4_pool_key', put)
    assert seed_v4_pool_keys(conn, chain=CHAIN, tape_path=tape,
        since_iso='2026-09-24T06:00:00+00:00', progress=progress) == 2
    assert progress['cursor']
    assert conn.execute('SELECT count(*) FROM memecoin_evm_v4_pool_keys').fetchone()[0] == 2


def test_runner_maintenance_drains_overflow_without_advancing_to_wall_time(tmp_path, monkeypatch):
    import ast
    from pathlib import Path
    import traceback
    from sniper.services import memecoin_v4_key_seed as seed

    keys = [_key(fee=fee) for fee in range(1, 202)]
    tape = _tape(tmp_path, [(_pool_id(key), key, '2026-09-24T07:00:00+00:00')
                            for key in keys])
    monkeypatch.setattr(seed, '_tape_path', lambda override=None: tape)
    path = Path('scripts/memecoin_runner_loop.py')
    block = next(node for node in ast.walk(ast.parse(path.read_text()))
                 if isinstance(node, ast.If) and any(
                     isinstance(part, ast.Name) and part.id == '_V4_KEY_SEED_PERIOD_S'
                     for part in ast.walk(node.test)))
    run = compile(ast.Module(body=[block], type_ignores=[]), str(path), 'exec')
    conn, messages = _ledger(), []
    state = {'last_v4_key_seed': 0, '_V4_KEY_SEED_PERIOD_S': 10,
             '_v4_key_seed_since': {}, '_seed_since_default': '2026-09-24T06:00:00+00:00',
             'conn': conn, 'real_utc_now_iso': lambda: '2026-09-24T07:01:00+00:00',
             '_p': messages.append, 'traceback': traceback}
    counts = []
    for cycle in (11, 22, 33):
        state['cycle'] = cycle
        exec(run, state)
        counts.append(conn.execute('SELECT count(*) FROM memecoin_evm_v4_pool_keys').fetchone()[0])
    assert not any(message.startswith('[err]') for message in messages), messages
    assert counts == [200, 201, 201]
