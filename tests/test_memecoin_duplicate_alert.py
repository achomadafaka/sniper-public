"""One alert per source buy: a same-source duplicate refusal never pages on its own.

Operator, 2026-09-27 ("fix this kind of signals appearing"). Two lanes see one source buy. The
first reserves; the second is refused by the one-position-per-coin guard with the reason "Another
entry attempt was in progress at decision time". Both pairs below are the live ledger rows:

  LOOT 66154  on-chain lane 2b7de10e reserved 4141 and FILLED; J7 1f3ead0d refused as its
              duplicate and paged REFUSED beside BOUGHT for the same buy.
  GAY         on-chain lane b67ad790 reserved 4142 and ABORTED on 'round trip 19.42% over 10.0%
              cap'; J7 f71ee06c refused as its duplicate at 20:57:38.928Z and paged first, so the
              per-coin refusal collapse muted the real answer 0.7 s later.

A block by ANOTHER wallet's position is a genuine refusal and still pages, and so does a blocker
whose wallet cannot be proven (fail open to paging).
"""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

DEXGEMS = "0bbbf8fd-dea4-5561-bd2e-54f98a573ff8"
SERALBERT = "30363fdb-12e2-5b3f-ae64-29aa2818094c"
LOOT = "F9SAHZVQ7U1mgx8V9Xk1a1EgPtrEVsxQJZa9CUw8uQSp"
GAY = "5mvu9VVUneWJtCMXFeRsGyUcR5NHRFWCpJpKpRsRBpRt"
LOOT_SIG = "3ydSSHuKSo5hUvfBX3ceyBDzrpF7TC9AkS9Cg5TXNDm96q6DjdfnhzXt1WPCL6ShEgFCYKxzLkyVfrK4Baz4pWm"
GAY_SIG = "65VcubXz8nKuRk38TMnAVWXAP3bipjjb31qN94sYUjskhWLKPjr4mNfDD9ZV3rvdu31dLZMPjFq2Bnt9M9pwtWUS"
ROUND_TRIP = "round trip 19.42% over 10.0% cap (sell back $105.92 of $131.45)"
DUPLICATE = "one position per coin: another entry attempt was in progress at decision time"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from sniper.services import memecoin_telegram as tg, memecoin_coin_names
    monkeypatch.setattr(memecoin_coin_names, "name_for", lambda *a, **k: "TEST")
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    for uid, handle in ((DEXGEMS, "DexGemsReal"), (SERALBERT, "seralberttrades")):
        m.add_wallet(conn, uid, handle)
        conn.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='solana' "
                     "WHERE source_user_id=?", (uid,))
    conn.commit()
    monkeypatch.setenv("DB_PATH", str(path))
    clock = {"now": "2026-09-27T20:24:22.000000+00:00"}
    monkeypatch.setattr(tg, "_real_utc_now_iso", lambda: clock["now"])
    sent = []
    monkeypatch.setattr(tg, "send_telegram", lambda text, **kw: sent.append(text) or (True, 1))
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", False)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_REFUSAL_COLLAPSE_S", 3600.0)
    yield conn, clock, sent
    conn.close()


def _attempt(conn, attempt_id, origin, uid, mint, started, lane):
    m.start_entry_attempt(conn, attempt_id=attempt_id, origin_event_id=origin,
                          signal_source=("onchain_watch" if lane is None else "follow_activity"),
                          delivery_lane=lane, network_id="solana", mint=mint, source_user_id=uid,
                          side="buy", decision_started_at=started, observed_at=started,
                          clock_domain="observed_real_utc")


def _refuse(conn, attempt_id, at, reason, blocker=None, status="refused"):
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_status=?, terminal_reason=?, "
                 "completed_at=?, gate_inputs_json=? WHERE attempt_id=?",
                 (status, reason, at,
                  json.dumps({"duplicate_blocker": blocker}) if blocker else None, attempt_id))
    conn.commit()


def _worker():
    from sniper.services import memecoin_telegram as tg
    return tg._NotificationWorker()


def test_loot_feed_without_retained_actor_stays_independent(ledger):
    conn, clock, sent = ledger
    _attempt(conn, "2b7de10ee0b44fcf9b85cf25728fa5d2", "onchain:" + LOOT_SIG, DEXGEMS, LOOT,
             "2026-09-27T20:24:22.230665+00:00", None)
    pid = m.reserve_pending(conn, mint=LOOT, source_user_id=DEXGEMS, strategy="real",
                            notional=97.46, reserved_at="2026-09-27T20:24:22.595880+00:00",
                            network_id="solana", origin_event_id="onchain:" + LOOT_SIG,
                            attempt_id="2b7de10ee0b44fcf9b85cf25728fa5d2")
    dup_origin = "swap:raybot:%s:6xpo:f9sa" % LOOT_SIG
    _attempt(conn, "1f3ead0d11764e9a8fa024da3cd0d6a1", dup_origin, DEXGEMS, LOOT,
             "2026-09-27T20:24:22.353915+00:00", "j7_wallets")
    _refuse(conn, "1f3ead0d11764e9a8fa024da3cd0d6a1", "2026-09-27T20:24:22.602421+00:00", DUPLICATE,
            {"kind": "reservation", "reservation_id": pid,
             "attempt_id": "2b7de10ee0b44fcf9b85cf25728fa5d2"})
    conn.execute("INSERT INTO onchain_wallet_detections(signature,source_user_id,address,network_id,mint,detected_at,created_at) VALUES(?,?,?,'solana',?,?,?)",
                 (LOOT_SIG, DEXGEMS, 'owner', LOOT, clock['now'], clock['now']))
    conn.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,mint,network_id) VALUES(?,?,?,'solana')",
                 (dup_origin, DEXGEMS, LOOT))
    conn.commit()
    w = _worker()
    clock["now"] = "2026-09-27T20:24:22.610000+00:00"
    w.cycle({"origin_event_id": dup_origin})
    assert len(sent) == 1 and "Another entry attempt" in sent[0]
    state = conn.execute("SELECT delivery_state FROM memecoin_notification_transitions "
                         "WHERE attempt_id='1f3ead0d11764e9a8fa024da3cd0d6a1'").fetchone()
    assert state[0] == "delivered", "a matching feed signature without retained actor is insufficient"


def test_opaque_gay_source_stays_independent_of_blocker_round_trip(ledger):
    conn, clock, sent = ledger
    clock["now"] = "2026-09-27T20:57:38.000000+00:00"
    blocker_origin = "onchain:" + GAY_SIG
    _attempt(conn, "b67ad790e2804f9095b2d2a7534868d8", blocker_origin, SERALBERT, GAY,
             "2026-09-27T20:57:38.190899+00:00", None)
    pid = m.reserve_pending(conn, mint=GAY, source_user_id=SERALBERT, strategy="real",
                            notional=131.45, reserved_at="2026-09-27T20:57:38.404101+00:00",
                            network_id="solana", origin_event_id=blocker_origin,
                            attempt_id="b67ad790e2804f9095b2d2a7534868d8")
    dup_origin = "swap:51a20c6c-3e1e-434e-9c56-73939114d3fb"
    _attempt(conn, "f71ee06c40ba41da95c51a1155c4c2b6", dup_origin, SERALBERT, GAY,
             "2026-09-27T20:57:38.720835+00:00", "j7_wallets")
    _refuse(conn, "f71ee06c40ba41da95c51a1155c4c2b6", "2026-09-27T20:57:38.928382+00:00", DUPLICATE,
            {"kind": "reservation", "reservation_id": pid,
             "attempt_id": "b67ad790e2804f9095b2d2a7534868d8"})
    w = _worker()
    clock["now"] = "2026-09-27T20:57:38.940000+00:00"
    w.cycle({"origin_event_id": dup_origin})
    assert len(sent) == 1 and "Another entry attempt" in sent[0]
    _refuse(conn, "b67ad790e2804f9095b2d2a7534868d8", "2026-09-27T20:57:39.611731+00:00",
            ROUND_TRIP, status="aborted")
    m.release_pending(conn, pid, "roundtrip_cap")
    clock["now"] = "2026-09-27T20:57:39.620000+00:00"
    w.cycle({"origin_event_id": blocker_origin})
    # Existing per-coin refusal cadence still applies to independent refusals.
    assert len(sent) == 1, sent
    from sniper.services.memecoin_entry_view import entry_view
    assert "round trip 19.42%" in entry_view(conn, attempt_id="b67ad790e2804f9095b2d2a7534868d8")["reason"]


@pytest.mark.parametrize("case", ["other_wallet_position", "blocker_unknown"])
def test_a_genuine_or_unprovable_block_still_pages(ledger, case):
    conn, clock, sent = ledger
    if case == "other_wallet_position":
        conn.execute("INSERT INTO memecoin_positions (id, source_user_id, mint, network_id, "
                     "execution_mode, is_open) VALUES (66000, ?, ?, 'solana', 'real', 1)",
                     (SERALBERT, LOOT))
        blocker = {"kind": "position", "position_id": 66000, "attempt_id": None}
        expected = "Open funded position at decision time."
    else:
        blocker = {"kind": "reservation", "reservation_id": 1, "attempt_id": "absent-attempt"}
        expected = "Another entry attempt was in progress at decision time."
    conn.commit()
    _attempt(conn, "dup", "swap:dup", DEXGEMS, LOOT, "2026-09-27T20:24:22.353915+00:00",
             "j7_wallets")
    _refuse(conn, "dup", "2026-09-27T20:24:22.602421+00:00", DUPLICATE, blocker)
    w = _worker()
    clock["now"] = "2026-09-27T20:24:22.610000+00:00"
    w.cycle({"origin_event_id": "swap:dup"})
    assert len(sent) == 1 and expected in sent[0], sent


def test_same_wallet_position_without_exact_source_stays_independent(ledger):
    """An existing position cannot prove the blocked buy has the same source transaction."""
    conn, clock, sent = ledger
    conn.execute("INSERT INTO memecoin_positions (id, source_user_id, mint, network_id, "
                 "execution_mode, is_open) VALUES (66154, ?, ?, 'solana', 'real', 1)",
                 (DEXGEMS, LOOT))
    conn.commit()
    _attempt(conn, "dup", "swap:dup", DEXGEMS, LOOT, "2026-09-27T20:24:40+00:00", "j7_wallets")
    _refuse(conn, "dup", "2026-09-27T20:24:40.2+00:00", DUPLICATE,
            {"kind": "position", "position_id": 66154, "attempt_id": None})
    w = _worker()
    clock["now"] = "2026-09-27T20:24:40.3+00:00"
    w.cycle({"origin_event_id": "swap:dup"})
    assert len(sent) == 1 and "Open funded position" in sent[0]


def test_loot_bought_card_times_the_result_at_the_confirmed_fill(ledger):
    """66154: the card's result stamp was memecoin_fills.created_at, which holds positions.opened_at
    (DECISION START, 20:24:22.2495Z), earlier than the detection (20:24:22.2768Z), so the card read
    'result time unknown (clocks disagree)'. The confirmed fill is opened_recorded_at, 1.44 s later."""
    from sniper.services.memecoin_entry_view import entry_view
    from sniper.services.memecoin_telegram import render_entry_notification
    conn, _clock, _sent = ledger
    attempt, origin = "2b7de10ee0b44fcf9b85cf25728fa5d2", "onchain:" + LOOT_SIG
    m.start_entry_attempt(conn, attempt_id=attempt, origin_event_id=origin,
                          signal_source="onchain_watch", network_id="solana", mint=LOOT,
                          source_user_id=DEXGEMS, side="buy",
                          source_created_at="2026-09-27T20:24:22.276780+00:00",
                          source_timestamp_trust="observed_detection",
                          decision_started_at="2026-09-27T20:24:22.230665+00:00",
                          observed_at="2026-09-27T20:24:22.276780+00:00",
                          clock_domain="observed_real_utc")
    _refuse(conn, attempt, "2026-09-27T20:24:23.715894+00:00", "filled", status="filled")
    conn.execute("INSERT INTO memecoin_positions (id, attempt_id, origin_event_id, source_user_id, "
                 "mint, network_id, execution_mode, is_open, entry_price, entry_notional, opened_at, "
                 "opened_recorded_at) VALUES (66154, ?, ?, ?, ?, 'solana', 'real', 0, 0.0001937, "
                 "97.46, '2026-09-27T20:24:22.249532+00:00', '2026-09-27T20:24:23.715089+00:00')",
                 (attempt, origin, DEXGEMS, LOOT))
    conn.execute("INSERT INTO memecoin_fills (id, position_id, kind, price, notional, created_at, "
                 "attempt_id, origin_event_id, network_id) VALUES (133403, 66154, 'entry', 0.0001937, "
                 "97.46, '2026-09-27T20:24:22.249532+00:00', ?, ?, 'solana')", (attempt, origin))
    conn.commit()
    view = entry_view(conn, attempt_id=attempt)
    assert view["status"] == "filled"
    assert view["status_updated_at"] == "2026-09-27T20:24:23.715089+00:00"
    text = render_entry_notification(view)
    assert "clocks disagree" not in text, text
    assert "1.4s receipt to result" in text, text
    # Decision start is 46 ms before the detection stamp: zero at the card's tenths, so it is no
    # longer printed as "stamps disagree by 0.0s" (2026-09-30).
    assert "+0.0s processed" in text and "processed unknown" not in text, text
    assert "processed to result" not in text, text
