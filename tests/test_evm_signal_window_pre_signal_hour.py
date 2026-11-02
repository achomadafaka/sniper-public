"""The 4663 signal-window recorder keeps each signal's PRE-SIGNAL hour (spec
2026-09-30-memecoin-pre-signal-path-recording-design.md, Gamma §3a, Alpha: option (a), 4663 only).

The whole-chain volume tape (data/memecoin_onchain_volume.db) prunes rows older than 3,600 s every
60 s, and the recorder runs once a minute, so the hour before a signal is copied at first sight and
whatever the tape already pruned is recorded as `partial_start_s`, never guessed. These tests drive
the recorder's own `run_once` on a built tape and ledger; nothing touches the network.
"""
import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("evm_signal_window_recorder",
                                               ROOT / "scripts" / "research" / "evm_signal_window_recorder.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

T0 = 1_800_000_000.0            # the source's block 10_000 lands at T0; one block per second
SRC_TX = "0x" + "a" * 64


def _dbs(oldest_block):
    led, tape, out = (sqlite3.connect(":memory:") for _ in range(3))
    led.execute("CREATE TABLE memecoin_entry_attempts(attempt_id, origin_event_id, mint, source_user_id, "
                "source_handle, decision_started_at, terminal_status, terminal_reason, network_id, side)")
    tape.executescript("""
      CREATE TABLE pool_ticks(id INTEGER PRIMARY KEY, chain_id, pool, kind, block_no, tx_sig, log_index, maker,
        amount0, amount1, observed_at, tick, tick_lower, tick_upper, liquidity, sqrt_price_x96, block_hash);
      CREATE TABLE block_times(chain_id, block_no, block_ts, fetched_at, block_hash);
      CREATE TABLE onchain_pool_protocols(chain_id, pool, protocol, mint);
      CREATE TABLE pool_meta(chain_id, pool, token0, token1, quote_side);
      CREATE TABLE tick_gaps(chain_id, from_block, to_block, reason, detected_at, resolved_at);
      CREATE TABLE volume_feed_resets(chain_id, at, reason, window_s);""")
    for b in range(oldest_block, 10_700):
        tape.execute("INSERT INTO block_times VALUES ('4663',?,?,'x',NULL)", (b, R.iso_s(T0 + b - 10_000)))
    tape.execute("INSERT INTO onchain_pool_protocols VALUES ('4663','0xpool','v4','0xmint')")
    tape.execute("INSERT INTO pool_meta VALUES ('4663','0xpool','0x0','0xmint',0)")
    for b, tx in ((6_300, "0xtoo_old"), (6_400, "0xhour_start"), (9_000, "0xpre"), (9_999, "0xjust_before"),
                  (10_000, SRC_TX), (10_300, "0xpost")):
        if b >= oldest_block:
            tape.execute("INSERT INTO pool_ticks(chain_id,pool,kind,block_no,tx_sig,log_index,amount0,amount1,"
                         "observed_at) VALUES ('4663','0xpool','v3_swap',?,?,0,'5','-7','x')", (b, tx))
    led.execute("INSERT INTO memecoin_entry_attempts VALUES ('a1',?,'0xmint','u','h',?,'refused','x','4663','buy')",
                ("swap:raybot:%s:0xtrader:0xmint" % SRC_TX, datetime.fromtimestamp(T0 + 1, timezone.utc).isoformat()))
    return led, tape, out


def _signal(out):
    return out.execute("SELECT src_block, pre_block, partial_start_s, pre_ticks_n, ticks_n FROM signals "
                       "WHERE source_tx=?", (SRC_TX,)).fetchone()


def test_the_hour_before_the_source_is_copied_when_the_tape_still_holds_it():
    led, tape, out = _dbs(oldest_block=6_000)
    R.run_once(led, tape, out, "4663", now=T0 + 30, window_s=600)
    src_block, pre_block, partial, pre_n, _post = _signal(out)
    assert (src_block, pre_block, partial) == (10_000, 6_400, 0.0)
    assert pre_n == 3                                        # 6_400, 9_000, 9_999; never the source's own
    blocks = sorted(b for (b,) in out.execute("SELECT block_no FROM ticks"))
    assert blocks == [6_400, 9_000, 9_999, 10_000, 10_300]   # 6_300 is before the hour


def test_what_the_tape_already_pruned_is_counted_in_seconds_never_guessed():
    led, tape, out = _dbs(oldest_block=6_500)                # pruned 100 s into the hour
    R.run_once(led, tape, out, "4663", now=T0 + 30, window_s=600)
    src_block, pre_block, partial, pre_n, _post = _signal(out)
    assert (pre_block, partial, pre_n) == (6_500, 100.0, 2)
    assert R.pre_hour_complete(partial) is False              # 100 s short: C2/C4/C5 are C0 (Gamma §3a)
    assert R.pre_hour_complete(90.0) is True and R.pre_hour_complete(0.0) is True
    assert R.pre_hour_complete(None) is False                 # no hour at all is not a full hour


def test_the_pre_window_is_fixed_at_first_sight_and_later_runs_keep_it():
    led, tape, out = _dbs(oldest_block=6_000)
    R.run_once(led, tape, out, "4663", now=T0 + 30, window_s=600)
    tape.execute("DELETE FROM pool_ticks WHERE block_no < 9_500")          # the tape prunes on
    tape.execute("DELETE FROM block_times WHERE block_no < 9_500")
    R.run_once(led, tape, out, "4663", now=T0 + 90, window_s=600)
    assert _signal(out)[1:4] == (6_400, 0.0, 3)                # nothing lost, nothing re-measured


def test_an_existing_database_gains_the_new_columns_in_place():
    led, tape, out = _dbs(oldest_block=6_000)
    out.executescript(R.DDL.replace(", pre_block INTEGER, pre_from_ts REAL, partial_start_s REAL, pre_ticks_n INTEGER", ""))
    out.execute("INSERT INTO signals(signal_key, chain_id, mint, first_decided_at, attempts_json, closed) "
                "VALUES ('old','4663','0xold','2026-09-28T00:00:00+00:00','[]',1)")
    R.run_once(led, tape, out, "4663", now=T0 + 30, window_s=600)
    cols = {r[1] for r in out.execute("PRAGMA table_info(signals)")}
    assert {"pre_block", "pre_from_ts", "partial_start_s", "pre_ticks_n"} <= cols
    assert out.execute("SELECT pre_block FROM signals WHERE signal_key='old'").fetchone() == (None,)
    assert _signal(out)[1] == 6_400


def test_the_inline_selftest_still_passes():
    assert R.selftest() == 0


def test_a_database_at_its_size_cap_says_so_in_the_run_result():
    """Gamma LOW 1 on 6fb9f75b4: past --max-mb new signals stop; the cron log line must say FULL."""
    led, tape, out = _dbs(oldest_block=6_000)
    res = R.run_once(led, tape, out, "4663", now=T0 + 30, window_s=600, full=True)
    assert (res["new"], res["full"], res["skipped_full"]) == (0, True, 1)
    assert R.run_once(led, tape, out, "4663", now=T0 + 30, window_s=600)["full"] is False
