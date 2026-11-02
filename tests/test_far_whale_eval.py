"""fwlm-v0 controls — shuffle-side + trend-matched generation (§9 C2-C3)."""
from sniper.hyperlive import far_whale_store as S
from sniper.hyperlive import far_whale_eval as E

R = 180_000   # poll round


def _seed_series(conn, asset="ETH", t0=0, n=40, mid0=3000.0, drift=0.0):
    for i in range(n):
        S.save_market_state(conn, {asset: (mid0 * (1 + drift * i), 1e6, 5e8, 2.0)},
                            ts_ms=t0 + i * R)


def _mk_settled_entry(conn, *, entry_ts, direction="down", d_bps=166.7):
    eid, _ = S.assign_episode(conn, {
        "asset": "ETH", "whale_address": "0xabc", "liquidation_price": 2950.0,
        "mid_price": 3000.0, "distance_bps": d_bps,
        "direction_to_liquidation": direction, "position_side": "long",
        "liquidation_type": "long_liquidation", "position_notional": 2e6,
        "position_size": 100.0, "spread_bps": 2.0}, signal_ts_ms=entry_ts)
    cid = S.insert_candidate(conn, {
        "asset": "ETH", "whale_address": "0xabc", "liquidation_price": 2950.0,
        "mid_price": 3000.0, "distance_bps": d_bps,
        "direction_to_liquidation": direction, "position_side": "long",
        "liquidation_type": "long_liquidation", "position_notional": 2e6,
        "position_size": 100.0, "spread_bps": 2.0},
        episode_id=eid, signal_ts_ms=entry_ts, is_primary=1, source_snapshot_hash="h")
    entry_id = S.insert_entry(conn, {
        "candidate_id": cid, "episode_id": eid, "asset": "ETH",
        "direction_to_liquidation": direction, "entry_ts_ms": entry_ts,
        "entry_price": 3000.0, "paper_notional": 1000.0, "d_bps": d_bps,
        "target_price": 2962.5, "adverse_stop_price": 3025.0,
        "liquidation_price": 2950.0, "spread_bps": 2.0, "is_primary": 1})
    S.settle_entry(conn, entry_id, {
        "exit_ts_ms": entry_ts + 10 * R, "exit_price": 2990.0, "exit_reason": "time_stop",
        "paper_pnl": 0.5, "mfe_bps": 40.0, "mae_bps": -10.0,
        "migrated_toward_liq": 0, "hit_liq_level": 0, "liq_fired": 0,
        "position_persisted": 1, "horizon_ms": 1_800_000})
    return entry_id


def _conn():
    c = S.connect(":memory:")
    S.ensure_schema(c)
    return c


def test_features_at_needs_full_lookback():
    conn = _conn()
    _seed_series(conn, t0=0, n=40)
    assert E.features_at(conn, "ETH", 2 * R) is None          # lookback incomplete
    f = E.features_at(conn, "ETH", 20 * R)
    assert set(f) == {"ret_30m_bps", "vol_30m_bps", "spread_bps",
                      "oi_delta_30m_pct", "hour_of_day"}


def test_shuffle_control_flips_direction_same_path():
    conn = _conn()
    # falling path: down-treatment wins, flipped 'up' control must lose
    for i in range(15):
        S.save_market_state(conn, {"ETH": (3000.0 - i * 5, 1e6, 5e8, 2.0)}, ts_ms=i * R)
    entry_id = _mk_settled_entry(conn, entry_ts=0)
    entry = dict(conn.execute("SELECT * FROM hl_far_whale_liq_paper_entries"
                              " WHERE entry_id=?", (entry_id,)).fetchone())
    ctl = E.shuffle_control(conn, entry)
    assert ctl is not None and ctl["direction_to_liquidation"] == "up"
    assert ctl["paper_pnl"] < 0


def test_trend_match_excludes_candidate_adjacent_moments():
    conn = _conn()
    _seed_series(conn, t0=0, n=200)
    entry_ts = 100 * R
    entry_id = _mk_settled_entry(conn, entry_ts=entry_ts)
    entry = dict(conn.execute("SELECT * FROM hl_far_whale_liq_paper_entries"
                              " WHERE entry_id=?", (entry_id,)).fetchone())
    ctl = E.trend_matched_control(conn, entry)
    assert ctl is not None
    assert abs(ctl["matched_ts_ms"] - entry_ts) >= 3_600_000   # outside exclusion window


def test_generate_controls_writes_both_rows_once():
    conn = _conn()
    _seed_series(conn, t0=0, n=200)
    _mk_settled_entry(conn, entry_ts=100 * R)
    r1 = E.generate_controls(conn)
    r2 = E.generate_controls(conn)          # idempotent — UNIQUE(entry_id, control_type)
    assert r1["shuffle"] == 1 and r1["trend"] == 1
    assert r2["shuffle"] == 0 and r2["trend"] == 0
    n = conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_controls").fetchone()[0]
    assert n == 2


def test_episode_ci_deterministic_and_clustered():
    by_ep = {f"e{i}": [10.0] for i in range(30)}
    a = E.episode_ci(by_ep)
    b = E.episode_ci(by_ep)
    assert a == b                       # fixed seed
    assert a[0] == 10.0 and a[1] <= 10.0 <= a[2]
    assert E.episode_ci({}) is None


def test_dose_response_orders_by_notional_over_distance():
    rows = ([{"position_notional": 5e6, "distance_bps": 60.0, "migrated_toward_liq": 1}] * 10
            + [{"position_notional": 1e6, "distance_bps": 150.0, "migrated_toward_liq": 1}] * 5
            + [{"position_notional": 1e6, "distance_bps": 150.0, "migrated_toward_liq": 0}] * 5
            + [{"position_notional": 2e5, "distance_bps": 290.0, "migrated_toward_liq": 0}] * 10)
    dr = E.dose_response(rows)
    assert dr["exists"] is True
    flat = E.dose_response([{"position_notional": 1e6, "distance_bps": 100.0,
                             "migrated_toward_liq": i % 2} for i in range(30)])
    assert flat["exists"] is False


def test_concentration_flags_single_whale_dominance():
    rows = [{"whale_address": "0xa", "paper_pnl": 90.0},
            {"whale_address": "0xb", "paper_pnl": 5.0},
            {"whale_address": "0xc", "paper_pnl": 5.0}]
    c = E.concentration(rows)
    assert c["max_pnl_share"] == 0.9 and c["ok"] is False


def test_evaluate_no_freeze_and_insufficient():
    conn = _conn()
    v = E.evaluate(conn, now_ms=1_000_000)
    assert v["status"] == "no_freeze"
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1e6}, "campaign_start_ts_ms": 0},
                  ts_ms=0)
    v = E.evaluate(conn, now_ms=1_000_000)
    assert v["status"] == "insufficient"


def test_evaluate_sparsity_kill_after_campaign_cap():
    conn = _conn()
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1e6}, "campaign_start_ts_ms": 0},
                  ts_ms=0)
    v = E.evaluate(conn, now_ms=61 * 86_400_000)     # 61 days, counts unmet
    assert v["status"] == "sparsity_killed"


def test_verdict_status_decision_logic():
    # clean pass
    assert E._verdict_status([], (5.0, 1.0, 9.0)) == "pass"
    # point-positive but CI spans 0, only the pnl reason -> decayed (not a pass)
    assert E._verdict_status(["net_pnl_ci_not_positive"], (2.0, -1.0, 5.0)) == "decayed"
    # net-pnl CI upper < 0 -> hard kill
    assert E._verdict_status(["net_pnl_ci_not_positive"], (-2.0, -5.0, -0.5)) == "kill"
    # §9: trend (C3) failure kills REGARDLESS of a positive pnl CI (no partial credit)
    assert E._verdict_status(["trend_delta_ci_not_positive"], (2.0, 1.0, 5.0)) == "kill"
    assert E._verdict_status(["no_dose_response"], (2.0, 1.0, 5.0)) == "kill"
    assert E._verdict_status(["concentration_breach"], (2.0, 1.0, 5.0)) == "kill"
    # only the shuffle control unmet (not structural), CI not upper<0 -> decayed
    assert E._verdict_status(["shuffle_delta_ci_not_positive"], (2.0, 1.0, 5.0)) == "decayed"


def _settled(conn, *, asset, whale, entry_ts, direction="down", d_bps=166.7,
             pnl=0.5, migrated=0):
    mid = 3000.0 if asset == "ETH" else 60000.0
    liq = mid * (1 - d_bps / 1e4)                     # below mid -> down (long_liquidation)
    cand = {"asset": asset, "whale_address": whale, "liquidation_price": liq,
            "mid_price": mid, "distance_bps": d_bps, "direction_to_liquidation": direction,
            "position_side": "long", "liquidation_type": "long_liquidation",
            "position_notional": 2e6, "position_size": 100.0, "spread_bps": 2.0}
    eid, _ = S.assign_episode(conn, cand, signal_ts_ms=entry_ts)
    cid = S.insert_candidate(conn, cand, episode_id=eid, signal_ts_ms=entry_ts,
                             is_primary=1, source_snapshot_hash="h")
    entry_id = S.insert_entry(conn, {
        "candidate_id": cid, "episode_id": eid, "asset": asset,
        "direction_to_liquidation": direction, "entry_ts_ms": entry_ts,
        "entry_price": mid, "paper_notional": 1000.0, "d_bps": d_bps,
        "target_price": mid * (1 - 0.75 * d_bps / 1e4),
        "adverse_stop_price": mid * (1 + 0.5 * d_bps / 1e4),
        "liquidation_price": liq, "spread_bps": 2.0, "is_primary": 1})
    S.settle_entry(conn, entry_id, {
        "exit_ts_ms": entry_ts + 10 * R, "exit_price": mid * 0.997, "exit_reason": "time_stop",
        "paper_pnl": pnl, "mfe_bps": 40.0, "mae_bps": -10.0,
        "migrated_toward_liq": migrated, "hit_liq_level": 0, "liq_fired": 0,
        "position_persisted": 1, "horizon_ms": 1_800_000})
    return entry_id


def test_evaluate_full_path_reaches_verdict(monkeypatch):
    conn = _conn()
    for i in range(250):
        for a, m in (("ETH", 3000.0), ("BTC", 60000.0)):
            S.save_market_state(conn, {a: (m - i * (m * 1e-5), 1e6, 5e8, 2.0)}, ts_ms=i * R)
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1e6, "BTC": 1e6}, "campaign_start_ts_ms": 0},
                  ts_ms=0)
    for asset, whale, r0 in [("ETH", "0xa", 50), ("ETH", "0xb", 90),
                             ("BTC", "0xc", 130), ("BTC", "0xd", 170)]:
        _settled(conn, asset=asset, whale=whale, entry_ts=r0 * R)
    E.generate_controls(conn)
    monkeypatch.setattr(E, "PASS_MIN_EPISODES", 3)
    monkeypatch.setattr(E, "PASS_MIN_OUTCOMES", 3)
    monkeypatch.setattr(E, "PASS_MIN_WHALES", 2)
    monkeypatch.setattr(E, "PASS_MIN_ASSETS", 2)
    v = E.evaluate(conn, now_ms=260 * R)
    assert v["status"] in {"pass", "decayed", "kill"}       # bar_met -> a real terminal verdict
    for k in ("ci_net_pnl", "ci_delta_shuffle", "ci_delta_trend", "dose_response",
              "concentration", "pnl_breakdown", "persistence_diagnostic"):
        assert k in v
    assert set(v["pnl_breakdown"]) == {"by_asset", "by_whale", "by_event"}
    assert v["counts"]["distinct_whales"] == 4 and v["counts"]["assets"] == 2
