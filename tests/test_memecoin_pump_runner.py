"""run_once opens a post-buy pump outcome for new large-buy sightings (curated + discovery)."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_runner as run


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


class _Auth:
    access = "t"; refresh = "r"; ca_id = "c"
    def refresh_token(self, http=None): return "t"


def _feed(*items):
    return {"responseObject": {"feed": list(items)}}


def _item(eid, uid, handle, price=0.005, size=3000.0):
    return {"id": eid, "type": "large_buy", "userId": uid, "tokenAddress": "MINT" + eid,
            "createdAt": "2026-07-07T10:00:00Z",
            "body": {"userHandle": handle, "ticker": "PEPE", "price": price,
                     "marketCap": "1000000", "currentSizeUsd": size, "isFirstBuy": True}}


def test_run_once_captures_pump_outcome_for_curated_and_discovery():
    c = _conn()
    feed = _feed(_item("bcur", "u1", "alice"),          # curated large buy
                 _item("bdisc", "u9", "stranger"))       # non-curated large buy (discovery)

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 07 Jul 2026 10:00:01 GMT"

    # host clock (received_at) differs from the feed's fomo createdAt ("10:00:00Z")
    run.run_once(c, _Auth(), 100.0, http=http, now_fn=lambda: "2026-07-07T17:20:00Z",
                 fill_fn=lambda *a, **k: (0.005, "jupiter"))
    outs = {r["fomo_event_id"]: dict(r) for r in c.execute("SELECT * FROM memecoin_signal_outcomes")}
    assert set(outs) == {"bcur", "bdisc"}                # both curated + discovery captured
    assert abs(outs["bcur"]["ref_price"] - 0.005) < 1e-9 and outs["bcur"]["status"] == "open"
    # REGRESSION (review CRITICAL): buy_ts must be the HOST-clock capture time (received_at),
    # not fomo's createdAt — else the +7h host skew fires all horizons at once. 17:20 not 10:00.
    assert outs["bcur"]["buy_ts"] == "2026-07-07T17:20:00Z"
    v2 = {r["upstream_event_id"]: dict(r) for r in c.execute(
        "SELECT * FROM memecoin_signal_outcomes_v2",
    )}
    assert set(v2) == {"bcur", "bdisc"}
    assert v2["bcur"]["signal_observed_at"] == "2026-07-07T17:20:00Z"
    assert v2["bcur"]["entry_observation_id"] is not None
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_horizon_marks_v2 WHERE outcome_id=?",
        (v2["bcur"]["outcome_id"],),
    ).fetchone()[0] == v2["bcur"]["expected_marks"]


def test_run_once_skips_small_buys():
    c = _conn()
    feed = _feed(_item("small", "u1", "alice", size=100.0))   # < discovery min ($1000)

    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 07 Jul 2026 10:00:01 GMT"

    run.run_once(c, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert c.execute("SELECT COUNT(*) FROM memecoin_signal_outcomes").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM memecoin_signal_outcomes_v2").fetchone()[0] == 0
