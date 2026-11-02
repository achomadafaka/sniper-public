"""F14: selected-protocol momentum shadow trust-boundary regressions."""
import pytest

from sniper.services import memecoin_volume_writer as writer
from sniper.settings import memecoin as cfg
from test_memecoin_funded_momentum_own import (
    AT, CHAIN, MINT as MOMENTUM_MINT, QUOTE, SINCE, _tape as momentum_tape,
)


def _momentum_store(lags=(0, 0, 0), observed=AT - 1):
    conn = momentum_tape([(AT - 1200, 5e-6)])
    conn.executescript("""
        CREATE TABLE volume_feed(chain_id TEXT,since REAL,through REAL,logs_seen INTEGER,received REAL);
        CREATE TABLE volume_feed_lag(chain_id TEXT,v4_lag_s REAL,v3_lag_s REAL,pons_lag_s REAL,observed REAL);
        CREATE TABLE volume_quote_prices(chain_id TEXT,token TEXT,usd REAL,observed REAL);
    """)
    conn.execute("INSERT INTO volume_feed VALUES(?,?,?,?,?)", (CHAIN, SINCE, AT - 1, 1, AT - 1))
    conn.execute("INSERT INTO volume_feed_lag VALUES(?,?,?,?,?)", (CHAIN, *lags, observed))
    conn.execute("INSERT INTO volume_quote_prices VALUES(?,?,?,?)", (CHAIN, QUOTE, 2000, AT - 1))
    return conn


@pytest.mark.parametrize("state", [
    "all_null", "selected_null", "lag_inf", "lag_nan", "lag_negative", "lag_wrong", "stale",
    "future", "time_missing", "row_missing", "table_missing",
])
def test_shadow_momentum_requires_current_selected_protocol_measurement(monkeypatch, state):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S", 30)
    conn = _momentum_store()
    if state == "all_null":
        conn.execute("UPDATE volume_feed_lag SET v4_lag_s=NULL,v3_lag_s=NULL,pons_lag_s=NULL")
    elif state in {"selected_null", "lag_inf", "lag_nan", "lag_negative", "lag_wrong"}:
        value = {"selected_null": None, "lag_inf": float("inf"),
                 "lag_nan": float("nan"), "lag_negative": -1, "lag_wrong": "bad"}[state]
        conn.execute("UPDATE volume_feed_lag SET v4_lag_s=?", (value,))
    elif state in {"stale", "future", "time_missing"}:
        value = {"stale": AT - 600, "future": AT + 60, "time_missing": None}[state]
        conn.execute("UPDATE volume_feed_lag SET observed=?", (value,))
    elif state == "row_missing":
        conn.execute("DELETE FROM volume_feed_lag")
    else:
        conn.execute("DROP TABLE volume_feed_lag")
    out = writer.momentum_series(conn, CHAIN, MOMENTUM_MINT, AT)
    conn.close()
    assert out["points"] == [], f"{state} became {len(out['points'])} healthy points"
    assert out["reason"] in {"protocol_lag_unknown", "protocol_silent"}


def test_current_selected_protocol_with_zero_lag_keeps_quiet_momentum(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S", 30)
    conn = _momentum_store((0, None, None))
    out = writer.momentum_series(conn, CHAIN, MOMENTUM_MINT, AT)
    assert out["reason"] == "ok" and len(out["points"]) == 31
    conn.close()


def test_legacy_volume_lag_compatibility_stays_unchanged():
    from sniper.services.memecoin_onchain_volume import _silent_protocols
    conn = _momentum_store((None, None, None))
    lags = writer._protocol_lags(conn, CHAIN)
    assert lags == {"v4": None, "v3": None, "pons": None}
    assert _silent_protocols([{"protocol": "v4"}], lags) == []
    conn.close()
