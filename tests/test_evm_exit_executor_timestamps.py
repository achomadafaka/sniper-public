"""EVM mirror of tests/test_solana_exit_executor_timestamps.py. close_submitted_at and closed_at
were stamped from ONE frozen now_iso -- exit_exit_evm.py:95-97 said so directly. `now_fn` fixes it:
MEASUREMENT ONLY, no decision changed. Unset, behavior is byte-identical to before (pinned here).
Supplied, close_submitted_at and closed_at become independently-clocked, real wall-clock facts."""
import sqlite3

from sniper.execution.evm_exit_executor import execute_exit_evm
from sniper.persistence import db_memecoin as m

DECISION_AT = "2026-09-08T12:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(c, mint="0xtok", entry_notional=10.0):
    pid = m.open_position(c, source_user_id="u", source_handle="(real)", mint=mint, ticker="",
                          entry_price=1.0, entry_notional=entry_notional, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at=DECISION_AT,
                          strategy="alpha_b", execution_mode="real", network_id="8453")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=entry_notional,
                  fill_source="chain", fraction=1.0, created_at=DECISION_AT, tx_sig="E")
    return m.get_position(c, pid)


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]
        it["i"] += 1
        return v
    return f


_QUOTE = {"sellAmount": "10", "buyAmount": "10", "minBuyAmount": "9",
          "transaction": {"to": "0xr"}, "issues": {"allowance": {"spender": "0xspender"}}}


def _seams():
    return dict(
        quote_fn=lambda mint, toks, slip: _QUOTE,
        approve_fn=lambda token, spender, amount: True,
        sign_fn=lambda tx: ("0xraw", "0xhash", 3),
        send_fn=lambda raw: None,
        confirm_fn=lambda h: "confirmed",
        usdc_fn=_seq([50.0, 65.0]),
        token_balance_fn=lambda mint: 10.0,
    )


def _clock(values):
    it = iter(values)
    return lambda: next(it)


def test_unset_now_fn_keeps_todays_behavior_all_three_collapse_to_one_instant():
    c = _c()
    pos = _pos(c)
    r = execute_exit_evm(c, pos, "b_stop", DECISION_AT, slippage_ladder=[3], **_seams())
    assert r["status"] == "closed"
    p = m.get_position(c, pos["id"])
    assert p["close_submitted_at"] == DECISION_AT
    assert p["closed_at"] == DECISION_AT


def test_now_fn_makes_submit_and_close_independently_clocked_real_instants():
    """The regression test Alpha asked for: fails if the three timestamps collapse to one value."""
    c = _c()
    pos = _pos(c)
    submit_at = "2026-09-08T12:00:01.500000+00:00"
    closed_at = "2026-09-08T12:00:03.250000+00:00"
    r = execute_exit_evm(c, pos, "b_stop", DECISION_AT, slippage_ladder=[3],
                         now_fn=_clock([submit_at, closed_at]), **_seams())
    assert r["status"] == "closed"
    p = m.get_position(c, pos["id"])
    assert p["close_submitted_at"] == submit_at
    assert p["closed_at"] == closed_at
    three = {DECISION_AT, p["close_submitted_at"], p["closed_at"]}
    assert len(three) == 3, f"timestamps collapsed: {three}"


def test_now_fn_a_clock_that_cannot_be_read_falls_back_to_decision_time_not_a_crash():
    c = _c()
    pos = _pos(c)
    r = execute_exit_evm(c, pos, "b_stop", DECISION_AT, slippage_ladder=[3],
                         now_fn=lambda: None, **_seams())
    assert r["status"] == "closed"
    p = m.get_position(c, pos["id"])
    assert p["close_submitted_at"] == DECISION_AT
    assert p["closed_at"] == DECISION_AT
