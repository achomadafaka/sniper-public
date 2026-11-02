"""Desk admission metadata stays immutable across queue and runner boundaries."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_intents as mi
from sniper.services import memecoin_origin as origin
from sniper.settings import memecoin as cfg


NOW = "2026-10-21T12:00:00+00:00"
RAILS = {"tp_pct": 60.0, "sl_pct": 25.0, "trail_pct": 15.0}
META = {"cohort_id": "desk:H:v1", "producer": "H", "opportunity_id": "H:receipt/one",
        "snapshot_hash": "a" * 64}


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", {
        "tp_pct": (15.0, 150.0), "sl_pct": (10.0, 40.0), "trail_pct": (8.0, 30.0)})
    monkeypatch.setattr(mi, "send_wake", lambda *args: None)


def ledger():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def registered(conn):
    cohort = m.register_desk_cohort(conn, producer="H", policy={"gates": "metadata-v1"},
                                    prompt_version="desk-judge-v6", created_at=NOW)
    return dict(META, cohort_id=cohort)


def queued(conn, **kw):
    args = dict(mint="Mint", network_id="solana", now_iso=NOW, size_usd=25.0,
                signal_source="desk", desk_ticket_id="ticket", rails=RAILS, rails_mode="custom")
    args.update(kw)
    return mi.enqueue(conn, **args)


def validator():
    validate = getattr(origin, "validate_desk_metadata", None)
    assert callable(validate), "desk metadata needs one shared strict validator"
    return validate


@pytest.mark.parametrize("producer", ["H", "U"])
def test_valid_metadata_preserves_exact_identifiers_and_boundary_length(producer):
    meta = dict(META, producer=producer, cohort_id="a" * 200)
    before = dict(meta)
    assert validator()(meta) == (True, None)
    assert meta == before


@pytest.mark.parametrize("meta", [
    None, True, [], {}, dict(META, producer="h"), dict(META, producer=True),
    dict(META, cohort_id=""), dict(META, cohort_id="a" * 201),
    dict(META, opportunity_id="two words"), dict(META, opportunity_id="bad\n"),
    dict(META, opportunity_id=123), dict(META, snapshot_hash="A" * 64),
    dict(META, snapshot_hash="a" * 63), dict(META, snapshot_hash="g" * 64),
    dict(META, extra="unapproved"), {k: v for k, v in META.items() if k != "producer"},
])
def test_malformed_or_extra_metadata_is_refused(meta):
    ok, reason = validator()(meta)
    assert ok is False and reason


def test_queue_refuses_missing_metadata_without_writing_an_intent():
    conn = ledger()
    with pytest.raises(ValueError, match="metadata"):
        queued(conn)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 0


def test_queue_persists_canonical_metadata_and_refuses_a_changed_replay():
    conn = ledger()
    first = queued(conn, desk_metadata=META)
    reordered = dict(reversed(list(META.items())))
    assert queued(conn, desk_metadata=reordered) == first
    raw = conn.execute("SELECT desk_metadata_json FROM memecoin_manual_intents").fetchone()[0]
    assert raw == '{"cohort_id":"desk:H:v1","opportunity_id":"H:receipt/one","producer":"H","snapshot_hash":"' + "a" * 64 + '"}'
    with pytest.raises(ValueError, match="different"):
        queued(conn, desk_metadata=dict(META, snapshot_hash="b" * 64))
    assert conn.execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 1


def test_manual_queue_remains_without_desk_metadata():
    conn = ledger()
    iid = mi.enqueue(conn, mint="manual", now_iso=NOW, size_usd=25.0)
    row = conn.execute("SELECT * FROM memecoin_manual_intents WHERE intent_id=?", (iid,)).fetchone()
    assert row["signal_source"] == "manual" and row["desk_metadata_json"] is None
    with pytest.raises(ValueError, match="only.*desk"):
        mi.enqueue(conn, mint="other", now_iso=NOW, size_usd=25.0, desk_metadata=META)


@pytest.mark.parametrize("terminal", ["expired", "killed"])
def test_status_exposes_frozen_request_after_admission_closes(monkeypatch, terminal):
    conn = ledger()
    meta = registered(conn)
    iid = queued(conn, desk_metadata=meta)
    if terminal == "expired":
        mi.expire_stale(conn, now_iso="2026-10-22T12:00:00+00:00")
    else:
        m.record_desk_cohort_cost(conn, cohort_id=meta["cohort_id"], event_id="bill", usd=200,
                                  kind="model", occurred_at=NOW, provenance="fixture:bill")
    def forbidden(*args, **kwargs):
        pytest.fail("a status read reran admission")
    monkeypatch.setattr(origin, "validate_desk_metadata", forbidden)
    monkeypatch.setattr(m, "desk_cohort_refusal", forbidden)
    status = mi.read_status(conn, iid)
    assert status["signal_source"] == "desk"
    assert status["desk_ticket_id"] == "ticket"
    assert status["desk_metadata"] == meta
    assert status["sl_pct"] == RAILS["sl_pct"]
    assert status["operation_state"] == (mi.EXPIRED if terminal == "expired" else mi.QUEUED)


@pytest.mark.parametrize("raw", [None, "{", "null", "[]"])
def test_status_reads_legacy_or_corrupt_metadata_as_unavailable(raw):
    conn = ledger()
    iid = queued(conn, desk_metadata=META)
    conn.execute("UPDATE memecoin_manual_intents SET desk_metadata_json=? WHERE intent_id=?", (raw, iid))
    status = mi.read_status(conn, iid)
    assert status["desk_metadata"] is None
    assert status["operation_state"] == mi.QUEUED


def test_manual_status_has_no_desk_identity():
    conn = ledger()
    iid = mi.enqueue(conn, mint="manual", now_iso=NOW, size_usd=25.0)
    status = mi.read_status(conn, iid)
    assert status["signal_source"] == "manual"
    assert status["desk_ticket_id"] is None
    assert status["desk_metadata"] is None


@pytest.mark.parametrize("raw", [None, "{", "null", "[]"])
def test_unsigned_legacy_or_corrupt_metadata_refuses_without_buying(raw):
    conn = ledger()
    iid = queued(conn, desk_metadata=META)
    conn.execute("UPDATE memecoin_manual_intents SET desk_metadata_json=? WHERE intent_id=?", (raw, iid))
    conn.commit()
    calls = []
    mi.drain(conn, lambda *a, **k: calls.append(k) or {"status": "filled"}, now_fn=lambda: NOW)
    status = mi.read_status(conn, iid)
    assert calls == []
    assert status["operation_state"] == mi.REFUSED
    assert "metadata" in status["operation_reason"]


def test_drain_passes_the_stored_metadata_without_touching_manual_calls():
    conn = ledger()
    queued(conn, desk_metadata=META)
    mi.enqueue(conn, mint="manual", now_iso=NOW, size_usd=25.0)
    calls = []
    mi.drain(conn, lambda *a, **k: calls.append(k) or {"status": "rejected"}, now_fn=lambda: NOW)
    assert "desk_metadata" not in calls[0]
    assert calls[1]["desk_metadata"] == META


def test_cohort_refusal_precedes_capacity_and_daily_queries(monkeypatch):
    conn = ledger()
    meta = registered(conn)
    m.record_desk_cohort_cost(conn, cohort_id=meta["cohort_id"], event_id="bill", usd=200,
                              kind="model", occurred_at=NOW, provenance="fixture:bill")
    def forbidden(*args, **kwargs):
        pytest.fail("a killed cohort reached the capacity/day checks")
    monkeypatch.setattr(m, "desk_open_count", forbidden)
    monkeypatch.setattr(m, "desk_loss_latched", forbidden)
    assert origin.desk_book_refusal(conn, now_iso=NOW, desk_metadata=meta) == "desk_cohort_killed"


def test_runner_refuses_missing_metadata_before_providers():
    from sniper.services import memecoin_real_runner as runner
    def forbidden(*args, **kwargs):
        pytest.fail("invalid metadata reached a provider")
    buy = runner.make_real_buy_fn({"solana": {"usdc_fn": forbidden, "sol_fn": forbidden}},
                                   stats_fn=forbidden)
    out = buy(ledger(), "Mint", network_id="solana", size_usd=25.0, signal_source="desk",
              desk_ticket_id="ticket", desk_rails=RAILS)
    assert out["status"] == "rejected" and "metadata" in out["reason"]


def test_runner_carries_metadata_into_the_event_for_the_frozen_contract(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    conn = ledger()
    meta = registered(conn)
    events = []
    def hook(conn, ev, *args, **kwargs):
        events.append(ev)
        return {"status": "rejected", "reason": "fixture"}
    monkeypatch.setattr(runner, "real_entry_hook", hook)
    buy = runner.make_real_buy_fn({}, stats_fn=lambda *a, **k: {"market_cap": 60_000, "price": 1})
    out = buy(conn, "Mint", network_id="solana", size_usd=25.0, signal_source="desk",
              desk_ticket_id="ticket", desk_rails=RAILS, desk_metadata=meta)
    assert out["reason"] == "fixture"
    assert events[0].desk == {"ticket_id": "ticket", "rails": RAILS, **meta}
    meta["producer"] = "U"
    assert events[0].desk["producer"] == "H"
