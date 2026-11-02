"""J7 on Solana, SHADOW (2026-09-28, operator: "fill only from the fast lanes"; Alpha GO).

MEMECOIN_J7_SOLANA_MODE=shadow: a J7 BUY on Solana from a wallet the on-chain lane covers runs every
entry gate and records its decision, but never reserves, quotes or signs. The on-chain and slot
lanes fill it. J7 decides about 0.12 s before the on-chain lane, so a shadow attempt row counted by
the lane's feed dedup would have skipped EVERY buy J7 saw: the dedup must ignore it.
"""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_address_registry as registry
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_onchain_lane import already_attempted_from_another_lane
from sniper.settings import memecoin as cfg
from test_memecoin_onchain_lane import (
    ARMED_UID, NOW, _slot_attempt, _slot_conn, _sweep)

COVERED_UID = "uid-covered"
_REAL_HEALTH = R.solana_watcher_healthy


@pytest.fixture(autouse=True)
def _shadow(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SOLANA_MODE", "shadow", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SOLANA_SHADOW_UNCOVERED", False, raising=False)
    monkeypatch.setattr(registry, "armed_verified_owners",
                        lambda conn, *a, **k: {"AddrCovered": (COVERED_UID, "covered")})
    monkeypatch.setattr(R, "solana_watcher_healthy", lambda *a, **k: True)


def _j7(uid=COVERED_UID, *, network="solana", side="buy", source="follow_activity"):
    return SimpleNamespace(side=side, signal_source=source, network_id=network,
                           mint="MintA" if network == "solana" else "0x" + "cc" * 20,
                           source_user_id=uid, delivery_lane="j7_wallets", fomo_event_id="e1")


def test_a_covered_j7_solana_buy_is_tagged_and_the_original_is_untouched():
    ev = _j7()
    tagged = R.j7_shadow_event(None, ev)
    assert tagged.delivery_lane == "j7_shadow" and ev.delivery_lane == "j7_wallets"


@pytest.mark.parametrize("why, ev", [
    ("uncovered wallet stays live", _j7("uid-j7-only")),
    ("EVM stays live", _j7(network="4663")),
    ("sells are mirror exits", _j7(side="sell")),
    ("other lanes are not J7", _j7(source="onchain_watch")),
])
def test_everything_else_stays_live(why, ev):
    assert R.j7_shadow_event(None, ev) is None, why


def test_live_mode_changes_nothing(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SOLANA_MODE", "live")
    assert R.j7_shadow_event(None, _j7()) is None


def test_an_unreadable_registry_keeps_the_buy_live(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry down")
    monkeypatch.setattr(registry, "armed_verified_owners", boom)
    assert R.j7_shadow_event(None, _j7()) is None


def test_the_uncovered_knob_shadows_uncovered_wallets_too(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SOLANA_SHADOW_UNCOVERED", True)
    assert R.j7_shadow_event(None, _j7("uid-j7-only")).delivery_lane == "j7_shadow"


def _hook(monkeypatch, conn):
    """The real hook up to the leg; the leg itself must never run in shadow."""
    from test_memecoin_entry_identity import FakeClock, NOW as T, _approve, _arm
    _arm(monkeypatch)
    _approve(conn, chains=("solana",))

    def leg(*a, **k):
        raise AssertionError("a shadow decision reached the leg")
    monkeypatch.setattr(R, "real_entry", leg)
    sent = []
    monkeypatch.setattr(R, "_notify_entry_projection", lambda *a, **k: sent.append((a, k)))
    queued = []
    monkeypatch.setattr(R, "_maybe_enqueue_retry", lambda *a, **k: queued.append(a))
    solana = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0, "kill_path": None,
              "token_safety_fn": lambda *a, **k: (True, "ok"), "execute_fn": leg}
    hook = R.make_real_hook(None, {"solana": solana}, now_fn=lambda: T, trade_size=10.0, max_age_s=120,
                            stats_fn=lambda mint, network_id=None: {"price": 0.25, "market_cap": 50_000.0},
                            trace_clock=FakeClock())
    return hook, sent, queued


def test_the_hook_records_the_decision_and_never_reaches_the_leg(monkeypatch):
    from test_memecoin_entry_identity import _conn, _event
    conn = _conn()
    monkeypatch.setattr(registry, "armed_verified_owners",
                        lambda c, *a, **k: {"Addr": ("u1", "alice")})
    hook, sent, queued = _hook(monkeypatch, conn)
    ev = _event(network_id="solana", mint="MintA", source="follow_activity")
    from datetime import datetime, timezone
    ev.created_at = ev.received_at_normalized = datetime.now(timezone.utc).isoformat()  # after the arm
    res = hook(conn, ev)
    assert res["j7_shadow"] is True
    assert "reservation and quote checks not run" in res["reason"]
    row = conn.execute("SELECT delivery_lane, terminal_status FROM memecoin_entry_attempts").fetchone()
    assert row[0] == "j7_shadow", "the tag must be on the attempt's FIRST row"
    assert res["status"] == "shadow", (res, tuple(row))
    assert queued == [], "the retry lane must never re-ask a shadow decision"
    assert sent == [], "a shadow decision sends no card; the fast lane's attempt carries it"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_the_onchain_lane_does_not_treat_a_shadow_decision_as_the_feeds(monkeypatch):
    """The J7-first race: J7's shadow row is written ~0.12 s before the lane sees the signature."""
    conn = _slot_conn("sigJ")
    started = (NOW - timedelta(seconds=6)).isoformat()
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, terminal_status, delivery_lane) VALUES (?,?,?,?,?,?)",
                 ("j7", "swap:raybot:sigJ:owner:MintsigJ", "follow_activity", started, "shadow",
                  "j7_shadow"))
    detected = NOW.isoformat()
    assert not already_attempted_from_another_lane(conn, "sigJ", detected)
    conn.execute("UPDATE memecoin_entry_attempts SET delivery_lane='j7_wallets'")
    assert already_attempted_from_another_lane(conn, "sigJ", detected), "a live J7 row still wins"


def test_after_a_slot_failure_the_onchain_lane_takes_the_buy_not_j7():
    """Alpha: in shadow mode J7 no longer buys covered wallets, so a slot min-out failure must fall
    back to the ON-CHAIN lane. While the slot send is unresolved the lane defers; once it is
    released as failed the lane runs its one fallback -- and J7's shadow row does not block it."""
    conn = _slot_conn()
    _slot_attempt(conn, pending="reserved")                                  # slot send in flight
    started = (NOW - timedelta(seconds=6)).isoformat()
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, terminal_status, delivery_lane) VALUES (?,?,?,?,?,?)",
                 ("j7", "swap:raybot:sigS:owner:MintsigS", "follow_activity", started, "shadow",
                  "j7_shadow"))
    calls, deferred = [], {}
    cursor, results = _sweep(conn, calls, deferred)
    assert calls == [] and results[0]["result"]["reason"] == "slot_lane_unresolved"
    conn.execute("UPDATE memecoin_pending_entries SET status='failed'")     # the min-out revert
    _sweep(conn, calls, deferred, cursor=cursor)
    assert calls == [("onchain:sigS", "onchain_fallback_after_slot_lane")]


# ------------------------------------------------------------------ shadow only while the watcher is up

import json as _json
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

_T = _dt(2026, 9, 28, 21, 0, tzinfo=_tz.utc)


def _beat(tmp_path, **fields):
    beat = {"ts": _T.isoformat(), "subs": 33, "subs_failed": 0, **fields}
    p = tmp_path / "onchain_watch.heartbeat"
    p.write_text(_json.dumps(beat))
    return p


@pytest.mark.parametrize("why, fields, age_s, healthy", [
    ("fresh and subscribed", {}, 10, True),
    ("three missed beats", {}, 46, False),
    ("a failed subscription", {"subs_failed": 1}, 1, False),
    ("no subscription", {"subs": 0}, 1, False),
    ("a heartbeat from the future", {}, -30, False),
    # 2026-09-28: the watcher now beats while it backs off between reconnects, so the beat is fresh
    # and still carries the subscription count a stale reader would trust.
    ("a fresh beat from a disconnected watcher", {"connected": False, "subs": 33,
                                                  "disconnected_since": _T.isoformat()}, 1, False),
    ("a fresh beat from a connected watcher", {"connected": True}, 1, True),
])
def test_watcher_health(monkeypatch, tmp_path, why, fields, age_s, healthy):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_WATCHER_MAX_AGE_S", 45.0)
    p = _beat(tmp_path, **fields)
    assert _REAL_HEALTH(now_wall=_T + _td(seconds=age_s), path=p) is healthy, why


def test_an_unreadable_heartbeat_is_unhealthy(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_WATCHER_MAX_AGE_S", 45.0)
    assert _REAL_HEALTH(path=tmp_path / "missing") is False


def test_a_dead_watcher_puts_a_covered_j7_buy_back_to_live(monkeypatch):
    """REGRESSION: in shadow a dead watcher dropped every covered buy in silence."""
    monkeypatch.setattr(R, "solana_watcher_healthy", lambda *a, **k: False)
    assert R.j7_shadow_event(None, _j7()) is None
