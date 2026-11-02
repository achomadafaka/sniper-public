import os

from sniper.algos.hl_microstructure import ab_compare as AB
from sniper.algos.hl_microstructure import persistence as P


def _conn(tmp_path):
    db = os.path.join(str(tmp_path), "hlm_abc.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    return conn


def _closed(conn, policy_id, *, asset, opened_ts, realized_pnl, notional=25.0, side="long"):
    pid = P.open_position(
        conn, policy_id=policy_id, asset=asset, side=side, entry_price=100.0,
        quantity=notional / 100.0, notional_usdc=notional, leverage=1.0, fee_usdc=0.01,
        opened_ts_ms=opened_ts,
    )
    P.close_position(conn, position_id=pid, exit_fee_usdc=0.01, realized_pnl=realized_pnl,
                     closed_ts_ms=opened_ts + 30_000, reason="time_stop")
    return pid


def test_ab_compare_segments_by_policy_id(tmp_path):
    conn = _conn(tmp_path)
    _closed(conn, "fusion_radar", asset="BTC", opened_ts=1_000, realized_pnl=0.10)
    _closed(conn, "fusion_radar", asset="ETH", opened_ts=2_000, realized_pnl=-0.20)
    _closed(conn, "fusion_gated_b1", asset="BTC", opened_ts=1_000, realized_pnl=0.10)
    _closed(conn, "fusion_gated_b1", asset="ETH", opened_ts=2_000, realized_pnl=0.05)

    report = AB.build_ab_compare(conn)
    arms = {a["policy_id"]: a for a in report["arms"]}

    assert arms["fusion_radar"]["closed"] == 2
    assert arms["fusion_radar"]["realized_net_bps"] == -20.0   # (40 + -80)/2
    assert arms["fusion_gated_b1"]["closed"] == 2
    assert arms["fusion_gated_b1"]["realized_net_bps"] == 30.0  # (40 + 20)/2
    assert arms["fusion_gated_b1"]["net_win_rate"] == 1.0


def test_ab_compare_holm_corrects_candidate_arms_only(tmp_path):
    conn = _conn(tmp_path)
    _closed(conn, "fusion_radar", asset="BTC", opened_ts=1_000, realized_pnl=0.10)
    _closed(conn, "fusion_gated_b1", asset="BTC", opened_ts=1_000, realized_pnl=0.10)
    _closed(conn, "fusion_gated_b2", asset="ETH", opened_ts=2_000, realized_pnl=0.10)

    report = AB.build_ab_compare(conn)
    arms = {a["policy_id"]: a for a in report["arms"]}

    # Candidate arms get a Holm-adjusted p; the control baseline does not.
    assert arms["fusion_gated_b1"]["holm_p"] is not None
    assert arms["fusion_gated_b2"]["holm_p"] is not None
    assert arms["fusion_radar"]["holm_p"] is None
    assert report["candidate_arms"] == ["fusion_gated_b1", "fusion_gated_b2"]


def test_ab_compare_cli_prints_markdown(tmp_path, capsys):
    import scripts.hl_microstructure_ab_compare as CLI
    conn = _conn(tmp_path)
    _closed(conn, "fusion_radar", asset="BTC", opened_ts=1_000, realized_pnl=0.10)
    db = conn.execute("PRAGMA database_list").fetchone()[2]

    assert CLI.main(["--db-path", db]) == 0
    out = capsys.readouterr().out
    assert "# Hyperliquid Microstructure A/B Compare" in out
    assert "fusion_radar" in out
