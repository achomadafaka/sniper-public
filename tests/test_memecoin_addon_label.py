"""The add-on label (memecoin_addon_label): Deadpool pos #73925, notdecu 2026-10-17 15:44Z. His $308
first buy at 1.90e-05 was our failed slot-lane attempt; 18 s later his $26 add-on and our fill at
2.334e-05. A label at attempt finish, never a gate: absent when he had no earlier buy, None when a
part is unknown, never 0."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_addon_label as A

UID, MINT = "pump:4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9", "2yEmn7cDuKMRGgJYQRm4Nj8rxMZvGGtA8JgAD8Lqpump"
FIRST = "mFXRoxtKfP4MtMQPphY35ShyQD3ajoZNrfJMHgZNGCUHvR1YLaKFEU21asj9FpZLoEDjnwtkbsZzaqngRXASYqQ"
ADD = "3enrfuwYmzJLU9UA4jwjSftRiE6DPvnyChzSBCCj1YqbsP9xF3CwX5LWu6dyqqZNyUhwxGRfyZtCBuXCNpy7N6wt"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _event(c, fid, usd, price, at, side="buy"):
    c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, network_id, side, "
              "their_size_usd, their_price, created_at) VALUES (?,?,?,?,?,?,?,?)",
              (fid, UID, MINT, "solana", side, usd, price, at))
    c.commit()


def _attempt(c, attempt_id, origin, at, status=None, reason=None):
    m.start_entry_attempt(c, attempt_id=attempt_id, decision_started_at=at, origin_event_id=origin,
                          signal_source="slot_lane", network_id="solana", mint=MINT, source_user_id=UID,
                          source_created_at=at)
    if status:
        c.execute("UPDATE memecoin_entry_attempts SET terminal_status=?, terminal_reason=?, completed_at=? WHERE attempt_id=?",
                  (status, reason, at, attempt_id))
        c.commit()


def _row(c, attempt_id):
    return dict(c.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?", (attempt_id,)).fetchone())


def _deadpool(c):
    _event(c, "onchain:" + FIRST, 308.0, 1.9014e-05, "2026-10-17T15:44:10.100000+00:00")
    _attempt(c, "first", "slotlane:" + FIRST, "2026-10-17T15:44:10.357586+00:00", "failed",
             "chain_error:BuySlippageBelowMinTokensOut")
    _attempt(c, "add", "slotlane:" + ADD, "2026-10-17T15:44:28.500000+00:00")


def test_the_deadpool_add_on_is_labelled_with_both_prices_and_the_missed_first_attempt(conn):
    _deadpool(conn)
    block = A.addon_block(conn, _row(conn, "add"), judged_usd=26.0, our_price=2.334e-05)
    assert block["first_buy_price"] == 1.9014e-05 and block["first_buy_usd"] == 308.0
    assert block["prior_net_usd"] == 308.0 and block["addon_share_pct"] == 7.8
    assert block["our_price_over_first_pct"] == 22.8
    assert block["missed_first_attempt_status"] == {"slotlane": "failed:chain_error:BuySlippageBelowMinTokensOut"}
    assert A.card_words(block) == "add-on 8% of his $334 net, +23% over his first buy"


def test_no_earlier_buy_means_no_block_not_zeros(conn):
    _attempt(conn, "add", "slotlane:" + ADD, "2026-10-17T15:44:28.500000+00:00")
    assert A.addon_block(conn, _row(conn, "add"), judged_usd=26.0, our_price=2.3e-05) is None


def test_a_buy_older_than_ten_minutes_is_not_a_first_buy(conn):
    _event(conn, "onchain:" + FIRST, 308.0, 1.9e-05, "2026-10-17T15:34:00+00:00")
    _attempt(conn, "add", "slotlane:" + ADD, "2026-10-17T15:44:28.500000+00:00")
    assert A.addon_block(conn, _row(conn, "add"), judged_usd=26.0, our_price=2.3e-05) is None


def test_the_judged_trades_own_reports_never_read_as_its_first_buy(conn):
    _event(conn, "onchain:" + ADD, 26.0, 2.36e-05, "2026-10-17T15:44:27.900000+00:00")     # same tx
    _event(conn, "swap:2b8e1f8e-0000-4000-8000-000000000000", 25.5, 2.36e-05,
           "2026-10-17T15:44:20+00:00")                                                       # J7, tx-less
    _attempt(conn, "add", "slotlane:" + ADD, "2026-10-17T15:44:28.500000+00:00")
    assert A.addon_block(conn, _row(conn, "add"), judged_usd=26.0, our_price=2.3e-05) is None


def test_unknown_parts_are_none_and_the_card_leaves_them_out(conn):
    _event(conn, "swap:1b8e1f8e-0000-4000-8000-000000000000", 308.0, None, "2026-10-17T15:40:00+00:00")
    _attempt(conn, "add", "slotlane:" + ADD, "2026-10-17T15:44:28.500000+00:00")
    block = A.addon_block(conn, _row(conn, "add"), judged_usd=None, our_price=None)
    assert block["first_buy_price"] is None and block["our_price_over_first_pct"] is None
    assert block["addon_share_pct"] is None and block["missed_first_attempt_status"] is None
    assert A.card_words(block) == "add-on after his first buy"


class _Trace:
    def __init__(self, conn, attempt_id, ctx=None):
        self.conn, self.attempt_id, self.marks, self._ctx = conn, attempt_id, [], ctx or {}

    def mark_substage(self, name, **kw):
        self.marks.append((name, kw))
        return True

    def shadow_context(self):
        return dict(self._ctx)


def test_the_label_is_marked_on_the_trace_and_the_knob_off_reads_nothing(conn, monkeypatch):
    from sniper.settings import memecoin as cfg
    _deadpool(conn)
    trace = _Trace(conn, "add", {"source_size_usd": 26.0})
    A.record_addon_label(trace, {"fill_price": 2.334e-05})
    assert trace.marks[0][0] == "addon_context" and trace.marks[0][1]["our_price_over_first_pct"] == 22.8
    monkeypatch.setattr(cfg, "MEMECOIN_ADDON_LABEL", False)
    quiet = _Trace(conn, "add", {"source_size_usd": 26.0})
    assert A.record_addon_label(quiet, {"fill_price": 2.334e-05}) is None and quiet.marks == []


def test_a_failing_read_never_raises_into_the_funded_result(conn):
    class Broken(_Trace):
        @property
        def conn(self):
            raise sqlite3.OperationalError("database is locked")

        @conn.setter
        def conn(self, value):
            pass
    assert A.record_addon_label(Broken(conn, "add"), {"fill_price": 1.0}) is None


def test_the_entry_card_shows_the_add_on_line():
    from sniper.services.memecoin_telegram import render_entry_notification
    view = {"status": "filled", "label": "Filled", "network_id": "solana", "filled_notional": 113.0,
            "filled_price": 2.334e-05, "position_id": 73925,
            "addon": {"name": "addon_context", "addon_share_pct": 7.8, "prior_net_usd": 308.0, "judged_usd": 26.0,
                      "our_price_over_first_pct": 22.8}}
    assert "Add-on: add-on 8% of his $334 net, +23% over his first buy" in render_entry_notification(view)


def test_both_finish_points_label_before_the_trace_finishes_and_the_view_carries_it():
    """The runner's and the slot lane's attempt finish both call record_addon_label before
    trace.finish (a finished trace takes no more substages); the entry view reads it back."""
    import inspect
    from sniper.services import memecoin_entry_view as V
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_slot_lane as L
    runner = inspect.getsource(R._finish_trace_for_result)
    assert runner.index("record_addon_label(") < runner.index("trace.finish(")
    lane = inspect.getsource(L)
    i = lane.index("record_addon_label(trace, result, judged_usd=facts.get(\"their_usd\"))")
    assert i < lane.index("changed = trace.finish(", i)
    assert '"addon_context"' in inspect.getsource(V)
