"""Continuous window-lifecycle + post-resolution reconciliation state machines (pure cores)."""
from __future__ import annotations

import json
import os

from sniper.algos.polymarket_crypto5m import lifecycle as LC
from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import reconciliation as RC
from sniper.algos.polymarket_crypto5m import settlement as S
from sniper.algos.polymarket_crypto5m.lifecycle import WindowState
from sniper.algos.polymarket_crypto5m.reconciliation import ReconcileStatus

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _spec(name="event_resolved_up.json"):
    with open(os.path.join(FX, name)) as f:
        spec, _ = MR.parse_event(json.load(f))
    return spec


# --- window lifecycle (#1) ------------------------------------------------------------------

def test_lifecycle_advances_through_states():
    spec = _spec()  # window 1781947200..1781947500
    mgr = LC.WindowLifecycle(settlement_timeout_s=600)
    mgr.discover(spec, now=1781947100)
    assert mgr.state(spec.condition_id) is WindowState.DISCOVERED
    mgr.start_capture(spec.condition_id)
    assert mgr.state(spec.condition_id) is WindowState.CAPTURING
    mgr.tick(now=1781947499)                          # still inside window
    assert mgr.state(spec.condition_id) is WindowState.CAPTURING
    mgr.tick(now=1781947501)                          # window_end passed
    assert mgr.state(spec.condition_id) is WindowState.AWAITING_RESOLUTION
    mgr.mark_reconciled(spec.condition_id)
    assert mgr.state(spec.condition_id) is WindowState.RECONCILED


def test_discovered_windows_are_drivable_and_expire_if_never_captured():
    spec = _spec()  # window ends 1781947500
    mgr = LC.WindowLifecycle(settlement_timeout_s=600)
    mgr.discover(spec, now=1781947100)
    # the driver must be able to find DISCOVERED windows to start capturing (the deadlock fix)
    assert [s.condition_id for s in mgr.discovered_windows()] == [spec.condition_id]
    # a DISCOVERED window never started, whose window has ended, must expire (not leak forever)
    mgr.tick(now=1781947501)
    assert mgr.state(spec.condition_id) is WindowState.EXPIRED
    assert mgr.prune_terminal() == 1
    assert mgr.count() == 0


def test_lifecycle_expires_on_settlement_timeout():
    spec = _spec()
    mgr = LC.WindowLifecycle(settlement_timeout_s=600)
    mgr.discover(spec, now=1781947100)
    mgr.start_capture(spec.condition_id)
    mgr.tick(now=1781947600)                          # awaiting (100s after close)
    assert mgr.state(spec.condition_id) is WindowState.AWAITING_RESOLUTION
    mgr.tick(now=1781947500 + 601)                    # > timeout after window_end
    assert mgr.state(spec.condition_id) is WindowState.EXPIRED


def test_lifecycle_active_tokens_and_subscription_diff():
    spec = _spec()
    mgr = LC.WindowLifecycle(settlement_timeout_s=600)
    mgr.discover(spec, now=1)
    mgr.start_capture(spec.condition_id)
    active = mgr.active_tokens()
    assert spec.up_token_id in active and spec.down_token_id in active
    # nothing subscribed yet -> both are to-subscribe; none to-unsubscribe
    assert set(mgr.to_subscribe(set())) == {spec.up_token_id, spec.down_token_id}
    # once reconciled, its tokens drop from active -> become to-unsubscribe
    mgr.tick(now=1781947501)
    mgr.mark_reconciled(spec.condition_id)
    assert mgr.active_tokens() == []
    assert set(mgr.to_unsubscribe({spec.up_token_id, spec.down_token_id})) == {
        spec.up_token_id, spec.down_token_id}


def test_lifecycle_caps_at_max_windows():
    mgr = LC.WindowLifecycle(settlement_timeout_s=600, max_windows=1)
    s1 = _spec("event_resolved_up.json")
    s2 = _spec("event_resolved_down.json")
    assert mgr.discover(s1, now=1) is True
    assert mgr.discover(s2, now=1) is False           # cap reached
    assert mgr.count() == 1


# --- post-resolution reconciliation (#3) ----------------------------------------------------

def test_needs_reconcile_only_for_closed_unresolved():
    spec = _spec()
    # closed window, no resolution row yet -> needs reconcile
    assert RC.needs_reconcile(window_end_ts=spec.window_end_ts, now=spec.window_end_ts + 10,
                              has_resolution=False) is True
    # already resolved -> no
    assert RC.needs_reconcile(window_end_ts=spec.window_end_ts, now=spec.window_end_ts + 10,
                              has_resolution=True) is False
    # not yet closed -> no
    assert RC.needs_reconcile(window_end_ts=spec.window_end_ts, now=spec.window_end_ts - 10,
                              has_resolution=False) is False


def test_reconcile_status_transitions():
    we = 1781947500
    assert RC.next_status(has_resolution=True, conflict=False, now=we + 5, window_end_ts=we,
                          timeout_s=600) is ReconcileStatus.RESOLVED
    assert RC.next_status(has_resolution=False, conflict=True, now=we + 5, window_end_ts=we,
                          timeout_s=600) is ReconcileStatus.CONFLICT
    assert RC.next_status(has_resolution=False, conflict=False, now=we + 5, window_end_ts=we,
                          timeout_s=600) is ReconcileStatus.PENDING
    assert RC.next_status(has_resolution=False, conflict=False, now=we + 601, window_end_ts=we,
                          timeout_s=600) is ReconcileStatus.TIMED_OUT


def test_reconcile_market_via_injected_fetch_resolves():
    raw = json.load(open(os.path.join(FX, "event_resolved_up.json")))
    spec = _spec()

    def fake_fetch(url):
        return [raw]   # gamma returns the resolved event

    st = RC.reconcile_market(fake_fetch, spec)
    assert isinstance(st, S.Settlement)
    assert st.winning_outcome == "Up" and st.reconciliation_status == "gamma_only"


def test_reconcile_market_detects_gamma_vs_ws_conflict():
    # gamma says Up, the WS settlement says Down -> CONFLICT must be reachable (quarantined winner)
    raw = json.load(open(os.path.join(FX, "event_resolved_up.json")))
    spec = _spec()
    ws_down = S.Settlement(spec.condition_id, spec.down_token_id, "Down", None, "resolved",
                           ("0", "1"), "websocket", "ws_only", None)
    st = RC.reconcile_market(lambda url: [raw], spec, ws_settlement=ws_down)
    assert st.reconciliation_status == "conflict"
    assert st.winning_outcome is None                      # quarantined, no consumable winner
    assert RC.status_for(st, now=spec.window_end_ts + 5, window_end_ts=spec.window_end_ts,
                         timeout_s=600) is ReconcileStatus.CONFLICT


def test_status_for_resolved_and_pending_and_timeout():
    we = 1781947500
    resolved = S.Settlement("c", "U", "Up", None, "resolved", ("1", "0"), "gamma", "gamma_only", 1.0)
    assert RC.status_for(resolved, now=we + 5, window_end_ts=we, timeout_s=600) is ReconcileStatus.RESOLVED
    assert RC.status_for(None, now=we + 5, window_end_ts=we, timeout_s=600) is ReconcileStatus.PENDING
    assert RC.status_for(None, now=we + 601, window_end_ts=we, timeout_s=600) is ReconcileStatus.TIMED_OUT
