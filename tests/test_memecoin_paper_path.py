"""Paper price path recorder (2026-09-27, operator: "add a paper path recorder too").

Every open PAPER position gets its own bounded price path from the price the paper bracket already
fetched this pass, so a shape table (MFE, MAE, value at +10/30/60/120/300 s) can be built for paper
as it is for funded. Measured before the build (7 days, 2,850 closed paper positions): the only
per-position paper price is memecoin_position_marks, per MINT, gap p50 33 s, 2 points in the first
60 s, and a mid on 100% of rows. The bracket prices every ~15 s. No new provider call is made here.

The rows are labelled basis='mid' so a consumer can never pool them with funded exact quotes.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_paper_path as P

OPEN = "2026-09-27T20:00:00+00:00"


def _at(seconds):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(OPEN) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "ledger.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def _pos(pid=1, mode="paper", net="solana", mint="MintA"):
    return {"id": pid, "execution_mode": mode, "network_id": net, "mint": mint,
            "opened_at": OPEN, "opened_recorded_at": _at(0.5)}


def _key(net, mint):
    return (net, mint)


def _run(conn, seconds, price, *, positions=None, source="dexscreener"):
    positions = positions or [_pos()]
    price_map = {(p["network_id"], p["mint"]): {"price": price, "source": source} for p in positions}
    return P.record_paper_path(conn, positions, price_map, _at(seconds), key_fn=_key)


def _rows(conn, pid=1):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_paper_path WHERE position_id=? ORDER BY t_offset_s", (pid,))]


# --- the thinning rule ----------------------------------------------------------------------------

def test_every_observation_in_the_first_minute_then_one_per_15s_then_one_per_60s():
    assert P.keep_kind(5.0, None, 1.0) == "sample"                     # first row
    st = (1, 5.0, 1.0, 1.0)
    assert P.keep_kind(9.0, st, 1.0) == "sample"                       # first 60 s: every one
    assert P.keep_kind(70.0, (5, 62.0, 1.0, 1.0), 1.0) is None         # 8 s after the last
    assert P.keep_kind(77.0, (5, 62.0, 1.0, 1.0), 1.0) == "sample"     # 15 s step to 300 s
    assert P.keep_kind(340.0, (20, 300.0, 1.0, 1.0), 1.0) is None      # 40 s after the last
    assert P.keep_kind(360.0, (20, 300.0, 1.0, 1.0), 1.0) == "sample"  # 60 s step after 300 s


def test_a_new_high_or_low_is_always_kept_and_the_cap_is_hard():
    st = (20, 300.0, 2.0, 0.5)
    assert P.keep_kind(301.0, st, 2.1) == "high"
    assert P.keep_kind(301.0, st, 0.4) == "low"
    assert P.keep_kind(301.0, (P.MAX_ROWS, 300.0, 2.0, 0.5), 9.9) is None


# --- the recorder ---------------------------------------------------------------------------------

def test_paper_rows_carry_basis_source_and_offset_from_the_fill(conn):
    assert _run(conn, 10.5, 0.002) == 1
    (row,) = _rows(conn)
    assert row["basis"] == "mid" and row["source"] == "dexscreener"
    assert row["t_offset_s"] == pytest.approx(10.0)                   # from opened_recorded_at
    assert row["price"] == 0.002 and row["kind"] == "sample"
    assert row["network_id"] == "solana" and row["mint"] == "MintA"


def test_the_path_is_bounded_across_passes(conn):
    for s in range(0, 3600, 15):                                      # an hour of 15 s passes, flat
        _run(conn, s + 0.5, 0.002)
    rows = _rows(conn)
    first_minute = [r for r in rows if r["t_offset_s"] <= 60]
    assert len(first_minute) == 5                                     # 0, 15, 30, 45, 60
    assert len(rows) < 80                                             # not 240
    gaps = [b["t_offset_s"] - a["t_offset_s"] for a, b in zip(rows, rows[1:])
            if a["t_offset_s"] > 300]
    assert min(gaps) >= 60


def test_a_funded_position_is_never_recorded_here(conn):
    assert _run(conn, 5, 0.002, positions=[_pos(pid=7, mode="real")]) == 0
    assert _rows(conn, 7) == []


@pytest.mark.parametrize("price", [None, 0, 0.0, -1.0, float("nan"), "x"])
def test_no_price_is_no_row_never_a_zero(conn, price):
    assert _run(conn, 5, price) == 0
    assert _rows(conn) == []


def test_a_coin_missing_from_the_price_map_writes_nothing(conn):
    assert P.record_paper_path(conn, [_pos()], {}, _at(5), key_fn=_key) == 0


def test_a_price_before_the_fill_is_not_a_path_point(conn):
    assert _run(conn, 0.1, 0.002) == 0                                # 0.4 s before the fill


def test_the_same_pass_twice_writes_one_row(conn):
    assert _run(conn, 20, 0.002) == 1
    assert _run(conn, 20, 0.002) == 0


def test_rollback_knob_writes_nothing(conn, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_PATH_ENABLED", False)
    assert _run(conn, 5, 0.002) == 0
    assert _rows(conn) == []


def test_the_bracket_pass_records_the_path_from_its_own_price_map(conn, monkeypatch):
    """Through the real bracket_pass: the recorder reads the price map the pass already fetched,
    so no provider is called for it."""
    from sniper.services import memecoin_runner as run
    from sniper.services import memecoin_exits

    pos = _pos()
    monkeypatch.setattr(m, "list_open_positions", lambda c, *a, **k: [dict(pos)])
    monkeypatch.setattr(memecoin_exits, "sweep_exits", lambda *a, **k: [])
    monkeypatch.setattr(memecoin_exits, "recover_orphaned_paper_closes", lambda *a, **k: None)
    calls = []

    def price_fn(mints):
        calls.append(tuple(mints))
        return {("solana", "MintA"): {"price": 0.003, "source": "dexscreener"}}
    run.bracket_pass(conn, price_fn=price_fn, now_fn=lambda: _at(12.5), normalize=False)
    assert len(calls) == 1
    (row,) = _rows(conn)
    assert row["price"] == 0.003 and row["t_offset_s"] == pytest.approx(12.0)
