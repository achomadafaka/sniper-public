"""The paper arm runs the funded volume-death rail, on the coins that have evidence for it.

vol_death is 32 of 206 funded closes in 7 days — 15% of funded exits had no paper counterpart at
all. The rail now runs on paper through `armed_exit_decision`, the SAME function the funded sweep
calls, so a knob move changes both sides together.

⚠️ IT CAN ONLY BE JUDGED WHERE SWAP EVIDENCE EXISTS, AND THAT IS 18% OF PAPER COINS. The rail reads
`memecoin_position_marks` with `source='fastlane_sol_swap'`, and that tape subscribes per open
FUNDED position: measured 2026-09-24, 19 of 106 solana paper coins carry any such mark, and the 20
coins funded also held are the same set. A paper-only coin has no swap evidence, ever.

That is SAFE — the rail refuses to accumulate silence unless the stream is provably alive, so a
coin with no marks yields a blind reason and never a would-fire — and it is UNSAFE TO READ NAIVELY,
because the firing rate would be measured over coins funded happened to hold. Covered and uncovered
holds are graded separately and never pooled.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_exits as ex


NOW = "2026-09-24T07:00:00+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(**over):
    # ⚠️ `experiment_status` IS LOAD-BEARING AND IT IS NOT DECORATION. `paper_policy_value` returns
    # the FALLBACK for any row whose status is not one of eligible / excluded_fill /
    # operator_modified, so a fixture without it reads every frozen parameter as absent — the rail
    # then declines for a reason that has nothing to do with what the test is asserting. The first
    # version of this file omitted it and `test_a_position_whose_policy_froze_no_threshold` passed
    # against a code path it never reached.
    base = {"id": 1, "network_id": "solana", "mint": "MINT", "entry_price": 1.0,
            "opened_at": "2026-09-24T06:00:00+00:00", "peak_price": 1.0,
            "experiment_status": "eligible",
            "assigned_policy_json": '{"params": {"voldeath_exit_s": 30.0, "max_hold_s": 99999.0}}'}
    base.update(over)
    return base


def test_the_fixture_actually_reaches_the_frozen_parameters():
    """Guards the trap above: if this fails, every other test here is proving nothing."""
    from sniper.services.memecoin_policy import paper_policy_value
    assert paper_policy_value(_pos(), "voldeath_exit_s", 0.0) == 30.0


def test_a_chain_without_the_swap_tape_is_never_judged(monkeypatch):
    """4663 has no fastlane_sol_swap tape, so the rail must not pretend to decide there."""
    called = []
    monkeypatch.setattr("sniper.services.memecoin_volume_death.armed_exit_decision",
                        lambda *a, **k: called.append(1) or (True, 99.0, "measured"))
    assert ex._paper_vol_death_reason(_conn(), _pos(network_id="4663"), NOW) is None
    assert called == [], "the evaluator is not even reached off solana"


def test_a_position_whose_policy_froze_no_threshold_is_not_judged(monkeypatch):
    """⚠️ THE REACH VALUE IS `voldeath_exit_s`. At 0 the rail is off for that row; give it 30 and
    the same fixture is decided. A test that leaves it unset proves nothing about either."""
    called = []
    monkeypatch.setattr("sniper.services.memecoin_volume_death.armed_exit_decision",
                        lambda *a, **k: called.append(1) or (True, 99.0, "measured"))
    pos = _pos(assigned_policy_json='{"params": {"voldeath_exit_s": 0.0}}')
    assert ex._paper_vol_death_reason(_conn(), pos, NOW) is None
    assert called == []


def test_the_rail_fires_on_measured_silence(monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_volume_death.armed_exit_decision",
                        lambda *a, **k: (True, 61.0, "measured_state_still"))
    monkeypatch.setattr("sniper.services.memecoin_volume_death.stream_healthy",
                        lambda *a, **k: True)
    assert ex._paper_vol_death_reason(_conn(), _pos(), NOW) == "vol_death"


def test_a_blind_coin_never_fires(monkeypatch):
    """No swap evidence is OUR outage, not the market's quiet — 82% of paper coins are here."""
    monkeypatch.setattr("sniper.services.memecoin_volume_death.armed_exit_decision",
                        lambda *a, **k: (False, None, "blind_frozen"))
    monkeypatch.setattr("sniper.services.memecoin_volume_death.stream_healthy",
                        lambda *a, **k: False)
    assert ex._paper_vol_death_reason(_conn(), _pos(), NOW) is None


def test_the_witness_is_persisted_under_its_own_reason(monkeypatch):
    """`memecoin_exit_trigger_audit` upserts on (position_id, reason, checked_minute), so sharing
    the funded `vol_death` label would REPLACE one verdict with the other in the same minute."""
    monkeypatch.setattr("sniper.services.memecoin_volume_death.armed_exit_decision",
                        lambda *a, **k: (True, 61.0, "measured_state_still"))
    monkeypatch.setattr("sniper.services.memecoin_volume_death.stream_healthy",
                        lambda *a, **k: True)
    conn = _conn()
    ex._paper_vol_death_reason(conn, _pos(), NOW)
    row = conn.execute("SELECT reason, allowed, detail FROM memecoin_exit_trigger_audit").fetchone()
    assert row is not None and row["reason"] == "paper_vol_death"
    assert row["allowed"] == 1 and "silence=61.0s" in row["detail"]


def test_the_price_rails_still_decide_first(monkeypatch):
    """Silence only decides what the timecap would otherwise have held to the end of its window."""
    monkeypatch.setattr("sniper.services.memecoin_volume_death.armed_exit_decision",
                        lambda *a, **k: (True, 61.0, "measured_state_still"))
    monkeypatch.setattr("sniper.services.memecoin_volume_death.stream_healthy",
                        lambda *a, **k: True)
    pos = _pos(assigned_policy_json='{"params": {"voldeath_exit_s": 30.0, "stop_pct": 35.0,'
                                    ' "max_hold_s": 99999.0}}')
    assert ex._frozen_real_shadow_reason(pos, 0.5, NOW, conn=_conn()) == "b_stop"


def test_without_a_connection_the_rail_cannot_run_and_says_nothing():
    """The legacy call shape must stay inert rather than silently skipping the rail as a decision."""
    assert ex._frozen_real_shadow_reason(_pos(), 1.0, NOW) is None


def test_the_rail_is_actually_wired_into_the_paper_decision(monkeypatch):
    """⚠️ THE NEGATIVE CONTROL CAUGHT THIS FILE WITH NO SUCH TEST. Removing the vol_death call from
    `_frozen_real_shadow_reason` left all eight earlier tests green, because seven call the helper
    directly and the eighth asserts a price rail that fires first. Nothing proved the rail reached
    the sweep's decision at all.

    The reach values here are the ones that let silence be the ONLY thing left to decide: no stop
    hit (mid at entry), a timecap far away, and the evaluator firing.
    """
    monkeypatch.setattr("sniper.services.memecoin_volume_death.armed_exit_decision",
                        lambda *a, **k: (True, 61.0, "measured_state_still"))
    monkeypatch.setattr("sniper.services.memecoin_volume_death.stream_healthy",
                        lambda *a, **k: True)
    pos = _pos(assigned_policy_json='{"params": {"voldeath_exit_s": 30.0, "stop_pct": 35.0,'
                                    ' "tp_pct": 25.0, "max_hold_s": 99999.0}}')
    assert ex._frozen_real_shadow_reason(pos, 1.0, NOW, conn=_conn()) == "vol_death"
