import os

from sniper.hyperlive import universe as U
from sniper.hyperlive import scan_log as SL


def _ctx(name, vol=1e8, mark=100, oracle=100, mid=100, funding=0.0, premium=0.0, prev=100, oi=1e6):
    return {"name": name, "ctx": {"dayNtlVlm": str(vol), "markPx": str(mark), "oraclePx": str(oracle),
                                  "midPx": str(mid), "funding": str(funding), "premium": str(premium),
                                  "prevDayPx": str(prev), "openInterest": str(oi)}}


def test_score_all_includes_every_market_ranked():
    rows = U.score_all([_ctx("BIG", vol=2e9), _ctx("TINY", vol=1e5, mark=110, oracle=100), _ctx("MID", vol=1e8)])
    assert len(rows) == 3                              # untradeable TINY is NOT dropped
    assert [r["rank"] for r in sorted(rows, key=lambda r: r["rank"])] == [1, 2, 3]
    assert rows[0]["rank"] == 1                        # highest score ranked first
    tiny = next(r for r in rows if r["asset"] == "TINY")
    assert tiny["tradeable"] is False and "ctx" in tiny


def test_change_signals_capture_basis_acceleration_and_oi_delta():
    cur = {"markPx": "101", "oraclePx": "100", "midPx": "101", "openInterest": "1300000", "premium": "0"}
    prev = {"markPx": "100.5", "oraclePx": "100", "midPx": "100.5", "openInterest": "1000000", "premium": "0"}
    prev2 = {"markPx": "100.2", "oraclePx": "100", "midPx": "100.2", "openInterest": "900000", "premium": "0"}
    cs = U.change_signals(cur, prev, prev2)
    assert round(cs["basis_change_bps"]) == 50         # basis went 50bps -> 100bps
    assert round(cs["basis_accel_bps"]) == 20          # change accelerated (50 vs prior 30)
    assert round(cs["oi_delta_pct"]) == 30             # OI +30%
    assert cs["change_score_v0"] > 0


def test_change_signals_handle_missing_prior():
    cs = U.change_signals({"markPx": "101", "oraclePx": "100"})   # no prev
    assert cs["basis_change_bps"] == 0.0 and cs["change_score_v0"] == 0.0


def test_scan_log_persists_all_rows(tmp_path):
    conn = SL.connect(os.path.join(str(tmp_path), "scan.sqlite"))
    SL.ensure_schema(conn)
    rows = [
        {"asset": "BTC", "selected_rank": 1, "selected_bool": True, "already_watched_bool": True,
         "hand_score_v0": 12.3, "change_score_v0": 4.1, "score_components": {"x": 1}, "change_components": {"y": 2},
         "lead_reason": "24h move", "day_ntl_vlm": 2e9, "mark_px": 100, "oracle_px": 100, "mid_px": 100,
         "funding": 0.0, "premium": 0.0, "prev_day_px": 99, "open_interest": 1e6, "prior_open_interest": 9e5, "oi_delta": 0.11},
        {"asset": "DOGE", "selected_rank": 88, "selected_bool": False, "already_watched_bool": False,
         "hand_score_v0": 2.0, "change_score_v0": 0.0, "lead_reason": "liquid baseline"},
    ]
    assert SL.log_refresh(conn, 1000, rows, refresh_interval_s=150, watchlist_state="BTC,ETH") == 2
    n = conn.execute("SELECT COUNT(*) FROM hyperlive_scan_log").fetchone()[0]
    sel = conn.execute("SELECT COUNT(*) FROM hyperlive_scan_log WHERE selected_bool=1").fetchone()[0]
    assert n == 2 and sel == 1


def test_scan_log_persists_binance_basis(tmp_path):
    conn = SL.connect(os.path.join(str(tmp_path), "basis.sqlite"))
    SL.ensure_schema(conn)
    SL.log_refresh(conn, 1000, [
        {"asset": "BTC", "mid_px": 100.0, "day_ntl_vlm": 1e8, "binance_basis_bps": 4.2},
        {"asset": "MET", "mid_px": 1.0, "day_ntl_vlm": 1e7},                # unmapped → NULL
    ], refresh_interval_s=150)
    got = dict(conn.execute("SELECT asset, binance_basis_bps FROM hyperlive_scan_log").fetchall())
    assert got["BTC"] == 4.2 and got["MET"] is None


def test_scan_log_migrates_old_schema(tmp_path):
    import sqlite3
    p = os.path.join(str(tmp_path), "old.sqlite")
    # an old-schema log WITHOUT the basis column
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE hyperlive_scan_log (id INTEGER PRIMARY KEY, scan_ts INTEGER, asset TEXT, "
              "mid_px REAL, day_ntl_vlm REAL, selected_bool INTEGER)")
    c.commit(); c.close()
    conn = SL.connect(p)
    SL.ensure_schema(conn)                                                  # must ALTER, not crash
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hyperlive_scan_log)")}
    assert "binance_basis_bps" in cols


def test_forward_labels_compute_move_dir_and_cost_clearing():
    from sniper.hyperlive import scan_audit as SA
    rows = [
        {"asset": "BTC", "scan_ts": 0, "mid": 100.0},
        {"asset": "BTC", "scan_ts": 150_000, "mid": 101.0},   # +100bps at 150s
        {"asset": "BTC", "scan_ts": 300_000, "mid": 99.0},    # -100bps from start at 300s
    ]
    labs = SA.forward_labels(rows)
    first = next(r for r in labs if r["scan_ts"] == 0)
    assert first["abs_move_150s_bps"] == 100.0 and first["dir_150s"] == 1
    assert first["cost_clearing_150s"] is True            # 100bps > 10bps hurdle
    assert first["abs_move_300s_bps"] == 100.0 and first["dir_300s"] == -1


def test_forward_labels_missing_future_is_none():
    from sniper.hyperlive import scan_audit as SA
    labs = SA.forward_labels([{"asset": "X", "scan_ts": 0, "mid": 100.0},
                              {"asset": "X", "scan_ts": 150_000, "mid": 100.05}])  # +5bps < hurdle
    first = labs[0]
    assert first["cost_clearing_150s"] is False           # 5bps doesn't clear 10bps
    assert first["abs_move_300s_bps"] is None             # no 300s row


def test_write_labels_roundtrips(tmp_path):
    import sqlite3
    from sniper.hyperlive import scan_audit as SA
    conn = sqlite3.connect(os.path.join(str(tmp_path), "lab.sqlite"))
    n = SA.write_labels(conn, [{"asset": "BTC", "scan_ts": 0, "abs_move_150s_bps": 100.0,
                                "dir_150s": 1, "cost_clearing_150s": True}])
    assert n == 1
    got = conn.execute("SELECT cost_clearing_150s FROM hyperlive_scan_labels WHERE asset='BTC'").fetchone()
    assert got[0] == 1


def test_recall_lift_detects_acceleration_advantage(tmp_path):
    import json as _json
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "lift.sqlite"))
    SL2.ensure_schema(conn)
    SA.ensure_labels_schema(conn)
    # one refresh: B/C have high hand-score but don't move; A/D move; D is found ONLY by accel.
    def row(asset, hand, accel_bps, mover):
        return {"asset": asset, "hand_score_v0": hand, "change_score_v0": 0.0,
                "change_components": {"basis_accel_bps": accel_bps}, "mid_px": 100.0}
    SL2.log_refresh(conn, 1000, [row("A", 5, 0, 1), row("B", 10, 0, 0), row("C", 9, 0, 0), row("D", 4, 8, 1)],
                    refresh_interval_s=150)
    for asset, mover in (("A", 1), ("B", 0), ("C", 0), ("D", 1)):
        conn.execute("INSERT INTO hyperlive_scan_labels(asset, scan_ts, cost_clearing_150s) VALUES (?,?,?)",
                     (asset, 1000, mover))
    conn.commit()
    res = SA.recall_lift(conn, top_n=2)
    assert res["status"] == "ok" and res["movers"] == 2
    assert res["lift_pp"] == 50.0 and res["promote"] is True   # accel top-2 catches D, hand top-2 misses both


def test_horse_race_learns_a_separating_signal(tmp_path):
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "hr.sqlite"))
    SL2.ensure_schema(conn); SA.ensure_labels_schema(conn)
    # 6 refreshes; each has a MOVER with high funding but LOW hand-score, and 2 non-movers
    # with high hand-score. hand misses the mover; a learned funding weight catches it.
    def r(asset, funding, hand):
        return {"asset": asset, "hand_score_v0": hand, "change_score_v0": 0.0,
                "score_components": {"funding_bps": funding}, "change_components": {}, "mid_px": 100.0}
    for ts in range(1000, 7000, 1000):
        SL2.log_refresh(conn, ts, [r("M", 10, 1), r("N1", 0, 10), r("N2", 0, 9)], refresh_interval_s=150)
        for a, mv in (("M", 1), ("N1", 0), ("N2", 0)):
            conn.execute("INSERT INTO hyperlive_scan_labels(asset, scan_ts, cost_clearing_150s) VALUES (?,?,?)", (a, ts, mv))
    conn.commit()
    hr = SA.horse_race(conn, top_n=1, train_frac=0.5)
    assert hr["status"] == "ok"
    assert hr["recall_at_12"]["learned"] > hr["recall_at_12"]["hand"]   # learning funding beats hand
    assert hr["winner"] == "learned"


def test_fit_weights_promotes_separating_component_in_raw_point_space(tmp_path):
    # Movers carry a big 24h move; non-movers carry only dislocation. fit_weights must return
    # weights (in opportunity raw-point space) where move_24h dominates and dislocation is small,
    # and those weights must drop straight into U.opportunity().
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "fit.sqlite"))
    SL2.ensure_schema(conn); SA.ensure_labels_schema(conn)

    def r(asset, move_pct, disloc_bps, hand):
        return {"asset": asset, "hand_score_v0": hand, "change_score_v0": 0.0, "day_ntl_vlm": 1e8,
                "score_components": {"move_pct": move_pct, "dislocation_bps": disloc_bps},
                "change_components": {}, "mid_px": 100.0}
    for ts in range(1000, 9000, 1000):
        SL2.log_refresh(conn, ts, [r("M", 6.0, 0.0, 1), r("N1", 0.0, 8.0, 2), r("N2", 0.0, 6.0, 2)],
                        refresh_interval_s=150)
        for a, mv in (("M", 1), ("N1", 0), ("N2", 0)):
            conn.execute("INSERT INTO hyperlive_scan_labels(asset, scan_ts, cost_clearing_150s) VALUES (?,?,?)", (a, ts, mv))
    conn.commit()
    res = SA.fit_weights(conn, min_movers=4)
    assert res["status"] == "ok"
    w = res["weights"]
    assert w["move_24h"] > w["dislocation"]                 # the mover signal wins
    assert max(w[c] for c in ("dislocation", "funding", "oi_shift", "premium", "move_24h")) == 1.5  # scaled to hand range
    assert w["volume"] == U.HAND_WEIGHTS["volume"]          # tiebreak fixed, not fit
    assert set(w) == set(U.WEIGHT_KEYS)                     # ready to drop into opportunity()


def test_fit_weights_excludes_subfloor_micro_cap_movers(tmp_path):
    # The signal the weights learn must come from the TRADEABLE pool we select from, not from
    # untradeable micro-caps. Here tradeable movers carry dislocation; sub-floor movers carry a
    # big premium. A correct fit weights dislocation, NOT premium.
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "floor.sqlite"))
    SL2.ensure_schema(conn); SA.ensure_labels_schema(conn)

    def r(asset, vol, disloc_bps, premium_bps, hand):
        return {"asset": asset, "hand_score_v0": hand, "change_score_v0": 0.0, "day_ntl_vlm": vol,
                "score_components": {"dislocation_bps": disloc_bps, "premium_bps": premium_bps},
                "change_components": {}, "mid_px": 100.0}
    for ts in range(1000, 9000, 1000):
        SL2.log_refresh(conn, ts, [
            r("BIG", 1e8, 20.0, 0.0, 1),       # tradeable mover: dislocation
            r("MICRO", 1e5, 0.0, 80.0, 1),     # sub-floor mover: premium (must be ignored)
            r("BIGN", 1e8, 0.0, 0.0, 2),       # tradeable non-mover
        ], refresh_interval_s=150)
        for a, mv in (("BIG", 1), ("MICRO", 1), ("BIGN", 0)):
            conn.execute("INSERT INTO hyperlive_scan_labels(asset, scan_ts, cost_clearing_150s) VALUES (?,?,?)", (a, ts, mv))
    conn.commit()
    res = SA.fit_weights(conn, min_movers=4, min_volume=5_000_000)
    assert res["status"] == "ok"
    assert res["movers"] == 8                                # only the 8 BIG movers counted, not MICRO
    assert res["weights"]["dislocation"] == 1.5             # liquid signal dominates
    assert res["weights"]["premium"] == 0.0                 # micro-cap signal contributes nothing


def test_fit_weights_insufficient_movers_returns_status(tmp_path):
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "fit2.sqlite"))
    SL2.ensure_schema(conn); SA.ensure_labels_schema(conn)
    res = SA.fit_weights(conn, min_movers=30)
    assert res["status"] == "insufficient"


def test_recall_at_horizon_measures_shared_selection_at_long_horizon(tmp_path):
    # B (swing validation): recall_at_horizon measures whether the logged top-12 selection
    # catches movers at an arbitrary forward horizon. BTC (selected) moves; DOGE (not selected)
    # moves more — recall should be 0.5 (1 of 2 movers selected), precision 1.0.
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "hz.sqlite"))
    SL2.ensure_schema(conn)
    def row(asset, mid, selected):
        return {"asset": asset, "mid_px": mid, "day_ntl_vlm": 1e8,
                "selected_bool": selected, "hand_score_v0": 0.0, "change_score_v0": 0.0}
    SL2.log_refresh(conn, 0, [row("BTC", 100.0, True), row("DOGE", 100.0, False)], refresh_interval_s=150)
    SL2.log_refresh(conn, 1000, [row("BTC", 101.0, True), row("DOGE", 102.0, False)], refresh_interval_s=150)
    res = SA.recall_at_horizon(conn, 1000, tol_ms=200, min_move_bps=25, min_volume=5_000_000)
    assert res["status"] == "ok"
    assert res["movers"] == 2 and res["recall"] == 0.5 and res["precision"] == 1.0
    assert res["horizon_s"] == 1


def test_recall_at_horizon_insufficient_when_no_future(tmp_path):
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "hz2.sqlite"))
    SL2.ensure_schema(conn)
    SL2.log_refresh(conn, 0, [{"asset": "BTC", "mid_px": 100.0, "day_ntl_vlm": 1e8, "selected_bool": True}],
                    refresh_interval_s=150)
    res = SA.recall_at_horizon(conn, 1_800_000, min_volume=5_000_000)   # no 30-min-future row
    assert res["status"] == "insufficient" and res["movers"] == 0


def test_basis_direction_edge_detects_aligned_convergence(tmp_path):
    # Directional thesis: HL chases Binance. When basis>0 (Binance above HL), HL should move UP.
    # Build mapped tradeable rows where a +basis is consistently followed by an up move that
    # clears the fee -> edge True, hit_rate 1.0, after_fee>0.
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "dir.sqlite"))
    SL2.ensure_schema(conn)
    mid = 100.0
    for k, ts in enumerate(range(0, 12 * 150_000, 150_000)):
        SL2.log_refresh(conn, ts, [{"asset": "EIGEN", "mid_px": mid, "day_ntl_vlm": 1e8,
                                    "binance_basis_bps": 12.0}], refresh_interval_s=150)
        mid *= 1.005 if k % 2 == 0 else 1.004      # +50bps / +40bps up moves (std>0, all aligned)
    res = SA.basis_direction_edge(conn, horizon_ms=150_000, basis_threshold_bps=2.0,
                                  min_volume=5_000_000, min_n=5, fee_bps=9.0)
    assert res["status"] == "ok"
    assert res["hit_rate"] == 1.0
    assert res["mean_aligned_bps"] > 40 and res["after_fee_bps"] > 0
    assert res["edge"] is True


def test_basis_direction_edge_rejects_anti_aligned(tmp_path):
    # basis>0 but HL moves DOWN (anti-aligned) -> no edge, low hit rate.
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "dir2.sqlite"))
    SL2.ensure_schema(conn)
    mid = 100.0
    for ts in range(0, 12 * 150_000, 150_000):
        SL2.log_refresh(conn, ts, [{"asset": "EIGEN", "mid_px": mid, "day_ntl_vlm": 1e8,
                                    "binance_basis_bps": 12.0}], refresh_interval_s=150)
        mid *= 0.995                                # consistently DOWN against a +basis
    res = SA.basis_direction_edge(conn, horizon_ms=150_000, basis_threshold_bps=2.0,
                                  min_volume=5_000_000, min_n=5, fee_bps=9.0)
    assert res["status"] == "ok"
    assert res["hit_rate"] == 0.0 and res["after_fee_bps"] < 0 and res["edge"] is False


def test_basis_direction_edge_insufficient_below_threshold(tmp_path):
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "dir3.sqlite"))
    SL2.ensure_schema(conn)
    for ts in range(0, 6 * 150_000, 150_000):       # |basis| 1bps < 2bps threshold -> filtered out
        SL2.log_refresh(conn, ts, [{"asset": "BTC", "mid_px": 100.0, "day_ntl_vlm": 1e8,
                                    "binance_basis_bps": 1.0}], refresh_interval_s=150)
    res = SA.basis_direction_edge(conn, basis_threshold_bps=2.0, min_volume=5_000_000, min_n=5)
    assert res["status"] == "insufficient"


def test_cross_venue_lift_detects_basis_selection_signal(tmp_path):
    # C: each refresh has a MOVER with a big Binance->HL basis but low hand-score, plus 2
    # non-movers with high hand-score. hand misses the mover; |basis| catches it. cross_venue_lift
    # must show basis/combined recall > hand recall on held-out, and recommend promotion.
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "cv.sqlite"))
    SL2.ensure_schema(conn); SA.ensure_labels_schema(conn)

    def r(asset, hand, basis_bps):
        return {"asset": asset, "hand_score_v0": hand, "mid_px": 100.0, "day_ntl_vlm": 1e8,
                "score_components": {}, "change_components": {}, "binance_basis_bps": basis_bps}
    for ts in range(1000, 9000, 1000):
        SL2.log_refresh(conn, ts, [r("M", 1.0, 30.0), r("N1", 10.0, 0.5), r("N2", 9.0, 0.2)],
                        refresh_interval_s=150)
        for a, mv in (("M", 1), ("N1", 0), ("N2", 0)):
            conn.execute("INSERT INTO hyperlive_scan_labels(asset, scan_ts, cost_clearing_150s) VALUES (?,?,?)", (a, ts, mv))
    conn.commit()
    res = SA.cross_venue_lift(conn, top_n=1, train_frac=0.5, min_volume=5_000_000, min_movers=4)
    assert res["status"] == "ok"
    assert res["basis_recall"] > res["hand_recall"]
    assert res["combined_recall"] >= res["hand_recall"]
    assert res["promote"] is True


def test_cross_venue_lift_insufficient_without_basis_data(tmp_path):
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "cv2.sqlite"))
    SL2.ensure_schema(conn); SA.ensure_labels_schema(conn)
    # rows with NO binance_basis_bps (the state right after the column is added)
    for ts in range(1000, 9000, 1000):
        SL2.log_refresh(conn, ts, [{"asset": "M", "hand_score_v0": 1.0, "mid_px": 100.0,
                                    "day_ntl_vlm": 1e8, "score_components": {}}], refresh_interval_s=150)
        conn.execute("INSERT INTO hyperlive_scan_labels(asset, scan_ts, cost_clearing_150s) VALUES (?,?,?)", ("M", ts, 1))
    conn.commit()
    res = SA.cross_venue_lift(conn, min_volume=5_000_000)
    assert res["status"] == "insufficient"


def test_evaluate_weights_compares_candidate_vs_base_on_heldout(tmp_path):
    # Each refresh: 1 mover with a big 24h move + 3 non-movers with big dislocation. A move-only
    # candidate ranks the mover first (recall 1.0); a dislocation-only base ranks a non-mover
    # first (recall 0.0). evaluate_weights must report cand>base on held-out and promote=True.
    from sniper.hyperlive import scan_log as SL2
    from sniper.hyperlive import scan_audit as SA
    conn = SL2.connect(os.path.join(str(tmp_path), "ev.sqlite"))
    SL2.ensure_schema(conn); SA.ensure_labels_schema(conn)

    def r(asset, move_pct, disloc_bps, mover):
        return {"asset": asset, "hand_score_v0": 0.0, "change_score_v0": 0.0, "day_ntl_vlm": 1e8,
                "score_components": {"move_pct": move_pct, "dislocation_bps": disloc_bps},
                "change_components": {}, "mid_px": 100.0}, mover
    for ts in range(1000, 11000, 1000):
        recs = [r("M", 8.0, 0.0, 1), r("N1", 0.0, 30.0, 0), r("N2", 0.0, 20.0, 0), r("N3", 0.0, 10.0, 0)]
        SL2.log_refresh(conn, ts, [x[0] for x in recs], refresh_interval_s=150)
        for (row, mv) in recs:
            conn.execute("INSERT INTO hyperlive_scan_labels(asset, scan_ts, cost_clearing_150s) VALUES (?,?,?)",
                         (row["asset"], ts, mv))
    conn.commit()
    cand = {"dislocation": 0.0, "funding": 0.0, "oi_shift": 0.0, "premium": 0.0, "move_24h": 1.5, "volume": 0.0}
    base = {"dislocation": 1.5, "funding": 0.0, "oi_shift": 0.0, "premium": 0.0, "move_24h": 0.0, "volume": 0.0}
    ev = SA.evaluate_weights(conn, cand, base, top_n=1, train_frac=0.5, min_volume=5_000_000)
    assert ev["status"] == "ok"
    assert ev["cand_recall"] == 1.0 and ev["base_recall"] == 0.0
    assert ev["promote"] is True

    # symmetric: a base that already catches the mover means a no-better candidate does NOT promote
    ev2 = SA.evaluate_weights(conn, base, cand, top_n=1, train_frac=0.5, min_volume=5_000_000)
    assert ev2["cand_recall"] == 0.0 and ev2["base_recall"] == 1.0
    assert ev2["promote"] is False
