"""Task 2 (operator surface) — separate V6 paper API endpoints.

GET routes open the isolated V6 book read-only and never touch the funded contract; the censor route
is the only mutation, gated on an open position and an >=8-char trimmed reason. The funded
/real/positions route must keep working unchanged.
"""
import json
import inspect
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.persistence import db_memecoin_v6_paper as v6db
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services.memecoin_v6_engine import fingerprint, policy_document, policy_hash
from sniper.settings import memecoin as cfg

NOW = "2026-07-26T12:00:00+00:00"


def _entry(entry_id="s1", network="solana", mint="MintSol", token_qty=1_000_000):
    doc = policy_document()
    qp = {"in_atomic": 25_000_000, "out_atomic": token_qty}
    return {
        "entry_id": entry_id, "origin_event_id": f"evt-{entry_id}", "network_id": network,
        "mint": mint, "ticker": "PEP", "source_wallet_id": "w", "source_wallet_handle": "@w",
        "signal_source": "global_feed", "notional_usd_micros": 25_000_000,
        "entry_price": "0.000042",
        "quote_in_asset": "USDC", "quote_in_atomic": 25_000_000, "quote_in_decimals": 6,
        "quote_out_asset": mint, "quote_out_atomic": token_qty, "quote_out_decimals": 6,
        "token_qty_atomic": token_qty, "route_target": "raydium", "pool_or_vault": "poolX",
        "quote_id": "q", "quote_provider": "jupiter", "quote_age_s": 1.0,
        "entry_mcap": None, "entry_liquidity": None,
        "signal_ts": NOW, "quote_ts": NOW, "fill_ts": NOW,
        "policy_hash": policy_hash(), "policy_document": doc,
        "quote_payload": qp, "quote_fingerprint": fingerprint(qp),
        "route_binding": {"venue": "jupiter"}, "costs": {"slippage_bps": 300},
        "net_pnl_computable": True,
        "source_receipt_id": f"receipt-{entry_id}",
        "adapter_version": None,
        "fill_model_version": None,
        "rule_version": None,
        "evaluator_version": None,
        "launch_mode": None,
    }


def _client(monkeypatch, tmp_path, *, seed=True, extra_ctx=None):
    v6_path = str(tmp_path / "v6.sqlite")
    if seed:
        conn = v6db.connect(v6_path)
        v6db.record_entry_fill(conn, _entry("s1", "solana", "MintSol"))
        conn.close()
    monkeypatch.setattr(cfg, "MEMECOIN_V6_PAPER_DB", v6_path if seed else str(tmp_path / "absent.sqlite"))
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c, **(extra_ctx or {})}))
    return app.test_client(), v6_path


def _csrf(client):
    token = client.get("/api/memecoin/v6-paper/positions").get_json()["csrf_token"]
    return {"X-CSRF-Token": token}


def test_absent_book_returns_honest_empty(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, seed=False)
    r = client.get("/api/memecoin/v6-paper/positions")
    assert r.status_code == 200
    body = r.get_json()
    assert {key: body[key] for key in (
        "mode", "funded", "fill_label", "positions", "runtime")} == {
        "mode": "v6_paper", "funded": False, "fill_label": "modeled_live_quote",
        "positions": [], "runtime": None}
    assert body["csrf_token"]


def test_positions_list_and_detail_and_summary(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    lst = client.get("/api/memecoin/v6-paper/positions").get_json()
    assert lst["funded"] is False and lst["fill_label"] == "modeled_live_quote"
    assert lst["arm"]["state"] == "NOT RUNNING"
    assert lst["arm"]["identity"] == {"source": "funded_copy_system"}
    assert [p["entry_id"] for p in lst["positions"]] == ["s1"]
    card = lst["positions"][0]
    assert card["fill_kind"] == "modeled_live_quote"
    assert {"latest_observed_at", "modeled_price_return_frac", "inventory",
            "market_facts", "decision_context"} <= set(card)
    assert {"action", "severity", "why", "watching", "missing_facts"} <= \
        set(card["decision_context"])
    assert all({"rule_id", "state", "label"} <= set(item)
               for item in card["decision_context"]["watching"])
    assert "feed_health" in lst["runtime"] and lst["csrf_token"]
    assert lst["summary"]["current_policy"]["policy_hash"] == policy_hash()

    detail = client.get("/api/memecoin/v6-paper/positions/s1")
    assert detail.status_code == 200
    dj = detail.get_json()
    assert dj["entry_evidence"]["mint"] == "MintSol" and dj["fill_label"] == "modeled_live_quote"
    assert {"break_episode", "active_intent", "quote_status"} <= set(dj)

    summ = client.get("/api/memecoin/v6-paper/summary").get_json()
    assert summ["net_modeled_pnl_usd"] is None
    assert summ["net_unavailable_reason"] == "unknown_cost_components"
    assert summ["admission"]["states"] == {
        "queued": 0, "inflight": 0, "retry_wait": 0, "filled": 0,
        "terminal_unfilled": 0,
    }
    assert summ["current_policy"]["policy_hash"] == policy_hash()
    assert len(summ["policy_cohorts"]) == 1
    assert len(summ["measurement_cohorts"]) == 1


def test_positions_uses_cockpit_snapshot_not_full_summary(monkeypatch, tmp_path):
    from sniper.services import memecoin_v6_paper_report as report

    monkeypatch.setattr(
        report, "summary",
        lambda _conn: (_ for _ in ()).throw(AssertionError("full summary called")),
    )
    client, _ = _client(monkeypatch, tmp_path)

    response = client.get("/api/memecoin/v6-paper/positions")
    assert response.status_code == 200
    assert response.get_json()["summary"]["current_policy"]["policy_hash"] == policy_hash()


def test_v6_stream_once_returns_scrubbed_snapshot(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    response = client.get("/api/memecoin/v6-paper/stream?once=1", buffered=True)

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    body = response.get_data(as_text=True)
    assert body.count("event: snapshot") == 1
    payload = json.loads(next(
        line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")
    ))
    assert payload["funded"] is False
    assert payload["fill_label"] == "modeled_live_quote"
    assert payload["summary"]["current_policy"]["policy_hash"] == policy_hash()
    assert payload["csrf_token"]
    for forbidden in ("reservation_id", "tx_sig", "signed_payload", "private_key"):
        assert forbidden not in body


def test_v6_stream_revision_includes_current_arm_state():
    source = inspect.getsource(create_api_memecoin_blueprint)
    assert "arm = _v6_arm()" in source
    assert "revision = (" in source and "rpt.cockpit_revision(v6c)" in source
    assert "json.dumps(arm, sort_keys=True" in source


def test_v6_summary_snapshot_is_cached_and_censor_invalidates(monkeypatch, tmp_path):
    from sniper.services import memecoin_v6_paper_report as report

    calls = 0
    original = report.summary

    def counted(conn):
        nonlocal calls
        calls += 1
        return original(conn)

    monkeypatch.setattr(report, "summary", counted)
    client, _ = _client(monkeypatch, tmp_path)

    assert client.get("/api/memecoin/v6-paper/summary").status_code == 200
    assert client.get("/api/memecoin/v6-paper/summary").status_code == 200
    assert calls == 1

    response = client.post(
        "/api/memecoin/v6-paper/positions/s1/censor",
        json={"reason": "duplicate open coin"}, headers=_csrf(client),
    )
    assert response.status_code == 200
    assert client.get("/api/memecoin/v6-paper/summary").status_code == 200
    assert calls == 2


def test_detail_missing_position_is_404(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/api/memecoin/v6-paper/positions/nope").status_code == 404


def test_detail_forwards_bounded_timeline_cursors(monkeypatch, tmp_path):
    from sniper.services import memecoin_v6_paper_report as report
    seen = []
    original = report.position_detail

    def capture(conn, position_id, **kwargs):
        seen.append(kwargs)
        return original(conn, position_id, **kwargs)

    monkeypatch.setattr(report, "position_detail", capture)
    client, _ = _client(monkeypatch, tmp_path)

    response = client.get(
        "/api/memecoin/v6-paper/positions/s1?observation_limit=3&decision_limit=4"
        "&before_observation=20&before_decision=30")

    assert response.status_code == 200
    assert seen == [{
        "observation_limit": 3, "decision_limit": 4,
        "before_observation": 20, "before_decision": 30,
    }]
    assert client.get(
        "/api/memecoin/v6-paper/positions/s1?observation_limit=999").status_code == 400


def test_forbidden_keys_absent_from_v6_responses(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    blob = client.get("/api/memecoin/v6-paper/positions").get_data(as_text=True)
    blob += client.get("/api/memecoin/v6-paper/positions/s1").get_data(as_text=True)
    for forbidden in ("reservation_id", "tx_sig", "signed_payload", "private_key",
                      "manual_sell_url", "mirror_url", "override_url", "kill_switch_url"):
        assert forbidden not in blob


def test_funded_positions_route_still_works(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.get("/api/memecoin/real/positions")
    assert r.status_code == 200                    # funded contract unchanged, independent of V6


def test_censor_requires_eight_char_reason(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/api/memecoin/v6-paper/positions/s1/censor", json={"reason": "short"},
                    headers=_csrf(client))
    assert r.status_code == 400


def test_censor_open_position_then_idempotent_then_conflict(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    headers = _csrf(client)
    first = client.post("/api/memecoin/v6-paper/positions/s1/censor",
                        json={"reason": "broken experiment"}, headers=headers)
    assert first.status_code == 200 and first.get_json()["status"] == "censored_operator"
    again = client.post("/api/memecoin/v6-paper/positions/s1/censor",
                        json={"reason": "broken experiment"}, headers=headers)
    assert again.status_code == 200                # idempotent same reason
    conflict = client.post("/api/memecoin/v6-paper/positions/s1/censor",
                           json={"reason": "different reason text"}, headers=headers)
    assert conflict.status_code == 409             # non-open -> conflict


def test_censor_missing_position_is_404(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/api/memecoin/v6-paper/positions/nope/censor",
                    json={"reason": "broken experiment"}, headers=_csrf(client))
    assert r.status_code == 404


def test_censor_rejects_missing_csrf_before_writable_open(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(
        v6db, "connect",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("writable V6 DB opened before CSRF rejection")),
    )
    response = client.post(
        "/api/memecoin/v6-paper/positions/s1/censor", json={"reason": "broken experiment"})
    assert response.status_code == 403


def test_get_routes_do_not_write_the_book(monkeypatch, tmp_path):
    # a GET must not create the book if it is absent (read-only, connect_existing)
    client, _ = _client(monkeypatch, tmp_path, seed=False)
    import os
    absent = str(tmp_path / "absent.sqlite")
    client.get("/api/memecoin/v6-paper/positions")
    assert not os.path.exists(absent)
