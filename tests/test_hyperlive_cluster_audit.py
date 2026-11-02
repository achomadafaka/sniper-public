import os

from sniper.hyperlive import persistence as P
from sniper.hyperlive import cluster_audit as CA


def _conn(tmp_path):
    c = P.connect(os.path.join(str(tmp_path), "clh.sqlite"))
    P.ensure_schema(c)
    return c


def test_save_cluster_history_appends_timeseries(tmp_path):
    c = _conn(tmp_path)
    # same asset, two polls -> two rows (NOT an upsert like hyperlive_clusters)
    P.save_cluster_history(c, {"BTC": ("long", 30.0, 2_000_000.0)}, {"BTC": 100.0}, ts_ms=1000)
    P.save_cluster_history(c, {"BTC": ("long", 12.0, 2_500_000.0)}, {"BTC": 100.5}, ts_ms=2000)
    rows = c.execute("SELECT asset, mid, side, distance_bps, notional, ts_ms FROM hyperlive_cluster_history "
                     "ORDER BY ts_ms").fetchall()
    assert len(rows) == 2
    assert rows[0]["mid"] == 100.0 and rows[1]["distance_bps"] == 12.0


def test_cluster_cascade_edge_detects_magnet_pull(tmp_path):
    # Near + big cluster ABOVE price (side=long). Price then moves UP toward it, clearing the fee.
    # aligned move = toward-cluster move; edge should be True.
    c = _conn(tmp_path)
    mid = 100.0
    for k, ts in enumerate(range(0, 12 * 180_000, 180_000)):
        P.save_cluster_history(c, {"ETH": ("long", 20.0, 5_000_000.0)}, {"ETH": mid}, ts_ms=ts)
        mid *= 1.006 if k % 2 == 0 else 1.005          # +60/+50bps UP toward the long cluster
    res = CA.cluster_cascade_edge(c, horizon_ms=180_000, max_distance_bps=50.0,
                                  min_notional=1_000_000.0, min_n=5, fee_bps=9.0)
    assert res["status"] == "ok"
    assert res["hit_rate"] == 1.0
    assert res["mean_aligned_bps"] > 40 and res["after_fee_bps"] > 0 and res["edge"] is True


def test_cluster_cascade_edge_rejects_when_price_runs_away_from_cluster(tmp_path):
    # Cluster is long (above) but price falls AWAY from it -> anti-aligned -> no edge.
    c = _conn(tmp_path)
    mid = 100.0
    for ts in range(0, 12 * 180_000, 180_000):
        P.save_cluster_history(c, {"ETH": ("long", 20.0, 5_000_000.0)}, {"ETH": mid}, ts_ms=ts)
        mid *= 0.994
    res = CA.cluster_cascade_edge(c, horizon_ms=180_000, max_distance_bps=50.0,
                                  min_notional=1_000_000.0, min_n=5, fee_bps=9.0)
    assert res["status"] == "ok"
    assert res["hit_rate"] == 0.0 and res["after_fee_bps"] < 0 and res["edge"] is False


def test_cluster_cascade_edge_filters_far_and_small_clusters(tmp_path):
    # Cluster is far (200bps) and small (below min_notional) -> excluded -> insufficient.
    c = _conn(tmp_path)
    mid = 100.0
    for ts in range(0, 12 * 180_000, 180_000):
        P.save_cluster_history(c, {"ETH": ("long", 200.0, 50_000.0)}, {"ETH": mid}, ts_ms=ts)
        mid *= 1.006
    res = CA.cluster_cascade_edge(c, horizon_ms=180_000, max_distance_bps=50.0,
                                  min_notional=1_000_000.0, min_n=5)
    assert res["status"] == "insufficient"
