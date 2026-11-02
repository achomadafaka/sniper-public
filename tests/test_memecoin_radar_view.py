from sniper.services.memecoin_radar_view import (
    chain_label, drop_top_two, advisory_rating,
)


def test_chain_label_known_and_passthrough():
    assert chain_label("solana") == "SOL"
    assert chain_label("8453") == "Base"
    assert chain_label("4663") == "Robinhood"
    assert chain_label("143") == "143"      # unknown passes through raw
    assert chain_label(None) == "?"


def test_drop_top_two_removes_two_largest():
    assert drop_top_two([100.0, 50.0, 5.0, 5.0, -20.0]) == -10.0  # keep 5+5-20
    assert drop_top_two([10.0]) == 0.0                            # fewer than 2 -> empty
    assert drop_top_two([]) == 0.0


def test_advisory_rating_high_mcap_is_yellow():
    ep = {"chain_id": "solana", "token_address": "MINT",
          "first_source_price": 1.0, "first_executable_price": 1.0}
    q = {"id": 7, "price": 1.02, "market_cap": 40_000_000.0, "liquidity": 50_000.0,
         "observed_at": "2026-07-16T10:00:20+00:00", "source_ts": "2026-07-16T10:00:00+00:00"}
    rating, reasons = advisory_rating(ep, q, "2026-07-16T10:00:00+00:00",
                                      "2026-07-16T10:00:20+00:00")
    assert rating == "yellow"
    assert "entry_mcap_ge_30m" in reasons


def test_advisory_rating_clean_is_green():
    ep = {"chain_id": "solana", "token_address": "MINT",
          "first_source_price": 1.0, "first_executable_price": 1.0}
    q = {"id": 7, "price": 1.02, "market_cap": 500_000.0, "liquidity": 50_000.0,
         "observed_at": "2026-07-16T10:00:20+00:00", "source_ts": "2026-07-16T10:00:00+00:00"}
    rating, reasons = advisory_rating(ep, q, "2026-07-16T10:00:10+00:00",
                                      "2026-07-16T10:00:20+00:00")
    assert rating == "green", reasons
    assert reasons == []


# --- Tasks 2-4: DB-backed projection tests ---
import json as _json
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_radar_view import (
    radar_rows, coin_timeline, radar_summary,
)

NOW = "2026-07-16T10:05:00+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _episode(c, ep_id, nid, mint, **kw):
    c.execute(
        "INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, "
        "started_ts, ended_ts, status, consensus_cluster_count, first_source_price, "
        "first_executable_price, last_signal_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ep_id, nid, mint, kw.get("started_ts", "2026-07-16T10:00:00+00:00"),
         kw.get("ended_ts"), kw.get("status", "open"),
         kw.get("consensus", 1), kw.get("first_source_price", 1.0),
         kw.get("first_executable_price", 1.0),
         kw.get("last_signal_ts", "2026-07-16T10:04:00+00:00")))


def _feed(c, nid, mint, uid, **kw):
    c.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, "
        "normalized_side, source_user_id, source_handle, network_id, mint, ticker, "
        "their_price, their_size_usd, source_ts, observed_at, lag_s, was_curated, "
        "was_real_approved, raw_json, capture_version, clock_domain) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw.get("uev", f"ev-{uid}-{mint}"), "buy", kw.get("side", "buy"), uid,
         kw.get("handle", uid), nid, mint, kw.get("ticker", "TICK"),
         kw.get("their_price", 1.0), kw.get("size", 500.0),
         kw.get("source_ts", "2026-07-16T10:00:00+00:00"),
         kw.get("observed_at", "2026-07-16T10:00:03+00:00"), kw.get("lag_s", 3.0),
         1, 0, "{}", 2, "observed_real_utc"))


def _quote(c, nid, mint, **kw):
    obs = kw.get("observed_at", "2026-07-16T10:04:30+00:00")
    c.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, "
        "observed_at, source_ts, price, liquidity, market_cap, volume_24h, quality, "
        "contexts, capture_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw.get("sweep", "sw-" + obs), nid, mint, obs,
         kw.get("source_ts", "2026-07-16T10:04:29+00:00"),
         kw.get("price", 1.2), kw.get("liquidity", 60_000.0),
         kw.get("mcap", 800_000.0), kw.get("vol", 0.0), kw.get("quality", "ok"),
         "[]", 2))


def _position(c, nid, mint, strategy, **kw):
    c.execute(
        "INSERT INTO memecoin_positions(network_id, mint, strategy, execution_mode, "
        "is_open, entry_price, last_quote_price, peak_price, max_gain_pct, realized_pnl, "
        "exit_reason, opened_at, closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (nid, mint, strategy, kw.get("mode", "paper"), kw.get("is_open", 0),
         kw.get("entry", 1.0), kw.get("last", 1.4), kw.get("peak", 1.8),
         kw.get("gain", 80.0), kw.get("pnl", 12.0), kw.get("reason", "tp"),
         kw.get("opened", "2026-07-16T10:01:00+00:00"),
         kw.get("closed", "2026-07-16T10:03:00+00:00")))


def _rating(c, nid, mint, rating, source, ep_id=None):
    c.execute(
        "INSERT INTO memecoin_radar_ratings(attempt_id, episode_id, network_id, mint, "
        "signal_source, rating_version, rating, reason_codes_json, facts_json, "
        "facts_hash, created_at, clock_domain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"att-{mint}-{rating}", ep_id, nid, mint, source, 1, rating, "[]", "{}",
         "h", "2026-07-16T10:02:00+00:00", "observed_real_utc"))


def test_radar_rows_projects_watching_episode():
    c = _conn()
    _episode(c, "ep1", "solana", "MINT", first_source_price=1.0,
             first_executable_price=1.1)
    _feed(c, "solana", "MINT", "u1", ticker="ABC")
    _feed(c, "solana", "MINT", "u2")
    _quote(c, "solana", "MINT", price=1.32, mcap=430_000.0)
    rows = radar_rows(c, now_iso=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["network_id"] == "solana" and r["mint"] == "MINT"
    assert r["chain_label"] == "SOL"
    assert r["ticker"] == "ABC"
    assert r["phase"] == "watching"
    assert r["buyers"] == 2
    assert r["mcap"] == 430_000.0
    assert round(r["runup_pct"], 1) == 20.0
    assert r["attempt_rating"] is None
    assert r["advisory_rating"] in {"green", "yellow", "red"}
    assert r["age_s"] == 300.0
    assert r["us_status"] == "watching"   # no real position, no actionable decision
    assert r["heat"] == "hot"             # last signal 60s ago, not giving back


def test_radar_rows_exposes_latest_observation_wallet_and_route_truth():
    c = _conn()
    _episode(c, "ep1", "4663", "0xabc")
    _feed(c, "4663", "0xabc", "u1", handle="alice")
    _quote(c, "4663", "0xabc", price=1.2, liquidity=None, mcap=900_000.0,
           vol=44_000.0, quality="probe_ok",
           observed_at="2026-07-16T10:04:30+00:00")
    c.execute(
        "INSERT INTO memecoin_positions(source_user_id,source_handle,network_id,mint,strategy,"
        "execution_mode,is_open,realized_pnl,opened_at,closed_at) "
        "VALUES('u1','alice','4663','0xabc','alpha_b','paper',0,25.0,"
        "'2026-07-16T10:00:00+00:00','2026-07-16T10:03:00+00:00')")

    row = radar_rows(c, now_iso=NOW)[0]

    assert row["latest_price"] == 1.2
    assert row["quote_observed_at"] == "2026-07-16T10:04:30+00:00"
    assert row["quote_age_s"] == 30.0 and row["quote_state"] == "fresh"
    assert row["quote_quality"] == "probe_ok"
    assert row["liquidity"] is None and row["volume_24h"] == 44_000.0
    assert row["source_user_id"] == "u1"
    assert row["wallet_ab_pnl"] == 25.0 and row["wallet_ab_closes"] == 1
    assert row["route_state"] == "unknown"
    assert row["attribution_state"] == "not_attempted"


def test_radar_rows_marks_stale_unknown_and_proven_route_block_without_guessing():
    c = _conn()
    _episode(c, "stale", "4663", "0xstale")
    _feed(c, "4663", "0xstale", "u1")
    _quote(c, "4663", "0xstale", observed_at="2026-07-16T10:00:00+00:00")
    c.execute(
        "INSERT INTO memecoin_events(fomo_event_id,mint,network_id,source_user_id,side,created_at,"
        "signal_source,real_decision,real_decision_reason,real_decision_at) "
        "VALUES('route','0xstale','4663','u1','buy','2026-07-16T10:01:00+00:00',"
        "'global_feed','route_failed','no sell route','2026-07-16T10:01:01+00:00')")
    _episode(c, "unknown", "4663", "0xunknown")
    _feed(c, "4663", "0xunknown", "u2")

    rows = {row["mint"]: row for row in radar_rows(c, now_iso=NOW)}

    assert rows["0xstale"]["quote_state"] == "stale"
    assert rows["0xstale"]["route_state"] == "blocked"
    assert rows["0xunknown"]["latest_price"] is None
    assert rows["0xunknown"]["quote_age_s"] is None
    assert rows["0xunknown"]["quote_state"] == "unknown"
    assert rows["0xunknown"]["route_state"] == "unknown"


def test_radar_rows_us_status_hold_and_passed():
    c = _conn()
    # coin we hold (open real position)
    _episode(c, "ep-hold", "solana", "HOLD")
    _feed(c, "solana", "HOLD", "u1")
    _position(c, "solana", "HOLD", "real_live", mode="real", is_open=1,
              entry=1.0, last=1.4, gain=80.0)
    # coin we passed (mcap gate) — event carries the real_decision
    _episode(c, "ep-pass", "8453", "PASS")
    _feed(c, "8453", "PASS", "u2")
    c.execute(
        "INSERT INTO memecoin_events(mint, network_id, source_user_id, side, "
        "created_at, real_decision, real_decision_reason) "
        "VALUES('PASS','8453','u2','buy','2026-07-16T10:01:00+00:00',"
        "'skipped_mcap','unresolved')")
    rows = {r["mint"]: r for r in radar_rows(c, now_iso=NOW)}
    assert rows["HOLD"]["us_status"] == "hold"
    assert "+40%" in (rows["HOLD"]["us_detail"] or "")   # (1.4-1.0)/1.0
    assert rows["PASS"]["us_status"] == "passed"
    assert rows["PASS"]["us_detail"] == "mcap gate"


def test_radar_rows_same_address_two_chains_are_distinct():
    c = _conn()
    _episode(c, "ep-b", "8453", "0xABC")
    _episode(c, "ep-r", "4663", "0xABC")
    _feed(c, "8453", "0xABC", "u1"); _feed(c, "4663", "0xABC", "u2")
    rows = radar_rows(c, now_iso=NOW)
    keys = {(r["network_id"], r["mint"]) for r in rows}
    assert keys == {("8453", "0xABC"), ("4663", "0xABC")}


def test_radar_rows_filters_by_chain_and_fast():
    c = _conn()
    _episode(c, "ep1", "solana", "M1")
    _feed(c, "solana", "M1", "u1", source_ts="2026-07-16T10:04:50+00:00")
    _episode(c, "ep2", "8453", "M2")
    _feed(c, "8453", "M2", "u2", source_ts="2026-07-16T09:00:00+00:00")
    assert {r["mint"] for r in radar_rows(c, chain="solana", now_iso=NOW)} == {"M1"}
    assert {r["mint"] for r in radar_rows(c, fast=True, now_iso=NOW)} == {"M1"}


def test_coin_timeline_separates_claimed_and_observed():
    c = _conn()
    _episode(c, "ep1", "solana", "MINT")
    _feed(c, "solana", "MINT", "u1", source_ts="2026-07-16T10:00:00+00:00",
          observed_at="2026-07-16T10:00:03+00:00", lag_s=3.0)
    _quote(c, "solana", "MINT", observed_at="2026-07-16T10:00:05+00:00", price=1.1)
    _quote(c, "solana", "MINT", observed_at="2026-07-16T10:01:05+00:00", price=1.5)
    _position(c, "solana", "MINT", "alpha_b", pnl=25.0)
    tl = coin_timeline(c, "solana", "MINT", now_iso=NOW)
    assert tl["chain_label"] == "SOL"
    assert tl["episode"]["episode_id"] == "ep1"
    s = tl["signals"][0]
    assert s["claimed_source_ts"] == "2026-07-16T10:00:00+00:00"
    assert s["observed_at"] == "2026-07-16T10:00:03+00:00"
    assert s["lag_s"] == 3.0
    assert len(tl["price_path"]) == 2
    assert tl["price_path"][0]["observed_at"] < tl["price_path"][1]["observed_at"]
    assert tl["arm_positions"][0]["strategy"] == "alpha_b"


def test_radar_summary_arms_outcomes_and_drop_top2():
    c = _conn()
    _episode(c, "ep1", "solana", "MINT")
    _rating(c, "solana", "MINT", "green", "global_feed", "ep1")
    _rating(c, "solana", "MINT", "yellow", "global_feed", "ep1")
    for pnl in (100.0, 60.0, 5.0, -20.0):
        _position(c, "solana", "MINT", "alpha_b", pnl=pnl)
    s = radar_summary(c, now_iso=NOW)
    key = "SOL/global_feed"
    assert s["outcomes_by_chain_source"][key] == {"green": 1, "yellow": 1, "red": 0}
    arm = next(a for a in s["paper_arms"] if a["arm"] == "alpha_b")
    assert arm["closed"] == 4
    assert arm["pnl"] == 145.0
    assert arm["pnl_drop_top2"] == -15.0


def test_radar_summary_candidate_gate_progress():
    from sniper.research.memecoin_strategy.store import ensure_strategy_tables
    c = _conn()
    ensure_strategy_tables(c)
    c.execute(
        "INSERT INTO memecoin_strategy_candidates(candidate_id, name, status, spawn_index, "
        "generation, predicate_json, exit_policy_json, sizing_json, costs_json, "
        "controls_json, gates_json, params_hash, born_at, discovery_cutoff, "
        "capture_version_floor, execution_mode, timecap_s, alpha_alloc, notes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c1", "peak_stall", "candidate_active", 1, 0, "{}", "{}", "{}", "{}", "{}",
         _json.dumps({"paired_closes_per_chain": "30-50"}), "p", "2026-07-16T00:00:00+00:00",
         "2026-07-16T00:00:00+00:00", 2, "paper", 600, 0.5, ""))
    c.execute(
        "INSERT INTO memecoin_candidate_positions(candidate_id, signal_id, episode_key, "
        "network_id, mint, evidence_stage, execution_mode, timecap_s, entry_price, "
        "entry_notional, quantity, entry_cost, opened_at, status, stop_hits) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c1", 1, "solana:MINT", "solana", "MINT", "stage1", "paper", 600, 1.0,
         10.0, 10.0, 10.0, "2026-07-16T10:00:00+00:00", "closed", 0))
    s = radar_summary(c, now_iso=NOW)
    cand = s["candidates"][0]
    assert cand["candidate_id"] == "c1"
    assert cand["paired_closes_target"] == "30-50"
    assert cand["closed_by_chain"] == {"SOL": 1}


class _SpyConn:
    def __init__(self, real):
        self._real = real; self.writes = []
    def execute(self, sql, *a, **k):
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in {"INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE"}:
            self.writes.append(sql)
        return self._real.execute(sql, *a, **k)
    def __getattr__(self, name):
        return getattr(self._real, name)


def test_radar_view_never_writes():
    real = _conn()
    _episode(real, "ep1", "solana", "MINT")
    _feed(real, "solana", "MINT", "u1"); _quote(real, "solana", "MINT")
    _position(real, "solana", "MINT", "alpha_b")
    spy = _SpyConn(real)
    radar_rows(spy, now_iso=NOW)
    radar_summary(spy, now_iso=NOW)
    coin_timeline(spy, "solana", "MINT", now_iso=NOW)
    assert spy.writes == []
