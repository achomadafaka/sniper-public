"""Offline exact-cb8eba81 causal-input review witnesses; temporary databases only."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_paper as paper
from sniper.services import memecoin_desk_lessons as lessons
from sniper.services import memecoin_desk_ps_tape as ps
from test_desk_paper import tape, VQ, VT
from test_desk_lessons_v3 import ps_tape

@pytest.fixture(autouse=True)
def committed_tape(monkeypatch):
    """These fixtures write no universe heartbeat: the tape counts as committed through `now` (the frontier rule
    itself is pinned by tests/test_desk_paper_codex_frontier_witnesses.py, which does not use this)."""
    monkeypatch.setattr(paper, "_cut", lambda desk_dir, now, venue: now)


MINT = "7WF3wosXD5FtRN65NvsG2AvAkmhAG773ddYiY9cspump"
AT = datetime(2026, 10, 27, 0, 0, 1, tzinfo=timezone.utc).timestamp()


def complete_curve(root, at):
    tape(root, MINT, [(at, VQ, VT)]).close()
    with sqlite3.connect(root / "swaps" / (lessons.utc_day(at) + ".db")) as conn:
        conn.execute("UPDATE swaps SET real_token=0")


def test_midnight_does_not_hide_a_recorded_curve_gap(tmp_path):
    tape(tmp_path, MINT, [(AT - 4, VQ, VT)]).close()
    with sqlite3.connect(tmp_path / "gaps" / (lessons.utc_day(AT - 4) + ".db")) as conn:
        conn.execute("INSERT INTO gaps VALUES(?, 'stream_reset', 1)", (AT - 2,))
    state, why = paper.curve_state(tmp_path, MINT, AT)
    assert state is None and why == "tape_gap", (state, why)


def test_existing_unreadable_curve_gaps_are_unknown(tmp_path):
    tape(tmp_path, MINT, [(AT + 1, VQ, VT)]).close()
    (tmp_path / "gaps" / (lessons.utc_day(AT) + ".db")).write_bytes(b"not sqlite")
    state, why = paper.curve_state(tmp_path, MINT, AT + 3)
    assert state is None, (state, why)


def test_stale_pumpswap_state_is_not_an_executable_exit(tmp_path):
    at = AT + 300
    complete_curve(tmp_path, at - 122)
    ps_tape(tmp_path, [(at - 121, 1, 1, "0")])
    lamports, why = paper.sell_proceeds(tmp_path, MINT, 1000, at)
    assert lamports is None, (lamports, why)


@pytest.mark.parametrize("invalid", ["null_fee", "zero_liquidity"])
def test_new_invalid_pumpswap_state_blocks_old_liquidity(tmp_path, invalid):
    at = AT + 300
    complete_curve(tmp_path, at - 6)
    ps_tape(tmp_path, [(at - 5, 1, 1, "0"), (at - 2, 1, 1, None if invalid == "null_fee" else "0")])
    if invalid == "zero_liquidity":
        with sqlite3.connect(ps.tape_path(tmp_path)) as conn:
            conn.execute("UPDATE swaps_ps SET post_quote_reserve_atomic='0' WHERE received_at=?", (at - 2,))
    lamports, why = paper.sell_proceeds(tmp_path, MINT, 1000, at)
    assert lamports is None, (invalid, lamports, why)


def test_capture_gap_after_pumpswap_state_blocks_exit(tmp_path):
    at = AT + 300
    complete_curve(tmp_path, at - 6)
    ps_tape(tmp_path, [(at - 5, 1, 1, "0")], gaps=(at - 2,))
    lamports, why = paper.sell_proceeds(tmp_path, MINT, 1000, at)
    assert lamports is None, (lamports, why)


def test_fx_history_excludes_future_observations():
    fx = paper.FxHistory()
    fx.add(180.0, AT - 10)
    fx.add(200.0, AT + 10)
    assert fx.at(AT) == 180.0


def test_pool_identity_does_not_borrow_another_mints_state(tmp_path):
    at = AT + 300
    complete_curve(tmp_path, at - 6)
    ps_tape(tmp_path, [(at - 5, 1, 1, "0")])
    other = "So11111111111111111111111111111111111111112"
    assert lessons.load_pool(tmp_path, other, at - 3600, at) == []


@pytest.mark.parametrize("field", ["quote_mint", "cashback_fee_bps"])
def test_unknown_curve_currency_or_cashback_is_not_guessed(tmp_path, field):
    tape(tmp_path, MINT, [(AT + 1, VQ, VT)]).close()
    with sqlite3.connect(tmp_path / "swaps" / (lessons.utc_day(AT) + ".db")) as conn:
        conn.execute("UPDATE swaps SET " + field + "=NULL")
    state, why = paper.curve_state(tmp_path, MINT, AT + 3)
    assert state is None, (field, state, why)


def test_nonzero_cashback_is_not_claimed_native_executable(tmp_path):
    tape(tmp_path, MINT, [(AT + 1, VQ, VT)]).close()
    with sqlite3.connect(tmp_path / "swaps" / (lessons.utc_day(AT) + ".db")) as conn:
        conn.execute("UPDATE swaps SET cashback_fee_bps=500")
    lamports, why = paper.sell_proceeds(tmp_path, MINT, 10**12, AT + 3)
    assert lamports is None, (lamports, why)


def test_a_missing_tape_day_is_a_gap_not_no_gap(tmp_path):
    """Alpha on c50a739f8: an absent gaps file is not 'no gap' (10-25 had no tape at all). Last swap the day before,
    nothing for the asked day: unknown state, never a quote from the old reserves."""
    tape(tmp_path, MINT, [(AT - 7200, VQ, VT)]).close()            # 10-26 22:00Z; no 10-27 swaps file
    state, why = paper.curve_state(tmp_path, MINT, AT + 600, max_quiet_s=86400)
    assert state is None and why == "tape_day_missing", (state, why)
