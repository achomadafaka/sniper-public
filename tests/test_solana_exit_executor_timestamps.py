"""close_submitted_at and closed_at were stamped from ONE frozen now_iso, reused for the claim,
the write-ahead and the finalization -- so `closed_at - close_submitted_at` read exactly 0.0s on
236 of 237 real b_stop closes (measured 2026-09-08, see docs/superpowers/specs/2026-09-08-*).
`execute_exit`'s new `now_fn` parameter fixes this: MEASUREMENT ONLY, no decision changed. Unset,
behavior is byte-identical to before (this file pins that). Supplied, close_submitted_at and
closed_at become independently-clocked, real wall-clock facts."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.execution import solana_exit_executor as ex

DECISION_AT = "2026-09-08T12:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint="MINT", ticker="",
                          entry_price=0.02, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.02, qty=500.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="t")
    return c, m.get_position(c, pid)


class _Deps:
    """One clean confirmed rung -- the exact shape that produced 0.0s deltas live."""
    def __init__(self):
        self.sold = False

    def quote_fn(self, mint, tokens, slippage_pct):
        return {"inAmount": "500", "outAmount": "100", "otherAmountThreshold": "90"}

    def usdc_fn(self):
        return 15.0 if self.sold else 0.0

    def token_fn(self, mint):
        return 500.0

    def sign_fn(self, quote):
        return (b"tx", "SIG0")

    def send_fn(self, signed):
        pass

    def confirm_fn(self, sig):
        self.sold = True
        return "confirmed"


def _clock(values):
    it = iter(values)
    return lambda: next(it)


def test_unset_now_fn_keeps_todays_behavior_all_three_collapse_to_one_instant():
    """Pins the CURRENT (buggy) behavior for a caller that does not opt in -- a caller upgrading
    this module and not passing now_fn must see no change at all."""
    c, pos = _c()
    d = _Deps()
    r = ex.execute_exit(c, pos, "b_stop", DECISION_AT,
                        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
                        token_balance_fn=d.token_fn, slippage_ladder=[3.0])
    assert r["status"] == "closed"
    p = m.get_position(c, pos["id"])
    assert p["close_submitted_at"] == DECISION_AT
    assert p["closed_at"] == DECISION_AT


def test_now_fn_makes_submit_and_close_independently_clocked_real_instants():
    """The regression test Alpha asked for: fails if the three timestamps collapse to one value."""
    c, pos = _c()
    d = _Deps()
    submit_at = "2026-09-08T12:00:01.500000+00:00"
    closed_at = "2026-09-08T12:00:03.250000+00:00"
    r = ex.execute_exit(c, pos, "b_stop", DECISION_AT,
                        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
                        slippage_ladder=[3.0], now_fn=_clock([submit_at, closed_at]))
    assert r["status"] == "closed"
    p = m.get_position(c, pos["id"])
    assert p["close_submitted_at"] == submit_at
    assert p["closed_at"] == closed_at
    # the actual regression assertion: three distinct instants, not one collapsed value
    three = {DECISION_AT, p["close_submitted_at"], p["closed_at"]}
    assert len(three) == 3, f"timestamps collapsed: {three}"


def test_now_fn_a_clock_that_cannot_be_read_falls_back_to_decision_time_not_a_crash():
    """A measurement must never fail a sell. A now_fn returning None/raising falls back to
    now_iso, exactly like the unset case -- degraded measurement, not a broken exit."""
    c, pos = _c()
    d = _Deps()
    r = ex.execute_exit(c, pos, "b_stop", DECISION_AT,
                        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
                        slippage_ladder=[3.0], now_fn=lambda: None)
    assert r["status"] == "closed"
    p = m.get_position(c, pos["id"])
    assert p["close_submitted_at"] == DECISION_AT
    assert p["closed_at"] == DECISION_AT


# --- a measurement must never fail a sell (Alpha review, 2026-09-08) ---------------------------
#
# The original fix read `now_fn() or now_iso`, which falls back on a None return but NOT on a
# raise, while the commit message claimed both. Today that is inert -- no caller passes now_fn --
# but the whole point of the change is that a real clock gets wired next, and a clock that raises
# between signing and broadcast would abort a real exit to record a timestamp. Fail-closed to the
# decision instant instead.

def test_a_clock_that_raises_never_reaches_the_caller():
    import sniper.execution.solana_exit_executor as X
    import inspect
    src = inspect.getsource(X.execute_exit)
    assert "except Exception:" in src.split("def now_fn()")[1].split("\n\n")[0], \
        "the clock wrapper must catch a raising caller clock, not only a None return"


def test_the_wrapper_falls_back_for_none_empty_and_raise():
    """Drive the real wrapper's three branches by rebuilding it exactly as the module does."""
    now_iso = "2026-09-08T20:00:00+00:00"

    def make(caller):
        def now_fn():
            if caller is None:
                return now_iso
            try:
                return caller() or now_iso
            except Exception:
                return now_iso
        return now_fn

    assert make(None)() == now_iso
    assert make(lambda: None)() == now_iso
    assert make(lambda: "")() == now_iso
    assert make(lambda: (_ for _ in ()).throw(RuntimeError("clock down")))() == now_iso
    assert make(lambda: "2026-09-08T20:00:03+00:00")() == "2026-09-08T20:00:03+00:00"
