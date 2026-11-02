"""Real positions must track their high-water mark (peak_price) each exit sweep, and the
/real/positions route must surface max_gain_pct derived from it.

Bug (2026-07-15 UI audit): the "Peak" column on the real-money position board was blank for
EVERY open real position. Root cause was two-fold — the real exit sweep never advanced
peak_price (it stayed frozen at entry, so coins that were up +5..+15% still read peak==entry),
and max_gain_pct is only stamped at close, so an OPEN position never had a value to show. The
paper sweep already tracks peak via _track_peak; the real sweep must do the same, and the route
derives the live max-favorable-excursion from peak_price/entry_price.
"""
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg
from sniper.routes.api_memecoin import create_api_memecoin_blueprint

NOW = "2026-07-15T00:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, *, entry_price=1.0, entry_notional=50.0, mint="So1", network_id="solana"):
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
        entry_price=entry_price, entry_notional=entry_notional, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at=NOW,
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id=network_id)
    return pid


def _no_exit_context():
    # a valid mid means the sweep never needs a preview; exit_fn must never fire in these tests
    return {"solana": {
        "exit_fn": lambda *a, **k: pytest.fail("no exit should fire on a modest gain"),
        "preview_exit_fn": lambda pos: pytest.fail("no preview needed when a live mid exists"),
    }}


def test_real_sweep_advances_peak_to_new_high(monkeypatch):
    # disable every exit rail so the position stays open and we can observe peak tracking in isolation
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    c = _c()
    pid = _open_real(c)                                  # entry 1.0 -> peak seeded at entry
    for _ in range(2):                                   # a second tick confirms the first (2026-10-17)
        rr.real_exit_sweep(c, _no_exit_context(),
                           price_fn=lambda mints: {("solana", "So1"): {"price": 1.20}},  # +20%
                           now_fn=lambda: NOW)
    assert m.get_position(c, pid)["peak_price"] == pytest.approx(1.20)


def test_real_sweep_peak_is_a_high_water_mark(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    c = _c()
    pid = _open_real(c)
    for px in (1.20, 1.20, 1.05, 1.10):   # up (confirmed), back down, partway up — peak holds at 1.20
        rr.real_exit_sweep(c, _no_exit_context(),
                           price_fn=lambda mints, _p=px: {("solana", "So1"): {"price": _p}},
                           now_fn=lambda: NOW)
    assert m.get_position(c, pid)["peak_price"] == pytest.approx(1.20)


def test_swap_mark_updates_only_the_nominal_peak(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    c = _c()
    pid = _open_real(c)
    c.execute(
        "UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?",
        (1.0, pid),
    )
    c.commit()
    context = {
        "solana": {
            "exit_fn": lambda *args: pytest.fail("the modest mark must not exit"),
            "preview_exit_fn": lambda _pos: None,
        }
    }
    for _ in range(2):                                   # a second tick confirms the first (2026-10-17)
        rr.real_exit_sweep(
            c,
            context,
            price_fn=lambda _mints: {
                ("solana", "So1"): {
                    "price": 1.20,
                    "source": "fastlane_sol_swap",
                    "quality": "live",
                }
            },
            now_fn=lambda: NOW,
        )
    pos = m.get_position(c, pid)
    assert pos["peak_price"] == pytest.approx(1.20)
    assert pos["executable_peak_price"] == pytest.approx(1.0)


def test_real_sweep_spike_tick_does_not_ratchet_peak(monkeypatch):
    # peak_price is a ratchet (only rises, never revised down) — a single implausible up-spike must
    # be quarantined before it can corrupt the operator's MFE forever, exactly like the paper sweep
    # (finding-memecoin-bad-tick-spike-guard-2026-07-14: Jotchua $0.0034 -> $17.099 phantom +$376k).
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    c = _c()
    pid = _open_real(c, entry_price=1.0)                 # baseline = entry 1.0
    ctx = {"solana": {
        "exit_fn": lambda *a, **k: pytest.fail("a spike tick must not fire an exit"),
        "preview_exit_fn": lambda pos: None,             # no exact mid -> mint simply unquoted
    }}
    rr.real_exit_sweep(c, ctx,
                       price_fn=lambda mints: {("solana", "So1"): {"price": 50.0}},  # 50x spike
                       now_fn=lambda: NOW)
    assert m.get_position(c, pid)["peak_price"] == pytest.approx(1.0)   # spike rejected, peak intact


def test_real_positions_route_derives_max_gain_from_peak(monkeypatch):
    # route must not depend on a live quote to compute MFE — stub dexscreener to offline
    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats_batch_keyed",
                        lambda keys, http=None: {})
    c = _c()
    pid = _open_real(c, entry_price=1.0)
    m.update_position_peak(c, pid, 1.5, NOW, 0)          # a tracked +50% display high-water mark
    m.update_position_executable_peak(c, pid, 1.5)       # the funded max gain reads the executable one
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    r = app.test_client().get("/api/memecoin/real/positions")
    assert r.status_code == 200
    row = next(p for p in r.get_json()["positions"] if p["id"] == pid)
    assert row["max_gain_pct"] == pytest.approx(50.0)
