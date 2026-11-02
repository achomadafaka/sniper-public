"""Fill timing where the operator reads it (spec rev 2, 2.6): the Telegram entry alert line, computed at
send time from what is stored (no hold), degrading to "resolving" with no timing file (Gamma LOW1)."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_fill_timing as ft
from test_memecoin_entry_view import NOW, attempt, fill, view

SRC = "5uRiphjyAVbVVhFEeBWTFfMBsWfTidMJRtZBUwBR2DsGdp11QLAYpkY2rhEpGSooHTk4"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))       # no timing file yet: first boot
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def _slot_trace(source, landing):
    return json.dumps({"source_evidence": {"source_slot": source},
                       "submission_events": [{"stage": "chain_confirmation", "landing_slot": landing}]})


def test_a_slot_lane_fill_view_carries_landed_s_plus_one(conn):
    attempt(conn, status="filled", origin="slotlane:" + SRC, signal_source="slot_lane",
            delivery_lane="slot_lane_processed", tx_sig="signature", trace_json=_slot_trace(100, 101))
    fill(conn, origin="slotlane:" + SRC)
    assert view(conn, attempt_id="a1")["fill_timing_line"] == "Landed S+1"


def test_an_onchain_fill_names_the_slot_lane_and_the_largest_stage(conn):
    m.record_onchain_detection(conn, signature=SRC, source_user_id="u", handle="alice", address="A", mint="MINT",
                               detected_at=NOW, created_at=NOW, source_slot=100)
    trace = json.dumps({"substage_events": [{"name": "attempt_row", "head_slot": 103, "head_age_ms": 20}],
                        "submission_events": [{"stage": "chain_confirmation", "landing_slot": 105}]})
    attempt(conn, status="filled", origin="onchain:" + SRC, signal_source="onchain_watch", tx_sig="signature",
            trace_json=trace)
    fill(conn, origin="onchain:" + SRC)
    line = view(conn, attempt_id="a1")["fill_timing_line"]
    assert line == "Landed S+5 · slot lane never saw it · no stage measured"


def test_a_j7_fill_without_its_source_slot_says_resolving(conn):
    attempt(conn, status="filled", origin="swap:raybot:%s:addr:mint" % SRC, signal_source="follow_activity",
            tx_sig="signature", trace_json=json.dumps({"submission_events": [
                {"stage": "chain_confirmation", "landing_slot": 105}]}))
    fill(conn, origin="swap:raybot:%s:addr:mint" % SRC)
    assert view(conn, attempt_id="a1")["fill_timing_line"] == "Landed S+? (source slot resolving)"


def test_a_refusal_has_no_line(conn):
    attempt(conn, status="refused", origin="onchain:" + SRC, signal_source="onchain_watch")
    assert view(conn, attempt_id="a1")["fill_timing_line"] == ""


def test_the_alert_prints_the_line_after_the_times():
    from sniper.services.memecoin_telegram import render_entry_notification
    base = {"status": "filled", "network_id": "solana", "ticker": "COIN", "source_handle": "alice",
            "source_size_usd": 100.0, "filled_notional": 20.0, "filled_price": 0.001,
            "observed_at": NOW, "decision_started_at": NOW}
    text = render_entry_notification(dict(base, fill_timing_line="Landed S+7 · slot lane: handoff pool_unknown"))
    lines = text.splitlines()
    assert "Landed S+7 · slot lane: handoff pool_unknown" in lines
    assert lines.index("Landed S+7 · slot lane: handoff pool_unknown") > next(
        i for i, l in enumerate(lines) if l.startswith("Times (UTC)"))
    assert "Landed" not in render_entry_notification(dict(base, fill_timing_line=""))


def test_a_broken_lookup_degrades_and_never_raises(conn, monkeypatch):
    monkeypatch.setattr(ft, "detection_of", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    line = ft.view_line(conn, {"terminal_status": "filled", "tx_sig": "x", "network_id": "solana",
                              "origin_event_id": "onchain:" + SRC})
    assert line == "Landed S+? (resolving)"


# --- the dashboard (spec 2.6): the Real positions row and the Copy tab's "Fills today" -------------

def _route_client(monkeypatch, tmp_path):
    from test_memecoin_trade_names import _client, _no_dex
    from sniper.services import memecoin_coin_names as coin_names
    monkeypatch.setattr(coin_names, "_default_batch", _no_dex)
    return _client(monkeypatch, tmp_path)


def test_an_open_position_carries_its_fill_timing_line(monkeypatch, tmp_path):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    cl, c = _route_client(monkeypatch, tmp_path)
    attempt(c, aid="a9", status="filled", origin="slotlane:" + SRC, signal_source="slot_lane",
            tx_sig="signature", trace_json=_slot_trace(100, 102))
    m.open_position(c, source_user_id="u1", source_handle="alice", mint="MINT", ticker="COIN",
                    entry_price=0.6, entry_notional=100.0, entry_fill_source="chain", entry_lag_s=0.5,
                    their_size_usd=500.0, opened_at=NOW, strategy="alpha_b", execution_mode="real",
                    network_id="solana", signal_source="slot_lane", attempt_id="a9")
    rows = cl.get("/api/memecoin/real/positions").get_json()["positions"]
    assert rows[0]["fill_timing"] == "Landed S+2 · no stage measured"


def test_fills_today_lists_the_resolver_rows_with_the_attempt(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    cl, c = _route_client(monkeypatch, tmp_path)
    assert cl.get("/api/memecoin/fill-timing/today").get_json() == {"rows": [], "note": "no timing file yet"}
    today = datetime.now(timezone.utc).isoformat()
    attempt(c, aid="a9", status="filled", origin="slotlane:" + SRC, signal_source="slot_lane",
            tx_sig="signature", ticker="COIN", completed_at=today)
    t = ft.connect(tmp_path / "data" / "memecoin_fill_timing.db")
    t.execute("INSERT INTO memecoin_fill_timing (attempt_id, network_id, lane, source_slot, landing_slot, gap, cause, "
              "cause_detail, stages_json, state, completed_at) VALUES ('a9','solana','slot_lane',100,101,1,'on_time',"
              "'largest: land 1 slots','{}','complete',?)", (today,))
    t.commit()
    body = cl.get("/api/memecoin/fill-timing/today").get_json()
    (row,) = body["rows"]
    assert (row["ticker"], row["gap"], row["cause"], row["line"]) == ("COIN", 1, "on_time", "Landed S+1")


def test_fills_today_refreshes_solana_confirmation_observation_without_rewriting_cache(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    cl, c = _route_client(monkeypatch, tmp_path)
    today = datetime.now(timezone.utc).isoformat()
    start = datetime.fromisoformat(NOW)
    trace = json.loads(_slot_trace(100, 100))
    trace["substage_events"] = [{"name": "slot_receipt", "source_slot": 100, "head_at_recv": 98},
                                {"name": "slot_pre_send", "head_slot": 98}]
    reason = "chain_error:BuySlippageBelowMinBaseAmountOut"
    attempt(c, aid="oracle", status="failed", origin="slotlane:" + SRC,
            signal_source="slot_lane", tx_sig="signature", completed_at=today,
            transaction_submitted_at=(start + timedelta(milliseconds=48)).isoformat(),
            chain_confirmation_completed_at=(start + timedelta(milliseconds=535)).isoformat(),
            terminal_reason=reason, trace_json=json.dumps(trace))
    t = ft.connect(tmp_path / "data" / "memecoin_fill_timing.db")
    cached = reason + " · largest: land 2 slots"
    t.execute("INSERT INTO memecoin_fill_timing (attempt_id, network_id, stages_json, completed_at, state, "
              "source_slot, landing_slot, gap, cause, cause_detail) "
              "VALUES ('oracle','solana',?,?,'complete',100,100,0,'price',?)",
              (json.dumps({"detect": -2, "decide_ms": 48, "land_slots": 2, "land_ms": 5}), today, cached))
    t.commit()
    (row,) = cl.get("/api/memecoin/fill-timing/today").get_json()["rows"]
    assert row["line"] == "Landed S+0 · " + reason + " · largest: confirmation observed 487 ms"
    assert row["detail"].endswith("largest: confirmation observed 487 ms")
    assert row["cause"] == "price"
    assert row["stages"]["land_ms"] == pytest.approx(487)
    assert row["stages"]["land_slots"] == 2 and row["stages"]["detect"] == -2
    assert t.execute("SELECT cause_detail FROM memecoin_fill_timing WHERE attempt_id='oracle'").fetchone()[0] == cached
    t.close()


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_fills_today_refreshes_completed_yh_duplicate_explanation(monkeypatch, tmp_path, refresh_fails):
    from datetime import datetime, timezone
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    cl, c = _route_client(monkeypatch, tmp_path)
    today = datetime.now(timezone.utc).isoformat()
    ref, mint = "0xsource", "0xmint"
    submitted, decision = "2026-10-07T15:45:54.036452+00:00", "2026-10-07T15:45:54.165138+00:00"
    attempt(c, aid="yh", chain="56", status="filled", origin="swap:raybot:%s:addr:%s" % (ref, mint),
            mint=mint, signal_source="follow_activity", tx_sig="ours", completed_at=today,
            observed_at="2026-10-07T15:45:53.096867+00:00", transaction_submitted_at=submitted,
            chain_confirmation_completed_at="2026-10-07T15:45:54.409511+00:00")
    attempt(c, aid="duplicate", chain="56", status="refused", origin="onchain:56:%s:%s" % (ref, mint),
            mint=mint, signal_source="onchain_watch", decision_started_at=decision,
            terminal_reason="unresolved signed transaction on 56")
    stages = {"detect": 1.096867, "decide_ms": 939.585, "land_ms": 373.059}
    with ft.connect(tmp_path / "data" / "memecoin_fill_timing.db") as t:
        t.execute("INSERT INTO memecoin_fill_timing (attempt_id,network_id,source_slot,landing_slot,gap,"
                  "source_time,cause,cause_detail,stages_json,state,completed_at) "
                  "VALUES ('yh','56',100,104,4,1791387952,'fast_lane_skipped',?,?, 'complete',?)",
                  ("bsc lane: hook:rejected:unresolved signed transaction on 56", json.dumps(stages), today))
    with sqlite3.connect(tmp_path / "data" / "memecoin_bsc_fast_lane.db") as lane:
        lane.execute("CREATE TABLE bsc_lane_events (id INTEGER,tx_hash TEXT,mint TEXT,reason TEXT,written_at TEXT)")
        lane.execute("INSERT INTO bsc_lane_events VALUES (1,?,?,?,?)",
                     (ref, mint, "hook:rejected:unresolved signed transaction on 56",
                      "2026-10-07T15:45:54.171553+00:00"))
    if refresh_fails:
        monkeypatch.setattr(ft, "_details", lambda *_args: (_ for _ in ()).throw(RuntimeError("unavailable")))
    (row,) = cl.get("/api/memecoin/fill-timing/today").get_json()["rows"]
    assert row["gap"] == 4
    if refresh_fails:
        assert row["stages"] == stages
    else:
        assert {key: row["stages"][key] for key in stages} == pytest.approx(stages)
    if refresh_fails:
        assert row["cause"] == "fast_lane_skipped"
        assert row["line"].startswith("Landed +4 blocks · bsc lane: hook:rejected:")
        return
    assert row["cause"] == "decide_slow"
    assert row["line"] == ("Landed +4 blocks · BSC duplicate suppressed after broadcast · "
                           "source block-to-receipt 1.1 s (coarse) · largest: detect-to-submit 940 ms")
    assert row["detail"] == ("BSC duplicate suppressed after broadcast · "
                             "source block-to-receipt 1.1 s (coarse) · largest: detect-to-submit 940 ms")


def test_a_raising_timing_line_never_breaks_the_view(conn, monkeypatch):
    """Gamma LOW1: the call site has its own guard."""
    monkeypatch.setattr(ft, "view_line", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    attempt(conn, status="filled", origin="slotlane:" + SRC, signal_source="slot_lane",
            tx_sig="signature", trace_json=_slot_trace(100, 101))
    fill(conn, origin="slotlane:" + SRC)
    assert view(conn, attempt_id="a1")["fill_timing_line"] == "Landed S+? (resolving)"


def test_closed_history_keeps_its_own_fill_timing(monkeypatch, tmp_path):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    cl, c = _route_client(monkeypatch, tmp_path)
    attempt(c, aid="old-fill", status="filled", origin="slotlane:" + SRC,
            signal_source="slot_lane", tx_sig="signature", trace_json=_slot_trace(100, 102))
    pid = m.open_position(c, source_user_id="u1", source_handle="alice", mint="MINT", ticker="COIN",
                          entry_price=0.6, entry_notional=100, entry_fill_source="chain", entry_lag_s=0.5,
                          their_size_usd=500, opened_at=NOW, strategy="alpha_b", execution_mode="real",
                          network_id="solana", signal_source="slot_lane", attempt_id="old-fill")
    m.close_position(c, pid, NOW, 5, "trail")
    t = ft.connect(tmp_path / "data" / "memecoin_fill_timing.db")
    t.execute("INSERT INTO memecoin_fill_timing (attempt_id, network_id, stages_json, completed_at, state, "
              "source_slot, landing_slot, gap, cause, cause_detail) "
              "VALUES (?, ?, ?, ?, 'complete',100,102,2,'land_slow','largest: land 1 slots')", ("old-fill", "solana", json.dumps(
                  {"detect": 0, "decide_ms": 12, "land_slots": 1}), NOW))
    t.commit()
    t.close()
    row = cl.get("/api/memecoin/real/closed").get_json()["closed"][0]
    assert row["fill_timing"]["line"] == "Landed S+2 · largest: detect-to-submit 12 ms"
    assert row["fill_timing"]["stages"]["detect"] == 0
    assert row["fill_timing"]["stages"]["land_slots"] == 1
    assert row["fill_timing"]["stages"]["decide_ms"] == 12
