"""The forward replay harness: record the pool state a funded fill actually hit.

The gap it closes: the impact gate could only be replayed against a proxy, because nothing stored
pool state at fill time and the tape retains about an hour.
"""
import importlib.util
import os
import sqlite3

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "research", "capture_fill_pool_state.py")
_SPEC = importlib.util.spec_from_file_location("capture_fill_pool_state", _PATH)
cap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cap)

NOW = "2026-09-23T09:30:00+00:00"


def _stores(tmp_path, ticks_rows=(), fills=()):
    led = sqlite3.connect(str(tmp_path / "led.db"))
    led.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, mint TEXT, "
                "execution_mode TEXT, network_id TEXT, opened_at TEXT)")
    led.execute("CREATE TABLE memecoin_fills (position_id INTEGER, kind TEXT, tx_sig TEXT)")
    for pid, mint, tx in fills:
        led.execute("INSERT INTO memecoin_positions VALUES (?,?,'real','4663','2026-09-23T08:00:00')",
                    (pid, mint))
        led.execute("INSERT INTO memecoin_fills VALUES (?,'entry',?)", (pid, tx))
    led.commit()
    vol = sqlite3.connect(str(tmp_path / "vol.db"))
    vol.execute("CREATE TABLE pool_ticks (chain_id TEXT, pool TEXT, block_no INTEGER, "
                "log_index INTEGER, tx_sig TEXT, liquidity TEXT, sqrt_price_x96 TEXT)")
    for row in ticks_rows:
        vol.execute("INSERT INTO pool_ticks VALUES (?,?,?,?,?,?,?)", row)
    vol.commit()
    cap.ensure(vol)
    return led, vol


def test_it_records_the_tick_before_ours_as_the_pre_fill_state(tmp_path):
    """Our own Swap carries the price AFTER the swap, so the pre-state is the preceding tick."""
    rows = [("4663", "0xpool", 100, 0, "0xearlier", "5000", "111"),
            ("4663", "0xpool", 101, 3, "0xours", "4000", "222")]
    led, vol = _stores(tmp_path, rows, [(7, "0xmint", "0xours")])
    planned, written, unknown = cap.capture(led, vol, since="2026-09-01", now_iso=NOW, confirm=True)
    assert (written, unknown) == (1, 0)
    got = vol.execute("SELECT pool, fill_block, pre_block, pre_liquidity, our_liquidity "
                      "FROM entry_fill_pool_state WHERE position_id=7").fetchone()
    assert got == ("0xpool", 101, 100, "5000", "4000")
    led.close(); vol.close()


def test_a_fill_the_tape_cannot_answer_writes_no_row(tmp_path):
    """Unknown is not zero: no tick for our transaction must not become a row of nulls."""
    led, vol = _stores(tmp_path, [], [(8, "0xmint", "0xmissing")])
    planned, written, unknown = cap.capture(led, vol, since="2026-09-01", now_iso=NOW, confirm=True)
    assert (planned, written, unknown) == ([], 0, 1)
    assert vol.execute("SELECT COUNT(*) FROM entry_fill_pool_state").fetchone()[0] == 0
    led.close(); vol.close()


def test_our_swap_with_no_earlier_tick_is_unknown_not_guessed(tmp_path):
    rows = [("4663", "0xpool", 101, 3, "0xours", "4000", "222")]
    led, vol = _stores(tmp_path, rows, [(9, "0xmint", "0xours")])
    _planned, written, unknown = cap.capture(led, vol, since="2026-09-01", now_iso=NOW, confirm=True)
    assert (written, unknown) == (0, 1)
    led.close(); vol.close()


def test_a_dry_run_writes_nothing_and_a_second_run_is_idempotent(tmp_path):
    rows = [("4663", "0xpool", 100, 0, "0xearlier", "5000", "111"),
            ("4663", "0xpool", 101, 3, "0xours", "4000", "222")]
    led, vol = _stores(tmp_path, rows, [(10, "0xmint", "0xours")])
    planned, written, _u = cap.capture(led, vol, since="2026-09-01", now_iso=NOW, confirm=False)
    assert len(planned) == 1 and written == 0
    assert vol.execute("SELECT COUNT(*) FROM entry_fill_pool_state").fetchone()[0] == 0
    cap.capture(led, vol, since="2026-09-01", now_iso=NOW, confirm=True)
    planned2, written2, _u2 = cap.capture(led, vol, since="2026-09-01", now_iso=NOW, confirm=True)
    assert (planned2, written2) == ([], 0), "an already-captured fill must not be re-read"
    led.close(); vol.close()


def test_the_tick_chosen_is_on_our_own_pool(tmp_path):
    """A busier pool in the same block must not supply the pre-state."""
    rows = [("4663", "0xother", 100, 0, "0xnoise", "9999", "999"),
            ("4663", "0xpool", 99, 1, "0xearlier", "5000", "111"),
            ("4663", "0xpool", 101, 3, "0xours", "4000", "222")]
    led, vol = _stores(tmp_path, rows, [(11, "0xmint", "0xours")])
    cap.capture(led, vol, since="2026-09-01", now_iso=NOW, confirm=True)
    got = vol.execute("SELECT pre_liquidity, pre_block FROM entry_fill_pool_state").fetchone()
    assert got == ("5000", 99)
    led.close(); vol.close()
