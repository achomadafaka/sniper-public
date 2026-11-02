"""Exits must carry the same quote-vs-fill column entries carry.

`execution_slippage_pct` was NULL on every exit leg: `book_full_exit_and_finalize` hardcoded a
9-column INSERT that had no quote fields, so the only way to ask "what did execution cost us on
the way out" was to pull swap logs off-chain. The quote object the executor signed is in scope at
the fill site, so the number is recoverable without a single extra network call.

Sign convention is shared with the entry side (`entry_attribution.buy_drift`): the ratio is always
fill/quote, so an exit that receives LESS than quoted is NEGATIVE.
"""
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.execution import solana_exit_executor as ex


def _c(entry_notional=10.0):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint="MINT", ticker="",
                          entry_price=0.02, entry_notional=entry_notional,
                          entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="t", strategy="alpha_b", execution_mode="real")
    return c, m.get_position(c, pid)


class _Deps:
    """500 tokens sold; `out_atomic` is the quoted USDC out in 6-decimal atomic units."""
    def __init__(self, received, out_atomic="16000000"):
        self.received, self.out_atomic, self.sold = received, out_atomic, False
    def quote_fn(self, mint, tokens, slippage_pct):
        q = {"inAmount": "500", "otherAmountThreshold": "1"}
        if self.out_atomic is not None:
            q["outAmount"] = self.out_atomic
        return q
    def usdc_fn(self):
        return self.received if self.sold else 0.0
    def token_fn(self, mint):
        return 500.0
    def sign_fn(self, quote):
        return (b"tx", "SIG0")
    def send_fn(self, signed):
        pass
    def confirm_fn(self, sig):
        self.sold = True
        return "confirmed"


def _run(c, pos, d):
    return ex.execute_exit(c, pos, "b_timecap", "2026-07-09T01:00:00Z",
                           quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
                           confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn,
                           token_balance_fn=d.token_fn, slippage_ladder=[3.0])


def _fill(c, pid):
    return c.execute("SELECT quote_price, execution_slippage_pct, price FROM memecoin_fills "
                     "WHERE position_id=? AND fraction=1.0", (pid,)).fetchone()


def test_exit_short_of_its_quote_records_negative_slippage():
    # quoted 16.0 USDC for 500 tokens (0.032/token); received 15.0 (0.030/token) -> -6.25%
    c, pos = _c(); d = _Deps(received=15.0, out_atomic="16000000")
    assert _run(c, pos, d)["status"] == "closed"
    f = _fill(c, pos["id"])
    assert abs(f["quote_price"] - 0.032) < 1e-12
    assert abs(f["price"] - 0.030) < 1e-12
    # direction is load-bearing: fill/quote, so short-of-quote is NEGATIVE. The inverted ratio
    # would read +6.67 and a swapped sign -6.67 vs +6.25 -- both magnitudes differ, so this
    # single assert fails under either mutation.
    assert abs(f["execution_slippage_pct"] - (-6.25)) < 1e-9


def test_exit_above_its_quote_records_positive_slippage():
    # received 20.0 for a 16.0 quote -> +25.0
    c, pos = _c(); d = _Deps(received=20.0, out_atomic="16000000")
    assert _run(c, pos, d)["status"] == "closed"
    assert abs(_fill(c, pos["id"])["execution_slippage_pct"] - 25.0) < 1e-9


def test_quote_without_out_amount_leaves_unknown_not_zero():
    # a Raydium venue quote carries no outAmount. Unknown must stay NULL: a 0.0 here would read
    # as "this exit executed exactly at its quote" and pollute every slippage aggregate.
    c, pos = _c(); d = _Deps(received=15.0, out_atomic=None)
    assert _run(c, pos, d)["status"] == "closed"
    f = _fill(c, pos["id"])
    assert f["quote_price"] is None and f["execution_slippage_pct"] is None


def test_unparseable_out_amount_does_not_raise_on_a_confirmed_sell():
    # the sell landed; a measurement must never turn it into an exception on the way out.
    c, pos = _c(); d = _Deps(received=15.0, out_atomic="not-a-number")
    assert _run(c, pos, d)["status"] == "closed"
    assert _fill(c, pos["id"])["execution_slippage_pct"] is None


def _run_partial(c, pos, d, fraction):
    return ex.execute_partial_exit(c, pos, fraction, "2026-07-09T01:00:00Z",
                                   # the Solana partial seam (`_partial_quote`) captures the
                                   # fraction and quotes `atomic * fraction`, so the returned
                                   # quote already covers the SOLD amount, not the whole bag.
                                   quote_fn=d.quote_fn,
                                   sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
                                   usdc_fn=d.usdc_fn, token_balance_fn=d.token_fn,
                                   slippage_ladder=[3.0])


def test_derisk_leg_prices_its_quote_against_the_SOLD_quantity():
    """A funded de-risk is a low-volume path, but an instrument present on one exit leg and
    absent on the other reads later as 'de-risks never slip'. The partial quote is sized to the
    fraction, so the unit price must divide by the SOLD quantity, not the whole bag. Dividing by
    the bag would understate the quote 2.5x here and invert the sign of the finding.
    """
    # 500 held, 40% sold = 200 tokens; quoted 6.0 USDC (0.030/token); received 5.4 (0.027) -> -10%
    c, pos = _c(); d = _Deps(received=5.4, out_atomic="6000000")
    assert _run_partial(c, pos, d, 0.4)["status"] == "derisked"
    f = c.execute("SELECT quote_price, execution_slippage_pct FROM memecoin_fills "
                  "WHERE kind='derisk' AND position_id=?", (pos["id"],)).fetchone()
    assert abs(f["quote_price"] - 0.030) < 1e-12
    assert abs(f["execution_slippage_pct"] - (-10.0)) < 1e-9


def test_derisk_without_a_quote_stays_unknown():
    c, pos = _c(); d = _Deps(received=5.4, out_atomic=None)
    assert _run_partial(c, pos, d, 0.4)["status"] == "derisked"
    f = c.execute("SELECT quote_price, execution_slippage_pct FROM memecoin_fills "
                  "WHERE kind='derisk' AND position_id=?", (pos["id"],)).fetchone()
    assert f["quote_price"] is None and f["execution_slippage_pct"] is None
