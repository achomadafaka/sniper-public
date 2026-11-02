"""Independent watcher witnesses; fake model, source contracts, and temporary observer DB only."""

import json
import sqlite3

import pytest

from test_desk_risk_gate_integration import gated  # noqa: F401
from test_memecoin_desk_risk_watcher import T0, W, broken_bars


@pytest.mark.parametrize("blocked_by", ["backoff", "hourly_cap"])
def test_unconsumed_attention_retries_after_the_source_event_ages_out(gated, blocked_by):
    event_at = gated.t - 1

    def attention(pid, now):
        return [("sell_burst", event_at)] if pid == 1 and now - event_at <= W.ATTN_WINDOW_S else []

    good_model = gated.model
    if blocked_by == "backoff":
        gated.model = lambda *_: {"call_error": "rate_limited", "text": None}
        assert gated.tick(attention_fn=attention)["failed_calls"] == 1
        gated.model = good_model
    else:
        assert gated.tick(attention_fn=attention, hourly_cap=0)["skipped"] == 1
    # No price movement and no newly fired event. The blocked wake still needs one current-evidence evaluation.
    assert gated.tick(61, attention_fn=attention, hourly_cap=2 if blocked_by == "backoff" else 1)["calls"] == 1


def test_ps_structure_break_cannot_outlive_the_mints_loss_aware_coverage(gated, monkeypatch):
    monkeypatch.setattr(W.clock, "offset_s", lambda: 0)
    monkeypatch.setattr(W, "SB_CLOSES", True)
    monkeypatch.setattr(W, "pre_fill_bars", lambda *_: [])
    monkeypatch.setattr(W, "capture_proof", lambda _d, now, *_a, **_k: {"from": T0 - 600, "through": now - 1})
    gated.bars = [dict(b, venue="ps") for b in broken_bars()]
    # Global capture is current, but a loss on THIS pool makes its pre-loss support/noise unavailable.
    gated.evidence[1].update(coverage_from=gated.t - 10, known_through=gated.t - 2, n5=None)
    gated.answer = lambda blocks: json.dumps([{"k": b["k"], "a": "E", "r": None, "c": 10} for b in blocks])
    assert W.structure_break_of(gated.positions[0], gated.bars, gated.tmp, gated.t, 0) is not None
    gated.tick(attention_fn=lambda *_: [])
    row = next(r for r in gated.rows() if r["key"] == "pos:1")
    assert row["action"] == "HOLD"
    assert row["expires_at"] is None


def test_pending_attention_retains_original_identity_across_restart_and_source_expiry(tmp_path):
    path, now = tmp_path / "risk.db", T0 + 80
    fresh = {1: [("sell_burst", now - 1), ("sell_burst", now - 1)]}
    pending, backoff = W.pending_attention(path, {1}, fresh, now)
    assert pending == {1: [("sell_burst", now - 1)]} and backoff == 0
    pending, backoff = W.pending_attention(path, {1}, {}, now + 61)
    assert pending == {1: [("sell_burst", now - 1)]} and backoff == 0


def test_attention_ack_is_atomic_and_done_receipt_prevents_source_replay(tmp_path):
    path, now = tmp_path / "risk.db", T0 + 80
    key = (1, "sell_burst", now - 1)
    W.pending_attention(path, {1}, {1: [(key[1], key[2])]}, now)
    out = W.connect(path)
    try:
        W.ack_attention(out, {key})
        out.rollback()
    finally:
        out.close()
    assert W.pending_attention(path, {1}, {}, now)[0] == {1: [(key[1], key[2])]}
    out = W.connect(path)
    try:
        W.ack_attention(out, {key})
        out.commit()
    finally:
        out.close()
    assert W.pending_attention(path, {1}, {1: [(key[1], key[2])]}, now + 1)[0] == {}
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT detected_at,done FROM risk_attention_events").fetchall() == [(now - 1, 1)]


def test_pending_cleanup_prunes_closed_positions_and_old_done_receipts(tmp_path):
    path, now = tmp_path / "risk.db", T0 + 80
    W.pending_attention(path, {1, 2}, {1: [("a", now - 1)], 2: [("b", now - 1)]}, now)
    out = W.connect(path)
    try:
        W.ack_attention(out, {(1, "a", now - 1)})
        out.commit()
    finally:
        out.close()
    assert W.pending_attention(path, {1}, {}, now + 61)[0] == {}
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM risk_attention_events").fetchone()[0] == 0


@pytest.mark.parametrize("error", ["auth_expired", "rate_limited"])
def test_pending_queue_restores_response_based_backoff_after_a_long_failed_call(tmp_path, error):
    path, now = tmp_path / "risk.db", T0 + 80
    out = W.connect(path)
    try:
        out.execute("INSERT INTO risk_calls(cycle_at,call_started_at,response_at,n_positions,prompt_hash,call_error,live,risk_version) "
                    "VALUES(?,?,?,1,'h',?,1,'test')", (now - 120, now - 120, now - 10, error))
        out.commit()
    finally:
        out.close()
    assert W.pending_attention(path, {1}, {}, now) == ({}, now + 50)
    assert W.pending_attention(path, {1}, {}, now + 51) == ({}, 0)


def test_pending_queue_drops_unproved_fresh_events_and_closed_position_input(tmp_path):
    now = T0 + 80
    fresh = {1: [("valid", now - 1), ("future", now + 1), ("old", now - 61),
                 ("unknown", None), ("", now - 1)], 2: [("closed", now - 1)]}
    assert W.pending_attention(tmp_path / "risk.db", {1}, fresh, now)[0] == {1: [("valid", now - 1)]}


def test_watcher_restarts_with_a_pending_budget_wake_after_source_expiry(gated):
    event_at = gated.t - 1
    attention = lambda pid, now: [("sell_burst", event_at)] if pid == 1 else []
    assert gated.tick(attention_fn=attention, hourly_cap=0)["calls"] == 0
    gated.state = W.new_state()
    assert gated.tick(61, attention_fn=lambda *_: [], hourly_cap=1)["calls"] == 1
    assert gated.calls[0][0]["attn"] == []
    row = next(r for r in reversed(gated.rows()) if r["key"] == "pos:1")
    assert json.loads(row["gate_json"])["pending_attention"] == [[1, "sell_burst", event_at]]
    with sqlite3.connect(gated.out) as c:
        assert c.execute("SELECT detected_at,done FROM risk_attention_events WHERE position_id=1").fetchall() == [(event_at, 1)]


def test_watcher_restart_preserves_failure_backoff_and_pending_wake(gated):
    event_at = gated.t - 1
    model = gated.model
    gated.model = lambda *_: {"call_error": "rate_limited", "text": None}
    assert gated.tick(attention_fn=lambda pid, now: [("sell_burst", event_at)] if pid == 1 else [])["failed_calls"] == 1
    failed_at = gated.t
    gated.state = W.new_state()
    gated.model = model
    assert gated.tick(1, attention_fn=lambda *_: [], hourly_cap=2)["calls"] == 0
    assert gated.state["backoff_until"] == failed_at + 60
    assert gated.tick(61, attention_fn=lambda *_: [], hourly_cap=2)["calls"] == 1
    assert gated.calls[0][0]["attn"] == []
