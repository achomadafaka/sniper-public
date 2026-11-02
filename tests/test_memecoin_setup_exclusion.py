"""Entry-setup exclusion: open + in-flight reserved + re-entry cooldown (one-position-per-coin)."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import _setup_exclusion_keys

NOW = "2026-07-19T12:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    # pending-entries table may be ensured lazily elsewhere; create it for the test if absent
    c.execute("CREATE TABLE IF NOT EXISTS memecoin_pending_entries("
              "id INTEGER PRIMARY KEY, mint TEXT, network_id TEXT, status TEXT DEFAULT 'reserved')")
    return c


def _pos(c, pid, mint, *, net="solana", is_open=1, closed_at=None):
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, network_id, "
              "is_open, execution_mode, opened_at, closed_at) VALUES(?,?,?,?,?,?,?,'real',?,?)",
              (pid, mint, "T", 0.001, 50.0, net, is_open, "2026-07-19T00:00:00+00:00", closed_at))


def test_excludes_open_reserved_and_recent_close_but_not_old_close():
    c = _c()
    _pos(c, 1, "OPEN")                                          # open -> excluded
    _pos(c, 2, "COOL", is_open=0, closed_at="2026-07-19T06:00:00+00:00")   # closed 6h ago -> cooldown
    _pos(c, 3, "OLD", is_open=0, closed_at="2026-07-17T00:00:00+00:00")    # closed 60h ago -> allowed
    c.execute("INSERT INTO memecoin_pending_entries(id, mint, network_id, status) "
              "VALUES(9,'FLIGHT','4663','reserved')")           # in-flight -> excluded
    c.execute("INSERT INTO memecoin_pending_entries(id, mint, network_id, status) "
              "VALUES(10,'DONE','solana','filled')")            # not reserved -> allowed
    c.commit()
    keys = _setup_exclusion_keys(c, NOW, cooldown_h=24.0)
    assert ("solana", "OPEN") in keys
    assert ("solana", "COOL") in keys
    assert ("4663", "FLIGHT") in keys
    assert ("solana", "OLD") not in keys                        # past the cooldown
    assert ("solana", "DONE") not in keys                       # pending not reserved
