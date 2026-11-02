"""Dedicated crypto5m SQLite namespace — schema + idempotent persistence. Local DB only."""
from __future__ import annotations

import json
import os

from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m.models import (
    CaptureGap, ChainlinkTick, PriceToBeatQuality, ReferenceObservation, Settlement,
)

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")
CODE_V = "test-1"


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "cap.sqlite"))
    P.ensure_schema(conn)
    return conn


def _spec():
    with open(os.path.join(FX, "event_resolved_up.json")) as f:
        spec, _ = MR.parse_event(json.load(f))
    return spec


def test_ensure_schema_creates_all_tables_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    P.ensure_schema(conn)  # twice must not error
    rows = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ("crypto5m_markets", "crypto5m_market_rules", "crypto5m_book_events",
              "crypto5m_book_snapshots", "crypto5m_chainlink_ticks", "crypto5m_price_to_beat",
              "crypto5m_reference_observations", "crypto5m_settlements", "crypto5m_capture_gaps"):
        assert t in rows


def test_db_path_is_configurable_and_not_paper_trades(tmp_path):
    path = os.path.join(str(tmp_path), "nested", "x.sqlite")
    conn = P.connect(path)
    P.ensure_schema(conn)
    assert os.path.exists(path)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "paper_trades" not in names


def test_upsert_market_roundtrip_idempotent_preserves_raw(tmp_path):
    conn = _conn(tmp_path)
    spec = _spec()
    P.upsert_market(conn, spec, code_version=CODE_V, source_lineage="gamma", now_ts=100)
    P.upsert_market(conn, spec, code_version=CODE_V, source_lineage="gamma", now_ts=200)  # again
    n = conn.execute("SELECT COUNT(*) FROM crypto5m_markets").fetchone()[0]
    assert n == 1
    got = P.get_market(conn, spec.condition_id)
    assert got["slug"] == spec.slug
    assert got["window_start_ts"] == spec.window_start_ts
    assert json.loads(got["raw_payload"]) == spec.raw_payload
    assert got["last_updated_ts"] == 200


def test_upsert_market_allows_null_price_to_beat(tmp_path):
    conn = _conn(tmp_path)
    with open(os.path.join(FX, "event_live.json")) as f:
        spec, _ = MR.parse_event(json.load(f))
    P.upsert_market(conn, spec, code_version=CODE_V, source_lineage="gamma", now_ts=1)
    got = P.get_market(conn, spec.condition_id)
    assert got["price_to_beat"] is None


def test_record_rule_idempotent_on_hash(tmp_path):
    conn = _conn(tmp_path)
    spec = _spec()
    from sniper.algos.polymarket_crypto5m import rule_contract as RC
    v = RC._verdict(RC.RuleReason.SUPPORTED)
    P.record_rule(conn, condition_id=spec.condition_id, rule_text=spec.raw_rule_text,
                  rule_text_hash=spec.rule_text_hash, resolution_source=spec.resolution_source,
                  verdict=v, now_ts=1)
    P.record_rule(conn, condition_id=spec.condition_id, rule_text=spec.raw_rule_text,
                  rule_text_hash=spec.rule_text_hash, resolution_source=spec.resolution_source,
                  verdict=v, now_ts=2)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_market_rules").fetchone()[0] == 1
    # a different rule text/hash -> a new row (rule change tracked)
    P.record_rule(conn, condition_id=spec.condition_id, rule_text="changed",
                  rule_text_hash="deadbeef", resolution_source=spec.resolution_source,
                  verdict=v, now_ts=3)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_market_rules").fetchone()[0] == 2


def test_append_book_event_idempotent_on_exchange_identity_not_recv_ts(tmp_path):
    conn = _conn(tmp_path)
    kw = dict(condition_id="0xc", token_id="tok1", side="Up", event_type="book",
              exchange_ts=1000, recv_ts=1001, reconnect_generation=0,
              quality_flags=[], raw_payload={"a": 1}, now_ts=5)
    assert P.append_book_event(conn, **kw) is True
    assert P.append_book_event(conn, **kw) is False                       # exact dup ignored
    # replaying the SAME exchange event with a new local receive time must NOT insert a duplicate
    assert P.append_book_event(conn, **dict(kw, recv_ts=9999, now_ts=9)) is False
    # a genuinely different exchange event (new exchange_ts) -> new row
    assert P.append_book_event(conn, **dict(kw, exchange_ts=1001)) is True
    # a re-snapshot after a reconnect (new generation) -> a legitimately distinct row
    assert P.append_book_event(conn, **dict(kw, reconnect_generation=1)) is True
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_book_events").fetchone()[0] == 3


def test_write_snapshot_stores_best_and_depth(tmp_path):
    conn = _conn(tmp_path)
    sid = P.write_snapshot(conn, condition_id="0xc", token_id="tok1", side="Up",
                           bids=[[0.31, 425.0], [0.30, 10.0]], asks=[[0.32, 206.0]],
                           best_bid=0.31, best_ask=0.32, spread=0.01, book_hash="h1",
                           tick_size=0.01, min_order_size=5, exchange_ts=1000, recv_ts=1001,
                           source_event="book", quality_flags=["book_stale"],
                           reconnect_generation=0, code_version=CODE_V, now_ts=5)
    row = conn.execute("SELECT * FROM crypto5m_book_snapshots WHERE id=?", (sid,)).fetchone()
    assert row["best_bid"] == 0.31 and row["best_ask"] == 0.32 and row["spread"] == 0.01
    assert json.loads(row["bids"]) == [[0.31, 425.0], [0.30, 10.0]]
    assert json.loads(row["quality_flags"]) == ["book_stale"]


def test_append_chainlink_tick_idempotent(tmp_path):
    conn = _conn(tmp_path)
    t = ChainlinkTick("btc/usd", 1781948758000, 1781948758835, 1781948759,
                      63610.23870821435, "63610238708214345000000", False)
    assert P.append_chainlink_tick(conn, t, window_start_ts=1781948700) is True
    assert P.append_chainlink_tick(conn, t, window_start_ts=1781948700) is False
    row = conn.execute("SELECT * FROM crypto5m_chainlink_ticks").fetchone()
    assert row["full_accuracy_value"] == "63610238708214345000000"
    assert row["window_start_ts"] == 1781948700


def test_record_price_to_beat_upsert(tmp_path):
    conn = _conn(tmp_path)
    for q in (PriceToBeatQuality.UNAVAILABLE, PriceToBeatQuality.VALIDATED_RTDS_RECONSTRUCTION):
        P.record_price_to_beat(conn, condition_id="0xc", candidate_rule="last_at_or_before",
                               value="63456.0478", source_ts_ms=1781947200000, recv_ts=1781947201,
                               known_latency_ms=850, quality=q, gamma_oracle=63456.04789961816,
                               abs_error="0.0001", match=True, code_version=CODE_V, now_ts=5)
    rows = conn.execute("SELECT * FROM crypto5m_price_to_beat").fetchall()
    assert len(rows) == 1                                    # upsert on (condition_id, rule)
    assert rows[0]["quality"] == PriceToBeatQuality.VALIDATED_RTDS_RECONSTRUCTION.value


def test_record_settlement_and_reference_and_gap(tmp_path):
    conn = _conn(tmp_path)
    P.record_settlement(conn, Settlement("0xc", "tokU", "Up", "2026-06-20T09:25:00Z", "resolved",
                                         ("1", "0"), "gamma", "agree", 63456.04789961816),
                        code_version=CODE_V, now_ts=5)
    P.record_settlement(conn, Settlement("0xc", "tokU", "Up", "2026-06-20T09:25:00Z", "resolved",
                                         ("1", "0"), "websocket", "agree", 63456.04789961816),
                        code_version=CODE_V, now_ts=6)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_settlements").fetchone()[0] == 1  # upsert
    P.record_reference_observation(conn, ReferenceObservation("0xc", "binance", "start",
                                   1781947200, 63450.0), code_version=CODE_V, now_ts=5)
    P.record_gap(conn, CaptureGap("0xc", 1781947200, "websocket_gap", 1781947205,
                 "rest_resnapshot", 1), now_ts=5)
    c = P.counts(conn)
    assert c["crypto5m_settlements"] == 1
    assert c["crypto5m_reference_observations"] == 1
    assert c["crypto5m_capture_gaps"] == 1
