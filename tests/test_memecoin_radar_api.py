import sqlite3
from datetime import datetime, timedelta

from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.util.clock import real_utc_now_iso
from _web_csrf import csrf_client


def _client():
    """Build an app over an in-memory DB seeded with one fresh episode.

    Timestamps are derived from real_utc_now_iso() — the same corrected-UTC
    clock domain radar_rows() uses for its recency window — so the seeded
    episode is always inside the default 24h window. Hardcoded dates rot:
    they pass on the day they're written and fail the next day.
    """
    now = datetime.fromisoformat(real_utc_now_iso())
    started = (now - timedelta(minutes=5)).isoformat()
    obs_at = (now - timedelta(minutes=5, seconds=-3)).isoformat()
    last_sig = (now - timedelta(minutes=1)).isoformat()
    q_src = (now - timedelta(seconds=31)).isoformat()
    q_at = (now - timedelta(seconds=30)).isoformat()

    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute(
        "INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, "
        "started_ts, status, consensus_cluster_count, first_source_price, "
        "first_executable_price, last_signal_ts) VALUES('ep1','solana','MINT',"
        "?,'open',2,1.0,1.1,?)", (started, last_sig))
    c.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, "
        "normalized_side, source_user_id, source_handle, network_id, mint, ticker, "
        "source_ts, observed_at, lag_s, was_curated, was_real_approved, raw_json, "
        "capture_version, clock_domain) VALUES('e1','buy','buy','u1','u1','solana',"
        "'MINT','ABC',?,?,3.0,1,0,'{}',2,'observed_real_utc')", (started, obs_at))
    c.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, "
        "observed_at, source_ts, price, liquidity, market_cap, volume_24h, quality, "
        "contexts, capture_version) VALUES('s1','solana','MINT',"
        "?,?,1.3,60000,430000,0,'ok','[]',2)", (q_at, q_src))
    c.commit()  # close the transaction so the blueprint's ensure_* can run
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return csrf_client(app), {"started": started, "obs_at": obs_at}


def test_radar_list_endpoint():
    client, _ = _client()
    r = client.get("/api/memecoin/radar").get_json()
    assert "episodes" in r and len(r["episodes"]) == 1
    assert r["episodes"][0]["chain_label"] == "SOL"
    assert r["episodes"][0]["phase"] == "watching"


def test_radar_summary_endpoint():
    client, _ = _client()
    r = client.get("/api/memecoin/radar/summary").get_json()
    assert "paper_arms" in r and "outcomes_by_chain_source" in r
    assert "candidates" in r


def test_radar_timeline_endpoint():
    client, seeds = _client()
    r = client.get("/api/memecoin/radar/solana/MINT").get_json()
    assert r["chain_label"] == "SOL"
    assert r["signals"][0]["claimed_source_ts"] == seeds["started"]
    assert r["signals"][0]["observed_at"] == seeds["obs_at"]


def test_radar_list_chain_filter():
    client, _ = _client()
    r = client.get("/api/memecoin/radar?chain=8453").get_json()
    assert r["episodes"] == []


def test_missed_entry_report_endpoint_is_read_only_projection():
    client, _ = _client()
    response = client.get("/api/memecoin/missed-entry-report?hours=12&chain=4663")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["policy"] == "diagnostic_60_60_v1"
    assert payload["window_hours"] == 12.0
    assert payload["summary"]["deduped_episodes"] == 0


def test_latency_report_endpoint_returns_thin_sample_truth():
    client, _ = _client()
    response = client.get("/api/memecoin/latency-report?hours=48&chain=4663")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["window_hours"] == 48.0
    assert payload["attempts"] == 0
    assert payload["substages"] == []


def test_shadow_signal_endpoint_is_read_only_and_filterable():
    client, _ = _client()
    response = client.get("/api/memecoin/shadow-signals?chain=4663&source=global_feed")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {"signals": []}


def test_wallet_recommendations_endpoint_is_read_only():
    client, _ = _client()
    response = client.get(
        "/api/memecoin/wallets/recommendations?chain=4663&days=14&min_paired=7"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["window_days"] == 14.0
    assert payload["min_paired"] == 7
    assert payload["exact_event_only"] is True
    assert payload["recommendations"] == []


def test_exit_policy_tournament_endpoint_is_read_only():
    client, _ = _client()
    response = client.get(
        "/api/memecoin/exit-policy-tournament?chain=4663&days=21&min_paired=40"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["window_days"] == 21.0
    assert payload["min_paired"] == 40
    assert payload["exact_origin_only"] is True
    assert payload["legacy_matching_used"] is False
    assert payload["segments"] == []


def test_coin_radar_endpoints_reject_invalid_or_unbounded_query_values():
    client, _ = _client()

    for path in (
        "/api/memecoin/universe?limit=bad",
        "/api/memecoin/universe?window_hours=nan",
        "/api/memecoin/universe?min_liq=-1",
        "/api/memecoin/universe?limit=100000",
        "/api/memecoin/radar/triage?top_n=bad",
        "/api/memecoin/coin-search?q=MINT&limit=bad",
    ):
        response = client.post(path) if "/triage" in path else client.get(path)
        assert response.status_code == 400, path
        assert response.get_json()["error"] == "invalid_query"
