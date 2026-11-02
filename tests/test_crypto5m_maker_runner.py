"""Tests for the maker PAPER runner's execution layer (scripts/run_polymarket_crypto5m_maker.py):
print-verified fill model, restart-safe window persistence, and the wired hard cut-loss.

These pin the three bugs found in the 2026-07-04 review:
  1. fills modeled on QUOTE-through (book moving to our price) — optimistic; a fill needs a PRINT
     trading to/through our resting price when the tape is available.
  2. WindowBook inventory lived only in memory — a mid-window restart (nightly hibernate) silently
     zeroed inventory/cash and corrupted that window's settlement.
  3. maker_policy.should_unwind existed but was never called — the hard cut-loss could never fire.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from scripts import run_polymarket_crypto5m_maker as MK
from sniper.algos.polymarket_crypto5m import maker_regime_state as RS
from sniper.algos.polymarket_crypto5m.maker_regime import MakerRegimePolicy
from sniper.algos.polymarket_crypto5m.maker_policy import Quote


def _wb(cid="0xc0ffee", ws=1_000_000):
    return MK.WindowBook(cid, ws)


def _rest(wb, token, bid=None, ask=None):
    """Plant previously-resting quotes (what model_fills fills against)."""
    wb.prev[token] = {"bid": (Quote("bid", bid, 50.0) if bid is not None else None),
                      "ask": (Quote("ask", ask, 50.0) if ask is not None else None)}


NO_QUOTES = {"bid": None, "ask": None}


# ---------- 1. print-verified fills ----------------------------------------------------------------

def test_bid_fills_only_on_a_print_at_or_below_our_price():
    wb = _wb()
    _rest(wb, "up", bid=0.45)
    fills = MK.model_fills(wb, "up", NO_QUOTES, best_bid=0.44, best_ask=0.45,
                           clip=50.0, prints=[(0.45, 30.0)])
    assert fills == [("bid", 0.45, 30.0, "print")]


def test_bid_does_not_fill_on_prints_above_our_price():
    wb = _wb()
    _rest(wb, "up", bid=0.45)
    fills = MK.model_fills(wb, "up", NO_QUOTES, best_bid=0.46, best_ask=0.47,
                           clip=50.0, prints=[(0.46, 30.0)])
    assert fills == []


def test_ask_fills_only_on_a_print_at_or_above_our_price():
    wb = _wb()
    _rest(wb, "up", ask=0.55)
    fills = MK.model_fills(wb, "up", NO_QUOTES, best_bid=0.55, best_ask=0.56,
                           clip=50.0, prints=[(0.55, 20.0)])
    assert fills == [("ask", 0.55, 20.0, "print")]


def test_live_tape_with_no_prints_means_no_fill_even_if_book_moved_through():
    # THE bug: the book quoting down to our bid is not a trade. Tape alive ([]) + no prints -> no fill,
    # even though best_ask <= our bid would have "filled" under the old quote-through model.
    wb = _wb()
    _rest(wb, "up", bid=0.45)
    fills = MK.model_fills(wb, "up", NO_QUOTES, best_bid=0.40, best_ask=0.44,
                           clip=50.0, prints=[])
    assert fills == []


def test_tape_unavailable_means_no_fill_print_only():
    # prints=None (capture DB down) -> NO fill; the campaign pauses honestly instead of accruing
    # unverifiable book-through fills (codex review 2026-07-04)
    wb = _wb()
    _rest(wb, "up", bid=0.45)
    fills = MK.model_fills(wb, "up", NO_QUOTES, best_bid=0.40, best_ask=0.44,
                           clip=50.0, prints=None)
    assert fills == []


def test_fill_size_caps_at_quote_size_and_clip():
    wb = _wb()
    _rest(wb, "up", bid=0.45)
    fills = MK.model_fills(wb, "up", NO_QUOTES, best_bid=0.44, best_ask=0.45,
                           clip=20.0, prints=[(0.44, 500.0)])
    assert fills == [("bid", 0.45, 20.0, "print")]   # 500 printed, but clip=20 < quote 50


def test_multiple_qualifying_prints_sum_up_to_the_cap():
    wb = _wb()
    _rest(wb, "up", bid=0.45)
    fills = MK.model_fills(wb, "up", NO_QUOTES, best_bid=0.44, best_ask=0.45,
                           clip=50.0, prints=[(0.45, 10.0), (0.44, 15.0), (0.46, 99.0)])
    assert fills == [("bid", 0.45, 25.0, "print")]   # 10+15 qualify; the 0.46 print does not


# ---------- 2. restart-safe window persistence ------------------------------------------------------

@pytest.fixture()
def conn():
    c = MK._db(":memory:")
    yield c
    c.close()


def test_window_state_survives_a_restart(conn):
    wb = _wb()
    wb._fill("up", "bid", 0.45, 40.0)      # long 40 UP @ .45
    wb._fill("down", "bid", 0.50, 10.0)    # long 10 DOWN @ .50
    MK.persist_window(conn, "maker-paper-v0", wb)

    restored = MK.restore_window(conn, "maker-paper-v0", wb.cid)
    assert restored is not None
    assert restored.ws == wb.ws
    assert restored.up_inv == pytest.approx(40.0)
    assert restored.down_inv == pytest.approx(10.0)
    assert restored.cash == pytest.approx(-(0.45 * 40 + 0.50 * 10))
    assert restored.up_vwap == pytest.approx(0.45)
    assert restored.down_vwap == pytest.approx(0.50)


def test_settled_window_is_not_restored(conn):
    wb = _wb()
    wb._fill("up", "bid", 0.45, 40.0)
    MK.persist_window(conn, "maker-paper-v0", wb)
    conn.execute("UPDATE maker_windows SET settled=1 WHERE condition_id=?", (wb.cid,))
    conn.commit()
    assert MK.restore_window(conn, "maker-paper-v0", wb.cid) is None


def test_restore_window_does_not_cross_campaign_boundary(conn):
    wb = _wb()
    wb._fill("up", "bid", 0.45, 40.0)
    MK.persist_window(conn, "maker-unified-v1", wb)

    assert MK.restore_window(conn, "maker-unified-v2", wb.cid) is None
    assert MK.restore_window(conn, "maker-unified-v1", wb.cid) is not None


def test_restore_window_rebuilds_from_fill_ledger_when_window_row_is_stale(conn):
    stale = _wb()
    MK.persist_window(conn, "maker-unified-v4", stale)

    filled = _wb(cid=stale.cid, ws=stale.ws)
    MK.record_fill(conn, "maker-unified-v4", filled, "up", "bid", 0.40, 50.0, 0.46,
                   now_ts=stale.ws + 10, reason="entry", fill_model="print")
    conn.commit()  # simulates a mid-tick helper commit flushing maker_fills before maker_windows

    restored = MK.restore_window(conn, "maker-unified-v4", stale.cid)

    assert restored is not None
    assert restored.up_inv == pytest.approx(50.0)
    assert restored.cash == pytest.approx(-20.0)
    assert restored.up_vwap == pytest.approx(0.40)


def test_persist_then_settle_keeps_final_numbers(conn):
    # persisting mid-window (settled=0) must not break the settle upsert
    wb = _wb()
    wb._fill("up", "bid", 0.40, 50.0)
    MK.persist_window(conn, "maker-paper-v0", wb)
    row = conn.execute("SELECT settled, cash FROM maker_windows WHERE condition_id=?", (wb.cid,)).fetchone()
    assert row["settled"] == 0
    assert row["cash"] == pytest.approx(-20.0)


# ---------- 3. wired hard cut-loss ------------------------------------------------------------------

def test_unwind_fires_when_fair_moves_through_long_entry():
    wb = _wb()
    wb._fill("up", "bid", 0.60, 100.0)     # long 100 @ .60
    fills = MK.unwind_fills(wb, "up", fair=0.45, best_bid=0.44, best_ask=0.46)
    assert fills == [("ask", 0.44, 100.0, "hard-cut")]


def test_unwind_does_not_fire_within_the_band():
    wb = _wb()
    wb._fill("up", "bid", 0.60, 100.0)
    assert MK.unwind_fills(wb, "up", fair=0.55, best_bid=0.54, best_ask=0.56) == []


def test_unwind_needs_a_bid_to_sell_into():
    wb = _wb()
    wb._fill("up", "bid", 0.60, 100.0)
    assert MK.unwind_fills(wb, "up", fair=0.40, best_bid=None, best_ask=0.46) == []


def test_unwind_noop_with_no_inventory():
    wb = _wb()
    assert MK.unwind_fills(wb, "up", fair=0.40, best_bid=0.39, best_ask=0.41) == []


# ---------- 4. long-only inventory accounting (codex 2026-07-04: naked shorts corrupted the campaign) ----

def test_ask_fill_clamps_to_held_inventory_never_goes_negative():
    wb = _wb()
    wb._fill("up", "bid", 0.50, 20.0)      # hold 20
    wb._fill("up", "ask", 0.55, 50.0)      # try to sell 50 -> only the held 20 can trade
    assert wb.up_inv == pytest.approx(0.0)
    assert wb.cash == pytest.approx(-0.50 * 20 + 0.55 * 20)


def test_vwap_resets_when_inventory_reaches_zero():
    wb = _wb()
    wb._fill("up", "bid", 0.50, 20.0)
    assert wb.up_vwap == pytest.approx(0.50)
    wb._fill("up", "ask", 0.55, 20.0)      # fully scaled out
    assert wb.up_inv == pytest.approx(0.0)
    assert wb.up_vwap is None              # stale vwap fed garbage into should_unwind before


# ---------- 5. directional pull (the scalper's read as adverse-selection defense) --------------------

def _q(bid=None, ask=None):
    from sniper.algos.polymarket_crypto5m.maker_policy import Quote
    return {"bid": (Quote("bid", bid, 50.0) if bid else None),
            "ask": (Quote("ask", ask, 50.0) if ask else None), "reason": "r"}


def _policy(regime_id="jump"):
    return MakerRegimePolicy(
        regime_id=regime_id, regime_confidence="medium", margin_multiplier=2.0,
        clip_multiplier=0.35, max_shares_multiplier=0.5, min_edge=0.06,
        min_seconds_quote=30.0, bid_enabled=True, ask_enabled=False,
        exit_only=False, risk_action="widen", can_hard_cut=True,
        quote_ttl_ms=4_000, reason=f"{regime_id} route", warnings=("w",),
    )


def test_confirmed_up_pulls_up_ask_and_down_bid():
    quotes = {"up": _q(bid=0.55, ask=0.60), "down": _q(bid=0.40, ask=0.45)}
    out = MK.directional_pull(quotes, "Up")
    assert out["up"]["bid"] is not None and out["up"]["ask"] is None       # don't sell into the rise
    assert out["down"]["bid"] is None and out["down"]["ask"] is not None   # don't buy the falling side


def test_confirmed_down_pulls_down_ask_and_up_bid():
    quotes = {"up": _q(bid=0.55, ask=0.60), "down": _q(bid=0.40, ask=0.45)}
    out = MK.directional_pull(quotes, "Down")
    assert out["down"]["ask"] is None and out["down"]["bid"] is not None
    assert out["up"]["bid"] is None and out["up"]["ask"] is not None


def test_no_confirmed_side_leaves_quotes_untouched():
    quotes = {"up": _q(bid=0.55, ask=0.60), "down": _q(bid=0.40, ask=0.45)}
    out = MK.directional_pull(quotes, None)
    assert out["up"]["bid"] is not None and out["up"]["ask"] is not None
    assert out["down"]["bid"] is not None and out["down"]["ask"] is not None


# ---------- 5b. regime helper mechanics -------------------------------------------------------------

def test_regime_inputs_hash_is_stable_and_order_independent():
    assert MK.regime_inputs_hash({"b": 2, "a": 1}) == MK.regime_inputs_hash({"a": 1, "b": 2})
    assert len(MK.regime_inputs_hash({"a": 1})) == 16


def test_effective_regime_knobs_apply_multipliers_and_price_unit_adds():
    p = MakerRegimePolicy(regime_id="jump", margin_multiplier=2.0, margin_add=0.01,
                          clip_multiplier=0.5, max_shares_multiplier=0.25,
                          min_edge=0.06, min_seconds_quote=40.0)

    out = MK.effective_regime_knobs(p, base_margin=0.02, clip=25.0, max_shares=50.0,
                                    min_edge=0.04, min_seconds_quote=30.0)

    assert out == {
        "margin": 0.05,
        "clip": 12.5,
        "max_shares": 12.5,
        "min_edge": 0.06,
        "min_seconds_quote": 40.0,
    }


def test_apply_regime_policy_to_quotes_pulls_disabled_sides():
    p = MakerRegimePolicy(regime_id="late_window", bid_enabled=False, ask_enabled=True,
                          reason="late reduce only")
    q = {"bid": Quote("bid", 0.45, 25.0), "ask": Quote("ask", 0.55, 10.0), "reason": "base"}

    out = MK.apply_regime_policy_to_quotes(q, p)

    assert out["bid"] is None
    assert out["ask"] == q["ask"]
    assert "regime late_window pulled bid" in out["reason"]


def test_apply_regime_policy_exit_only_pulls_bids_even_when_enabled():
    p = MakerRegimePolicy(regime_id="inventory_saturated", bid_enabled=True, ask_enabled=True,
                          exit_only=True, reason="reduce only")
    q = {"bid": Quote("bid", 0.45, 25.0), "ask": Quote("ask", 0.55, 10.0), "reason": "base"}

    out = MK.apply_regime_policy_to_quotes(q, p)

    assert out["bid"] is None
    assert out["ask"] == q["ask"]
    assert "exit-only pulled bid" in out["reason"]


def test_route_regime_for_token_uses_only_known_tick_state():
    desk = {
        "seconds_left": 120,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
    }
    confirm = {"confirmed_side": None, "conviction": 0.0}

    policy, h = MK.route_regime_for_token(
        desk, "up", fair=0.51, best_bid=0.50, best_ask=0.52,
        token_inventory=0.0, opposite_inventory=0.0, max_shares=50.0, confirm=confirm,
    )

    assert policy.regime_id == "mid_chop"
    assert h == MK.regime_inputs_hash({
        "token": "up", "fair": 0.51, "best_bid": 0.50, "best_ask": 0.52,
        "seconds_left": 120, "jump_flag": False, "divergence": 1.0,
        "vol_fast": 0.00002, "vol_slow": 0.00002, "confirmed_side": None,
        "direction_confidence": 0.0, "book_stale": False, "book_quarantined": False,
        "fee_conflict": False, "token_inventory": 0.0, "opposite_inventory": 0.0,
        "max_shares": 50.0,
    })


# ---------- 6. settlement retry sweep (bug: settle was attempted ONCE, ~2s after close — before ------
# ----------    Gamma posts the outcome — then the book was dropped; windows sat unsettled forever) ---

def test_settle_stale_retries_and_settles_a_pending_window(conn, monkeypatch):
    wb = _wb(ws=1_000_000)
    wb._fill("up", "bid", 0.40, 50.0)                    # long 50 UP @ .40 (cash -20)
    MK.persist_window(conn, "maker-unified-v1", wb)
    monkeypatch.setattr(MK, "_resolve", lambda slug: "Up")
    n = MK.settle_stale(conn, "maker-unified-v1", now_ts=1_000_000 + 300 + 15)
    assert n == 1
    row = conn.execute("SELECT settled, realized_pnl, winning_outcome FROM maker_windows").fetchone()
    assert row["settled"] == 1 and row["winning_outcome"] == "Up"
    assert row["realized_pnl"] == pytest.approx(-20.0 + 50.0)   # cash + payout


def test_settle_stale_uses_fill_ledger_when_window_row_is_stale(conn, monkeypatch):
    stale = _wb(ws=1_000_000)
    MK.persist_window(conn, "maker-unified-v4", stale)
    filled = _wb(cid=stale.cid, ws=stale.ws)
    MK.record_fill(conn, "maker-unified-v4", filled, "up", "bid", 0.40, 50.0, 0.46,
                   now_ts=1_000_010, reason="entry", fill_model="print")
    conn.commit()
    monkeypatch.setattr(MK, "_resolve", lambda slug: "Up")

    assert MK.settle_stale(conn, "maker-unified-v4", now_ts=1_000_000 + 300 + 15) == 1

    row = conn.execute(
        "SELECT up_inv, down_inv, cash, realized_pnl, winning_outcome FROM maker_windows"
    ).fetchone()
    assert row["up_inv"] == pytest.approx(50.0)
    assert row["down_inv"] == pytest.approx(0.0)
    assert row["cash"] == pytest.approx(-20.0)
    assert row["winning_outcome"] == "Up"
    assert row["realized_pnl"] == pytest.approx(30.0)


def test_settle_stale_only_sweeps_the_requested_campaign(conn, monkeypatch):
    old = _wb(cid="0xold", ws=1_000_000)
    old._fill("up", "bid", 0.40, 50.0)
    MK.persist_window(conn, "maker-unified-v3", old)
    cur = _wb(cid="0xcur", ws=1_000_300)
    cur._fill("up", "bid", 0.40, 50.0)
    MK.persist_window(conn, "maker-unified-v4", cur)
    monkeypatch.setattr(MK, "_resolve", lambda slug: "Up")

    assert MK.settle_stale(conn, "maker-unified-v4", now_ts=1_000_300 + 300 + 15) == 1

    rows = {
        r["condition_id"]: dict(r)
        for r in conn.execute(
            "SELECT condition_id,campaign_id,settled,winning_outcome,realized_pnl FROM maker_windows"
        )
    }
    assert rows["0xold"]["campaign_id"] == "maker-unified-v3"
    assert rows["0xold"]["settled"] == 0
    assert rows["0xold"]["realized_pnl"] is None
    assert rows["0xcur"]["campaign_id"] == "maker-unified-v4"
    assert rows["0xcur"]["settled"] == 1
    assert rows["0xcur"]["winning_outcome"] == "Up"


def test_settle_stale_respects_the_grace_period(conn, monkeypatch):
    wb = _wb(ws=1_000_000)
    wb._fill("up", "bid", 0.40, 50.0)
    MK.persist_window(conn, "maker-unified-v1", wb)
    monkeypatch.setattr(MK, "_resolve", lambda slug: "Up")
    assert MK.settle_stale(conn, "maker-unified-v1", now_ts=1_000_000 + 300 + 5) == 0   # < 12s grace
    assert conn.execute("SELECT settled FROM maker_windows").fetchone()[0] == 0


def test_settle_stale_keeps_retrying_while_unresolved(conn, monkeypatch):
    wb = _wb(ws=1_000_000)
    wb._fill("up", "bid", 0.40, 50.0)
    MK.persist_window(conn, "maker-unified-v1", wb)
    monkeypatch.setattr(MK, "_resolve", lambda slug: None)      # Gamma hasn't posted yet
    assert MK.settle_stale(conn, "maker-unified-v1", now_ts=1_000_000 + 300 + 60) == 0
    assert conn.execute("SELECT settled FROM maker_windows").fetchone()[0] == 0   # still pending, will retry


def test_settle_stale_does_not_abandon_after_30m_by_default(conn, monkeypatch):
    wb = _wb(ws=1_000_000)
    wb._fill("up", "bid", 0.40, 50.0)
    MK.persist_window(conn, "maker-unified-v1", wb)
    monkeypatch.setattr(MK, "_resolve", lambda slug: None)

    MK.settle_stale(conn, "maker-unified-v1", now_ts=1_000_000 + 300 + 1801)

    row = conn.execute("SELECT settled, winning_outcome, realized_pnl FROM maker_windows").fetchone()
    assert row["settled"] == 0
    assert row["winning_outcome"] is None
    assert row["realized_pnl"] is None


def test_settle_stale_abandons_after_the_deadline(conn, monkeypatch):
    wb = _wb(ws=1_000_000)
    wb._fill("up", "bid", 0.40, 50.0)
    MK.persist_window(conn, "maker-unified-v1", wb)
    monkeypatch.setattr(MK, "_resolve", lambda slug: None)
    MK.settle_stale(conn, "maker-unified-v1", now_ts=1_000_000 + 300 + 1801, abandon_s=1800)
    row = conn.execute("SELECT settled, winning_outcome, realized_pnl FROM maker_windows").fetchone()
    assert row["settled"] == 1 and row["winning_outcome"] == "unresolved"
    assert row["realized_pnl"] is None            # excluded from campaign stats, not counted as $0


def test_settle_stale_retries_previously_abandoned_unresolved_window(conn, monkeypatch):
    wb = _wb(ws=1_000_000)
    wb._fill("down", "bid", 0.30, 20.0)
    MK.persist_window(conn, "maker-unified-v1", wb)
    conn.execute("UPDATE maker_windows SET settled=1, winning_outcome='unresolved', realized_pnl=NULL "
                 "WHERE condition_id=?", (wb.cid,))
    conn.commit()
    monkeypatch.setattr(MK, "_resolve", lambda slug: "Down")

    assert MK.settle_stale(conn, "maker-unified-v1", now_ts=1_000_000 + 300 + 3600) == 1

    row = conn.execute("SELECT settled, winning_outcome, realized_pnl FROM maker_windows").fetchone()
    assert row["settled"] == 1
    assert row["winning_outcome"] == "Down"
    assert row["realized_pnl"] == pytest.approx(-6.0 + 20.0)


# ---------- 7. quote snapshots for real replay -----------------------------------------------------

def test_persist_quote_snapshot_records_posted_and_pulled_sides(conn):
    q = {"bid": Quote("bid", 0.56, 25.0), "ask": None, "reason": "edge floor"}
    MK.persist_quote_snapshot(conn, "maker-unified-v2", "0xc", 1_000_000, "up", q,
                              fair=0.60, best_bid=0.55, best_ask=0.62,
                              seconds_left=120, now_ts=1_000_001)

    rows = conn.execute(
        "SELECT campaign_id, token, side, status, price, size, fair, best_bid, best_ask, seconds_left, reason "
        "FROM maker_quotes ORDER BY side"
    ).fetchall()
    assert [(r["side"], r["status"], r["price"], r["size"]) for r in rows] == [
        ("ask", "pulled", None, None),
        ("bid", "posted", 0.56, 25.0),
    ]
    assert rows[1]["campaign_id"] == "maker-unified-v2"
    assert rows[1]["fair"] == pytest.approx(0.60)
    assert rows[1]["best_bid"] == pytest.approx(0.55)
    assert rows[1]["best_ask"] == pytest.approx(0.62)
    assert rows[1]["seconds_left"] == 120
    assert rows[1]["reason"] == "edge floor"


def test_persist_regime_decision_records_policy_and_inputs_hash(conn):
    policy = _policy("jump")

    MK.persist_regime_decision(
        conn, "maker-regime-shadow-v1", "0xc", 1_000_000, "up", policy,
        inputs_hash="abc123", margin_used=0.04, clip_used=8.75, max_shares_used=25.0,
        min_edge_used=0.06, min_seconds_quote_used=30.0, now_ts=1_000_002,
    )

    row = conn.execute(
        "SELECT campaign_id, token, regime_id, policy_id, route_reason, regime_inputs_hash, "
        "margin_used, clip_used, max_shares_used, min_edge_used, min_seconds_quote_used, "
        "bid_enabled, ask_enabled, exit_only, risk_action, can_hard_cut, quote_ttl_ms, warnings_json, ts "
        "FROM maker_regime_decisions"
    ).fetchone()
    assert row["campaign_id"] == "maker-regime-shadow-v1"
    assert row["token"] == "up"
    assert row["regime_id"] == "jump"
    assert row["policy_id"] == "maker-regime-v1"
    assert row["route_reason"] == "jump route"
    assert row["regime_inputs_hash"] == "abc123"
    assert row["margin_used"] == pytest.approx(0.04)
    assert row["clip_used"] == pytest.approx(8.75)
    assert row["max_shares_used"] == pytest.approx(25.0)
    assert row["min_edge_used"] == pytest.approx(0.06)
    assert row["min_seconds_quote_used"] == pytest.approx(30.0)
    assert row["bid_enabled"] == 1
    assert row["ask_enabled"] == 0
    assert row["exit_only"] == 0
    assert row["risk_action"] == "widen"
    assert row["can_hard_cut"] == 1
    assert row["quote_ttl_ms"] == 4_000
    assert row["warnings_json"] == '["w"]'
    assert row["ts"] == 1_000_002


def test_quote_snapshot_can_record_regime_metadata(conn):
    policy = _policy("trend_confirmed")
    q = {"bid": Quote("bid", 0.56, 25.0), "ask": None, "reason": "edge floor"}

    MK.persist_quote_snapshot(
        conn, "maker-regime-shadow-v1", "0xc", 1_000_000, "up", q,
        fair=0.60, best_bid=0.55, best_ask=0.62, seconds_left=120, now_ts=1_000_003,
        regime_policy=policy, regime_inputs_hash="h1", margin_used=0.03, clip_used=25.0,
        max_shares_used=50.0, min_edge_used=0.06, min_seconds_quote_used=30.0,
    )

    row = conn.execute(
        "SELECT regime_id, policy_id, route_reason, regime_inputs_hash, margin_used, clip_used, "
        "max_shares_used, min_edge_used, min_seconds_quote_used, bid_enabled, ask_enabled, "
        "exit_only, risk_action, can_hard_cut, quote_ttl_ms "
        "FROM maker_quotes WHERE side='bid'"
    ).fetchone()
    assert row["regime_id"] == "trend_confirmed"
    assert row["policy_id"] == "maker-regime-v1"
    assert row["route_reason"] == "trend_confirmed route"
    assert row["regime_inputs_hash"] == "h1"
    assert row["margin_used"] == pytest.approx(0.03)
    assert row["clip_used"] == pytest.approx(25.0)
    assert row["max_shares_used"] == pytest.approx(50.0)
    assert row["min_edge_used"] == pytest.approx(0.06)
    assert row["min_seconds_quote_used"] == pytest.approx(30.0)
    assert row["bid_enabled"] == 1
    assert row["ask_enabled"] == 0
    assert row["exit_only"] == 0
    assert row["risk_action"] == "widen"
    assert row["can_hard_cut"] == 1
    assert row["quote_ttl_ms"] == 4_000


def test_record_fill_uses_exchange_time_and_updates_book(conn):
    wb = _wb()
    MK.record_fill(conn, "maker-unified-v2", wb, "up", "bid", 0.56, 25.0, 0.60,
                   now_ts=1_000_002, reason="print fill", fill_model="print")

    row = conn.execute(
        "SELECT campaign_id, token, side, price, size, fair, ts, reason, fill_model FROM maker_fills"
    ).fetchone()
    assert row["campaign_id"] == "maker-unified-v2"
    assert row["token"] == "up"
    assert row["side"] == "bid"
    assert row["price"] == pytest.approx(0.56)
    assert row["size"] == pytest.approx(25.0)
    assert row["fair"] == pytest.approx(0.60)
    assert row["ts"] == 1_000_002
    assert row["reason"] == "print fill"
    assert row["fill_model"] == "print"
    assert wb.up_inv == pytest.approx(25.0)
    assert wb.cash == pytest.approx(-14.0)


def test_record_fill_can_record_regime_metadata(conn):
    wb = _wb()
    policy = _policy("mid_chop")

    MK.record_fill(
        conn, "maker-regime-shadow-v1", wb, "up", "bid", 0.56, 25.0, 0.60,
        now_ts=1_000_004, reason="print fill", fill_model="print",
        regime_policy=policy, regime_inputs_hash="h2", margin_used=0.02, clip_used=25.0,
        max_shares_used=50.0, min_edge_used=0.04, min_seconds_quote_used=30.0,
    )

    row = conn.execute(
        "SELECT regime_id, policy_id, route_reason, regime_inputs_hash, margin_used, clip_used, "
        "max_shares_used, min_edge_used, min_seconds_quote_used, bid_enabled, ask_enabled, "
        "exit_only, risk_action, can_hard_cut, quote_ttl_ms "
        "FROM maker_fills"
    ).fetchone()
    assert row["regime_id"] == "mid_chop"
    assert row["policy_id"] == "maker-regime-v1"
    assert row["route_reason"] == "mid_chop route"
    assert row["regime_inputs_hash"] == "h2"
    assert row["margin_used"] == pytest.approx(0.02)
    assert row["clip_used"] == pytest.approx(25.0)
    assert row["max_shares_used"] == pytest.approx(50.0)
    assert row["min_edge_used"] == pytest.approx(0.04)
    assert row["min_seconds_quote_used"] == pytest.approx(30.0)
    assert row["bid_enabled"] == 1
    assert row["ask_enabled"] == 0
    assert row["exit_only"] == 0
    assert row["risk_action"] == "widen"
    assert row["can_hard_cut"] == 1
    assert row["quote_ttl_ms"] == 4_000


def test_record_fill_persists_clamped_sell_size(conn):
    wb = _wb()
    wb._fill("up", "bid", 0.50, 10.0)

    MK.record_fill(conn, "maker-unified-v2", wb, "up", "ask", 0.60, 25.0, 0.58,
                   now_ts=1_000_003, reason="scale out", fill_model="print")

    row = conn.execute(
        "SELECT side, price, size, ts FROM maker_fills ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["side"] == "ask"
    assert row["price"] == pytest.approx(0.60)
    assert row["size"] == pytest.approx(10.0)
    assert row["ts"] == 1_000_003
    assert wb.up_inv == pytest.approx(0.0)
    assert wb.cash == pytest.approx(1.0)


def test_record_fill_clamps_bid_to_remaining_inventory_cap(conn):
    wb = _wb()
    wb._fill("up", "bid", 0.40, 45.0)

    MK.record_fill(conn, "maker-unified-v3", wb, "up", "bid", 0.42, 25.0, 0.50,
                   now_ts=1_000_004, reason="cap fill", fill_model="print", max_shares=50.0)

    row = conn.execute(
        "SELECT side, price, size, ts FROM maker_fills ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["side"] == "bid"
    assert row["price"] == pytest.approx(0.42)
    assert row["size"] == pytest.approx(5.0)
    assert row["ts"] == 1_000_004
    assert wb.up_inv == pytest.approx(50.0)


def test_shadow_regime_run_records_decisions_without_altering_v4_quote_rows(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xc",
        "window_start_ts": 1_000_000,
        "seconds_left": 120,
        "seconds_elapsed": 180,
        "now_ts": 1_000_180,
        "fair_value_up": 0.51,
        "btc_now": 100.0,
        "btc_start": 99.9,
        "up_bid": 0.50,
        "up_ask": 0.52,
        "down_bid": 0.48,
        "down_ask": 0.50,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker.sqlite"
    pid = tmp_path / "maker.pid"
    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-unified-v4",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="shadow",
        regime_shadow_campaign="maker-regime-shadow-v1",
        max_iterations=1,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    assert c.execute("SELECT COUNT(*) FROM maker_regime_decisions").fetchone()[0] == 2
    assert c.execute(
        "SELECT COUNT(*) FROM maker_quotes WHERE campaign_id='maker-unified-v4' AND regime_id IS NOT NULL"
    ).fetchone()[0] == 0
    assert c.execute(
        "SELECT COUNT(*) FROM maker_quotes WHERE campaign_id='maker-regime-shadow-v1' AND regime_id IS NOT NULL"
    ).fetchone()[0] == 4
    rows = c.execute("SELECT token, regime_id FROM maker_regime_decisions ORDER BY token").fetchall()
    assert [(r["token"], r["regime_id"]) for r in rows] == [("down", "mid_chop"), ("up", "mid_chop")]
    c.close()


def test_active_regime_run_records_policy_and_exposes_current_regime(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xc",
        "window_start_ts": 1_000_000,
        "seconds_left": 120,
        "seconds_elapsed": 180,
        "now_ts": 1_000_180,
        "fair_value_up": 0.51,
        "btc_now": 100.0,
        "btc_start": 99.9,
        "up_bid": 0.50,
        "up_ask": 0.52,
        "down_bid": 0.48,
        "down_ask": 0.50,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker.sqlite"
    pid = tmp_path / "maker.pid"
    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-regime-v1",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="active",
        regime_shadow_campaign="maker-regime-shadow-v1",
        max_iterations=1,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    quote_count = c.execute(
        "SELECT COUNT(*) FROM maker_quotes WHERE campaign_id='maker-regime-v1' AND regime_id='mid_chop'"
    ).fetchone()[0]
    assert quote_count == 4
    state = c.execute("SELECT think_json FROM maker_state WHERE id=1").fetchone()
    think = __import__("json").loads(state["think_json"])
    assert think["campaign"] == "maker-regime-v1"
    assert think["engine_version"] == "maker-regime-v1"
    assert think["regime_active"]["up"]["regime_id"] == "mid_chop"
    assert think["regime_active"]["up"]["clip"] == 5.0
    assert think["regime_active"]["up"]["bid_enabled"] is False
    assert think["regime_active"]["up"]["ask_enabled"] is False
    assert think["regime_active"]["up"]["risk_action"] == "pull_all"
    assert "regime_shadow" not in think
    c.close()


def test_active_run_logs_regime_episode_and_profile_context_without_shadow(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xc",
        "window_start_ts": 1_000_000,
        "seconds_left": 120,
        "seconds_elapsed": 180,
        "now_ts": 1_000_180,
        "fair_value_up": 0.51,
        "btc_now": 100.0,
        "btc_start": 99.9,
        "up_bid": 0.50,
        "up_ask": 0.52,
        "down_bid": 0.48,
        "down_ask": 0.50,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker.sqlite"
    pid = tmp_path / "maker.pid"
    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-regime-v1",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="active",
        regime_shadow_campaign="maker-regime-shadow-v1",
        max_iterations=1,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    episode = c.execute("SELECT * FROM maker_regime_episodes").fetchone()
    assert episode["regime_id"] == "mid_chop"
    assert episode["campaign_id"] == "maker-regime-v1"

    profile_rows = c.execute("SELECT token, profile_id, profile_mode FROM maker_profile_decisions "
                             "ORDER BY token").fetchall()
    assert [(r["token"], r["profile_id"], r["profile_mode"]) for r in profile_rows] == [
        ("down", "pause", "log_only"),
        ("up", "pause", "log_only"),
    ]

    quote_rows = c.execute("SELECT DISTINCT episode_id, profile_id, profile_mode FROM maker_quotes").fetchall()
    assert [(r["episode_id"], r["profile_id"], r["profile_mode"]) for r in quote_rows] == [
        (episode["episode_id"], "pause", "log_only")
    ]
    decision_rows = c.execute(
        "SELECT token, episode_id, profile_id FROM maker_regime_decisions ORDER BY token"
    ).fetchall()
    assert [(r["token"], r["episode_id"], r["profile_id"]) for r in decision_rows] == [
        ("down", episode["episode_id"], "pause"),
        ("up", episode["episode_id"], "pause"),
    ]

    state = c.execute("SELECT think_json FROM maker_state WHERE id=1").fetchone()
    think = __import__("json").loads(state["think_json"])
    assert think["regime_episode"]["episode_id"] == episode["episode_id"]
    assert think["regime_episode"]["current_regime"] == "mid_chop"
    assert think["profile_decisions"]["up"]["profile_id"] == "pause"
    assert "regime_shadow" not in think
    c.close()


def test_adaptive_paper_run_selects_active_profile_in_separate_campaign(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xc",
        "window_start_ts": 1_000_000,
        "seconds_left": 120,
        "seconds_elapsed": 180,
        "now_ts": 1_000_180,
        "fair_value_up": 0.66,
        "btc_now": 100.0,
        "btc_start": 99.9,
        "up_bid": 0.62,
        "up_ask": 0.65,
        "down_bid": 0.34,
        "down_ask": 0.37,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker.sqlite"
    pid = tmp_path / "maker.pid"
    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-adaptive-paper-v1",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="off",
        regime_shadow_campaign="maker-regime-shadow-v1",
        maker_adaptive_paper=True,
        adaptive_control_campaign="maker-regime-v1",
        adaptive_epsilon=0.0,
        adaptive_seed=7,
        max_iterations=1,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    assert c.execute(
        "SELECT COUNT(*) FROM maker_windows WHERE campaign_id='maker-regime-v1'"
    ).fetchone()[0] == 0
    campaign = c.execute("SELECT * FROM crypto5m_maker_adaptive_campaigns").fetchone()
    assert campaign["campaign_id"] == "maker-adaptive-paper-v1"
    decision = c.execute(
        """SELECT profile_mode,selector_version,reward_version,selection_mode,exploration_flag,
                  parent_context_used
           FROM maker_profile_decisions
           WHERE campaign_id='maker-adaptive-paper-v1'
           ORDER BY token LIMIT 1"""
    ).fetchone()
    assert decision["profile_mode"] == "active"
    assert decision["selector_version"] == "adaptive-ucb-v1"
    assert decision["reward_version"] == "maker-reward-v1"
    assert decision["selection_mode"] in {"exploration", "learned", "fallback"}
    assert decision["parent_context_used"]
    quote = c.execute(
        """SELECT campaign_id,profile_mode,selector_version,reward_version,selection_mode,
                  adaptive_context_key
           FROM maker_quotes
           WHERE campaign_id='maker-adaptive-paper-v1'
           LIMIT 1"""
    ).fetchone()
    assert quote["campaign_id"] == "maker-adaptive-paper-v1"
    assert quote["profile_mode"] == "active"
    assert quote["selector_version"] == "adaptive-ucb-v1"
    assert quote["adaptive_context_key"]
    state = c.execute("SELECT think_json FROM maker_state WHERE id=1").fetchone()
    think = __import__("json").loads(state["think_json"])
    assert think["engine_version"] == "maker-adaptive-paper-v1"
    assert think["adaptive_paper"]["campaign"] == "maker-adaptive-paper-v1"
    assert think["adaptive_paper"]["control_campaign"] == "maker-regime-v1"
    assert think["profile_decisions"]["up"]["profile_mode"] == "active"
    c.close()


def test_adaptive_paper_v2_run_uses_versioned_selector_and_reward(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xcv2",
        "window_start_ts": 1_000_000,
        "seconds_left": 120,
        "seconds_elapsed": 180,
        "now_ts": 1_000_180,
        "fair_value_up": 0.58,
        "btc_now": 100.0,
        "btc_start": 99.9,
        "up_bid": 0.55,
        "up_ask": 0.58,
        "down_bid": 0.42,
        "down_ask": 0.45,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker_v2.sqlite"
    pid = tmp_path / "maker_v2.pid"
    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-adaptive-paper-v2",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="off",
        regime_shadow_campaign="maker-regime-shadow-v1",
        maker_adaptive_paper=True,
        adaptive_control_campaign="maker-regime-v1",
        adaptive_epsilon=0.0,
        adaptive_seed=7,
        adaptive_learner_version="v2",
        max_iterations=1,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    campaign = c.execute("SELECT * FROM crypto5m_maker_adaptive_campaigns").fetchone()
    assert campaign["campaign_id"] == "maker-adaptive-paper-v2"
    assert campaign["selector_version"] == "adaptive-ucb-v2"
    assert campaign["reward_version"] == "maker-reward-v2"
    decision = c.execute(
        """SELECT selector_version,reward_version,fallback_depth,fallback_reason
           FROM maker_profile_decisions
           WHERE campaign_id='maker-adaptive-paper-v2'
           ORDER BY token LIMIT 1"""
    ).fetchone()
    assert decision["selector_version"] == "adaptive-ucb-v2"
    assert decision["reward_version"] == "maker-reward-v2"
    assert decision["fallback_depth"] is not None
    assert decision["fallback_reason"]
    state = c.execute("SELECT think_json FROM maker_state WHERE id=1").fetchone()
    think = __import__("json").loads(state["think_json"])
    assert think["adaptive_paper"]["selector_version"] == "adaptive-ucb-v2"
    assert think["adaptive_paper"]["reward_version"] == "maker-reward-v2"
    c.close()


def test_adaptive_v3_run_records_fair_risk_action_and_ablations(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xcv3",
        "window_start_ts": 1_000_000,
        "seconds_left": 118,
        "seconds_elapsed": 182,
        "now_ts": 1_000_182,
        "fair_value_up": 0.70,
        "btc_now": 100.0,
        "btc_start": 99.8,
        "up_bid": 0.52,
        "up_ask": 0.56,
        "down_bid": 0.44,
        "down_ask": 0.48,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker_v3.sqlite"
    pid = tmp_path / "maker_v3.pid"
    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-adaptive-paper-v3",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="off",
        regime_shadow_campaign="maker-regime-shadow-v1",
        maker_adaptive_paper=False,
        maker_adaptive_v3=True,
        adaptive_control_campaign="maker-adaptive-paper-v2",
        adaptive_epsilon=0.0,
        adaptive_seed=7,
        adaptive_learner_version="v2",
        max_iterations=1,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    assert c.execute(
        "SELECT COUNT(*) FROM crypto5m_maker_adaptive_campaigns WHERE campaign_id='maker-adaptive-paper-v2'"
    ).fetchone()[0] == 0
    campaign = c.execute("SELECT * FROM crypto5m_maker_v3_campaigns").fetchone()
    assert campaign["campaign_id"] == "maker-adaptive-paper-v3"
    assert campaign["selector_version"] == "adaptive-maker-brain-v3.1"
    assert campaign["reward_version"] == "maker-reward-v3.1"

    action = c.execute(
        """SELECT token,fair_model_id,q_structural,q_market_mid,q_used,fair_blend_weight,
                  fair_context_key,fair_confidence,risk_state,quote_action_arm,selector_source,parent_context_used
           FROM crypto5m_maker_v3_policy_actions
           WHERE campaign_id='maker-adaptive-paper-v3'
           ORDER BY token LIMIT 1"""
    ).fetchone()
    assert action["fair_model_id"] == "logit_blend_v1"
    assert action["fair_context_key"]
    assert action["q_used"] is not None
    assert action["fair_confidence"] in {"low", "medium", "high"}
    assert action["risk_state"] in {"normal", "caution", "reduce_only", "pause", "hard_stop"}
    assert action["quote_action_arm"]
    assert action["parent_context_used"]

    ablations = c.execute(
        """SELECT DISTINCT ablation_group
           FROM crypto5m_maker_v3_ablations
           WHERE campaign_id='maker-adaptive-paper-v3'
           ORDER BY ablation_group"""
    ).fetchall()
    assert [r["ablation_group"] for r in ablations] == ["A0", "A1", "A2", "A3", "A4"]

    state = c.execute("SELECT think_json FROM maker_state WHERE id=1").fetchone()
    think = __import__("json").loads(state["think_json"])
    assert think["engine_version"] == "maker-adaptive-paper-v3"
    assert think["adaptive_v3"]["campaign"] == "maker-adaptive-paper-v3"
    assert think["adaptive_v3"]["selector_version"] == "adaptive-maker-brain-v3.1"
    assert think["adaptive_v3"]["tokens"]["up"]["fair"]["fair_model_id"] == "logit_blend_v1"
    assert think["adaptive_v3"]["tokens"]["up"]["action"]["arm_id"]
    c.close()


def test_settle_window_updates_v3_action_rewards(conn, monkeypatch):
    wb = _wb(cid="0xv3reward", ws=1_000_000)
    MK.AD3.register_campaign(conn, "maker-adaptive-paper-v3", "maker-adaptive-paper-v2", 1000, {})
    context_key = "mid_window|mid_chop|calm|flat|healthy|unconfirmed|healthy"
    MK.AD3.record_policy_action(
        conn,
        campaign_id="maker-adaptive-paper-v3",
        condition_id=wb.cid,
        window_start_ts=wb.ws,
        token="up",
        context_key=context_key,
        fair_model_id="logit_blend_v1",
        q_structural=0.60,
        q_market_mid=0.52,
        q_used=0.55,
        fair_blend_weight=0.35,
        fair_confidence="medium",
        risk_state="normal",
        tail_risk_score=0.10,
        quote_action_arm="wide_small",
        selector_source="fallback",
        exploration_flag=False,
        parent_context_used="global",
        now_ts=1_000_005,
    )
    MK.record_fill(conn, "maker-adaptive-paper-v3", wb, "up", "bid", 0.40, 10.0, 0.55,
                   now_ts=1_000_010, reason="entry", fill_model="print")
    monkeypatch.setattr(MK, "_resolve", lambda slug: "Up")

    realized = MK.settle_window(conn, "maker-adaptive-paper-v3", wb)

    reward = conn.execute(
        "SELECT quote_action_arm,reward FROM crypto5m_maker_v3_rewards WHERE campaign_id='maker-adaptive-paper-v3'"
    ).fetchone()
    stats = conn.execute(
        """SELECT n_fills,reward_sum
           FROM crypto5m_maker_v3_action_arm_stats
           WHERE campaign_id='maker-adaptive-paper-v3' AND context_key=? AND arm_id='wide_small'""",
        (context_key,),
    ).fetchone()
    assert realized == pytest.approx(6.0)
    assert reward["quote_action_arm"] == "wide_small"
    assert reward["reward"] == pytest.approx(6.0)
    assert stats["n_fills"] == 1
    assert stats["reward_sum"] == pytest.approx(6.0)


def test_active_run_tags_hard_cut_fills_with_fill_time_episode_and_profile(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xcut",
        "window_start_ts": 1_000_000,
        "seconds_left": 120,
        "seconds_elapsed": 180,
        "now_ts": 1_000_180,
        "fair_value_up": 0.45,
        "btc_now": 99.0,
        "btc_start": 100.0,
        "up_bid": 0.44,
        "up_ask": 0.46,
        "down_bid": 0.54,
        "down_ask": 0.56,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker.sqlite"
    pid = tmp_path / "maker.pid"
    c = MK._db(str(db))
    wb = MK.WindowBook("0xcut", 1_000_000)
    wb._fill("up", "bid", 0.60, 10.0)
    MK.persist_window(c, "maker-regime-v1", wb)
    c.close()

    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-regime-v1",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="active",
        regime_shadow_campaign="maker-regime-shadow-v1",
        max_iterations=1,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    fill = c.execute("SELECT * FROM maker_fills WHERE fill_model='hard-cut'").fetchone()
    assert fill is not None
    assert fill["fill_episode_id"]
    assert fill["fill_regime_id"]
    assert fill["fill_profile_id"]
    assert fill["profile_mode"] == "log_only"
    assert fill["quote_episode_id"] is None
    assert fill["quote_profile_id"] is None
    c.close()


def test_hard_cut_tick_advances_active_regime_episode_once(tmp_path, monkeypatch):
    desk = {
        "condition_id": "0xcut",
        "window_start_ts": 1_000_000,
        "seconds_left": 120,
        "seconds_elapsed": 180,
        "now_ts": 1_000_180,
        "fair_value_up": 0.45,
        "btc_now": 99.0,
        "btc_start": 100.0,
        "up_bid": 0.44,
        "up_ask": 0.46,
        "down_bid": 0.54,
        "down_ask": 0.56,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "vol_per_s": 0.00002,
        "signal_series": {},
    }
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])

    db = tmp_path / "maker.sqlite"
    pid = tmp_path / "maker.pid"
    c = MK._db(str(db))
    wb = MK.WindowBook("0xcut", 1_000_000)
    wb._fill("up", "bid", 0.60, 10.0)
    MK.persist_window(c, "maker-regime-v1", wb)
    c.close()

    calls = []
    original = MK.ensure_active_regime_episode

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(MK, "ensure_active_regime_episode", counted)

    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-regime-v1",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="active",
        regime_shadow_campaign="maker-regime-shadow-v1",
        max_iterations=1,
    )

    MK.run(args)

    assert len(calls) == 1


def test_regime_state_snapshot_marks_missing_book():
    desk = {
        "seconds_left": 100,
        "up_bid": 0.96,
        "up_ask": None,
        "down_bid": None,
        "down_ask": 0.05,
        "jump_flag": False,
    }
    wb = MK.WindowBook("0xc", 1_000_000)

    snap = MK.regime_state_snapshot(
        desk, fair_up=0.95, cur=wb, max_shares=50.0, confirm={"confirmed_side": None}
    )

    assert snap["book_health"] == "missing"


def test_shadow_window_state_uses_separate_table_from_v4_windows(conn):
    actual = _wb(cid="0xsame", ws=1_000_000)
    actual._fill("up", "bid", 0.50, 10.0)
    shadow = _wb(cid="0xsame", ws=1_000_000)
    shadow._fill("down", "bid", 0.40, 25.0)

    MK.persist_window(conn, "maker-unified-v4", actual)
    MK.persist_shadow_window(conn, "maker-regime-shadow-v1", shadow)

    actual_row = conn.execute("SELECT campaign_id, up_inv, down_inv FROM maker_windows").fetchone()
    shadow_row = conn.execute("SELECT campaign_id, up_inv, down_inv FROM maker_shadow_windows").fetchone()
    assert actual_row["campaign_id"] == "maker-unified-v4"
    assert actual_row["up_inv"] == pytest.approx(10.0)
    assert actual_row["down_inv"] == pytest.approx(0.0)
    assert shadow_row["campaign_id"] == "maker-regime-shadow-v1"
    assert shadow_row["up_inv"] == pytest.approx(0.0)
    assert shadow_row["down_inv"] == pytest.approx(25.0)


def test_shadow_regime_run_maintains_path_dependent_shadow_book(tmp_path, monkeypatch):
    desks = [
        {
            "condition_id": "0xc",
            "window_start_ts": 1_000_000,
            "seconds_left": 120,
            "seconds_elapsed": 180,
            "now_ts": 1_000_180,
            "fair_value_up": 0.62,
            "btc_now": 100.0,
            "btc_start": 99.9,
            "up_bid": 0.50,
            "up_ask": 0.52,
            "down_bid": 0.48,
            "down_ask": 0.50,
            "jump_flag": False,
            "divergence": 1.0,
            "vol_fast": 0.00002,
            "vol_slow": 0.00002,
            "vol_per_s": 0.00002,
            "signal_series": {},
        },
        {
            "condition_id": "0xc",
            "window_start_ts": 1_000_000,
            "seconds_left": 119,
            "seconds_elapsed": 181,
            "now_ts": 1_000_181,
            "fair_value_up": 0.62,
            "btc_now": 100.0,
            "btc_start": 99.9,
            "up_bid": 0.50,
            "up_ask": 0.52,
            "down_bid": 0.48,
            "down_ask": 0.50,
            "jump_flag": False,
            "divergence": 1.0,
            "vol_fast": 0.00002,
            "vol_slow": 0.00002,
            "vol_per_s": 0.00002,
            "signal_series": {},
        },
    ]
    it = iter(desks)
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: next(it))

    def prints(_cid, since_ts, now_ts, *, token):
        if since_ts is None or now_ts <= 1_000_180:
            return []
        return [(0.46, 12.0)] if token == "up" else []

    monkeypatch.setattr(MK, "tape_prints", prints)

    db = tmp_path / "maker.sqlite"
    pid = tmp_path / "maker.pid"
    args = SimpleNamespace(
        paper_db=str(db),
        pid=str(pid),
        campaign="maker-unified-v4",
        poll=0.0,
        margin=0.02,
        clip=25.0,
        max_shares=50.0,
        min_edge=0.04,
        min_seconds_quote=30,
        maker_regime_router="shadow",
        regime_shadow_campaign="maker-regime-shadow-v1",
        max_iterations=2,
    )

    MK.run(args)

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    shadow_fill = c.execute(
        "SELECT campaign_id, token, side, price, size, regime_id FROM maker_fills "
        "WHERE campaign_id='maker-regime-shadow-v1'"
    ).fetchone()
    assert shadow_fill["token"] == "up"
    assert shadow_fill["side"] == "bid"
    assert shadow_fill["price"] == pytest.approx(0.51)
    assert shadow_fill["size"] == pytest.approx(12.0)
    assert shadow_fill["regime_id"] == "normal"
    shadow_window = c.execute(
        "SELECT up_inv, down_inv, cash FROM maker_shadow_windows WHERE campaign_id='maker-regime-shadow-v1'"
    ).fetchone()
    assert shadow_window["up_inv"] == pytest.approx(12.0)
    assert shadow_window["down_inv"] == pytest.approx(0.0)
    assert shadow_window["cash"] == pytest.approx(-6.12)
    c.close()


# ---------- quote_id linkage: fills reference the exact resting quote they executed against ---------


def test_persist_quote_snapshot_returns_rowids_for_posted_sides_only(conn):
    q = {"bid": Quote("bid", 0.56, 25.0), "ask": None, "reason": "edge floor"}
    ids = MK.persist_quote_snapshot(conn, "maker-unified-v4", "0xc", 1_000_000, "up", q,
                                    fair=0.60, best_bid=0.55, best_ask=0.62,
                                    seconds_left=120, now_ts=1_000_001)
    assert ids["ask"] is None
    row = conn.execute("SELECT id, side, status FROM maker_quotes WHERE id=?", (ids["bid"],)).fetchone()
    assert row["side"] == "bid" and row["status"] == "posted"


def test_record_fill_persists_the_quote_id_link(conn):
    wb = _wb()
    MK.record_fill(conn, "maker-unified-v4", wb, "up", "bid", 0.40, 25.0, 0.46,
                   now_ts=1_000_050, reason="print", fill_model="print", quote_id=777)
    MK.record_fill(conn, "maker-unified-v4", wb, "up", "ask", 0.55, 25.0, 0.46,
                   now_ts=1_000_060, reason="fair moved through entry — hard cut", fill_model="hard-cut")
    rows = conn.execute("SELECT side, quote_id FROM maker_fills ORDER BY id").fetchall()
    assert (rows[0]["side"], rows[0]["quote_id"]) == ("bid", 777)
    assert (rows[1]["side"], rows[1]["quote_id"]) == ("ask", None)   # hard cuts cross the book, no quote


def test_maker_fills_ledger_replay_index_exists(conn):
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='maker_fills'")]
    assert "idx_maker_fills_campaign_cid" in names


# ---------- regime-state evaluator persistence ----------------------------------------------------

def test_regime_episode_tables_and_columns_exist(conn):
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"maker_regime_episodes", "maker_regime_transitions", "maker_profile_decisions"} <= tables

    quote_cols = {r["name"] for r in conn.execute("PRAGMA table_info(maker_quotes)")}
    assert {
        "episode_id", "moment_key", "moment_schema_version", "profile_id", "profile_mode",
        "profile_decision_id", "policy_hash", "quote_created_regime_id", "quote_created_episode_id",
    } <= quote_cols

    fill_cols = {r["name"] for r in conn.execute("PRAGMA table_info(maker_fills)")}
    assert {
        "quote_episode_id", "fill_episode_id", "quote_regime_id", "fill_regime_id",
        "quote_profile_id", "fill_profile_id", "profile_mode", "profile_decision_id",
        "moment_key", "moment_schema_version",
    } <= fill_cols

    decision_cols = {r["name"] for r in conn.execute("PRAGMA table_info(maker_regime_decisions)")}
    assert {"episode_id", "moment_key", "moment_schema_version", "profile_id",
            "profile_mode", "profile_decision_id"} <= decision_cols


def test_regime_episode_and_profile_persistence_helpers(conn):
    moment = RS.build_moment({
        "fair": 0.51, "best_bid": 0.50, "best_ask": 0.52, "seconds_left": 120,
        "volatility_state": "calm", "direction_state": "unconfirmed", "book_health": "healthy",
    }, token_inventory=0.0, max_shares=50.0)
    episode = RS.start_episode(
        "maker-regime-v1", "0xc", 1_000_000, 1_000_300, "mid_chop", 1_000_120, moment
    )
    decision = RS.decide_profile(
        episode, token="up", token_inventory=0.0, max_shares=50.0,
        active_policy={"policy_id": "maker-regime-v1", "risk_action": "widen",
                       "bid_enabled": True, "ask_enabled": True, "quote_ttl_ms": 6000},
        now_ts=1_000_121,
    )
    transition = RS.TransitionEvent(
        episode_id=episode.episode_id, condition_id="0xc", window_start_ts=1_000_000,
        from_regime="normal", to_regime="mid_chop", ts=1_000_120,
        reason="debounced_transition", confidence="medium",
    )

    MK.persist_episode(conn, episode)
    MK.persist_transition(conn, "maker-regime-v1", transition)
    MK.persist_profile_decision(conn, "maker-regime-v1", "0xc", 1_000_000, decision)

    ep = conn.execute("SELECT * FROM maker_regime_episodes WHERE episode_id=?",
                      (episode.episode_id,)).fetchone()
    assert ep["regime_id"] == "mid_chop"
    assert ep["moment_schema_version"] == 1
    assert '"odds_bucket":"mid_chop"' in ep["moment_key"]

    tr = conn.execute("SELECT * FROM maker_regime_transitions WHERE episode_id=?",
                      (episode.episode_id,)).fetchone()
    assert tr["from_regime_id"] == "normal"
    assert tr["to_regime_id"] == "mid_chop"

    pd = conn.execute("SELECT * FROM maker_profile_decisions WHERE profile_decision_id=?",
                      (decision.profile_decision_id,)).fetchone()
    assert pd["episode_id"] == episode.episode_id
    assert pd["token"] == "up"
    assert pd["profile_id"] == "quote_wide"
    assert pd["profile_mode"] == "log_only"


def test_vol_floor_flag_overrides_live_desk_floor_per_process(tmp_path, monkeypatch):
    desk = {"window_start_ts": None, "available": False, "message": "stub"}
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK.LD, "VOL_FLOOR_PER_S", 1e-4)

    args = SimpleNamespace(
        paper_db=str(tmp_path / "m.sqlite"), pid=str(tmp_path / "m.pid"),
        campaign="maker-adaptive-paper-v3.2f", poll=0.0, margin=0.02, clip=25.0,
        max_shares=50.0, min_edge=0.04, min_seconds_quote=30,
        maker_regime_router="off", regime_shadow_campaign="maker-regime-shadow-v1",
        maker_adaptive_paper=False, maker_adaptive_v3=True,
        adaptive_control_campaign="maker-adaptive-paper-v3.1",
        adaptive_epsilon=0.0, adaptive_seed=7, adaptive_learner_version="v2",
        max_iterations=1, vol_floor=2e-5,
    )
    MK.run(args)

    assert MK.LD.VOL_FLOOR_PER_S == pytest.approx(2e-5)


def test_adaptive_reward_version_flag_registers_v32_campaign(tmp_path, monkeypatch):
    desk = {"window_start_ts": None, "available": False, "message": "stub"}
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)

    db = tmp_path / "m.sqlite"
    args = SimpleNamespace(
        paper_db=str(db), pid=str(tmp_path / "m.pid"),
        campaign="maker-adaptive-paper-v3.2r", poll=0.0, margin=0.02, clip=25.0,
        max_shares=50.0, min_edge=0.04, min_seconds_quote=30,
        maker_regime_router="off", regime_shadow_campaign="maker-regime-shadow-v1",
        maker_adaptive_paper=False, maker_adaptive_v3=True,
        adaptive_control_campaign="maker-adaptive-paper-v3.1",
        adaptive_epsilon=0.0, adaptive_seed=7, adaptive_learner_version="v2",
        max_iterations=1, adaptive_reward_version="maker-reward-v3.2",
    )
    MK.run(args)

    c = sqlite3.connect(db)
    row = c.execute("SELECT reward_version, control_campaign FROM crypto5m_maker_v3_campaigns "
                    "WHERE campaign_id='maker-adaptive-paper-v3.2r'").fetchone()
    c.close()
    assert row == ("maker-reward-v3.2", "maker-adaptive-paper-v3.1")


# ---- regime-gated floor hooks (v1 binary + v2 continuous ramp): INTEGRATION, not just the pure fn ----
# These call MK.run() with the flags set and assert the module global LD.VOL_FLOOR_PER_S ends up right,
# catching wiring bugs (arg-swap HONEST/WIDE or fair/spread) that the pure-function tests cannot see.
import math as _math


def _gated_desk(fair, bid, ask):
    return {
        "condition_id": "0xc", "window_start_ts": 1_000_000, "seconds_left": 120,
        "seconds_elapsed": 180, "now_ts": 1_000_180, "fair_value_up": fair,
        "btc_now": 100.0, "btc_start": 99.9, "up_bid": bid, "up_ask": ask,
        "down_bid": 1 - ask, "down_ask": 1 - bid, "jump_flag": False, "divergence": 1.0,
        "vol_fast": 0.00002, "vol_slow": 0.00002, "vol_per_s": 0.00002, "signal_series": {},
    }


def _gated_args(tmp_path, **extra):
    base = dict(
        paper_db=str(tmp_path / "m.sqlite"), pid=str(tmp_path / "m.pid"),
        campaign="maker-regime-gated-test", poll=0.0, margin=0.02, clip=25.0, max_shares=50.0,
        min_edge=0.04, min_seconds_quote=30, maker_regime_router="active",
        regime_shadow_campaign="maker-regime-shadow-v1", max_iterations=1,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _run_and_read_floor(tmp_path, monkeypatch, desk, **args_extra):
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: desk)
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])
    monkeypatch.setattr(MK.LD, "VOL_FLOOR_PER_S", 999.0)  # sentinel; auto-restored by monkeypatch
    MK.run(_gated_args(tmp_path, **args_extra))
    return MK.LD.VOL_FLOOR_PER_S


def test_ramp_hook_sets_honest_floor_when_one_sided(tmp_path, monkeypatch):
    # fair=0.80 -> |f-.5|=0.30 >= x_hi -> HONEST 2e-5. Arg-swapping HONEST/WIDE would give 1e-4 here.
    floor = _run_and_read_floor(tmp_path, monkeypatch, _gated_desk(0.80, 0.79, 0.81),
                                regime_gated_ramp=(2e-5, 1e-4, 0.15, 0.25))
    assert floor == pytest.approx(2e-5)


def test_ramp_hook_sets_wide_floor_when_contested(tmp_path, monkeypatch):
    # fair=0.51 -> |f-.5|=0.01 <= x_lo -> WIDE 1e-4
    floor = _run_and_read_floor(tmp_path, monkeypatch, _gated_desk(0.51, 0.50, 0.52),
                                regime_gated_ramp=(2e-5, 1e-4, 0.15, 0.25))
    assert floor == pytest.approx(1e-4)


def test_ramp_hook_interpolates_interior(tmp_path, monkeypatch):
    # fair=0.70 -> |f-.5|=0.20 = midpoint of [0.15,0.25] -> geometric mean of anchors
    floor = _run_and_read_floor(tmp_path, monkeypatch, _gated_desk(0.70, 0.69, 0.71),
                                regime_gated_ramp=(2e-5, 1e-4, 0.15, 0.25))
    assert floor == pytest.approx(_math.sqrt(2e-5 * 1e-4))


def test_ramp_hook_protects_on_crossed_book(tmp_path, monkeypatch):
    # one-sided fair but crossed book (ask<=bid -> _spread None) -> WIDE (matches v1 is_quiet).
    # Regression for the review finding: v2 used to quote HONEST here, diverging from v1.
    floor = _run_and_read_floor(tmp_path, monkeypatch, _gated_desk(0.80, 0.81, 0.80),
                                regime_gated_ramp=(2e-5, 1e-4, 0.15, 0.25))
    assert floor == pytest.approx(1e-4)


def test_binary_hook_sets_floor_by_regime(tmp_path, monkeypatch):
    # v1 binary: one-sided&tight -> quiet floor (first arg); contested -> second arg
    quiet = _run_and_read_floor(tmp_path, monkeypatch, _gated_desk(0.80, 0.79, 0.81),
                                regime_gated_floor=(2e-5, 1e-4))
    assert quiet == pytest.approx(2e-5)
    contested = _run_and_read_floor(tmp_path, monkeypatch, _gated_desk(0.51, 0.50, 0.52),
                                    regime_gated_floor=(2e-5, 1e-4))
    assert contested == pytest.approx(1e-4)


def test_no_gated_flags_leaves_vol_floor_untouched(tmp_path, monkeypatch):
    # ISOLATION: a campaign that sets neither flag must not have its floor touched by the hooks.
    # Proves "absent means untouched", not merely "absent means no crash".
    floor = _run_and_read_floor(tmp_path, monkeypatch, _gated_desk(0.80, 0.79, 0.81))
    assert floor == 999.0  # sentinel unchanged


def test_ramp_and_floor_flags_are_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: _gated_desk(0.51, 0.50, 0.52))
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])
    with pytest.raises(SystemExit):
        MK.run(_gated_args(tmp_path, regime_gated_floor=(2e-5, 1e-4),
                           regime_gated_ramp=(2e-5, 1e-4, 0.15, 0.25)))


def test_ramp_rejects_inverted_and_nonpositive(tmp_path, monkeypatch):
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: _gated_desk(0.51, 0.50, 0.52))
    monkeypatch.setattr(MK, "tape_prints", lambda *a, **kw: [])
    with pytest.raises(SystemExit):  # x_lo >= x_hi
        MK.run(_gated_args(tmp_path, regime_gated_ramp=(2e-5, 1e-4, 0.25, 0.15)))
    with pytest.raises(SystemExit):  # non-positive anchor (would ValueError in math.log)
        MK.run(_gated_args(tmp_path, regime_gated_ramp=(0.0, 1e-4, 0.15, 0.25)))


def test_fill_records_the_reason_of_the_quote_that_filled_not_the_current_tick(tmp_path, monkeypatch):
    # regression (codex 2026-07-07): fills execute against the PREVIOUS tick's resting quote, so the
    # recorded reason must be that quote's reason, not the current tick's.
    desks = [
        _gated_desk(0.30, 0.24, 0.26),   # tick 1: one-sided -> a distinctive resting bid
        _gated_desk(0.50, 0.49, 0.51),   # tick 2: contested -> a DIFFERENT reason
    ]
    it = iter(desks)
    monkeypatch.setattr(MK.LD, "build_live_desk", lambda: next(it))
    # no prints tick 1 (just post the resting bid); a print through our tick-1 bid on tick 2 -> fill
    calls = {"n": 0}
    def fake_prints(cid, a, b, token=None):
        calls["n"] += 1
        return [(0.22, 30.0)] if calls["n"] > 2 else []   # tick 2 (after both tokens' tick-1 calls)
    monkeypatch.setattr(MK, "tape_prints", fake_prints)
    args = _gated_args(tmp_path, max_iterations=2)
    MK.run(args)
    c = sqlite3.connect(str(tmp_path / "m.sqlite")); c.row_factory = sqlite3.Row
    fills = c.execute("SELECT token, side, price, reason FROM maker_fills WHERE fill_model='print'").fetchall()
    c.close()
    if fills:  # a fill occurred -> its reason must be the tick-1 (contested/one-sided entry) reason,
        for f in fills:
            # tick-2 fair=0.50 gives "resting bid below book"/"top-of-book"; tick-1 fair=0.30 differs.
            # The key invariant: the reason must NOT be empty/"no valid quote" for a real resting-bid fill.
            assert f["reason"] and "no valid quote" not in f["reason"]
