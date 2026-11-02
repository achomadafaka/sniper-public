import sqlite3
import json
from datetime import datetime, timedelta

from flask import Flask

from sniper.persistence import db_memecoin as ledger
from sniper.persistence import db_memecoin_captures as store
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.util.clock import real_utc_now_iso


T0 = "2026-08-29T12:00:00+00:00"
T1 = "2026-08-29T12:00:01+00:00"
T2 = "2026-08-29T12:00:02+00:00"
T3 = "2026-08-29T12:00:03+00:00"


def _slot():
    return {
        "attempt_id": "attempt-1",
        "origin_event_id": "event-1",
        "network_id": "solana",
        "mint": "So1Mint",
        "ticker": "FAST",
        "source_user_id": "wallet-1",
        "source_handle": "alice",
        "gate_family": "chase_first_seen",
        "refusal_reason": "coin ran 4.2x above first-seen price (cap 3.0x)",
        "refused_at": T0,
        "refusal_price": 1.0,
    }


def _capture_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(store.SCHEMA)
    store._add_missing_columns(conn)
    return conn


def test_slot_show_and_visibility_clocks_are_write_once():
    conn = _capture_conn()
    first = store.show_operator_slot(conn, _slot(), now_iso=T0)
    second = store.show_operator_slot(conn, _slot(), now_iso=T1)
    visible = store.mark_operator_slot_visible(conn, "attempt-1", now_iso=T1)
    visible_again = store.mark_operator_slot_visible(conn, "attempt-1", now_iso=T2)

    assert first["slot_shown_at"] == second["slot_shown_at"] == T0
    assert visible["slot_visible_at"] == visible_again["slot_visible_at"] == T1


def test_slot_decision_is_write_once_and_stores_named_drift():
    conn = _capture_conn()
    store.show_operator_slot(conn, _slot(), now_iso=T0)
    first = store.decide_operator_slot(
        conn, "attempt-1", decision="dismiss", now_iso=T2,
        decided_size_pct=None, intent_id=None, quote_price_at_decision=1.15,
    )
    second = store.decide_operator_slot(
        conn, "attempt-1", decision="enter", now_iso=T3,
        decided_size_pct=0.13, intent_id="late", quote_price_at_decision=1.30,
    )

    assert first["operator_decision"] == second["operator_decision"] == "dismiss"
    assert second["decided_at"] == T2
    assert second["slot_drift_from_refusal_pct"] == 15.0
    assert second["intent_id"] is None


def test_slot_fills_the_refusal_price_once_when_the_first_quote_arrives_late():
    conn = _capture_conn()
    first = {**_slot(), "refusal_price": None}
    store.show_operator_slot(conn, first, now_iso=T0)
    store.show_operator_slot(conn, _slot(), now_iso=T1)

    row = store.decide_operator_slot(
        conn, "attempt-1", decision="dismiss", now_iso=T2,
        quote_price_at_decision=1.15,
    )

    assert row["refusal_price"] == 1.0
    assert row["slot_drift_from_refusal_pct"] == 15.0


def _web_client(tmp_path, *, network_id="solana", market_prices=None, refused_at=None):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    ledger.ensure_memecoin_tables(conn)
    now = refused_at or real_utc_now_iso()
    rows = [
        ("selection", "event-selection", "So1Select", "SELECT", "wallet-1", "alice",
         "refused", "coin ran 4.2x above first-seen price (cap 3.0x)"),
        ("safety", "event-safety", "So1Safety", "SAFE", "wallet-2", "bob",
         "refused", "exact quote is +43% above source price (cap 30%)"),
    ]
    conn.executemany(
        "INSERT INTO memecoin_entry_attempts"
        "(attempt_id,origin_event_id,network_id,mint,ticker,source_user_id,source_handle,"
        "decision_started_at,terminal_status,terminal_reason,completed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(aid, origin, network_id, mint, ticker, uid, handle, now, status, reason, now)
         for aid, origin, mint, ticker, uid, handle, status, reason in rows],
    )
    conn.execute(
        "UPDATE memecoin_entry_attempts SET source_price=?,source_market_cap=?,"
        "chase_source_size_usd=? WHERE attempt_id='selection'",
        (0.00001234, 83_000.0, 224.0),
    )
    for index, (observed_at, price) in enumerate(market_prices or (), start=1):
        conn.execute(
            "INSERT INTO memecoin_quote_observations_v2"
            "(sweep_id,network_id,mint,observed_at,price,quality,contexts,capture_version) "
            "VALUES(?,?,?,?,?,'ok','refusal-slot-test',2)",
            (f"market-{index}", network_id, "So1Select", observed_at, price),
        )
    conn.commit()
    path = tmp_path / "refusals.db"
    app = Flask(__name__)
    app.secret_key = "refusal-slot-test"
    app.register_blueprint(create_api_memecoin_blueprint({
        "conn": conn,
        "refusal_db_path": str(path),
        "real_buy_fn": lambda *a, **k: {"status": "queued"},
    }))
    app.config["REFUSAL_TEST_LEDGER"] = conn
    return app.test_client(), path


def _ledger_of(client):
    return client.application.config["REFUSAL_TEST_LEDGER"]


def _csrf(client):
    return {"X-CSRF-Token": client.get(
        "/api/memecoin/real/status").get_json()["csrf_token"]}


def test_refusal_api_lists_selection_gates_and_excludes_safety_gates(tmp_path):
    client, _ = _web_client(tmp_path)
    response = client.get("/api/memecoin/real/refusal-slots")

    assert response.status_code == 200
    slots = response.get_json()["slots"]
    assert [row["attempt_id"] for row in slots] == ["selection"]
    assert slots[0]["gate_family"] == "chase_first_seen"
    assert slots[0]["refusal_reason"].startswith("coin ran 4.2x")
    assert slots[0]["source_handle"] == "alice"
    assert slots[0]["source_price"] == 0.00001234
    assert slots[0]["source_market_cap"] == 83_000.0
    assert slots[0]["source_size_usd"] == 224.0


def test_refusal_slot_exposes_only_recorded_effective_source_floor(tmp_path):
    client, _ = _web_client(tmp_path)
    conn = _ledger_of(client)
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_reason=?,trace_json=? "
                 "WHERE attempt_id='selection'", (
                     "source buy $995 (0.587%) under mcap floor 0.800% ($1600)",
                     json.dumps({"substage_events": [{"name": "source_buy_floor",
                         "floor_usd": 1600, "effective_floor_usd": 1568,
                         "private_key": "SECRET"}]})))
    conn.commit()
    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]
    assert slot["source_buy_effective_floor_usd"] == 1568
    assert "SECRET" not in json.dumps(slot)


def test_robinhood_refusal_uses_same_basis_stored_market_prices(tmp_path):
    now = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    client, _ = _web_client(
        tmp_path,
        network_id="4663",
        refused_at=(now - timedelta(seconds=10)).isoformat(),
        market_prices=[
            ((now - timedelta(seconds=11)).isoformat(), 0.00001232),
            ((now - timedelta(seconds=1)).isoformat(), 0.00001302),
        ],
    )

    slots = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"]

    assert len(slots) == 1
    assert slots[0]["network_id"] == "4663"
    assert slots[0]["price_basis"] == "market"
    assert slots[0]["refusal_price"] == 0.00001232
    assert slots[0]["quote_price"] == 0.00001302
    assert slots[0]["price_drift_pct"] == 5.681818


def test_refusal_api_records_show_visibility_and_decision_once(tmp_path):
    client, _ = _web_client(tmp_path)
    headers = _csrf(client)

    shown = client.post(
        "/api/memecoin/real/refusal-slots/selection/shown",
        json={"visible": True}, headers=headers,
    )
    decision = client.post(
        "/api/memecoin/real/refusal-slots/selection/decision",
        json={"decision": "enter", "size_pct_equity": 0.13, "intent_id": "intent-1"},
        headers=headers,
    )
    retry = client.post(
        "/api/memecoin/real/refusal-slots/selection/decision",
        json={"decision": "dismiss"}, headers=headers,
    )

    assert shown.status_code == 200
    row = decision.get_json()["slot"]
    assert row["operator_decision"] == "enter"
    assert row["decided_size_pct"] == 0.13
    assert row["intent_id"] == "intent-1"
    assert retry.get_json()["slot"]["operator_decision"] == "enter"


def test_refusal_decision_accepts_an_override_enter_without_a_preset(tmp_path):
    """The operator's "Enter anyway" names a dollar amount, never a 0.13/0.26 equity preset --
    the decision record is research attribution only (2026-09-22), it never gates the real buy,
    which already happened by the time this call runs."""
    client, _ = _web_client(tmp_path)
    headers = _csrf(client)

    decision = client.post(
        "/api/memecoin/real/refusal-slots/selection/decision",
        json={"decision": "enter", "override": True, "intent_id": "intent-override-1"},
        headers=headers,
    )

    assert decision.status_code == 200
    row = decision.get_json()["slot"]
    assert row["operator_decision"] == "enter"
    assert row["decided_size_pct"] is None
    assert row["intent_id"] == "intent-override-1"


def test_refusal_decision_override_still_requires_an_intent_id(tmp_path):
    client, _ = _web_client(tmp_path)
    headers = _csrf(client)

    decision = client.post(
        "/api/memecoin/real/refusal-slots/selection/decision",
        json={"decision": "enter", "override": True},
        headers=headers,
    )

    assert decision.status_code == 400


def test_refusal_decision_enter_without_override_still_needs_a_preset(tmp_path):
    """Unchanged behaviour for the plain (non-override) enter path."""
    client, _ = _web_client(tmp_path)
    headers = _csrf(client)

    decision = client.post(
        "/api/memecoin/real/refusal-slots/selection/decision",
        json={"decision": "enter", "size_pct_equity": 0.5, "intent_id": "intent-2"},
        headers=headers,
    )

    assert decision.status_code == 400


def test_refusal_countdown_uses_the_first_shown_clock(tmp_path, monkeypatch):
    from sniper.routes import api_memecoin

    client, _ = _web_client(tmp_path)
    base = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    shown_at = base + timedelta(seconds=30)
    monkeypatch.setattr(api_memecoin, "_now_iso", lambda: shown_at.isoformat())
    response = client.post(
        "/api/memecoin/real/refusal-slots/selection/shown",
        json={"visible": True}, headers=_csrf(client),
    )
    assert response.status_code == 200

    monkeypatch.setattr(
        api_memecoin, "_now_iso", lambda: (shown_at + timedelta(seconds=60)).isoformat())
    slots = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"]

    assert slots[0]["remaining_s"] == 60

    monkeypatch.setattr(
        api_memecoin, "_now_iso", lambda: (shown_at + timedelta(seconds=100)).isoformat())
    slots = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"]
    assert slots[0]["remaining_s"] == 20


def _display_rows(path, now, rows, *, origin="event-selection", network="4663"):
    caps = store.connect(path)
    for age, price, observed in rows:
        store.record_observation(caps, {
            "capture_id": f"{network}:So1Select:{origin}", "origin_event_id": origin,
            "network_id": network, "mint": "So1Select", "refused_at": now.isoformat(),
            "observed_at": observed.isoformat(), "age_s": age, "quote_price": price,
            "quote_price_basis": "display", "quality": "ok"})
    caps.close()


def test_evm_slot_prices_from_display_captures_before_the_market_tape(tmp_path):
    """Alpha 2026-09-17: 4663 had 12/12 unpriced cards while 8 held display capture rows. The
    capture lane's display marks now price the card, named 'display', never 'route'."""
    now = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    client, path = _web_client(
        tmp_path, network_id="4663", refused_at=(now - timedelta(seconds=10)).isoformat(),
        market_prices=[((now - timedelta(seconds=11)).isoformat(), 0.00001232),
                       ((now - timedelta(seconds=1)).isoformat(), 0.00001302)])
    _display_rows(path, now, ((3.0, 0.5, now - timedelta(seconds=7)),
                              (8.0, 0.9, now - timedelta(seconds=2))))

    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]

    assert slot["price_basis"] == "display"
    assert (slot["refusal_price"], slot["quote_price"]) == (0.5, 0.9)
    assert slot["quote_observed_at"] == (now - timedelta(seconds=2)).isoformat()


def test_evm_slot_prices_from_display_captures_when_the_market_tape_is_empty(tmp_path):
    now = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    client, path = _web_client(
        tmp_path, network_id="5042", refused_at=(now - timedelta(seconds=10)).isoformat())
    _display_rows(path, now, ((4.0, 2.0, now - timedelta(seconds=6)),),
                  network="5042")

    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]

    assert (slot["price_basis"], slot["refusal_price"], slot["quote_price"]) == ("display", 2.0, 2.0)
    assert slot["price_drift_pct"] == 0.0


def test_a_display_now_price_older_than_the_slot_window_is_unknown(tmp_path):
    """A stopped display lane must not print a two-minute-old chart price as "now"."""
    now = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    client, path = _web_client(
        tmp_path, network_id="4663", refused_at=(now - timedelta(seconds=100)).isoformat())
    _display_rows(path, now, ((2.0, 0.5, now - timedelta(seconds=130)),))

    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]

    assert (slot["price_basis"], slot["refusal_price"]) == ("display", 0.5)
    assert slot["quote_price"] is None and slot["price_drift_pct"] is None


def test_market_basis_slot_carries_the_observation_time_of_its_now_price(tmp_path):
    now = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    seen = (now - timedelta(seconds=44)).isoformat()
    client, _ = _web_client(
        tmp_path, network_id="4663", refused_at=(now - timedelta(seconds=50)).isoformat(),
        market_prices=[((now - timedelta(seconds=51)).isoformat(), 0.00001232), (seen, 0.00001302)])

    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]

    assert (slot["price_basis"], slot["quote_observed_at"]) == ("market", seen)


def test_refusal_slot_takes_the_trader_size_from_the_event_when_the_attempt_has_none(tmp_path):
    """90 of 93 cards in 24 h had no size: only the chase path writes chase_source_size_usd.
    The copied trade's own event row carries it (89 of 90)."""
    client, _ = _web_client(tmp_path)
    ledger = _ledger_of(client)
    ledger.execute("UPDATE memecoin_entry_attempts SET chase_source_size_usd=NULL "
                   "WHERE attempt_id='selection'")
    ledger.execute("INSERT INTO memecoin_events(fomo_event_id,mint,side,their_size_usd) "
                   "VALUES('event-selection','So1Select','buy',512.5)")
    ledger.commit()

    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]

    assert (slot["source_size_usd"], slot["source_size_basis"]) == (512.5, "event")


def test_refusal_slot_size_basis_names_the_attempt_and_unknown(tmp_path):
    client, _ = _web_client(tmp_path)
    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]
    assert (slot["source_size_usd"], slot["source_size_basis"]) == (224.0, "attempt")

    ledger = _ledger_of(client)
    ledger.execute("UPDATE memecoin_entry_attempts SET chase_source_size_usd=0 "
                   "WHERE attempt_id='selection'")
    ledger.commit()
    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]
    assert (slot["source_size_usd"], slot["source_size_basis"]) == (None, None)


def test_refusal_market_tape_reads_seek_the_coin_index(tmp_path):
    """Live 2026-09-17: the before/after reads took idx_mc_quotes_v2_quality_time and walked every
    'ok' row of 12.8 M until the mint appeared, 1.58 s for a mint with no observation."""
    now = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    client, _ = _web_client(tmp_path, network_id="4663",
                            refused_at=(now - timedelta(seconds=10)).isoformat())
    ledger = _ledger_of(client)
    seen = []
    ledger.set_trace_callback(seen.append)
    client.get("/api/memecoin/real/refusal-slots")
    ledger.set_trace_callback(None)

    reads = [sql for sql in seen if "memecoin_quote_observations_v2" in sql and "observed_at<=" in sql
             or "memecoin_quote_observations_v2" in sql and "observed_at>=" in sql]
    assert len(reads) == 2
    for sql in reads:
        plan = " ".join(str(r[3]) for r in ledger.execute("EXPLAIN QUERY PLAN " + sql))
        assert "idx_mc_quotes_v2_coin_time" in plan, plan


def test_missing_source_market_cap_is_derived_from_the_coins_supply_not_shown_as_zero():
    # xp 2026-09-17 07:51Z: J7 relayed market cap 0.0 and the card read "bought at $0".
    from sniper.routes import api_memecoin as api
    assert api._derived_market_cap(
        [{"their_price": 0.000878, "their_market_cap": 806_231.0}], 0.000908) == round(806_231.0 / 0.000878 * 0.000908)


def test_derived_market_cap_is_unknown_without_a_priced_event():
    from sniper.routes import api_memecoin as api
    assert api._derived_market_cap([], 0.0009) is None
    assert api._derived_market_cap([{"their_price": 0.001, "their_market_cap": 0.0}], 0.0009) is None
    assert api._derived_market_cap([{"their_price": 0.001, "their_market_cap": 900.0}], None) is None


def test_refusal_market_cap_does_not_cross_chains(tmp_path):
    client, _ = _web_client(tmp_path, network_id="5042")
    conn = _ledger_of(client)
    queries = []
    conn.set_trace_callback(queries.append)
    conn.execute("UPDATE memecoin_entry_attempts SET source_market_cap=0,source_price=1 "
                 "WHERE attempt_id='selection'")
    conn.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id,side,their_price,"
                 "their_market_cap,created_at) VALUES('foreign','So1Select','56','buy',2,2000000,?)",
                 (real_utc_now_iso(),))
    conn.commit()
    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]
    assert slot["source_market_cap"] is None
    assert slot["source_market_cap_basis"] is None

    conn.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id,side,their_price,"
                 "their_market_cap,created_at) VALUES('same','So1Select','arc','buy',2,400000,?)",
                 (real_utc_now_iso(),))
    conn.commit()
    slot = client.get("/api/memecoin/real/refusal-slots").get_json()["slots"][0]
    assert slot["source_market_cap"] == 200000
    query = next(sql for sql in queries if "SELECT their_price, their_market_cap FROM memecoin_events" in sql)
    plan = conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("SEARCH memecoin_events USING INDEX idx_mc_events_mint" in row[3] for row in plan)
