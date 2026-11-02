"""The funded callers enforce only a known in-memory volume refusal, before reservation."""
from types import SimpleNamespace
import time

import pytest

import test_memecoin_slot_lane as TL
import test_memecoin_source_buy_floor as TF
from test_memecoin_slot_lane import conn
from sniper.services import memecoin_real_runner as R
from sniper.services import memecoin_slot_lane as L
from sniper.services.memecoin_slot_signal import PUMP_PROGRAM, PUMP_AMM_PROGRAM


class Cache:
    def __init__(self, state):
        self.state, self.calls = state, []

    def check(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(state=self.state, reason="m5_under_floor", volume_usd=1.,
                               required_usd=90.)


def test_real_cache_snapshot_refuses_native_entry_without_an_entry_read(conn, tmp_path, monkeypatch):
    import sqlite3
    import test_memecoin_solana_volume_cache as VC
    from sniper.persistence import db_sol_volume as db
    from sniper.services.memecoin_solana_volume_cache import VolumeCache
    tape = sqlite3.connect(":memory:")
    db.ensure_tables(tape)
    VC.coverage(tape, mint=TL.CURVE_EVENT["mint"], venue="pump_curve",
                market_pool=TL.CURVE_EVENT["mint"])
    stamp = time.monotonic()
    cache = VolumeCache(clock=lambda: stamp)
    cache.refresh(tape, now=VC.NOW, own_owners={"OurWallet"}, published_mono=stamp)
    tape.close()  # Entry cannot reach the connection used by the background reader.
    monkeypatch.setattr(db, "coverage_rows", lambda *_a, **_k: pytest.fail("entry read coverage"))
    monkeypatch.setattr(db, "swap_rows", lambda *_a, **_k: pytest.fail("entry read swap tape"))
    lane, sent = TL._live_lane(conn, tmp_path, monkeypatch)
    lane.volume_cache = cache
    result = lane.handle(TL._buy())
    assert result["reason"] == "solana_volume_under_floor"
    assert result["solana_volume"]["volume_usd"] == 0.
    assert sent == []


def test_native_volume_refusal_precedes_reservation_and_build(conn, tmp_path, monkeypatch):
    lane, sent = TL._live_lane(conn, tmp_path, monkeypatch)
    lane.volume_cache = cache = Cache("refuse")
    result = lane.handle(TL._buy())
    assert result["reason"] == "solana_volume_under_floor"
    assert sent == []
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    assert L.policy_refusal(result["reason"])
    assert cache.calls[0]["mint"] == cache.calls[0]["market_pool"] == TL.CURVE_EVENT["mint"]
    assert cache.calls[0]["source_signature"] == TL.CURVE["signature"]


@pytest.mark.parametrize("state", [None, "unknown", "allow"])
def test_native_unknown_or_disabled_keeps_the_existing_entry(conn, tmp_path, monkeypatch, state):
    lane, sent = TL._live_lane(conn, tmp_path, monkeypatch)
    lane.volume_cache = Cache(state) if state else None
    result = lane.handle(TL._buy())
    assert result["status"] == "filled", result
    assert sent == [1]


def test_native_cache_wrapper_reads_only_memory(monkeypatch):
    cache = Cache("allow")
    buy = TL._buy(venue="pump_amm", pool="ExactPool")
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("gate waited"))
    result = L.cached_volume_decision(cache, buy, {"mint": "Mint", "mcap_usd": 4500},
                                      TL._snap(time.monotonic()), now_mono=12.)
    assert result.state == "allow"
    assert cache.calls == [dict(mint="Mint", venue="pump_amm", market_pool="ExactPool",
                               source_signature=buy.signature, mcap_usd=4500,
                               sol_usd=150., now_mono=12.)]
    assert L.cached_volume_decision(None, buy, {}, None) is None


@pytest.fixture
def ordinary(monkeypatch):
    from sniper.services import memecoin_arming
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES", True)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 0.)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.)
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", 0.)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *_a, **_k: None)
    monkeypatch.setattr(R, "_warm_mint_age", lambda *_a, **_k: None)

    def run(state, *, manual=False):
        conn = TF._conn()
        ev = TF._event(2000., market_cap=10000.)
        ev.source_program, ev.source_pool = PUMP_PROGRAM, "CurveAddress"
        ev.fomo_event_id = "onchain:" + TL.CURVE["signature"]
        if manual:
            ev.signal_source = "manual"
        reached = []
        ctx = TF._context(reached)
        ctx["solana_volume_cache"] = cache = Cache(state) if state else None
        result = R.real_entry_hook(conn, ev, {"solana": ctx}, now_fn=lambda: TF.NOW,
                                   trade_size=10., max_age_s=120,
                                   classification_context={"effective_mcap": 10000.})
        return conn, result, reached, cache
    return run


def test_ordinary_fallback_enforces_the_same_cache_before_reservation(ordinary):
    conn, result, reached, cache = ordinary("refuse")
    assert result["reason"] == "solana_volume_under_floor", result
    assert reached == []
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    assert cache.calls[0]["source_signature"] == TL.CURVE["signature"]
    assert cache.calls[0]["mcap_usd"] == 10000.


@pytest.mark.parametrize("state", [None, "unknown", "allow"])
def test_ordinary_unknown_or_disabled_keeps_existing_entry(ordinary, state):
    _, result, reached, _ = ordinary(state)
    assert reached == ["So1Mint"], result


def test_manual_entry_keeps_existing_selection_bypass(ordinary):
    _, result, reached, cache = ordinary("refuse", manual=True)
    assert reached == ["So1Mint"], result
    assert cache.calls == []


def test_ordinary_uses_exact_program_pool_and_existing_price(monkeypatch):
    from sniper.services import memecoin_live_marks as marks
    monkeypatch.setattr(marks, "fresh_quote_usd", lambda keys, *, now_iso: {marks.WSOL: 150.})
    ev = SimpleNamespace(mint="ExactMint", source_program=PUMP_AMM_PROGRAM,
                         source_pool="ExactPool", fomo_event_id="onchain:" + TL.CURVE["signature"])
    cache = Cache("allow")
    result = R.cached_solana_volume_decision(cache, ev, mcap_usd=10000., now_iso=TF.NOW)
    assert result.state == "allow"
    assert cache.calls[0]["venue"] == "pump_amm"
    assert cache.calls[0]["market_pool"] == "ExactPool"
    assert cache.calls[0]["sol_usd"] == 150.
    ev.source_program = "UnknownProgram"
    assert R.cached_solana_volume_decision(cache, ev, mcap_usd=10000., now_iso=TF.NOW) is None
    assert len(cache.calls) == 1


def test_background_cache_starts_once_with_the_funded_owner(monkeypatch, tmp_path):
    import test_memecoin_real_runner as real_tests
    from sniper.services import memecoin_solana_volume_cache as volume
    starts = []
    monkeypatch.setattr(volume, "start_volume_cache", lambda **kw: starts.append(kw) or Cache("allow"))
    real_tests.test_build_real_context_builds_when_armed_with_creds(monkeypatch, tmp_path)
    assert len(starts) == 1
    assert len(starts[0]["own_owners"]) == 1
    assert starts[0]["own_owners"][0]


def test_cache_startup_failure_leaves_existing_context_available(monkeypatch, tmp_path):
    import test_memecoin_real_runner as real_tests
    from sniper.services import memecoin_solana_volume_cache as volume
    def unavailable(**_kwargs):
        raise RuntimeError("partial install")
    monkeypatch.setattr(volume, "start_volume_cache", unavailable)
    real_tests.test_build_real_context_builds_when_armed_with_creds(monkeypatch, tmp_path)


def test_heartbeat_keeps_volume_status_and_drops_arbitrary_fields(tmp_path):
    import json
    from sniper.services import memecoin_runner as run
    path = tmp_path / "heartbeat.json"
    run.write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={
        "solana_volume_gate": {"state": "ready", "pools": 3, "skipped": 2,
                               "last_error": None, "enabled": True,
                               "min_m5_mcap_ratio": .02, "max_age_s": 5., "window_s": 300.,
                               "secret": "hidden"}})
    assert json.loads(path.read_text())["slot_lane"]["solana_volume_gate"] == {
        "state": "ready", "pools": 3, "skipped": 2, "last_error": None, "enabled": True,
        "min_m5_mcap_ratio": .02, "max_age_s": 5., "window_s": 300.}


@pytest.mark.parametrize("values", [
    ("true", float("nan"), float("inf"), True), (None, 0, -1., 10 ** 1000),
])
def test_heartbeat_rejects_malformed_volume_config(tmp_path, values):
    import json
    from sniper.services import memecoin_runner as run
    path = tmp_path / "heartbeat.json"
    fields = ("enabled", "min_m5_mcap_ratio", "max_age_s", "window_s")
    run.write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={
        "solana_volume_gate": dict(zip(fields, values), state="unknown", pools=0)})
    status = json.loads(path.read_text())["slot_lane"]["solana_volume_gate"]
    assert status["pools"] == 0
    assert {key: status[key] for key in fields} == dict.fromkeys(fields)


def test_heartbeat_reads_current_background_status_only():
    status = {"state": "warming", "pools": 0}
    context = {"solana": {"solana_volume_cache": SimpleNamespace(status=lambda: dict(status))}}
    assert R.solana_volume_status_for_contexts(context) == status
    status.update(state="ready", pools=1)
    assert R.solana_volume_status_for_contexts(context) == status


@pytest.mark.parametrize(("key", "value"), [
    ("MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO", float("nan")),
    ("MEMECOIN_SOLANA_VOLUME_MIN_M5_MCAP_RATIO", 1.1),
    ("MEMECOIN_SOLANA_VOLUME_MAX_AGE_S", 6.),
])
def test_enabled_gate_settings_reject_invalid_thresholds(monkeypatch, key, value):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(R.cfg, "MEMECOIN_SOLANA_VOLUME_GATE_ENABLED", True)
    monkeypatch.setattr(R.cfg, key, value)
    with pytest.raises(RuntimeError, match=key):
        R.cfg.validate_real_exit_policy()


def test_volume_refusal_cannot_create_an_ordinary_handoff(conn, tmp_path, monkeypatch):
    lane, _ = TL._live_lane(conn, tmp_path, monkeypatch)
    cache = Cache("refuse")
    lane.volume_cache = cache
    handoffs = []
    lane.handoff = SimpleNamespace(offer=lambda *args: handoffs.append(args))
    lane.routed_handoff = SimpleNamespace(write=lambda row: handoffs.append(row))
    lane.miss_learner = SimpleNamespace(is_retry=lambda sig: True)
    buy = TL._buy(routed=SimpleNamespace(mint=TL.CURVE_EVENT["mint"]))
    result = lane.handle(buy)
    assert result["reason"] == "solana_volume_under_floor", result
    assert handoffs == []


@pytest.mark.parametrize("old_attempt", [None, "build_refused", "landed_fail", "routed_handoff"])
def test_latest_volume_policy_refusal_removes_ordinary_fallback_authority(old_attempt):
    import test_memecoin_onchain_lane as OL
    from sniper.services import memecoin_onchain_lane as onchain, memecoin_slot_skips as skips
    sig = "volume-" + str(old_attempt)
    conn = OL._slot_conn(sig=sig)
    if old_attempt in ("build_refused", "landed_fail"):
        reason = "slot_lane_build:unsupported" if old_attempt == "build_refused" else "chain_error:failed"
        OL._slot_attempt(conn, sig=sig, terminal="aborted", pending="failed", reason=reason)
        if old_attempt == "landed_fail":
            conn.execute("UPDATE memecoin_entry_attempts SET tx_sig='old-signed-failure'")
    elif old_attempt == "routed_handoff":
        L.ROUTED_HANDOFFS.mark(sig, time.monotonic())
    skips.note(sig, "rejected", "solana_volume_under_floor")
    assert onchain.slot_lane_verdict(conn, sig, OL.now_iso()) == ("decided", None)
    calls = []
    _, results = OL._sweep(conn, calls, {})
    assert calls == []
    assert results[0]["result"]["reason"] == "duplicate_of_slot_lane"


@pytest.mark.parametrize("owner", ["reserved", "confirmed", "signed", "unresolved", "held"])
def test_latest_volume_policy_keeps_native_ownership_precedence(owner, monkeypatch):
    import test_memecoin_onchain_lane as OL
    from sniper.services import memecoin_onchain_lane as onchain, memecoin_slot_skips as skips
    sig = "volume-owner-" + owner
    conn = OL._slot_conn(sig=sig)
    if owner == "held":
        pending = L.PendingLegs()
        pending.add(SimpleNamespace(signature=sig), deadline=time.monotonic() + 10.)
        monkeypatch.setattr(L, "CONFIRMED_PENDING", pending)
    else:
        OL._slot_attempt(conn, sig=sig, terminal=None if owner == "unresolved" else "submitted",
                         pending=owner if owner in ("reserved", "confirmed") else None)
        if owner == "signed":
            conn.execute("UPDATE memecoin_entry_attempts SET tx_sig='still-may-land'")
    skips.note(sig, "rejected", "solana_volume_under_floor")
    expected = "filled" if owner == "confirmed" else "in_flight"
    assert onchain.slot_lane_verdict(conn, sig, OL.now_iso()) == (expected, None)


@pytest.mark.parametrize("older_attempt", [False, True])
def test_added_fallback_policy_check_never_waits_for_skip_writer_lock(older_attempt):
    import sqlite3
    import threading
    import test_memecoin_onchain_lane as OL
    from sniper.services import memecoin_onchain_lane as onchain, memecoin_slot_skips as skips
    sig = 'volume-no-lock-' + str(older_attempt)
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.executescript(OL.SLOT_SCHEMA)
    if older_attempt:
        OL._slot_attempt(conn, sig=sig, terminal='aborted', pending='failed',
                         reason='slot_lane_build:unsupported')
    skips.note(sig, 'rejected', 'solana_volume_under_floor')
    finished, results = threading.Event(), []
    def verdict():
        try:
            results.append(onchain.slot_lane_verdict(conn, sig, OL.now_iso()))
        finally:
            finished.set()
    worker = threading.Thread(target=verdict, daemon=True)
    with skips._LAST_LOCK:
        worker.start()
        completed_without_lock = finished.wait(.2)
    worker.join(1.)
    conn.close()
    assert completed_without_lock, 'the new volume policy check waited for the skip writer lock'
    assert results == [('decided', None)]
