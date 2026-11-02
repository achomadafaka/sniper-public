"""E14 on Solana, step 0 (spec 2026-10-09 Solana pre-signal feed, operator approved via Alpha).

- The funded classifier records a Solana own-momentum shadow built from OUR marks already on the mark
  tape: the paper vault stream's `paper_tick:sol_*` and the funded swap marks' `fastlane_sol_*`.
- The series takes one kind only: spot-type marks (state, curve) when they make 3 points, else
  swap-type marks (execution prices). The two are never mixed.
- A series is flagged `includes_own_fill` when one of our funded fills on the coin falls inside its
  window, because then the path partly measures our own impact.
- The slot lane records its classification and its prefetched leg scale on EVERY row, rejected or not.
  Today a `slot_lane_scaled_clip` rejection leaves classification_json NULL.
- THE SHADOW DECIDES NOTHING. The phase, regime, risk scale, post-peak nomination and data quality are
  the provider's with or without it.
"""
import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_sol_momentum as sm
from sniper.services.memecoin_market_state import (classify_funded_entry, confirm_post_peak,
                                                   own_momentum_facts)

MINT = "SoLMint111111111111111111111111111111111111"
AT = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def _iso(seconds_before):
    return (AT - timedelta(seconds=seconds_before)).isoformat()


def _book():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _mark(c, seconds_before, mid, source, quality="live", mint=MINT):
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
              "VALUES ('solana', ?, ?, ?, ?, ?)", (mint, _iso(seconds_before), mid, source, quality))


def _series(c):
    return sm.own_marks_series(c, MINT, AT.timestamp())


def test_spot_marks_make_the_series_when_they_have_three_points():
    c = _book()
    for s, p in ((600, 1.0), (300, 1.5), (60, 1.2)):
        _mark(c, s, p, "paper_tick:sol_state" if s == 300 else "paper_tick:sol_curve")
    for s, p in ((500, 9.0), (200, 9.0), (100, 9.0)):
        _mark(c, s, p, "paper_tick:sol_swap")
    out = _series(c)
    assert out["reason"] == "ok" and out["protocol"] == "marks:spot"
    assert [p for _, p in out["points"]] == [1.0, 1.5, 1.2]


def test_swap_marks_only_when_spot_marks_are_too_few_and_never_mixed():
    c = _book()
    _mark(c, 600, 1.0, "fastlane_sol_state")
    for s, p in ((500, 2.0), (200, 2.2), (100, 2.4)):
        _mark(c, s, p, "fastlane_sol_swap")
    out = _series(c)
    assert out["protocol"] == "marks:swap"
    assert [p for _, p in out["points"]] == [2.0, 2.2, 2.4]


def test_only_our_own_live_marks_inside_the_window_count():
    c = _book()
    _mark(c, 1000, 5.0, "paper_tick:sol_curve")                  # before the window
    _mark(c, 300, 5.0, "paper_dexscreener")                      # provider
    _mark(c, 250, 5.0, "dexscreener")                            # provider
    _mark(c, 200, 5.0, "paper_tick:sol_curve", quality="quarantined_spike")
    _mark(c, 100, 5.0, "paper_tick:sol_curve", mint="OtherMint")
    _mark(c, -5, 5.0, "paper_tick:sol_curve")                     # after the decision
    out = _series(c)
    assert out["reason"] == "no_own_marks" and out["points"] == []


def test_the_series_feeds_the_shared_phase_rule():
    c = _book()
    for s, p in ((600, 1.0), (450, 1.5), (300, 1.4), (150, 1.0), (30, 0.9)):
        _mark(c, s, p, "paper_tick:sol_curve")
    facts = own_momentum_facts({**_series(c), "read_at": "x", "read_ms": 1.0, "wait_would_ms": 0.0})
    assert facts["phase"] == "post_peak" and facts["own_peak_price"] == 1.5
    assert facts["includes_own_fill"] is False


def _real_position_fill(c, at_seconds_before, *, mode="real", network="solana"):
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint=MINT, ticker="T", entry_price=1.0,
        entry_notional=100.0, entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=0.0,
        opened_at=_iso(at_seconds_before), strategy="real" if mode == "real" else "funded_clone",
        execution_mode=mode, network_id=network)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="chain", fraction=1.0, created_at=_iso(at_seconds_before))
    c.commit()


@pytest.mark.parametrize("seconds_before,mode,flag", [
    (300, "real", True),          # our funded fill inside the window
    (1200, "real", False),        # before the window
    (300, "paper", False),        # paper is simulated, no impact
])
def test_our_own_funded_fill_in_the_window_is_flagged(seconds_before, mode, flag):
    c = _book()
    for s, p in ((600, 1.0), (300, 1.1), (60, 1.2)):
        _mark(c, s, p, "fastlane_sol_swap")
    _real_position_fill(c, seconds_before, mode=mode)
    assert _series(c)["includes_own_fill"] is flag


def test_a_missing_ledger_is_unknown_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "absent.db"))
    out = sm.read_momentum(MINT)
    assert out["reason"] in ("ledger_unavailable", "read_failed") and out["points"] == []


# --- the read starts on Solana and is only peeked --------------------------------------------------

def test_solana_starts_a_read_on_the_e14_pool(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    seen = []
    monkeypatch.setattr(sm, "read_momentum", lambda mint: seen.append(mint) or {"reason": "ok", "points": []})
    # Its own pool, built by the real constructor: the process-wide one (2 workers, reads expire
    # after 2 s queued) may still be busy with earlier tests' real-ledger reads in this xdist worker,
    # which failed this test with shadow_queue_expired. monkeypatch restores the shared pool after.
    monkeypatch.setattr(rr, "_MOMENTUM_POOL", None)
    holder = rr._start_momentum_read("solana", MINT)
    assert holder is not None
    holder["future"].result(timeout=5)
    assert seen == [MINT]
    assert rr._start_momentum_read("56", "0xabc") is None
    assert rr._start_momentum_read("1", "0xabc") is None


# --- THE CHECK THAT FAILS IF THE SHADOW EVER FEEDS A DECISION ----------------------------------------

KW = dict(network_id="solana", mint=MINT, signal_source="follow_activity",
          decision_at=AT.isoformat(), effective_mcap=50_000.0, assumed_micro=False,
          base_clip_usd=None, consensus_n_60m=None)


def test_a_hot_post_peak_own_series_changes_no_decision():
    """The own series is a +50% runup and a 40% giveback: post_peak and runup >= 20 on the own basis.
    Every field that decides (phase, regime, risk scale, data quality, reason codes, nomination) must be
    the provider's."""
    pts = [(AT.timestamp() - 600 + 30 * i, p) for i, p in enumerate([1.0, 1.2, 1.5, 1.4, 1.0, 0.9])]
    read = {"reason": "ok", "points": pts, "protocol": "marks:spot", "includes_own_fill": False}
    base = classify_funded_entry(_book(), market_stats={"price": 1.0}, **KW)
    shadow = classify_funded_entry(_book(), market_stats={"price": 1.0}, momentum_own=read, **KW)
    assert shadow.facts["momentum_own"]["phase"] == "post_peak"
    for field in ("momentum_phase", "market_regime", "risk_scale", "data_quality", "reason_codes",
                  "lifecycle", "coin_band", "liquidity_class"):
        assert getattr(shadow, field) == getattr(base, field), field
    assert confirm_post_peak(shadow, exact_quote_price=0.5) == confirm_post_peak(base, exact_quote_price=0.5)
    assert {k: v for k, v in shadow.facts.items() if k != "momentum_own"} == base.facts


# --- the slot lane records its classification on every row -----------------------------------------

from test_slot_lane_scaled_sol import _buy, _lane, conn  # noqa: E402,F401  (fixture reuse)


def test_a_scaled_clip_rejection_records_its_classification_and_leg_scale(conn, tmp_path, monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75, sol=2.0,
                    shadow=100.0, knob=False)
    assert ln.handle(_buy())["reason"] == lane.SCALED_CLIP_REASON
    row = conn.execute("SELECT classification_json, trace_json FROM memecoin_entry_attempts").fetchone()
    assert row["classification_json"] is not None
    cls = json.loads(row["classification_json"])
    assert cls["risk_scale"] == 0.75 and "market_regime" in cls and "runup_pct" in cls["facts"]
    events = json.loads(row["trace_json"])["substage_events"]
    mark = next(e for e in events if e["name"] == "slot_leg_scale")
    assert (mark["risk_scale"], mark["leg_scale"], mark["scaled"]) == (0.75, 1.0, True)


def test_an_unreadable_leg_scale_is_recorded_as_unknown_not_nan(conn, tmp_path, monkeypatch):
    ln, _rec = _lane(conn, tmp_path, monkeypatch, leg_scale=None, coin_scale=0.75, sol=2.0,
                     shadow=100.0, knob=False)
    ln.handle(_buy())
    events = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0]
                        )["substage_events"]
    mark = next(e for e in events if e["name"] == "slot_leg_scale")
    assert mark["leg_scale"] is None and mark["scaled"] is True


def test_carried_marks_are_swap_type_never_spot():
    """Gamma M1 on 7271a137a: `paper_tick:sol_state_carried` prices the pool's LAST SWAP (an execution
    price) while the reserves sit still; a prefix match on `paper_tick:sol_state` called it spot. 14 of 32
    live 'marks:spot' series mixed or were all carried (CkYC13iy 01:13-01:18Z)."""
    c = _book()
    for s, p in ((700, 6.2e-05), (600, 6.4e-05), (500, 6.8e-05), (400, 6.6e-05)):
        _mark(c, s, p, "paper_tick:sol_state_carried")
    _mark(c, 300, 7.0e-05, "paper_tick:sol_state")
    _mark(c, 200, 7.1e-05, "fastlane_sol_curve")
    out = _series(c)
    assert out["protocol"] == "marks:swap" and out["carried_n"] == 4
    assert len(out["points"]) == 4


def test_a_failing_warm_up_never_stops_the_slot_lane(monkeypatch):
    """Gamma BLOCK on bbe968dfd: the SHADOW read pool's warm-up ran unguarded in start_slot_lane, so a
    pool or thread error ("can't start new thread") would have stopped the FUNDED lane from starting."""
    from sniper.services import memecoin_real_runner as rr
    from sniper.execution import pumpswap_buy as ps
    from sniper.services import memecoin_slot_lane as lane
    from sniper.settings import memecoin as cfg
    from test_slot_lane_pool_prewarm import _Rpc
    starts = []

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.name = name

        def start(self):
            starts.append(self.name)
    monkeypatch.setattr("threading.Thread", Thread)

    def broken_pool():
        raise RuntimeError("can't start new thread")
    monkeypatch.setattr(rr, "_momentum_read_pool", broken_pool)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_MISS_LEARN", False)
    amm = type("Amm", (), {"pools": ps.PumpSwapPoolCache(rpc=_Rpc({}))})()
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    lane.start_slot_lane(rc, builders={"pump_amm": amm}, signal_start_fn=lambda *a, **k: None,
                         sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
                         conn_factory=object, readonly_conn_factory=object)
    # Independent refresh workers since 0927c1630 (the same list test_slot_lane_miss_learn expects).
    assert starts == ["slot-lane-prefetch-sol_usd", "slot-lane-prefetch-balances",
                      "slot-lane-prefetch-blockhash", "slot-lane-prefetch", "slot-lane"]
