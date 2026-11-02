"""Deterministic test of the active runner's MAKER-ENTRY execution + slip logging (maker-entry-v1).

Drives run() with a scripted sequence of desks (no network) and asserts: a confirmed directional read
POSTS a resting bid (not a taker fill at the ask), the bid fills only when a PRINT trades into it
(tape monkeypatched in tests), and decision_price / fill_slip / entry_ofi /
entry_conviction are recorded. fill_slip is now typically NEGATIVE (maker entry beats the taker
counterfactual) — whether that survives adverse selection is what the campaign measures.
"""
import importlib.util
import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "active_runner", os.path.join(ROOT, "scripts", "run_polymarket_crypto5m_active_trader.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _desk(now, ws, *, up_ask, up_bid, ofi):
    """A strong, order-flow-agreeing Up read (clears the 0.50 gate)."""
    return {"now_ts": now, "window_start_ts": ws, "seconds_left": 200, "seconds_elapsed": 100,
            "slug": f"btc-updown-5m-{ws}", "condition_id": "0xtest", "available": True,
            "up_ask": up_ask, "up_bid": up_bid, "down_ask": round(1 - up_bid, 2), "down_bid": round(1 - up_ask, 2),
            "ret_5s": 0.0008, "ret_15s": 0.0008, "ret_30s": 0.0006, "ema_slope": 0.0002,
            "book_imbalance": 0.4, "microprice_gap": 0.01, "volume_dir": 0.2,
            "tape_ofi_10s": ofi, "tape_ofi_30s": ofi, "tape_n_10s": 20,
            "vol_per_s": 0.00005, "fair_value_up": 0.6, "z": 0.0,
            "up_mid": round((up_ask + up_bid) / 2, 4),
            "down_mid": round(((1 - up_bid) + (1 - up_ask)) / 2, 4)}


def test_maker_entry_fills_when_the_book_trades_into_us(tmp_path, monkeypatch):
    ws = 1782000000
    # SUSTAINED Up read so DirectionState confirms (>=3 ticks) -> QUOTE a resting bid at bid+tick=0.50?
    # No: bid 0.49 + tick capped at ask-tick = 0.49. Tick4 the book trades DOWN through our bid -> FILL.
    desks = iter([
        _desk(ws + 100, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # tick1 read Up (persist 1)
        _desk(ws + 102, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # tick2 still Up (persist 2)
        _desk(ws + 104, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # tick3 confirmed -> QUOTE bid @ 0.49
        _desk(ws + 106, ws, up_ask=0.49, up_bid=0.48, ofi=0.6),   # tick4 ask traded down to 0.49 -> FILL
        _desk(ws + 108, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # tick5 hold
    ])
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
    monkeypatch.setattr(runner.LD, "tape_prints", lambda *a, **k: [(0.49, 50.0)])   # a print at our bid
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner, "EVCFG", runner.RZ.EVConfig(k=0.6))  # strong ATM read clears the realized-fee gate

    conn = runner._connect(str(tmp_path / "t.sqlite"))
    runner.run(conn, poll_interval=0, max_iterations=5)

    rows = conn.execute("SELECT side, entry_price, decision_price, fill_slip, entry_ofi, entry_conviction, "
                        "entry_persistence, status, entry_reason FROM crypto5m_active_positions").fetchall()
    assert len(rows) == 1
    r = dict(rows[0])
    assert r["side"] == "Up"
    assert r["decision_price"] == 0.50          # the ask a TAKER would have paid at confirmation (tick 3)
    assert r["entry_price"] == 0.49             # our resting bid — the market traded into us
    assert abs(r["fill_slip"] - (-0.01)) < 1e-9  # maker entry BEAT the taker counterfactual by 1c
    assert "[maker fill/" in r["entry_reason"]
    assert r["entry_ofi"] == 0.6                # the gate's order-flow input, persisted for the review
    assert r["entry_conviction"] is not None
    assert r["entry_persistence"] >= 3          # entered only after the direction PERSISTED
    assert r["status"] == "open"


def test_maker_quote_cancels_instead_of_chasing_a_runaway_move(tmp_path, monkeypatch):
    """The move confirms, we post a bid, the market RUNS AWAY upward and the read then unconfirms
    (flat reads) — the quote cancels without ever paying up. No position; that's a measured MISS."""
    ws = 1782000900
    flat = {"ret_5s": 0.0, "ret_15s": 0.0, "ret_30s": 0.0, "ema_slope": 0.0, "volume_dir": 0.0,
            "book_imbalance": 0.0, "microprice_gap": 0.0}
    d5 = _desk(ws + 108, ws, up_ask=0.56, up_bid=0.55, ofi=0.0)
    d5.update(flat)                                                # read gone
    desks = iter([
        _desk(ws + 100, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),
        _desk(ws + 102, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),
        _desk(ws + 104, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # confirmed -> QUOTE bid @ 0.49
        _desk(ws + 106, ws, up_ask=0.54, up_bid=0.53, ofi=0.6),   # ran away (reprice capped at 0.49: ask-tick at decision)
        d5,                                                        # read unconfirms -> CANCEL
    ])
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner, "EVCFG", runner.RZ.EVConfig(k=0.6))
    conn = runner._connect(str(tmp_path / "t4.sqlite"))
    runner.run(conn, poll_interval=0, max_iterations=5)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_active_positions").fetchone()[0] == 0


def test_no_signal_no_fill(tmp_path, monkeypatch):
    """A weak read (below the 0.50 gate) never becomes a SIGNAL, so nothing fills."""
    ws = 1782000300
    weak = {"now_ts": ws + 100, "window_start_ts": ws, "seconds_left": 200, "slug": f"btc-updown-5m-{ws}",
            "condition_id": "0xtest", "up_ask": 0.50, "up_bid": 0.49, "down_ask": 0.51, "down_bid": 0.50,
            "ret_15s": 0.00001, "vol_per_s": 0.0005, "tape_ofi_10s": 0.0}      # tiny z -> no vote
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: dict(weak))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    conn = runner._connect(str(tmp_path / "t2.sqlite"))
    runner.run(conn, poll_interval=0, max_iterations=3)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_active_positions").fetchone()[0] == 0


def test_far_otm_confirmed_tick_blocked_but_logged(tmp_path, monkeypatch):
    """A confirmed but far-OTM read (BTC ticking Up yet still a deep underdog: the 0.11 case) must be BLOCKED
    by the EV gate, yet still LOGGED to crypto5m_ev_observations (gain computable)."""
    ws = 1782000600
    def desk(now):
        d = _desk(now, ws, up_ask=0.12, up_bid=0.11, ofi=0.6)   # Up signals confirm, but price is deep-underdog
        d.update({"z": -1.3, "up_mid": 0.115, "down_mid": 0.885, "vol_per_s": 0.0002, "seconds_left": 200})
        return d
    desks = iter([desk(ws + 100), desk(ws + 102), desk(ws + 104), desk(ws + 106)])
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner, "EVCFG", runner.RZ.EVConfig())   # defaults: gate on realized -> far-OTM fails
    conn = runner._connect(str(tmp_path / "t3.sqlite"))
    runner.run(conn, poll_interval=0, max_iterations=4)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_ev_observations").fetchone()[0] >= 1
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_active_positions").fetchone()[0] == 0
    row = conn.execute("SELECT passed_realfee FROM crypto5m_ev_observations LIMIT 1").fetchone()
    assert row[0] == 0


def test_shadow_log_is_written_and_does_not_change_decisions(tmp_path, monkeypatch):
    ws = 1782010000
    # reuse the _desk helper; add the shadow fields the runner will log
    def mk(now, **kw):
        d = _desk(now, ws, up_ask=0.50, up_bid=0.49, ofi=0.6)
        d.update({"z_v2": -0.1, "z_ours": 0.0, "z_market": -0.2, "z_weight": 0.6,
                  "vol_slow": 1e-4, "vol_fast": 2e-4, "jump_flag": False, "divergence": 2.0})
        d.update(kw)
        return d
    desks = iter([mk(ws + 100), mk(ws + 102), mk(ws + 104),
                  mk(ws + 106, up_ask=0.49, up_bid=0.48),   # book trades into our 0.49 maker bid -> fill
                  mk(ws + 108)])
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
    monkeypatch.setattr(runner.LD, "tape_prints", lambda *a, **k: [(0.49, 50.0)])   # a print at our bid
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    monkeypatch.setattr(runner, "EVCFG", runner.RZ.EVConfig(k=0.6))

    conn = runner._connect(str(tmp_path / "t.sqlite"))
    runner.run(conn, poll_interval=0, max_iterations=5)

    # shadow rows logged for the ticks
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_z_shadow").fetchone()[0] >= 1
    # the decision path is unchanged: the SAME entry the ds-patient+EV path always made still happens
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_active_positions WHERE status='open'").fetchone()[0] == 1


def test_shadow_logger_does_not_change_the_decision_ledger(tmp_path):
    """The §8.E zero-live-risk invariant, proved properly: run the SAME desk sequence WITH the shadow logger
    active and WITH it disabled (record_tick no-op), and assert the resulting decision ledger is IDENTICAL."""
    ws = 1782010000

    def _desks():
        def mk(now, **kw):
            d = _desk(now, ws, up_ask=0.50, up_bid=0.49, ofi=0.6)
            d.update({"z_v2": -0.1, "z_ours": 0.0, "z_market": -0.2, "z_weight": 0.6,
                      "vol_slow": 1e-4, "vol_fast": 2e-4, "jump_flag": False, "divergence": 2.0})
            d.update(kw)
            return d
        return iter([mk(ws + 100), mk(ws + 102), mk(ws + 104),
                     mk(ws + 106, up_ask=0.49, up_bid=0.48),   # book trades into our maker bid -> fill
                     mk(ws + 108)])

    cols = "side, entry_price, decision_price, fill_slip, entry_conviction, entry_persistence, status"

    def run_once(tag, *, logger_on):
        with pytest.MonkeyPatch.context() as mp:
            desks = _desks()
            mp.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
            mp.setattr(runner.LD, "tape_prints", lambda *a, **k: [(0.49, 50.0)])   # a print at our bid
            mp.setattr(runner.time, "sleep", lambda *_: None)
            mp.setattr(runner, "EVCFG", runner.RZ.EVConfig(k=0.6))
            if not logger_on:
                mp.setattr(runner.ZL, "record_tick", lambda *a, **k: 0)   # disable the shadow write
            conn = runner._connect(str(tmp_path / f"t_{tag}.sqlite"))
            runner.run(conn, poll_interval=0, max_iterations=5)
            rows = [dict(r) for r in conn.execute(
                f"SELECT {cols} FROM crypto5m_active_positions ORDER BY entry_ts")]
            return rows

    with_logger = run_once("on", logger_on=True)
    without_logger = run_once("off", logger_on=False)
    assert with_logger == without_logger          # the shadow logger changes NO live decision
    assert with_logger and with_logger[0]["status"] == "open"
