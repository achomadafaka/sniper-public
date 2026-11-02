import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services.memecoin_exits import sweep_exits


class _Ev:
    side = "buy"; source_user_id = "u1"; source_handle = "alice"; mint = "M"; ticker = "T"
    their_price = 1.0; their_market_cap = 1e6; their_size_usd = 3000.0; is_first_buy = True
    fomo_event_id = "e1"; created_at = "t"; received_at = "t"; lag_s = 0.1; signal_source = "global_feed"


def test_full_arms_pipeline():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice")
    for s in ("control", "momentum_a", "alpha_b"):
        ct.on_copy_event(c, _Ev(), 100.0, "2026-07-07T10:00:00+00:00",
                         fill_fn=lambda *a, **k: (1.0, "jupiter"), strategy=s)
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1").fetchone()[0] == 3
    sweep_exits(c, {"M": {"price": 1.5, "liquidity": 1e15}}, "2026-07-07T10:00:15+00:00")   # new high
    sweep_exits(c, {"M": {"price": 1.3, "liquidity": 1e15}}, "2026-07-07T10:00:30+00:00")
    sweep_exits(c, {"M": {"price": 1.3, "liquidity": 1e15}}, "2026-07-07T10:00:45+00:00")
    # momentum_a rolled over and closed; control/alpha_b still open (no SL/rails hit)
    assert m.get_open_position(c, "u1", "M", "momentum_a") is None
    assert m.get_open_position(c, "u1", "M", "control") is not None
    summ = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert summ["momentum_a"]["closed"] == 1
