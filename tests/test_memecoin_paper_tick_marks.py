"""Paper step 1 (spec 2026-10-08 paper path without DexScreener, Gamma PASS f79ad24e3): the v1
engine's own tick is the paper mark and the paper path point.

- The tick lands on the mark tape as `paper_tick:<tick_kind>`.
- It lands on the paper path as basis `tick`, at the `last_quote_price` cadence (PERSIST_QUOTE_S).
- Marks and path points are written once per pass, so 49 positions do not each take the ledger's
  write lock.
- No fresh tick means no mark and no point, never a mid.
- The paper sweep stops writing `paper_dexscreener` marks for a coin that only v1 positions hold.
"""
import sqlite3

import pytest

from sniper.services import memecoin_exits as EX
from sniper.services import memecoin_paper_ticks as PT
from test_memecoin_paper_ticks import MINT, OPENED, T0, _engine, _ledger, _open as _open_at_wall, _set_price, _tape


def _open(c, m, **kw):
    """`opened_recorded_at` is stamped with the wall clock; these tests run on the fixture clock."""
    pid = _open_at_wall(c, m, **kw)
    c.execute("UPDATE memecoin_positions SET opened_recorded_at=? WHERE id=?", (OPENED, pid))
    c.commit()
    return pid


def _marks(c, source=None):
    q = "SELECT chain_id, mint, mid, source FROM memecoin_position_marks"
    rows = [dict(r) for r in c.execute(q)]
    return [r for r in rows if source is None or r["source"] == source]


def _path(c, pid):
    return [dict(r) for r in c.execute(
        "SELECT basis, source, price, kind FROM memecoin_paper_path WHERE position_id=? "
        "ORDER BY t_offset_s", (pid,))]


def test_a_live_tick_is_the_paper_mark_and_the_paper_path_point(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    eng, _clock = _engine(c, _tape(tmp_path, usd=1.10, received=T0), T0 + 1.0)
    eng.step()
    (mark,) = _marks(c)
    assert mark["source"] == "paper_tick:v4_spot" and mark["chain_id"] == "4663"
    assert mark["mint"] == MINT and mark["mid"] == pytest.approx(1.10, rel=1e-6)
    (point,) = _path(c, pid)
    assert point["basis"] == "tick" and point["source"] == "paper_tick:v4_spot"
    assert point["price"] == pytest.approx(1.10, rel=1e-6)


def test_no_fresh_tick_writes_no_mark_and_no_point(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    eng, _clock = _engine(c, _tape(tmp_path, usd=1.10, received=T0 - 600.0), T0 + 1.0)
    eng.step()
    assert _marks(c) == [] and _path(c, pid) == []


def test_the_point_follows_the_last_quote_cadence(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    tape = _tape(tmp_path, usd=1.10, received=T0)
    eng, clock = _engine(c, tape, T0 + 1.0)
    eng.step()
    clock["t"], clock["iso"] = T0 + 5.0, "2026-10-08T07:00:35+00:00"   # inside PERSIST_QUOTE_S
    eng.step()
    assert len(_path(c, pid)) == 1
    _set_price(tape, 1.12, received=T0 + PT.PERSIST_QUOTE_S)            # a fresh tick again
    clock["t"], clock["iso"] = T0 + 1.0 + PT.PERSIST_QUOTE_S, "2026-10-08T07:00:46+00:00"
    eng.step()
    assert len(_path(c, pid)) == 2


def test_marks_and_points_are_written_once_per_pass(tmp_path, monkeypatch):
    c, m = _ledger()
    a, b = _open(c, m), _open(c, m, arm="profile_matched_v1")
    calls = []
    real = EX.write_marks
    monkeypatch.setattr(EX, "write_marks", lambda *args, **kw: (calls.append(1), real(*args, **kw)))
    eng, _clock = _engine(c, _tape(tmp_path, usd=1.10, received=T0), T0 + 1.0)
    eng.step()
    assert calls == [1]
    assert len(_path(c, a)) == 1 and len(_path(c, b)) == 1


def test_the_sweep_writes_no_provider_mark_for_a_coin_only_v1_positions_hold():
    c, m = _ledger()
    _open(c, m)
    EX.sweep_exits(c, {("4663", MINT): {"price": 1.05, "liquidity": 1e6}},
                   "2026-10-08T07:01:00+00:00")
    assert _marks(c, EX.PAPER_SWEEP_SOURCE) == []


def test_a_v0_holder_keeps_the_provider_mark():
    c, m = _ledger()
    _open(c, m)
    _open(c, m, engine=PT.V0, arm="profile_matched_v1")
    EX.sweep_exits(c, {("4663", MINT): {"price": 1.05, "liquidity": 1e6}},
                   "2026-10-08T07:01:00+00:00")
    assert len(_marks(c, EX.PAPER_SWEEP_SOURCE)) == 1


def test_the_path_takes_a_basis_and_a_per_position_price():
    from sniper.services.memecoin_paper_path import record_paper_path
    c, m = _ledger()
    pid = _open(c, m)
    pos = dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    n = record_paper_path(c, [pos], {pid: {"price": 2.0, "source": "paper_tick:v4_spot"}},
                          "2026-10-08T07:00:10+00:00", basis="tick")
    assert n == 1
    assert _path(c, pid) == [{"basis": "tick", "source": "paper_tick:v4_spot", "price": 2.0,
                              "kind": "sample"}]


def test_a_tick_mark_and_a_provider_mid_on_one_coin_do_not_silence_each_other():
    """Gamma LOW on 24b9a8021: an unknown source falls in the 'mid' dedupe class, so a paper tick and
    a provider mid 5 s apart on one coin shared the 30 s floor and one of them was dropped."""
    c, _m = _ledger()
    EX.write_marks(c, {("4663", MINT): {"price": 1.0, "source": EX.PAPER_SWEEP_SOURCE}},
                   "2026-10-08T07:00:00+00:00")
    EX.write_marks(c, {("4663", MINT): {"price": 1.01, "source": "paper_tick:v4_spot"}},
                   "2026-10-08T07:00:05+00:00")
    assert {r["source"] for r in _marks(c)} == {EX.PAPER_SWEEP_SOURCE, "paper_tick:v4_spot"}
