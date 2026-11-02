"""Offline replay over a synthetic capture DB — end-to-end shadow output, no live APIs."""
from __future__ import annotations

import json
import os

from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import probability_model as PM
from sniper.algos.polymarket_crypto5m import replay as R
from sniper.algos.polymarket_crypto5m import settlement as S
from sniper.algos.polymarket_crypto5m import sim_persistence as SP
from sniper.algos.polymarket_crypto5m.models import ChainlinkTick

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _capture_db(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "cap.sqlite"))
    P.ensure_schema(conn)
    with open(os.path.join(FX, "event_resolved_up.json")) as f:
        spec, _ = MR.parse_event(json.load(f))
    P.upsert_market(conn, spec, code_version="t", source_lineage="gamma", now_ts=1)
    ws = spec.window_start_ts
    # chainlink ticks across the window (1/s), known immediately
    for i in range(-10, 300):
        t = ChainlinkTick("btc/usd", (ws + i) * 1000, None, ws + i, 63456.0 + i * 0.01,
                          str(int((63456.0 + i * 0.01) * 10**18)), False)
        P.append_chainlink_tick(conn, t, window_start_ts=ws)
    # one book snapshot per token, available from window_start+10
    for tok, side in ((spec.up_token_id, "Up"), (spec.down_token_id, "Down")):
        P.write_snapshot(conn, condition_id=spec.condition_id, token_id=tok, side=side,
                         bids=[[0.49, 1000]], asks=[[0.50, 1000]], best_bid=0.49, best_ask=0.50,
                         spread=0.01, book_hash="h", tick_size=0.01, min_order_size=5,
                         exchange_ts=ws + 10, recv_ts=ws + 10, source_event="periodic",
                         quality_flags=[], reconnect_generation=0, code_version="t", now_ts=ws + 10)
    P.record_settlement(conn, S.from_gamma(spec), code_version="t", now_ts=ws + 300)
    SP.ensure_sim_schema(conn)
    SP.record_fee_config(conn, dict(condition_id=spec.condition_id, fetched_ts=ws, fees_enabled=True,
                         taker_base_fee=1000, fee_rate=None, fee_status="fee_conflict",
                         observed_fee_rate_bps=0, decoder_version="v0", source="clob"), now_ts=ws)
    return conn, spec


def test_replay_produces_shadow_predictions_no_entries(tmp_path):
    conn, spec = _capture_db(tmp_path)
    summary = R.replay(conn, model=PM.MarketMidpointBaseline(), now_ts=0)
    assert summary["windows"] == 1
    assert summary["snapshots"] == len(R.FE.CHECKPOINTS)
    assert summary["predictions"] == len(R.FE.CHECKPOINTS)
    assert summary["settlement_covered"] == 1
    # boundary unlocked + uncalibrated baseline -> all shadow_prediction, NO entries
    assert summary["paper_entries"] == 0
    assert summary["by_status"].get("shadow_prediction", 0) >= 1
    assert "paper_entry" not in summary["by_status"]
    assert any("boundary" in b for b in summary["paper_activation_blockers"])
    # decisions actually persisted
    n = conn.execute("SELECT COUNT(*) FROM crypto5m_shadow_decisions").fetchone()[0]
    assert n == len(R.FE.CHECKPOINTS)


def test_replay_is_idempotent(tmp_path):
    conn, spec = _capture_db(tmp_path)
    R.replay(conn, model=PM.MarketMidpointBaseline(), now_ts=0)
    R.replay(conn, model=PM.MarketMidpointBaseline(), now_ts=1)   # re-run
    # content-keyed decisions/predictions/snapshots do not duplicate
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_shadow_decisions").fetchone()[0] == len(R.FE.CHECKPOINTS)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_feature_snapshots").fetchone()[0] == len(R.FE.CHECKPOINTS)


def test_threshold_diffusion_model_also_shadow_only(tmp_path):
    conn, spec = _capture_db(tmp_path)
    summary = R.replay(conn, model=PM.ThresholdDiffusionV0(), now_ts=0)
    assert summary["paper_entries"] == 0                 # diagnostic model never enters
    assert summary["by_status"].get("shadow_prediction", 0) >= 1


def test_format_summary_labels_diagnostic(tmp_path):
    conn, spec = _capture_db(tmp_path)
    summary = R.replay(conn, model=PM.MarketMidpointBaseline(), now_ts=0)
    text = R.format_summary(summary)
    assert "NOT strategy performance" in text and "shadow" in text.lower()


def test_feature_book_is_decision_time_not_post_latency_execution_book(tmp_path):
    # FIX: with latency>0 the FEATURE must use the decision-time book (<= cp), not the future fill book
    conn = P.connect(os.path.join(str(tmp_path), "lat.sqlite"))
    P.ensure_schema(conn)
    with open(os.path.join(FX, "event_resolved_up.json")) as f:
        spec, _ = MR.parse_event(json.load(f))
    P.upsert_market(conn, spec, code_version="t", source_lineage="gamma", now_ts=1)
    ws = spec.window_start_ts
    for i in range(-10, 300):
        P.append_chainlink_tick(conn, ChainlinkTick("btc/usd", (ws + i) * 1000, None, ws + i,
                                63456.0, str(int(63456.0 * 10**18)), False), window_start_ts=ws)
    for tok in (spec.up_token_id, spec.down_token_id):
        # decision-time book (mid 0.50) at ws+12, then a FUTURE book (mid 0.80) at ws+18
        P.write_snapshot(conn, condition_id=spec.condition_id, token_id=tok, side="Up",
                         bids=[[0.49, 1000]], asks=[[0.51, 1000]], best_bid=0.49, best_ask=0.51,
                         spread=0.02, book_hash="a", tick_size=0.01, min_order_size=5,
                         exchange_ts=ws + 12, recv_ts=ws + 12, source_event="periodic",
                         quality_flags=[], reconnect_generation=0, code_version="t", now_ts=ws + 12)
        P.write_snapshot(conn, condition_id=spec.condition_id, token_id=tok, side="Up",
                         bids=[[0.79, 1000]], asks=[[0.81, 1000]], best_bid=0.79, best_ask=0.81,
                         spread=0.02, book_hash="b", tick_size=0.01, min_order_size=5,
                         exchange_ts=ws + 18, recv_ts=ws + 18, source_event="periodic",
                         quality_flags=[], reconnect_generation=0, code_version="t", now_ts=ws + 18)
    # checkpoint 15 -> feature_ts ws+15 (book A only), exec ws+15+5=ws+20 (book B visible)
    R.replay(conn, model=PM.MarketMidpointBaseline(), checkpoints=(15,), latency_ms=5000, now_ts=0)
    row = conn.execute("SELECT features_json FROM crypto5m_feature_snapshots WHERE checkpoint=15"
                       ).fetchone()
    feats = json.loads(row[0])
    assert feats["up_midpoint_probability"] == 0.50   # decision-time book, NOT the future 0.80 book


def test_exec_ts_preserves_subsecond_latency():
    # Codex #5: latency_ms // 1000 collapsed all sub-second latencies to 0 (identical to no latency).
    assert R._exec_ts(1060, 0) == 1060
    assert R._exec_ts(1060, 250) == 1060.25          # was truncated to 1060
    assert R._exec_ts(1060, 999) == 1060.999         # was truncated to 1060
    assert R._exec_ts(1060, 1500) == 1061.5          # was truncated to 1061 (lost 500ms)
    assert R._exec_ts(1060, 250) > R._exec_ts(1060, 0)   # sub-second latency is no longer a no-op


def test_replay_populates_binance_family_features(tmp_path):
    # End-to-end: persisted Binance observations flow into the snapshot's family-C features via replay.
    from sniper.algos.polymarket_crypto5m.binance_feed import BinanceObservation
    conn, spec = _capture_db(tmp_path)
    ws = spec.window_start_ts
    for i in range(-10, 300):
        P.record_binance_observation(conn, BinanceObservation(
            "BTCUSDT", (ws + i) * 1000, ws + i, 63456.0 + i * 0.01, None, None, None, "rtds_price", {}),
            now_ts=ws + i)
    R.replay(conn, model=PM.MarketMidpointBaseline(), now_ts=0)
    row = conn.execute("SELECT features_json FROM crypto5m_feature_snapshots WHERE checkpoint=60"
                       ).fetchone()
    feats = json.loads(row[0])
    assert feats["binance_price_now"] is not None
    assert "binance_return_30s" in feats
    assert feats["binance_volume_30s"] is None       # rtds price-only -> no fabricated volume
    assert "basis_quality" in feats and "up_executable_price_10_usdc" in feats


def test_replay_ignores_non_btc_binance_obs(tmp_path):
    from sniper.algos.polymarket_crypto5m.binance_feed import BinanceObservation
    conn, spec = _capture_db(tmp_path)
    ws = spec.window_start_ts
    P.record_binance_observation(conn, BinanceObservation(
        "BTCUSDT", (ws + 50) * 1000, ws + 50, 63456.0, None, None, None, "rtds_price", {}), now_ts=ws + 50)
    P.record_binance_observation(conn, BinanceObservation(
        "ETHUSDT", (ws + 55) * 1000, ws + 55, 3000.0, None, None, None, "rtds_price", {}), now_ts=ws + 55)
    R.replay(conn, model=PM.MarketMidpointBaseline(), now_ts=0)
    row = conn.execute("SELECT features_json FROM crypto5m_feature_snapshots WHERE checkpoint=60"
                       ).fetchone()
    feats = json.loads(row[0])
    assert feats["binance_price_now"] == 63456.0     # ETH 3000 did NOT become BTC's price


def test_different_params_do_not_overwrite_decisions(tmp_path):
    conn, spec = _capture_db(tmp_path)
    R.replay(conn, model=PM.MarketMidpointBaseline(), min_edge=0.0, now_ts=0)
    R.replay(conn, model=PM.MarketMidpointBaseline(), min_edge=0.5, now_ts=1)   # different params
    # distinct params_sig -> distinct decision rows (no silent overwrite)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_shadow_decisions").fetchone()[0] == 2 * len(R.FE.CHECKPOINTS)
    # feature snapshots are latency/param-independent -> NOT duplicated
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_feature_snapshots").fetchone()[0] == len(R.FE.CHECKPOINTS)


def test_shadow_predictions_scored_against_settlement_winner(tmp_path):
    conn, spec = _capture_db(tmp_path)   # settles Up
    summary = R.replay(conn, model=PM.MarketMidpointBaseline(), now_ts=0)
    sc = summary["diagnostic_scoring"].get("market_midpoint_baseline")
    assert sc is not None and sc["n"] >= 1
    assert sc["brier"] is not None and "NOT strategy performance" in sc["label"]
