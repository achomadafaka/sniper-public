import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _row(uid="u1", pnl=50.0):
    return {"source_user_id": uid, "handle": "alice", "computed_at": "2026-07-06T00:00:00Z",
            "n_closed": 1, "realized_pnl_usd": pnl, "win_rate": 1.0,
            "coverage_ratio": 0.2, "dollar_weighted_return_pct": 12.5,
            "top3_win_share": 0.9, "undefeated": 1, "realized_max_drawdown_usd": 0.0,
            "pnl_pct_dispersion_ratio": None,
            "chain_mix": "{}", "provider_mix": "{}", "metrics_json": "{}"}


def test_upsert_then_list():
    c = _conn()
    m.upsert_trader_analytics(c, _row())
    rows = m.list_trader_analytics(c)
    assert len(rows) == 1
    assert rows[0]["source_user_id"] == "u1"
    assert rows[0]["realized_pnl_usd"] == 50.0
    assert rows[0]["coverage_ratio"] == 0.2


def test_upsert_is_idempotent_latest_wins():
    c = _conn()
    m.upsert_trader_analytics(c, _row(pnl=50.0))
    m.upsert_trader_analytics(c, _row(pnl=999.0))
    rows = m.list_trader_analytics(c)
    assert len(rows) == 1
    assert rows[0]["realized_pnl_usd"] == 999.0


def test_get_single():
    c = _conn()
    m.upsert_trader_analytics(c, _row())
    assert m.get_trader_analytics(c, "u1")["handle"] == "alice"
    assert m.get_trader_analytics(c, "nope") is None


def test_partial_row_uses_column_defaults():
    c = _conn()
    m.upsert_trader_analytics(c, {"source_user_id": "u2"})
    assert m.get_trader_analytics(c, "u2")["source_user_id"] == "u2"


def test_get_wallet_by_handle():
    c = _conn()
    m.add_wallet(c, "uX", "connerb", source="follow")
    assert m.get_wallet_by_handle(c, "connerb")["source_user_id"] == "uX"
    assert m.get_wallet_by_handle(c, "ghost") is None


def test_all_metric_columns_persist_roundtrip():
    # every key the metrics module emits must be a real column (guards drift)
    c = _conn()
    full = {k: 0 for k in m.TRADER_ANALYTICS_COLUMNS}
    full["source_user_id"] = "u9"
    full["chain_mix"] = "{}"
    full["provider_mix"] = "{}"
    full["metrics_json"] = "{}"
    m.upsert_trader_analytics(c, full)
    got = m.get_trader_analytics(c, "u9")
    assert got["source_user_id"] == "u9"
    assert set(m.TRADER_ANALYTICS_COLUMNS).issubset(set(got.keys()))
