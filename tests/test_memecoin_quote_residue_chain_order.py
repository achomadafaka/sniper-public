"""C06 (engineering review 2026-10-08, P1): residue ownership between two holding sells of one mint
is decided on the CHAIN timeline. The lower bound for a sell's residue is the previous sell's
on-chain landing (its receipt's slot and blockTime), never that sell's local `resolved_at`, which
is recovery time: a sell recovered late used to exclude residue that arrived after it landed, so
both sells booked `no_owner` and the proceeds were lost. When the order cannot be proved (another
sell's receipt unreadable, or both sells in one slot) the sell waits in a named state.
"""
from test_memecoin_quote_residue_credits import (  # noqa: F401  (conn fixture)
    OWNER, ZEC, NOW, _entry_residue, _position, _receipts, _sold, conn, sell_receipt)
from sniper.services import memecoin_quote_residue as Q

T_1510, T_1530 = 1791385800, 1791387000          # 2026-10-07 15:10:00Z / 15:30:00Z


def _receipt(consumed, block_time, slot, sig):
    r = sell_receipt(consumed=consumed, block_time=block_time, sig=sig)
    r["slot"] = slot
    return r


def _two_sells(conn, *, s1_resolved, s2_resolved):
    _position(conn, attempt="entry")
    _entry_residue(conn, atomic=100, attempt="entry", at="2026-10-07T15:15:00+00:00")
    _sold(conn, intent="sell1", balance=10, sig="s1", resolved_at=s1_resolved)
    _sold(conn, intent="sell2", balance=100, sig="s2", resolved_at=s2_resolved)


def _credits(conn):
    return conn.execute("SELECT intent_id, COUNT(*) FROM memecoin_quote_residue_credits "
                        "GROUP BY intent_id").fetchall()


def _states(out):
    return {r["intent_id"]: r["state"] for r in out}


def test_a_late_recovered_previous_sell_does_not_hide_residue_from_the_next_sell(conn):
    # sell1 landed 15:10 but resolved locally 15:20; residue 15:15; sell2 landed 15:30.
    _two_sells(conn, s1_resolved="2026-10-07T15:20:00+00:00", s2_resolved="2026-10-07T15:31:00+00:00")
    rec = _receipts({"s1": _receipt(10, T_1510, 100, "s1"), "s2": _receipt(100, T_1530, 200, "s2")})
    out = Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW)
    assert _states(out)["sell2"] == "credited"
    assert [tuple(r) for r in _credits(conn)] == [("sell2", 1)]


def test_reversed_local_resolution_order_still_uses_chain_order(conn):
    # sell1 landed FIRST on chain but was recovered AFTER sell2 resolved: sell2 still starts at 15:10.
    _two_sells(conn, s1_resolved="2026-10-07T15:40:00+00:00", s2_resolved="2026-10-07T15:31:00+00:00")
    rec = _receipts({"s1": _receipt(10, T_1510, 100, "s1"), "s2": _receipt(100, T_1530, 200, "s2")})
    Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW)
    assert [tuple(r) for r in _credits(conn)] == [("sell2", 1)]


def test_residue_before_the_previous_landing_is_never_the_next_sells(conn):
    _position(conn, attempt="entry")
    _entry_residue(conn, atomic=100, attempt="entry", at="2026-10-07T15:05:00+00:00")
    _sold(conn, intent="sell1", balance=10, sig="s1", resolved_at="2026-10-07T15:40:00+00:00")
    _sold(conn, intent="sell2", balance=100, sig="s2", resolved_at="2026-10-07T15:31:00+00:00")
    rec = _receipts({"s1": _receipt(10, T_1510, 100, "s1"), "s2": _receipt(100, T_1530, 200, "s2")})
    out = Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW)
    assert _states(out)["sell2"] == "no_owner"
    assert ("sell2", 1) not in [tuple(r) for r in _credits(conn)]


def test_an_unreadable_earlier_resolved_sell_bounds_conservatively_then_credits_once_readable(conn):
    # sell1 unreadable but resolved locally (15:20) before sell2 landed (15:30): it surely landed
    # first, and its resolution is a safe lower bound. The 15:15 residue waits, never misowned.
    _two_sells(conn, s1_resolved="2026-10-07T15:20:00+00:00", s2_resolved="2026-10-07T15:31:00+00:00")
    blind = _receipts({"s2": _receipt(100, T_1530, 200, "s2")})
    out = Q.credit_sells_once(conn, receipt_fn=blind, owner=OWNER, now_iso=NOW)
    assert _states(out)["sell2"] == "no_owner"
    assert _credits(conn) == []
    rec = _receipts({"s1": _receipt(10, T_1510, 100, "s1"), "s2": _receipt(100, T_1530, 200, "s2")})
    Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW)
    Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW)       # repeated: books once
    assert [tuple(r) for r in _credits(conn)] == [("sell2", 1)]


def test_two_sells_in_one_slot_are_ambiguous_never_guessed(conn):
    _two_sells(conn, s1_resolved="2026-10-07T15:20:00+00:00", s2_resolved="2026-10-07T15:31:00+00:00")
    rec = _receipts({"s1": _receipt(10, T_1530, 200, "s1"), "s2": _receipt(100, T_1530, 200, "s2")})
    out = Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW)
    assert _states(out)["sell2"] == "sell_order_ambiguous"
    assert _credits(conn) == []


def test_same_second_sells_order_by_slot(conn):
    _position(conn, attempt="entry")
    _entry_residue(conn, atomic=100, attempt="entry", at="2026-10-07T15:15:00+00:00")
    _sold(conn, intent="sell1", balance=10, sig="s1", resolved_at="2026-10-07T15:31:30+00:00")
    _sold(conn, intent="sell2", balance=100, sig="s2", resolved_at="2026-10-07T15:31:00+00:00")
    # same blockTime second, sell1 one slot earlier: sell1 is the previous sell of sell2
    rec = _receipts({"s1": _receipt(10, T_1530, 199, "s1"), "s2": _receipt(100, T_1530, 200, "s2")})
    out = Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW)
    assert _states(out)["sell2"] == "no_owner"            # the 15:15 residue predates sell1's landing


def test_an_unreadable_sell_resolved_after_this_landing_cannot_be_placed(conn):
    # sell1 unreadable and resolved locally at 15:40, after sell2 landed: it may have landed
    # before sell2 or after it, so sell2 waits rather than guess its lower bound.
    _two_sells(conn, s1_resolved="2026-10-07T15:40:00+00:00", s2_resolved="2026-10-07T15:31:00+00:00")
    blind = _receipts({"s2": _receipt(100, T_1530, 200, "s2")})
    out = Q.credit_sells_once(conn, receipt_fn=blind, owner=OWNER, now_iso=NOW)
    assert _states(out)["sell2"] == "sell_order_unknown"
    assert _credits(conn) == []


def _edge_case(conn, residue_at):
    _position(conn, attempt="entry")
    _entry_residue(conn, atomic=100, attempt="entry", at=residue_at)
    _sold(conn, intent="sell1", balance=10, sig="s1", resolved_at="2026-10-07T15:20:00+00:00")
    _sold(conn, intent="sell2", balance=100, sig="s2", resolved_at="2026-10-07T15:31:00+00:00")
    rec = _receipts({"s1": _receipt(10, T_1510, 100, "s1"), "s2": _receipt(100, T_1530, 200, "s2")})
    return _states(Q.credit_sells_once(conn, receipt_fn=rec, owner=OWNER, now_iso=NOW))


def test_a_residue_in_the_previous_sells_landing_second_is_ambiguous_never_given_away(conn):
    # sell1 landed in whole second 15:10:00; a residue at 15:10:00.4 may predate it on chain.
    assert _edge_case(conn, "2026-10-07T15:10:00.400000+00:00")["sell2"] == "residue_order_ambiguous"
    assert _credits(conn) == []


def test_a_residue_in_this_sells_landing_second_is_ambiguous_never_given_away(conn):
    assert _edge_case(conn, "2026-10-07T15:30:00.400000+00:00")["sell2"] == "residue_order_ambiguous"
    assert _credits(conn) == []
