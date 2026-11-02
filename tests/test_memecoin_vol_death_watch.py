"""The vol_death rail records its clock, not only its verdict (2026-09-23).

Position 64243 (dec0ded, 4663, badabeepp) exited `vol_death` 165 s after the fill at -20%, and the
operator asked whether the rail was late. Answering it needed the on-chain tape, which retains
about ONE HOUR -- so by morning the question is unanswerable. The audit writes a row only for
`POST_THRESHOLD_REASONS`, i.e. only once silence has already crossed the threshold; the clock
BELOW it, which is what "late" means, was never persisted.

This is observation only. The watch row must never become a second fire path, and it must not
clobber the fire: `memecoin_exit_trigger_audit` upserts on (position_id, reason, checked_minute),
so a row sharing the `vol_death` reason inside the same minute would REPLACE the verdict that
minute -- the lossy-upsert trap this ledger has already paid for once.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_the_watch_row_and_the_fire_row_survive_the_same_minute():
    c = _conn()
    m.record_exit_trigger_audit(
        c, position_id=64243, reason="vol_death_watch", display_price=3.8e-05,
        preview_usdc=None, allowed=False, detail="measured silence=12.3s",
        checked_at="2026-09-23T22:19:10+00:00", lane="sweep")
    m.record_exit_trigger_audit(
        c, position_id=64243, reason="vol_death", display_price=3.8e-05,
        preview_usdc=None, allowed=True, detail="measured_state_still silence=31.2s",
        checked_at="2026-09-23T22:19:59+00:00", lane="sweep")
    rows = [dict(r) for r in c.execute(
        "select reason, allowed, detail from memecoin_exit_trigger_audit"
        " where position_id=64243 order by id")]
    assert [r["reason"] for r in rows] == ["vol_death_watch", "vol_death"], rows
    assert rows[0]["allowed"] == 0 and rows[1]["allowed"] == 1


def test_the_watch_row_keeps_the_label_first_token_contract():
    """The covered/uncovered split reads the label out of `detail` as the first whitespace token,
    and the watch row is parsed by the same readers."""
    c = _conn()
    m.record_exit_trigger_audit(
        c, position_id=1, reason="vol_death_watch", display_price=None, preview_usdc=None,
        allowed=False, detail="measured silence=4.0s", checked_at="2026-09-23T22:19:10+00:00",
        lane="sweep")
    detail = c.execute("select detail from memecoin_exit_trigger_audit").fetchone()[0]
    assert detail.split()[0] == "measured"
    assert detail.split()[1].startswith("silence=")


def test_the_sweep_records_the_clock_below_the_threshold():
    """The runner must write the watch row for the PRE-threshold state. Source-level, like the
    label contract next door: this is about what the sweep persists, and a comment cannot satisfy
    it."""
    import pathlib

    from _source_guard import code_of

    # `code_of` re-unparses the AST, so quote style is normalised away -- match on the token.
    src = code_of(pathlib.Path("sniper/services/memecoin_real_runner.py"))
    assert "vol_death_watch" in src, "the pre-threshold clock is not persisted anywhere"
    assert "_vd_why == 'measured'" in src or '_vd_why == "measured"' in src, \
        "the watch row must be scoped to the pre-threshold state, never written unconditionally"


def test_a_blind_minute_writes_its_own_row_beside_the_others():
    """G5 (Gamma 2026-10-08): only measured minutes wrote a row, so a blind minute left no trace
    and coverage gaps could not be split by cause. A blind verdict writes `vol_death_blind`, on its
    own reason so the (position, reason, minute) upsert never replaces a watch or fire row."""
    c = _conn()
    for reason, detail in (("vol_death_watch", "measured silence=12.3s"),
                           ("vol_death_blind", "blind_frozen chain=1 silence=-1.0s")):
        m.record_exit_trigger_audit(
            c, position_id=7, reason=reason, display_price=None, preview_usdc=None,
            allowed=False, detail=detail, checked_at="2026-10-08T22:40:10+00:00", lane="sweep")
    rows = [tuple(r) for r in c.execute(
        "select reason, detail from memecoin_exit_trigger_audit where position_id=7 order by id")]
    assert [r[0] for r in rows] == ["vol_death_watch", "vol_death_blind"]
    assert rows[1][1].split()[0] == "blind_frozen" and "chain=1" in rows[1][1]


def test_the_sweep_records_every_verdict_that_wrote_nothing_before():
    """Every vol_death verdict leaves a row: measured -> watch, post-threshold -> vol_death, and
    everything else -> vol_death_blind, so none is silent."""
    import pathlib

    from _source_guard import code_of

    src = code_of(pathlib.Path("sniper/services/memecoin_real_runner.py"))
    assert "vol_death_blind" in src, "blind minutes are still not persisted"
    assert "_vd_why not in POST_THRESHOLD_REASONS" in src, \
        "the blind row must cover exactly the verdicts the other two rows do not"
