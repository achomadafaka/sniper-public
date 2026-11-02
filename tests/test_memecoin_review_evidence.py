"""F12/F13: deterministic paper price, lag and provenance regressions."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_paper_ticks as ticks
from test_memecoin_paper_ticks import (
    BLOCK_HASH, ETH_USD, MINT, POOL, T0, _engine, _ledger, _open, _row, _sqrt_for, _tape,
)


def _tick(conn):
    return ticks.EvmTapeTicks(lambda *_: ETH_USD).tick(
        conn, {"network_id": "4663", "mint": MINT}, POOL, T0 + 1)


@pytest.mark.parametrize("corruption", [
    "orphan", "missing_header", "tick_null", "header_null", "tick_short", "header_short",
    "not_hex", "wrong_chain", "observation_missing", "observation_future",
])
def test_paper_tick_requires_verified_canonical_identity_and_observation(tmp_path, corruption):
    conn = sqlite3.connect(_tape(tmp_path))
    if corruption == "orphan":
        conn.execute("UPDATE pool_ticks SET block_hash=?", ("0x" + "bb" * 32,))
    elif corruption == "missing_header":
        conn.execute("DELETE FROM block_times")
    elif corruption == "wrong_chain":
        conn.execute("UPDATE block_times SET chain_id='8453'")
    elif corruption == "observation_missing":
        conn.execute("UPDATE pool_ticks SET observed_at=NULL")
    elif corruption == "observation_future":
        conn.execute("UPDATE pool_ticks SET observed_at=?",
                     (datetime.fromtimestamp(T0 + 60, timezone.utc).isoformat(),))
    else:
        table = "pool_ticks" if corruption.startswith("tick") else "block_times"
        value = None if corruption.endswith("null") else "0xabc"
        if corruption == "not_hex":
            value = "0x" + "zz" * 32
            conn.execute("UPDATE pool_ticks SET block_hash=?", (value,))
        conn.execute(f"UPDATE {table} SET block_hash=?", (value,))
    tick, reason = _tick(conn)
    conn.close()
    assert tick is None, f"{corruption} became a priced paper tick: {tick}"
    assert reason in {"hash_unverified", "no_canonical_swap", "observation_unknown"}


def test_paper_skips_a_proven_orphan_for_an_older_verified_standing_spot(tmp_path):
    conn = sqlite3.connect(_tape(tmp_path, usd=1.5))
    conn.execute("UPDATE pool_ticks SET block_hash=?", ("0x" + "bb" * 32,))
    old = datetime.fromtimestamp(T0 - 600, timezone.utc).isoformat()
    conn.execute("INSERT INTO block_times VALUES ('4663',9,?,?)", (old, BLOCK_HASH))
    conn.execute("INSERT INTO pool_ticks(chain_id,pool,kind,block_no,log_index,observed_at,"
                 "sqrt_price_x96,block_hash) VALUES ('4663',?,'v3_swap',9,0,?,?,?)",
                 (POOL, old, str(_sqrt_for(1.2)), BLOCK_HASH))
    tick, reason = _tick(conn)
    conn.close()
    assert reason is None and tick.price == pytest.approx(1.2)
    assert tick.observed_at == T0 - 600 and tick.carried
    assert tick.tick_kind == "v4_spot" and tick.source == "tape_v4_spot"


@pytest.mark.parametrize("corruption", ["orphan", "missing_header", "not_hex"])
def test_paper_source_fallback_cannot_bind_an_unverified_source_swap(tmp_path, corruption):
    conn = sqlite3.connect(_tape(tmp_path))
    if corruption == "missing_header":
        conn.execute("DELETE FROM block_times")
    else:
        value = "0x" + ("bb" if corruption == "orphan" else "zz") * 32
        conn.execute("UPDATE pool_ticks SET block_hash=?", (value,))
        if corruption == "not_hex":
            conn.execute("UPDATE block_times SET block_hash=?", (value,))
    pos = dict(network_id="4663", mint=MINT, origin_event_id="swap:raybot:0xsrc:wallet:" + MINT)
    assert ticks.EvmTapeTicks.source_pool(conn, pos) is None
    conn.close()


@pytest.mark.parametrize("late", [False, True])
def test_orphan_holds_price_rails_and_marks_but_time_rails_continue(tmp_path, late):
    conn, persistence = _ledger()
    pid = _open(conn, persistence)
    path = _tape(tmp_path, usd=1.30)
    tape = sqlite3.connect(path)
    tape.execute("UPDATE pool_ticks SET block_hash=?", ("0x" + "bb" * 32,))
    tape.commit()
    tape.close()
    engine, _ = _engine(conn, path, T0 + 1,
                        iso="2026-10-08T07:14:00+00:00" if late else "2026-10-08T07:00:30+00:00")
    engine.step()
    row = _row(conn, pid)
    assert row["peak_price"] == pytest.approx(1.0)
    assert conn.execute("SELECT count(*) FROM memecoin_position_marks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM memecoin_paper_path").fetchone()[0] == 0
    if late:
        assert row["is_open"] == 0 and row["exit_reason"].startswith("b_timecap")
        decision = json.loads(row["exit_decision_json"])
        assert decision["price"] is None and decision["source"] is None
    else:
        assert row["is_open"] == 1
    conn.close()


def test_carried_evm_time_is_a_subset_of_live_time_with_original_provenance(tmp_path):
    conn, persistence = _ledger()
    pid = _open(conn, persistence)
    path = _tape(tmp_path)
    tape = sqlite3.connect(path)
    tape.execute("UPDATE pool_ticks SET observed_at=?",
                 (datetime.fromtimestamp(T0 - 600, timezone.utc).isoformat(),))
    tape.commit()
    tape.close()
    engine, clock = _engine(conn, path, T0 + 1)
    engine.step()
    clock["t"] = T0 + 2
    engine.step()
    state = engine._state[pid]
    assert state["live_s"] == state["carried_s"] == 1.0
    assert engine.counters["live:carried"] == 2
    engine._flush(conn, _row(conn, pid), ticks.binding_of(_row(conn, pid)), state, T0 + 2, None)
    restored, _ = _engine(conn, path, T0 + 3)
    restored.step()
    saved = json.loads(_row(conn, pid)["paper_tick_json"])
    assert saved["live_s"] >= 1 and saved["carried_s"] >= 1
    assert saved["carried_s"] <= saved["live_s"]
    conn.close()


@pytest.mark.parametrize("pool,column", [(POOL, "v4_lag_s"), ("0x" + "b3" * 20, "v3_lag_s")])
@pytest.mark.parametrize("lag", [None, -5.0, "-0.1", float("inf"), float("-inf"),
                                "NaN", "Infinity", "bad"])
def test_paper_tick_rejects_invalid_selected_protocol_lag(tmp_path, pool, column, lag):
    conn = sqlite3.connect(_tape(tmp_path, usd=1.30))
    if pool != POOL:
        conn.execute("UPDATE pool_meta SET pool=?", (pool,))
        conn.execute("UPDATE pool_ticks SET pool=?", (pool,))
    conn.execute(f"UPDATE volume_feed_lag SET {column}=?", (lag,))
    tick, reason = ticks.EvmTapeTicks(lambda *_: ETH_USD).tick(
        conn, {"network_id": "4663", "mint": MINT}, pool, T0 + 1)
    conn.close()
    assert tick is None, f"{lag!r} became a priced paper tick: {tick}"
    assert reason == "protocol_lag_unknown"


@pytest.mark.parametrize("pool,column", [(POOL, "v4_lag_s"), ("0x" + "b3" * 20, "v3_lag_s")])
def test_paper_tick_accepts_measured_zero_selected_protocol_lag(tmp_path, pool, column):
    conn = sqlite3.connect(_tape(tmp_path, usd=1.30, lag=(None, None)))
    if pool != POOL:
        conn.execute("UPDATE pool_meta SET pool=?", (pool,))
        conn.execute("UPDATE pool_ticks SET pool=?", (pool,))
    conn.execute(f"UPDATE volume_feed_lag SET {column}=0")
    tick, reason = ticks.EvmTapeTicks(lambda *_: ETH_USD).tick(
        conn, {"network_id": "4663", "mint": MINT}, pool, T0 + 1)
    conn.close()
    assert reason is None and tick.price == pytest.approx(1.30)


@pytest.mark.parametrize("lag", [-5.0, "NaN"])
@pytest.mark.parametrize("late", [False, True])
def test_invalid_paper_lag_holds_price_rails_but_time_rails_continue(tmp_path, lag, late):
    conn, persistence = _ledger()
    pid = _open(conn, persistence)
    path = _tape(tmp_path, usd=1.30, lag=(lag, 0.0))
    engine, _ = _engine(conn, path, T0 + 1,
                        iso="2026-10-08T07:14:00+00:00" if late else "2026-10-08T07:00:30+00:00")
    engine.step()
    row = _row(conn, pid)
    assert row["peak_price"] == pytest.approx(1.0)
    assert conn.execute("SELECT count(*) FROM memecoin_position_marks").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM memecoin_paper_path").fetchone()[0] == 0
    if late:
        assert row["is_open"] == 0 and row["exit_reason"].startswith("b_timecap")
        decision = json.loads(row["exit_decision_json"])
        assert decision["price"] is None and decision["source"] is None
    else:
        assert row["is_open"] == 1
    conn.close()
