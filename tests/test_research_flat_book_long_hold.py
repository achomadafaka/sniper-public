"""A Long Hold position (tracking paused) does not close the research flat-book gate.

Operator order via Alpha, 2026-09-30: KMS 67523 stays in Long Hold and must not block research. The
runner spends no funded reads on a tracking-paused position, so every research copy of the flat-book
gate ignores it: an open real Solana position with `tracking_paused_at` set reads as flat, one without
it reads as open. Each copy is exercised through the function its mid-read checks call.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

ROOT = Path(__file__).resolve().parents[1]
PAUSED = "2026-09-30T17:49:34.567212+00:00"


def _ledger(tmp_path, paused):
    path = tmp_path / "sniper_tracker.db"
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions (mint, network_id, execution_mode, is_open, tracking_paused_at) "
              "VALUES ('KMSmint', 'solana', 'real', 1, ?)", (PAUSED if paused else None,))
    c.commit()
    c.close()
    return path


def _script(rel):
    spec = importlib.util.spec_from_file_location(Path(rel).stem + "_flatbook", ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _refused(fn):
    """True when a one-shot study stops on its flat-book check; False when it gets past it."""
    try:
        fn()
    except SystemExit as e:
        return "position is open" in str(e)
    except Exception as e:                         # past the gate: a table or log the test lacks
        assert not isinstance(e, sqlite3.OperationalError) or "no such table" in str(e), e
    return False


def _wallet_link_graph(path, monkeypatch):
    from scripts.research import wallet_link_graph as g
    return g.solana_book_open(sqlite3.connect(path))


def _tx_stream(path, monkeypatch):
    mod = _script("scripts/research/tx_stream_venue_capture.py")
    monkeypatch.setattr(mod, "STATE_ROOT", str(path.parent))
    return mod.solana_book_open()


def _copier_crowd(path, monkeypatch):
    from scripts.research import copier_crowd_and_fees as C
    return C.open_solana_book(sqlite3.connect(path)) > 0


def _ws_byte_budget(path, monkeypatch):
    mod = _script("scripts/research/memecoin_ws_byte_budget.py")
    monkeypatch.setattr(mod, "ROOT", path.parent)
    return mod._sol_book_open() > 0


def _chase_source_split(path, monkeypatch):
    mod = _script("scripts/research/chase_source_split.py")
    monkeypatch.setattr(mod, "LEDGER", path)
    return mod.book_open() > 0


def _venue_census(path, monkeypatch):
    return _script("scripts/research/venue_census.py").book_open(path) > 0


def _slot_minout(path, monkeypatch):
    return not _script("scripts/research/slot_minout_grading.py").flat(lambda: sqlite3.connect(path))


def _source_exit_speed(path, monkeypatch):
    mod = _script("scripts/research/source_exit_speed.py")
    monkeypatch.setattr(mod.wsb, "LEDGER", str(path))
    return mod.real_solana_open() > 0


def _dbc_venue_gap(path, monkeypatch):
    mod = _script("scripts/research/dbc_venue_gap.py")
    monkeypatch.setattr(mod, "LEDGER", f"file:{path}?mode=ro")
    return _refused(lambda: mod.load("2026-09-30T00:00:00+00:00"))


def _slot_lane_eligibility(path, monkeypatch):
    mod = _script("scripts/research/slot_lane_eligibility.py")
    monkeypatch.setattr(mod, "LEDGER", f"file:{path}?mode=ro")
    return _refused(lambda: mod.main("2026-09-30T00:00:00+00:00"))


def _slot_end_shadow(path, monkeypatch):
    mod = _script("scripts/memecoin_refusal_capture_loop.py")
    _trace_fn, flat_fn, _record_fn = mod.slot_end_io(path, path.parent / "caps.db")
    return not flat_fn()


GATES = [_wallet_link_graph, _tx_stream, _copier_crowd, _ws_byte_budget, _chase_source_split, _venue_census,
         _slot_minout, _source_exit_speed, _dbc_venue_gap, _slot_lane_eligibility, _slot_end_shadow]


@pytest.mark.parametrize("gate", GATES, ids=[g.__name__.lstrip("_") for g in GATES])
def test_a_long_hold_position_leaves_the_book_flat_and_a_tracked_one_does_not(tmp_path, monkeypatch, gate):
    paused, tracked = tmp_path / "paused", tmp_path / "tracked"
    paused.mkdir()
    tracked.mkdir()
    assert gate(_ledger(paused, paused=True), monkeypatch) is False
    assert gate(_ledger(tracked, paused=False), monkeypatch) is True
