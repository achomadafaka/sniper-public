"""run_once: curated thesis capture + Telegram front-run alert — both best-effort, gated,
neither may break the copy path."""
import queue
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_runner as run
from sniper.settings import memecoin as cfg


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


class _Auth:
    access = "t"; refresh = "r"; ca_id = "c"
    def refresh_token(self, http=None): return "t"


def _buy(eid, uid, handle="alice", mint=None, size=3000.0):
    return {"id": eid, "type": "large_buy", "userId": uid, "tokenAddress": mint or ("MINT" + eid),
            "createdAt": "2026-07-08T10:00:00Z",
            "body": {"userHandle": handle, "ticker": "PEPE", "price": 0.005,
                     "marketCap": "1000000", "currentSizeUsd": size, "isFirstBuy": True}}


def _thesis(tid, uid, handle="alice", mint="MINTX", comment="conviction hold", pnl=-30.0):
    return {"id": tid, "type": "thesis_created", "userId": uid, "tokenAddress": mint,
            "networkId": "1", "createdAt": "2026-07-08T10:00:00Z",
            "body": {"userHandle": handle, "commentId": "c-" + tid, "ticker": "PEPE",
                     "comment": comment, "percentageUnrealizedPnl": pnl,
                     "positionNotionalUsd": 1000.0, "marketCap": 1e6, "price": 0.005}}


def _http_for(feed):
    def http(method, url, headers=None, body=None):
        return 200, {"responseObject": {"feed": feed}}, "Wed, 08 Jul 2026 10:00:01 GMT"
    return http


def _run(c, feed, alert_fn=None):
    return run.run_once(c, _Auth(), 100.0, http=_http_for(feed),
                        now_fn=lambda: "2026-07-08T17:20:00Z",
                        fill_fn=lambda *a, **k: (0.005, "jupiter"), alert_fn=alert_fn)


# ---- thesis capture ----

def test_captures_curated_thesis_not_noncurated():
    c = _conn()
    _run(c, [_buy("b1", "u1"), _thesis("t1", "u1"), _thesis("t2", "u9", handle="stranger")])
    rows = m.list_theses(c)
    assert [r["fomo_thesis_id"] for r in rows] == ["t1"]      # only curated u1


def test_thesis_capture_gated_off(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_THESIS_ENABLED", False)
    _run(c, [_thesis("t1", "u1")])
    assert m.list_theses(c) == []


def test_thesis_capture_failure_does_not_break_copy(monkeypatch):
    c = _conn()
    monkeypatch.setattr(m, "record_thesis", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _run(c, [_buy("b1", "u1"), _thesis("t1", "u1")])
    assert r["opened"] == 1        # buy still copied despite thesis capture raising


# ---- clock domain: position hold-stamps unified on real-UTC (deep review P2) ----

def test_run_once_stamps_opened_at_on_real_utc_not_host():
    # The runner stamped opened_at with the HOST clock (+7h) while the web stamps closed_at on
    # corrected real-UTC -> a runner-opened, web-closed position had a NEGATIVE hold. opened_at
    # now uses real-UTC; the pump path's received_at stays on the host clock (deliberate coupling).
    c = _conn()
    run.run_once(c, _Auth(), 100.0, http=_http_for([_buy("b1", "u1")]),
                 now_fn=lambda: "2026-07-08T17:20:00Z",        # host (+7h) -> received_at / pump
                 real_now_fn=lambda: "2026-07-08T10:20:00Z",   # corrected real-UTC -> hold stamp
                 fill_fn=lambda *a, **k: (0.005, "jupiter"))
    pos = m.get_open_position(c, "u1", "MINTb1")
    assert pos["opened_at"] == "2026-07-08T10:20:00Z"


def test_open_and_sweep_close_share_clock_so_hold_is_nonnegative():
    from datetime import datetime as _dt
    from sniper.services.memecoin_exits import sweep_exits
    c = _conn()
    run.run_once(c, _Auth(), 100.0, http=_http_for([_buy("b1", "u1")]),
                 now_fn=lambda: "2026-07-08T17:20:00Z",
                 real_now_fn=lambda: "2026-07-08T10:20:00Z",
                 fill_fn=lambda *a, **k: (0.005, "jupiter"))
    # exit sweep closes at real-UTC 10 minutes later (a -70% crash -> stop_loss)
    sweep_exits(c, {"MINTb1": {"price": 0.0015, "liquidity": 1e15}}, "2026-07-08T10:30:00Z")
    pos = dict(c.execute("SELECT * FROM memecoin_positions WHERE mint='MINTb1' AND strategy='control'").fetchone())
    assert pos["is_open"] == 0
    hold = (_dt.fromisoformat(pos["closed_at"]) - _dt.fromisoformat(pos["opened_at"])).total_seconds()
    assert hold >= 0 and abs(hold - 600.0) < 1e-6      # 10 min, not the -7h illusion


def test_bracket_pass_stamps_close_on_real_utc_by_default(monkeypatch):
    monkeypatch.setattr("sniper.util.clock.real_utc_now_iso", lambda *a, **k: "2026-07-08T10:00:00Z")
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="MZ", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-07T20:00:00+00:00", strategy="alpha_b")   # 14h before
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="j", fraction=1.0, created_at="2026-07-07T20:00:00+00:00")
    run.bracket_pass(c, price_fn=lambda mints: {})    # no quote -> b_timecap fires; DEFAULT clock
    pos = m.get_position(c, pid)
    assert pos["is_open"] == 0 and pos["closed_at"] == "2026-07-08T10:00:00Z"   # real-UTC, not host


# ---- telegram alert ----

def test_alert_off_by_default(monkeypatch):
    # Pin the gate explicitly (hermetic) — the sibling tests pin it True, so this one must pin False
    # rather than trust the ambient module default, which another test's env load + settings reload
    # can pollute (analyze_fomo_accounts used to load .fomo.env at import; MEMECOIN_TG_ALERTS=1 there).
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", False)
    c = _conn()
    calls = []
    _run(c, [_buy("b1", "u1")], alert_fn=lambda text: (calls.append(text) or (True, 5.0)))
    assert calls == []                                        # gate off -> no front-run alert
    assert c.execute("SELECT COUNT(*) FROM memecoin_alert_latency").fetchone()[0] == 0


def test_alert_fires_once_per_curated_buy(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    calls = []
    _run(c, [_buy("b1", "u1")], alert_fn=lambda text: (calls.append(text) or (True, 7.5)))
    assert len(calls) == 1 and "PEPE" in calls[0]
    row = dict(c.execute("SELECT * FROM memecoin_alert_latency WHERE fomo_event_id='b1'").fetchone())
    assert row["send_ok"] == 1 and row["send_ms"] == 7.5 and row["alert_sent_at"]
    # detected_at + alert_sent_at both CORRECTED real-UTC (same clock domain -> diffable);
    # local_ms measures our receipt->send-start gap. lag_s is fomo-delta (real UTC).
    assert row["detected_at"] and row["local_ms"] is not None
    from datetime import datetime as _dt
    d = _dt.fromisoformat(row["detected_at"]); s = _dt.fromisoformat(row["alert_sent_at"])
    assert (s - d).total_seconds() >= -1.0      # sent at/after detected (no 7h illusory gap)


def test_alert_dedups_on_redelivery(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    calls = []
    feed = [_buy("b1", "u1")]
    _run(c, feed, alert_fn=lambda t: (calls.append(t) or (True, 1.0)))
    _run(c, feed, alert_fn=lambda t: (calls.append(t) or (True, 1.0)))   # same event re-delivered
    assert len(calls) == 1


def test_alert_per_poll_cap(monkeypatch):
    c = _conn()
    for uid in ("u1", "u2", "u3"):
        m.add_wallet(c, uid, uid)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_MAX_PER_POLL", 2)
    calls = []
    r = _run(c, [_buy("b1", "u1"), _buy("b2", "u2"), _buy("b3", "u3")],
             alert_fn=lambda t: (calls.append(t) or (True, 1.0)))
    assert len(calls) == 2                                    # capped
    assert r["alerts"] == 2 and r["alerts_dropped"] == 1      # the 3rd is dropped + counted


def test_no_alert_on_sell_or_noncurated(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    calls = []
    sell = {"id": "s1", "type": "large_sell", "userId": "u1", "tokenAddress": "MINTs1",
            "createdAt": "2026-07-08T10:00:00Z",
            "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                     "marketCap": "1000000", "currentSizeUsd": 3000.0}}
    _run(c, [sell, _buy("b9", "u9", handle="stranger")],
         alert_fn=lambda t: (calls.append(t) or (True, 1.0)))
    assert calls == []


def test_alert_attaches_thesis(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    # pre-seed a thesis for (u1, MINTb1) so the buy alert can attach it
    m.record_thesis(c, {"fomo_thesis_id": "pre", "comment_id": "c", "source_user_id": "u1",
                        "handle": "alice", "mint": "MINTb1", "ticker": "PEPE",
                        "comment": "accumulating the dip", "has_link": False,
                        "their_pnl_pct": -12.0, "their_realized_pnl_pct": 0.0,
                        "their_notional_usd": 1000.0, "their_unrealized_pnl_usd": -120.0,
                        "market_cap": 1e6, "fdv": 2e6, "price": 0.005, "tag": None,
                        "network_id": "1", "swap_id": None, "trade_id": None,
                        "created_at": "2026-07-08T09:00:00Z", "received_at": "2026-07-08T16:00:00Z",
                        "lag_s": 1.0, "signal_source": "global_feed"})
    calls = []
    _run(c, [_buy("b1", "u1", mint="MINTb1")], alert_fn=lambda t: (calls.append(t) or (True, 1.0)))
    assert len(calls) == 1 and "accumulating the dip" in calls[0]


def test_alert_async_enqueues_send_off_copy_path(monkeypatch):
    # P1 (deep review): the Telegram send was synchronous in run_once (8s timeout x per-poll cap
    # = ~40s worst-case stall of the copy path + exit sweep). With alert_queue, run_once dedups +
    # builds the message synchronously (detection persisted) but hands the blocking SEND to a
    # sender thread -> the copy path never blocks. The sender records the send latency later.
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    calls = []
    q = queue.Queue()
    r = run.run_once(c, _Auth(), 100.0, http=_http_for([_buy("b1", "u1")]),
                     now_fn=lambda: "2026-07-08T17:20:00Z", fill_fn=lambda *a, **k: (0.005, "jupiter"),
                     alert_fn=lambda t: (calls.append(t) or (True, 8.0)), alert_queue=q)
    assert calls == []                         # SEND was not invoked on the copy path
    assert r["alerts"] == 1
    row = dict(c.execute("SELECT * FROM memecoin_alert_latency WHERE fomo_event_id='b1'").fetchone())
    assert row["detected_at"] and row["alert_sent_at"] is None   # detection sync, send pending
    # the sender thread drains the job -> real send + records latency
    job = q.get_nowait()
    run._send_prepared(c, job["fomo_event_id"], job["message"], job["local_ms"],
                       alert_fn=lambda t: (calls.append(t) or (True, 8.0)))
    assert len(calls) == 1 and "PEPE" in calls[0]
    row2 = dict(c.execute("SELECT * FROM memecoin_alert_latency WHERE fomo_event_id='b1'").fetchone())
    assert row2["send_ok"] == 1 and row2["send_ms"] == 8.0 and row2["alert_sent_at"]


def test_alert_dropped_when_queue_full_never_blocks_copy(monkeypatch):
    # platform review: a backed-up or dead sender must not stall the copy path. A FULL bounded
    # alert queue drops the alert (counted) via put_nowait rather than blocking on put().
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    q = queue.Queue(maxsize=1)
    q.put_nowait({"filler": 1})     # pre-fill so the queue is full
    r = run.run_once(c, _Auth(), 100.0, http=_http_for([_buy("b1", "u1")]),
                     now_fn=lambda: "2026-07-08T17:20:00Z", fill_fn=lambda *a, **k: (0.005, "jupiter"),
                     alert_queue=q)
    assert r["opened"] == 1                            # copy path completed (never blocked)
    assert r["alerts"] == 0 and r["alerts_dropped"] == 1   # alert dropped on the full queue, counted


def test_alert_send_failure_swallowed_and_logged(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_TG_ALERTS", True)
    r = _run(c, [_buy("b1", "u1")], alert_fn=lambda t: (False, 99.0))
    assert r["opened"] == 1                                   # copy intact
    row = dict(c.execute("SELECT * FROM memecoin_alert_latency WHERE fomo_event_id='b1'").fetchone())
    assert row["send_ok"] == 0 and row["send_ms"] == 99.0
