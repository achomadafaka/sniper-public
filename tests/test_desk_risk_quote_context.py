"""Read-only RISK quote context keeps residual quantity and unit price distinct from bag PnL."""
import sqlite3

import pytest

from test_desk_risk_exact_pnl import RS, T0


def series(extra=", open_qty REAL, unit_price REAL"):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_route_series(position_id INTEGER,seam TEXT,observed_at TEXT,expected_usdc REAL" + extra + ")")
    return c


@pytest.mark.parametrize("quantity,unit", [(75.0, 2.0), (None, None), (0.0, 0.0)])
def test_latest_exit_quote_exposes_its_stored_quantity_and_unit_without_coercion(quantity, unit):
    c = series()
    at = T0.isoformat()
    c.execute("INSERT INTO memecoin_route_series VALUES(1,'exit_decision',?,150,?,?)", (at, quantity, unit))
    c.execute("INSERT INTO memecoin_route_series VALUES(1,'peak_preview','9999',999,100,9.99)")
    c.execute("INSERT INTO memecoin_route_series VALUES(2,'exit_decision','9999',999,100,9.99)")
    assert RS.exact_mark(c, 1) == {"sweep_ts": at, "executable_exit_value": 150.0,
                                   "source": "route_series:exit_decision", "open_qty": quantity, "unit_price": unit}


@pytest.mark.parametrize("extra", ["", ",open_qty REAL", ",unit_price REAL"])
def test_legacy_quote_columns_leave_both_context_fields_unknown(extra):
    c = series(extra)
    c.execute("INSERT INTO memecoin_route_series(position_id,seam,observed_at,expected_usdc) VALUES(1,'exit_decision',?,150)", (T0.isoformat(),))
    assert RS.exact_mark(c, 1) == {"sweep_ts": T0.isoformat(), "executable_exit_value": 150.0,
                                   "source": "route_series:exit_decision", "open_qty": None, "unit_price": None}


def test_absent_quote_remains_absent():
    assert RS.exact_mark(series(), 1) is None


@pytest.mark.parametrize("message", ["database is locked", "disk I/O error"])
def test_real_quote_read_failures_propagate_without_a_legacy_fallback(message):
    class Broken:
        calls = 0

        def execute(self, *_):
            self.calls += 1
            raise sqlite3.OperationalError(message)

    c = Broken()
    with pytest.raises(sqlite3.OperationalError, match=message):
        RS.exact_mark(c, 1)
    assert c.calls == 1


@pytest.mark.parametrize("unknown_exit", [False, True])
def test_open_position_exposes_residual_fill_quantity_from_its_existing_readonly_connection(tmp_path, monkeypatch, unknown_exit):
    from sniper.persistence import db_core, db_memecoin as m

    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE memecoin_positions(id INTEGER,mint TEXT,signal_source TEXT,execution_mode TEXT,is_open INTEGER,entry_notional REAL,entry_price REAL,opened_at TEXT)")
        c.execute("INSERT INTO memecoin_positions VALUES(1,'M','desk','real',1,100,1,?)", (T0.isoformat(),))
        c.execute("CREATE TABLE memecoin_position_exit_overrides(position_id INTEGER,active INTEGER,source TEXT,tp_pct REAL,tp_target_mcap REAL,sl_pct REAL,sl_target_mcap REAL,trail_pct REAL,derisk_pct REAL,actor TEXT)")
        c.execute("CREATE TABLE memecoin_fills(id INTEGER,position_id INTEGER,kind TEXT,qty REAL)")
        c.executemany("INSERT INTO memecoin_fills VALUES(?,?,?,?)", [(1, 1, "entry", 100.0), (2, 1, "partial_exit", 25.0)])
        if unknown_exit:
            c.execute("INSERT INTO memecoin_fills VALUES(3,1,'partial_exit',NULL)")
        c.execute("CREATE TABLE memecoin_route_series(position_id INTEGER,seam TEXT,observed_at TEXT,expected_usdc REAL,open_qty REAL,unit_price REAL)")
        c.execute("INSERT INTO memecoin_route_series VALUES(1,'exit_decision',?,150,75,2)", (T0.isoformat(),))
    read = db_core.get_readonly_connection
    opened, quantities = [], []
    real_qty = m.open_qty

    def readonly(db):
        conn = read(db)
        opened.append(conn)
        return conn

    def qty(conn, position_id):
        quantities.append((conn, position_id))
        return real_qty(conn, position_id)

    monkeypatch.setattr(db_core, "get_readonly_connection", readonly)
    monkeypatch.setattr(m, "open_qty", qty)
    [(pos, policy, rail_ids, mark)] = RS.open_desk_positions(path)
    assert pos.get("open_qty_known") is (not unknown_exit)
    assert pos.get("open_qty") == 75.0
    assert len(opened) == 1 and quantities == [(opened[0], 1)]
    assert policy is None and rail_ids == ([2, 3] if unknown_exit else [2])
    assert mark["open_qty"] == 75.0 and mark["unit_price"] == 2.0
