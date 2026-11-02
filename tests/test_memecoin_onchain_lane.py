"""Stage-2 on-chain lane: detections -> real_hook with freshness, arming, and cursor rules."""
import sqlite3

import pytest
from datetime import datetime, timedelta, timezone

from sniper.services.memecoin_onchain_lane import (
    OnchainEv, startup_cursor, sweep_onchain_detections)

# THE WATCHER'S OWN CREATE, not a copy (2026-09-29): a copied schema went stale when the lane began
# reading source_observation_state, and 29 tests failed on a column production always has.
from scripts.research.onchain_wallet_watch import SCHEMA  # noqa: E402

NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
ARMED_UID = "uid-armed"
COLD_UID = "uid-not-armed"


def now_iso():
    return NOW.isoformat()


def conn_with(rows, *, slot_tables=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if slot_tables:                       # production always has them; unreadable = refusal
        conn.executescript(SLOT_SCHEMA)
    for sig, uid, age_s in rows:
        ts = (NOW - timedelta(seconds=age_s)).isoformat()
        conn.execute(
            "INSERT INTO onchain_wallet_detections "
            "(signature, source_user_id, handle, address, mint, detected_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (sig, uid, "h-" + uid, "addr", "Mint" + sig, ts, ts))
    return conn


def approved(_conn, _network_id="solana"):
    return {ARMED_UID}


def test_fresh_armed_detection_fires_hook_with_correct_event():
    conn = conn_with([("sig1", ARMED_UID, 5)])
    calls = []

    def hook(c, ev):
        calls.append(ev)
        return {"status": "filled"}

    cursor, results = sweep_onchain_detections(
        conn, hook, cursor=0, now_fn=now_iso, max_age_s=45, approved_ids_fn=approved)
    assert len(calls) == 1
    ev = calls[0]
    assert isinstance(ev, OnchainEv)
    assert ev.side == "buy"
    assert ev.signal_source == "onchain_watch"
    assert ev.mint == "Mintsig1"
    assert ev.source_user_id == ARMED_UID
    assert ev.network_id == "solana"
    assert ev.source_evidence['network_id'] == 'solana'
    assert ev.source_evidence['mint'] == 'Mintsig1'
    assert ev.fomo_event_id == "onchain:sig1"
    assert ev.received_at_normalized == ev.received_at == ev.created_at
    assert cursor == 1
    assert results[0]["result"] == {"status": "filled"}


def test_fresh_evm_detection_keeps_chain_and_source_price_controls():
    conn = conn_with([])
    ts = (NOW - timedelta(seconds=3)).isoformat()
    conn.execute(
        "INSERT INTO onchain_wallet_detections "
        "(signature,source_user_id,handle,address,network_id,mint,token_delta,source_size_usd,"
        "source_price,detected_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("4663:tx:mint", ARMED_UID, "armed", "0x" + "11" * 20, "4663",
         "0x" + "22" * 20, 50.0, 125.0, 2.5, ts, ts),
    )
    calls = []

    def approved_evm(_conn, network_id):
        return {ARMED_UID} if network_id == "4663" else set()

    sweep_onchain_detections(
        conn, lambda _conn, ev: calls.append(ev) or {"status": "filled"}, cursor=0,
        now_fn=now_iso, max_age_s=45, approved_ids_fn=approved_evm)

    assert len(calls) == 1
    ev = calls[0]
    assert ev.network_id == "4663"
    assert ev.source_evidence['network_id'] == '4663'
    assert ev.source_evidence['mint'] == '0x' + '22' * 20
    assert ev.their_size_usd == 125.0
    assert ev.their_price == 2.5
    assert ev.fomo_event_id == "onchain:4663:tx:mint"


def test_unarmed_detection_skipped_but_cursor_advances():
    conn = conn_with([("sig1", COLD_UID, 5)])
    calls = []
    cursor, results = sweep_onchain_detections(
        conn, lambda c, e: calls.append(e), cursor=0, now_fn=now_iso,
        max_age_s=45, approved_ids_fn=approved)
    assert calls == []
    assert results == []
    assert cursor == 1


def test_stale_detection_never_fires():
    conn = conn_with([("sig1", ARMED_UID, 300)])
    calls = []
    cursor, results = sweep_onchain_detections(
        conn, lambda c, e: calls.append(e), cursor=0, now_fn=now_iso,
        max_age_s=45, approved_ids_fn=approved)
    assert calls == []
    assert cursor == 1


def test_cursor_prevents_reprocessing():
    conn = conn_with([("sig1", ARMED_UID, 5)])
    hits = []

    def hook(c, ev):
        hits.append(ev.mint)
        return {"status": "filled"}

    cursor, _ = sweep_onchain_detections(
        conn, hook, cursor=0, now_fn=now_iso, max_age_s=45, approved_ids_fn=approved)
    cursor2, results2 = sweep_onchain_detections(
        conn, hook, cursor=cursor, now_fn=now_iso, max_age_s=45, approved_ids_fn=approved)
    assert hits == ["Mintsig1"]
    assert cursor2 == cursor
    assert results2 == []


def test_hook_exception_advances_cursor_and_records_error():
    conn = conn_with([("sig1", ARMED_UID, 5), ("sig2", ARMED_UID, 5)])
    seen = []

    def hook(c, ev):
        seen.append(ev.mint)
        if ev.mint == "Mintsig1":
            raise RuntimeError("boom")
        return {"status": "filled"}

    cursor, results = sweep_onchain_detections(
        conn, hook, cursor=0, now_fn=now_iso, max_age_s=45, approved_ids_fn=approved)
    assert cursor == 2
    assert seen == ["Mintsig1", "Mintsig2"]
    assert results[0]["result"]["status"] == "error"
    assert results[1]["result"]["status"] == "filled"


def test_startup_cursor_skips_stale_keeps_fresh():
    conn = conn_with([("old", ARMED_UID, 600), ("fresh", ARMED_UID, 10)])
    cur = startup_cursor(conn, now_fn=now_iso, max_age_s=45)
    assert cur == 1          # the stale row is below the cursor, the fresh one above
    _, results = sweep_onchain_detections(
        conn, lambda c, e: {"status": "filled"}, cursor=cur, now_fn=now_iso,
        max_age_s=45, approved_ids_fn=approved)
    assert [r["signature"] for r in results] == ["fresh"]


def test_startup_cursor_empty_table_is_zero():
    conn = conn_with([])
    assert startup_cursor(conn, now_fn=now_iso, max_age_s=45) == 0


def test_lane_heartbeat_written(tmp_path):
    from sniper.services.memecoin_onchain_lane import write_lane_heartbeat
    import json as _json
    hb = tmp_path / "hb.json"
    write_lane_heartbeat(cursor=7, now_iso="2026-08-21T12:00:00+00:00", path=hb)
    data = _json.loads(hb.read_text())
    assert data == {"ts": "2026-08-21T12:00:00+00:00", "cursor": 7}


# ---------------------------------------------------- 4663 rows (operator wired 2026-09-16)

def _evm_row(conn, *, uid=ARMED_UID, age_s=5, network_id="4663",
             mint="0x" + "cc" * 20, signature="4663:0xdeadbeef:0xcc"):
    ts = (NOW - timedelta(seconds=age_s)).isoformat()
    conn.execute(
        "INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, "
        "network_id, mint, token_delta, source_size_usd, source_price, block_time, detected_at, "
        "detect_latency_s, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (signature, uid, "CryoPalmar", "0x38d5a4f4d620849f5859de604bbd59efb7d68bde", network_id,
         mint, 1200.0, 412.5, 0.34375, 1789000000, ts, 2.1, ts))
    conn.commit()


def test_a_4663_detection_reaches_the_hook_as_a_4663_buy():
    """The lane must carry the row's own chain: a 4663 mint routed as Solana buys nothing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _evm_row(conn)
    seen = []

    def hook(_c, ev):
        seen.append(ev)
        return {"status": "filled"}

    asked = []

    def approved_ids(_c, network_id):
        asked.append(network_id)
        return {ARMED_UID}

    _cursor, results = sweep_onchain_detections(
        conn, hook, cursor=0, now_fn=now_iso, max_age_s=45, approved_ids_fn=approved_ids)
    assert len(seen) == 1 and len(results) == 1
    ev = seen[0]
    assert ev.network_id == "4663"
    assert ev.signal_source == "onchain_watch"
    assert ev.side == "buy"
    assert ev.mint == "0x" + "cc" * 20
    assert ev.their_size_usd == 412.5 and ev.their_price == 0.34375
    assert ev.fomo_event_id == "onchain:4663:0xdeadbeef:0xcc"
    assert asked == ["4663"]          # the roster gate is asked for THIS chain, not solana


def test_the_roster_gate_is_asked_per_chain():
    """An armed-on-solana wallet is not armed on 4663; the lane must not mix the two rosters."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _evm_row(conn)
    fired = []

    def approved_ids(_c, network_id):
        return {ARMED_UID} if network_id == "solana" else set()

    sweep_onchain_detections(conn, lambda c, ev: fired.append(ev), cursor=0, now_fn=now_iso,
                            max_age_s=45, approved_ids_fn=approved_ids)
    assert fired == []


def test_the_hook_resolves_a_4663_event_to_its_own_chain():
    """End of the lane's contract: the funded identity resolver accepts the event as 4663."""
    from sniper.services.memecoin_real_runner import resolve_entry_identity
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _evm_row(conn)
    row = conn.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    identity = resolve_entry_identity(conn, OnchainEv(row))
    assert identity.network_id == "4663"
    assert identity.status == "resolved"
    assert identity.source == "explicit_event"


def test_a_stale_4663_detection_is_dropped_by_the_lane_age_gate():
    """onchain_watch skips the follow-lane age cap and the pre-arm cohort check (both branch on
    signal_source == 'follow_activity'), so max_age_s is the ONLY freshness gate a detection has.
    After a watcher gap the backlog must not enter."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _evm_row(conn, age_s=5, signature="4663:0xfresh:0xcc")
    _evm_row(conn, age_s=600, signature="4663:0xstale:0xcc")
    fired = []
    cursor, results = sweep_onchain_detections(
        conn, lambda c, ev: fired.append(ev.fomo_event_id) or {"status": "filled"},
        cursor=0, now_fn=now_iso, max_age_s=45, approved_ids_fn=lambda c, n: {ARMED_UID})
    assert fired == ["onchain:4663:0xfresh:0xcc"]
    # the stale row still advances the cursor: a detection is a moment, not a queue item
    assert cursor == max(r["id"] for r in conn.execute("SELECT id FROM onchain_wallet_detections"))
    assert [r["signature"] for r in results] == ["4663:0xfresh:0xcc"]


def test_a_signature_the_feed_lane_already_attempted_is_skipped_not_paged():
    """FCAT 04253d70 (2026-09-23): J7 ran the buy first and aborted it on the round-trip cap;
    this lane re-delivered the same signature 0.7 s later and paged a wrong reason."""
    sig = "32aP6LWDe3dDRzVQiSKLyGpnot9PGHw4iH9W7imGirswjfywgPj12Cpp1ysJBFTAAHP417V9sLjEAdRbyYAMCwEh"
    conn = conn_with([(sig, ARMED_UID, 5), ("sigFresh", ARMED_UID, 5)])
    started = (NOW - timedelta(seconds=6)).isoformat()
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
        "decision_started_at) VALUES (?,?,?,?)",
        ("e9846c1b", "swap:raybot:%s:7xuao3ug:2ypwt4qk" % sig, "follow_activity", started))
    # This lane's OWN earlier attempt on another signature must not count as a feed attempt.
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
        "decision_started_at) VALUES (?,?,?,?)",
        ("self", "onchain:sigFresh", "onchain_watch", started))
    calls = []
    cursor, results = sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev.fomo_event_id) or {"status": "filled"},
        cursor=0, now_fn=now_iso, max_age_s=60, approved_ids_fn=approved)
    assert calls == ["onchain:sigFresh"]
    assert [r["result"] for r in results if r["signature"] == sig] == [
        {"status": "skipped", "reason": "duplicate_of_feed"}]
    assert cursor == 2


def test_a_duplicate_older_than_the_lookback_does_not_block_and_an_unreadable_table_refuses(
        monkeypatch):
    from sniper.services import memecoin_onchain_lane as O
    monkeypatch.setattr(O, "_OUTAGE", {"down": False})
    monkeypatch.setattr(O, "_page_slot_lane_outage", lambda detail: None)
    sig = "sigOld"
    conn = conn_with([(sig, ARMED_UID, 5)], slot_tables=False)
    calls = []
    # No memecoin_entry_attempts table at all: the duplicate check answers False, but the slot
    # state is unreadable, and that refuses (operator order 2026-10-06), never "no attempt".
    _cursor, results = sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev.fomo_event_id) or {"status": "filled"},
        cursor=0, now_fn=now_iso, max_age_s=60, approved_ids_fn=approved, deferred={})
    assert calls == [] and results[0]["result"]["reason"] == "slot_lane_state_unreadable"
    conn.executescript(SLOT_SCHEMA)
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
        "decision_started_at) VALUES (?,?,?,?)",
                 ("old", "swap:raybot:%s:o:m" % sig, "follow_activity",
                  (NOW - timedelta(seconds=600)).isoformat()))
    calls.clear()
    sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev.fomo_event_id) or {"status": "filled"},
        cursor=0, now_fn=now_iso, max_age_s=60, approved_ids_fn=approved)
    assert calls == ["onchain:sigOld"], "an attempt outside the lookback is not this transaction's"


def test_composite_evm_identity_matches_only_same_chain_coin_wallet_transaction():
    from sniper.services.memecoin_onchain_lane import already_attempted_from_another_lane
    from sniper.persistence import db_memecoin as m
    tx, mint = '0x' + 'ab' * 32, '0x' + 'cc' * 20
    conn = conn_with([], slot_tables=False)
    m.ensure_memecoin_tables(conn)
    conn.execute('INSERT INTO memecoin_entry_attempts(decision_started_at, signal_source, '
                 'origin_event_id, network_id, mint, source_user_id) VALUES(?,?,?,?,?,?)',
                 (now_iso(), 'j7', 'swap:pons:' + tx + ':' + mint, '4663', mint, ARMED_UID))
    signature = '4663:' + tx + ':' + mint
    assert already_attempted_from_another_lane(conn, signature, now_iso())
    for chain, coin, wallet, transaction, expected in (
        ('4663', mint, ARMED_UID, tx, True),
        ('8453', mint, ARMED_UID, tx, False),
        ('4663', '0x' + 'dd' * 20, ARMED_UID, tx, False),
        ('4663', mint, 'another-wallet', tx, False),
        ('4663', mint, ARMED_UID, '0x' + 'ee' * 32, False),
        ('4663', mint, ARMED_UID, '0xabc', False),
    ):
        assert already_attempted_from_another_lane(
            conn, chain + ':' + transaction + ':' + coin, now_iso(),
            source_user_id=wallet) is expected
    _evm_row(conn, signature=signature, mint=mint)
    calls = []
    def sweep():
        return sweep_onchain_detections(conn, lambda c, ev: calls.append(ev) or {},
            cursor=0, now_fn=now_iso, max_age_s=60, approved_ids_fn=approved)
    assert sweep()[1][0]['result']['reason'] == 'duplicate_of_feed'
    assert not calls
    conn.execute("UPDATE memecoin_entry_attempts SET source_user_id='another-wallet'")
    sweep()
    assert len(calls) == 1


def test_the_index_the_evm_dedupe_forces_exists_in_the_ledger_schema():
    """The EVM duplicate check names `INDEXED BY idx_mc_entry_view_started`.

    Without the hint the planner picks `idx_mc_entry_attempt_coin (network_id=?)` -- `LOWER(mint)`
    defeats its second column -- and walks every attempt on the chain: measured on the live ledger
    (23,402 attempts on 4663) at p50 2.371 ms against 0.023 ms with the hint. But a forced index
    that does not exist is a query ERROR, which this lane reads as "not attempted" -- the duplicate
    check would fail open. So the name the lane forces must be one the persistence layer creates.
    """
    import re
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_onchain_lane as lane
    import inspect

    forced = re.findall(r"INDEXED BY (\w+)", inspect.getsource(lane._other_lane_attempt_state))
    assert forced, "the EVM dedupe no longer forces an index; re-measure before removing this test"
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='memecoin_entry_attempts'")}
    for name in forced:
        assert name in present, "forced index %s is not created by the ledger schema" % name


# -- slot-lane fallback (2026-09-26, f4f73eec) --------------------------------------------------
# A confirmed detection whose transaction the SLOT LANE was still handling used to be consumed as
# a duplicate forever, so a slot send that then failed on chain never got an ordinary attempt
# (f4f73eec: detection 21:41:00.12, skipped 21:41:00.78, slot failure 21:41:01.85).

SLOT_SCHEMA = """
CREATE TABLE memecoin_entry_attempts (
    attempt_id TEXT PRIMARY KEY, origin_event_id TEXT, signal_source TEXT,
    decision_started_at TEXT, terminal_status TEXT, terminal_reason TEXT, delivery_lane TEXT,
    tx_sig TEXT, trace_json TEXT);
CREATE TABLE memecoin_pending_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT, status TEXT, resolution_reason TEXT,
    tx_sig TEXT, submission_bound_at TEXT);
"""


def _slot_conn(sig="sigS", age_s=5):
    return conn_with([(sig, ARMED_UID, age_s)])


def _slot_attempt(conn, sig="sigS", *, terminal=None, pending=None, reason=None):
    started = (NOW - timedelta(seconds=6)).isoformat()
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, terminal_status, terminal_reason) VALUES (?,?,?,?,?,?)",
                 ("slot-" + sig, "slotlane:" + sig, "slot_lane", started, terminal, reason))
    if pending is not None:
        conn.execute("INSERT INTO memecoin_pending_entries (attempt_id, status) VALUES (?,?)",
                     ("slot-" + sig, pending))


def _sweep(conn, calls, deferred, *, cursor=0, now=None, max_age_s=45):
    def hook(c, ev):
        calls.append((ev.fomo_event_id, getattr(ev, "delivery_lane", None)))
        return {"status": "refused"}
    return sweep_onchain_detections(
        conn, hook, cursor=cursor, now_fn=(now or now_iso), max_age_s=max_age_s,
        approved_ids_fn=approved, deferred=deferred)


def test_unresolved_slot_attempt_defers_then_one_fallback_after_chain_failure():
    conn = _slot_conn()
    _slot_attempt(conn, pending="reserved")                      # slot send in flight
    calls, deferred = [], {}
    cursor, results = _sweep(conn, calls, deferred)
    assert calls == [] and len(deferred) == 1 and cursor == 1
    assert results[0]["result"] == {"status": "deferred", "reason": "slot_lane_unresolved"}
    conn.execute("UPDATE memecoin_pending_entries SET status='failed'")   # chain_error release
    cursor, results = _sweep(conn, calls, deferred, cursor=cursor)
    assert calls == [("onchain:sigS", "onchain_fallback_after_slot_lane")]
    assert deferred == {} and cursor == 1
    _sweep(conn, calls, deferred, cursor=cursor)                  # nothing more, ever
    assert len(calls) == 1


@pytest.mark.parametrize("pending", ["reserved", "confirmed", "failed"])
def test_only_a_released_graduation_marks_the_fallback_for_fast_requote(pending):
    # The release commits before trace.finish: the pending row is already authoritative while
    # the attempt's terminal reason is still absent (INSTINCT 7f2098c3, 2026-10-05).
    conn = _slot_conn()
    _slot_attempt(conn, pending=pending)
    conn.execute("UPDATE memecoin_pending_entries SET resolution_reason=?",
                 ("chain_error:BondingCurveComplete",))
    seen = []
    sweep_onchain_detections(
        conn, lambda c, ev: seen.append(ev) or {"status": "aborted"}, cursor=0,
        now_fn=now_iso, max_age_s=45, approved_ids_fn=approved, deferred={})
    if pending != "failed":
        assert seen == []
    else:
        assert len(seen) == 1
        assert seen[0].slot_lane_failure_reason == "chain_error:BondingCurveComplete"
        assert seen[0].fomo_event_id == "onchain:sigS"


def test_native_slippage_does_not_retry_as_graduation_or_ordinary_fallback():
    conn = _slot_conn()
    _slot_attempt(conn, pending="failed", terminal="failed",
                  reason="chain_error:BuySlippageBelowMinTokensOut")
    seen = []
    sweep_onchain_detections(
        conn, lambda c, ev: seen.append(ev) or {}, cursor=0, now_fn=now_iso,
        max_age_s=45, approved_ids_fn=approved, deferred={})
    assert seen == []


def test_a_slot_fill_suppresses_the_fallback_forever():
    conn = _slot_conn()
    _slot_attempt(conn, terminal="filled", pending="confirmed")
    calls, deferred = [], {}
    _, results = _sweep(conn, calls, deferred)
    assert calls == [] and deferred == {}
    assert results[0]["result"] == {"status": "skipped", "reason": "slot_lane_filled"}


def test_a_slot_refusal_before_reserving_is_the_decision():
    conn = _slot_conn()
    _slot_attempt(conn, terminal="rejected")                      # no reservation row
    calls, deferred = [], {}
    _, results = _sweep(conn, calls, deferred)
    assert calls == [] and deferred == {}
    assert results[0]["result"] == {"status": "skipped", "reason": "duplicate_of_slot_lane"}


def test_an_unknown_slot_outcome_expires_with_the_detection_never_fires():
    conn = _slot_conn(age_s=40)
    _slot_attempt(conn, pending="reserved")                       # ambiguous send: may still land
    calls, deferred = [], {}
    cursor, _ = _sweep(conn, calls, deferred)
    later = lambda: (NOW + timedelta(seconds=10)).isoformat()     # detection now 50 s old
    _, results = _sweep(conn, calls, deferred, cursor=cursor, now=later)
    assert calls == [] and deferred == {}
    assert results[0]["result"] == {"status": "skipped", "reason": "slot_fallback_expired"}


def test_after_a_restart_the_ledger_prevents_a_second_fallback():
    # The deferral dict dies with the process; startup_cursor re-reads fresh rows. The fallback
    # attempt already in the ledger must stop a second one.
    conn = _slot_conn()
    _slot_attempt(conn, pending="failed")
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, decision_started_at, terminal_status) VALUES (?,?,?,?,?)",
                 ("fb", "onchain:sigS", "onchain_watch", (NOW - timedelta(seconds=2)).isoformat(),
                  "refused"))
    calls = []
    _, results = _sweep(conn, calls, {})
    assert calls == []
    assert results[0]["result"] == {"status": "skipped", "reason": "slot_fallback_already_attempted"}


def test_a_feed_attempt_still_wins_over_a_slot_fallback():
    conn = _slot_conn()
    _slot_attempt(conn, pending="failed")
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, decision_started_at, terminal_status) VALUES (?,?,?,?,?)",
                 ("j7", "swap:raybot:sigS:x:y", "follow_activity",
                  (NOW - timedelta(seconds=3)).isoformat(), "refused"))
    calls = []
    _, results = _sweep(conn, calls, {})
    assert calls == []
    assert results[0]["result"] == {"status": "skipped", "reason": "duplicate_of_feed"}


def test_the_deferral_is_bounded_by_count():
    from sniper.services import memecoin_onchain_lane as lane
    conn = _slot_conn()
    _slot_attempt(conn, pending="reserved")
    full = {("other%d" % i, ARMED_UID, "solana", "M"): {
        "id": 0, "signature": "other%d" % i, "source_user_id": ARMED_UID, "handle": "h",
        "network_id": "solana", "mint": "M", "source_size_usd": None, "source_price": None,
        "detected_at": now_iso()} for i in range(lane.MAX_SLOT_DEFERRED)}   # unresolved: kept
    calls = []
    _, results = _sweep(conn, calls, full)
    assert calls == [] and len(full) == lane.MAX_SLOT_DEFERRED
    assert results[0]["result"] == {"status": "skipped", "reason": "slot_fallback_capacity"}


def test_a_presign_refusal_is_provably_unsent_and_falls_back_once():
    # Build refused / signal_too_old / head_past: release_pending before any byte left -> failed.
    conn = _slot_conn()
    _slot_attempt(conn, terminal="aborted", pending="failed")
    calls = []
    _, results = _sweep(conn, calls, {})
    assert calls == [("onchain:sigS", "onchain_fallback_after_slot_lane")]


def test_a_bound_reserved_slot_send_never_falls_back_even_at_44s_and_expires():
    conn = _slot_conn(age_s=44)
    _slot_attempt(conn, pending="reserved")                       # signed + bound: may land
    calls, deferred = [], {}
    cursor, results = _sweep(conn, calls, deferred)                # detection 44 s old
    assert results[0]["result"]["status"] == "deferred" and calls == []
    at_45 = lambda: (NOW + timedelta(seconds=1)).isoformat()
    _, results = _sweep(conn, calls, deferred, cursor=cursor, now=at_45)
    assert results == [] and calls == [] and len(deferred) == 1   # 45.0 s: still fresh, held
    at_46 = lambda: (NOW + timedelta(seconds=2)).isoformat()
    _, results = _sweep(conn, calls, deferred, cursor=cursor, now=at_46)
    assert calls == [] and deferred == {}
    assert results[0]["result"] == {"status": "skipped", "reason": "slot_fallback_expired"}


def test_crash_restart_loses_the_deferral_rereads_the_row_and_attempts_once():
    conn = _slot_conn()
    _slot_attempt(conn, pending="reserved")
    calls = []

    def hook(c, ev):                        # the real hook writes its attempt row
        calls.append(ev.fomo_event_id)
        c.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, decision_started_at, terminal_status) VALUES (?,?,?,?,?)",
                  ("fb%d" % len(calls), ev.fomo_event_id, "onchain_watch", NOW.isoformat(),
                   "refused"))
        return {"status": "refused"}

    def sweep(cursor, deferred):
        return sweep_onchain_detections(conn, hook, cursor=cursor, now_fn=now_iso, max_age_s=45,
                                        approved_ids_fn=approved, deferred=deferred)
    cursor, _ = sweep(0, {})                                  # deferred in process 1 ...
    conn.execute("UPDATE memecoin_pending_entries SET status='failed'")
    # ... crash before the next sweep: the dict is gone, startup_cursor keeps the fresh row.
    restart_cursor = startup_cursor(conn, now_fn=now_iso, max_age_s=45)
    assert restart_cursor == 0
    sweep(restart_cursor, {})                                  # process 2: one fallback
    sweep(restart_cursor, {})                                  # another restart: none more
    assert calls == ["onchain:sigS"]


def test_a_scaled_clip_refusal_before_reserving_falls_back_once():
    """The slot lane refuses a regime-scaled clip before reserving (its prefetched leg is sized for
    the full clip). Nothing was signed, so the watcher gives that signature ONE ordinary attempt,
    which quotes the scaled clip itself; J7 relays none of the pump: wallets the lane serves."""
    from sniper.services.memecoin_slot_lane import SCALED_CLIP_REASON
    conn = _slot_conn()
    _slot_attempt(conn, terminal="rejected", reason=SCALED_CLIP_REASON)     # no reservation row
    calls, deferred = [], {}
    cursor, results = _sweep(conn, calls, deferred)
    assert calls == [("onchain:sigS", "onchain_fallback_after_slot_lane")] and deferred == {}
    _sweep(conn, calls, deferred, cursor=cursor)                            # nothing more
    assert len(calls) == 1


def test_a_trial_refusal_before_reserving_falls_back_once():
    """Trial arm (spec 2026-10-01): the slot lane cannot carry a trial's clip and marker, so the
    reservation refuses it as trial_unmarked with nothing held. One ordinary attempt follows, which
    the entry hook sizes to the trial clip and marks."""
    from sniper.services.memecoin_trial import TRIAL_UNMARKED
    conn = _slot_conn()
    _slot_attempt(conn, terminal="rejected", reason=TRIAL_UNMARKED + ": open trial #3 on solana")
    calls, deferred = [], {}
    cursor, _results = _sweep(conn, calls, deferred)
    assert calls == [("onchain:sigS", "onchain_fallback_after_slot_lane")] and deferred == {}
    _sweep(conn, calls, deferred, cursor=cursor)
    assert len(calls) == 1


# --- BACKFILLED DETECTIONS ARE AGED BY THEIR SOURCE BLOCK (2026-09-28, Alpha) -------------------
# A gap-backfill row's detected_at is when the backfill READ it; the buy happened at its block's
# time, up to 45 s earlier. The row carries that time in block_time plus backfill=true, and the lane
# (freshness, OnchainEv.created_at -> is_stale_entry and the hook's source age) ages it by the block.
# Live rows keep today's arrival stamp byte for byte, even when they carry a block_time.

def _bf_row(conn, sig, *, arrival_age_s, block_age_s, backfill=True):
    import json as _json
    arrival = (NOW - timedelta(seconds=arrival_age_s)).isoformat()
    conn.execute(
        "INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, mint, "
        "block_time, detected_at, created_at, source_valuation_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (sig, ARMED_UID, "h", "addr", "Mint" + sig, int((NOW - timedelta(seconds=block_age_s)).timestamp()),
         arrival, arrival, _json.dumps({"basis": "evm_onchain_watch", "backfill": backfill})))


def _bf_sweep(conn):
    calls = []
    sweep_onchain_detections(conn, lambda c, e: calls.append(e) or {"status": "filled"}, cursor=0,
                             now_fn=now_iso, max_age_s=45, approved_ids_fn=approved)
    return calls


def test_a_backfilled_buy_40s_old_reads_40s_old():
    conn = conn_with([])
    _bf_row(conn, "bf40", arrival_age_s=1, block_age_s=40)
    calls = _bf_sweep(conn)
    assert len(calls) == 1
    assert calls[0].created_at == (NOW - timedelta(seconds=40)).isoformat()   # the block, not arrival
    assert calls[0].received_at == (NOW - timedelta(seconds=1)).isoformat()   # when we read it


def test_a_backfilled_buy_50s_old_is_dropped():
    conn = conn_with([])
    _bf_row(conn, "bf50", arrival_age_s=1, block_age_s=50)
    assert _bf_sweep(conn) == []


def test_a_backfill_row_without_a_block_time_never_fires():
    conn = conn_with([])
    _bf_row(conn, "bfnone", arrival_age_s=1, block_age_s=0)
    conn.execute("UPDATE onchain_wallet_detections SET block_time=NULL WHERE signature='bfnone'")
    assert _bf_sweep(conn) == []


def test_a_live_row_keeps_its_arrival_stamp_even_with_a_block_time():
    conn = conn_with([])
    _bf_row(conn, "live", arrival_age_s=5, block_age_s=300, backfill=False)
    calls = _bf_sweep(conn)
    assert len(calls) == 1 and calls[0].created_at == (NOW - timedelta(seconds=5)).isoformat()


def test_a_retry_round_trip_keeps_every_event_field(monkeypatch):
    """Codex review 2026-10-06: the retry payload dropped source_program and source_pool, so a
    pump curve buy proved a pump mint on its first attempt and not on its retry (the history
    mcap's 1e9 supply), and the implied quote reserve telemetry. Every field must survive."""
    import json as _json
    from sniper.services import memecoin_onchain_lane as lane
    from sniper.services.memecoin_slot_signal import PUMP_PROGRAM
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    valuation = {"program_id": PUMP_PROGRAM, "pool": "BondingCurve1111", "basis": "pump_event_net_sol",
                 "gross_swap_size_usd": 101.0, "source_post_price": 0.00012, "impact_leg": "pump_amm",
                 "source_impact_ratio": 1.2, "source_size_usd": 100.0, "source_initiation": {"k": 1}}
    ev = lane.OnchainEv({"mint": "A71Uf3jwg57fNedSao9eFwm9eKSLYFbVJ85T4fuXpump", "source_user_id": "u",
                         "handle": "h", "network_id": "solana", "source_size_usd": 100.0,
                         "source_price": 0.0001, "source_valuation_json": _json.dumps(valuation),
                         "signature": "Sig1", "detected_at": "2026-10-06T20:30:00+00:00",
                         "address": "Wallet1", "source_slot": 123})
    back = lane.event_from_retry_payload(_json.loads(_json.dumps(lane.retry_payload(ev))))
    assert lane.pump_mint_proven(ev) and lane.pump_mint_proven(back)
    keys = (set(vars(ev)) | set(vars(back))) - {"native_failure_fallback"}  # rebuild sets False
    assert {k: (getattr(ev, k, None), getattr(back, k, None)) for k in keys
            if getattr(ev, k, None) != getattr(back, k, None)} == {}
