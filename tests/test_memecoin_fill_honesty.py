"""Exit-fill honesty: exits must pay modeled slippage (they filled at mid, zero
impact, while entries paid Jupiter slippage -> inflated PnL). Also the mirror-exit
zero-price guard and partial-exit no-op-when-no-price fixes."""
import sqlite3

from sniper.execution.solana_quote import slippage_frac, modeled_fill, honest_fill
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services.memecoin_brackets import evaluate_brackets


def test_slippage_frac_scales_and_caps():
    assert abs(slippage_frac(100, 10000) - 0.01) < 1e-9      # k=1 -> 100/10000
    assert slippage_frac(100, 0) == 0.5                       # unknown liq -> cap
    assert slippage_frac(10_000_000, 1000) == 0.5            # capped


def test_modeled_fill_direction():
    assert abs(modeled_fill(1.0, 100, 10000, "sell") - 0.99) < 1e-9   # sell: receive less
    assert abs(modeled_fill(1.0, 100, 10000, "buy") - 1.01) < 1e-9    # buy: pay up


def test_honest_fill_sell_applies_slippage_off_mid():
    stub = lambda mint, http=None: {"price": 1.0, "liquidity": 10000}
    price, src = honest_fill("MINT", 100, "sell", 0.0, stats_fn=stub)
    assert src == "dexscreener"
    assert abs(price - 0.99) < 1e-9                           # NOT the raw 1.0 mid


def test_honest_fill_returns_none_when_no_mid_and_no_fallback():
    stub = lambda mint, http=None: {}
    price, src = honest_fill("MINT", 100, "sell", 0.0, stats_fn=stub)
    assert price is None                                      # can't price -> caller must skip


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


class _Ev:
    def __init__(self, side):
        self.side = side; self.source_user_id = "u1"; self.source_handle = "alice"
        self.mint = "MINT"; self.ticker = "PEPE"; self.their_price = 0.0
        self.lag_s = 0.1; self.their_size_usd = 3000.0; self.fomo_event_id = "e1"


def test_mirror_exit_unpriceable_books_censored_noquote_not_a_phantom():
    c = _conn()
    # open a position
    ct.on_copy_event(c, _Ev("buy"), 100.0, "2026-07-05T10:00:00Z",
                     fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert m.get_open_position(c, "u1", "MINT") is not None
    # sell event but no executable quote -> a NOQUOTE close at entry (0 PnL, censored by the
    # report), never a fabricated price and never a fomo/mid substitute (fill model v2)
    r = ct.copy_event_to_arms(
        c, _Ev("sell"), 100.0, "2026-07-05T10:01:00Z", arms=["control"],
        fill_fn=lambda *a, **k: (None, "none"),
        exit_quote_fn=lambda mint, net, qty: (None, "quote_unavailable"))
    assert r == {"control": "exit"}
    assert m.get_open_position(c, "u1", "MINT") is None
    row = c.execute("SELECT exit_reason, realized_pnl FROM memecoin_positions").fetchone()
    # UPDATED 2026-08-29: an unpriceable mirror exit books NULL, not a fabricated flat close.
    assert row[0] == "mirror_noquote" and row[1] is None


def test_partial_exit_no_op_when_no_live_price():
    c = _conn()
    ct.on_copy_event(c, _Ev("buy"), 100.0, "2026-07-05T10:00:00Z",
                     fill_fn=lambda *a, **k: (0.005, "jupiter"))
    pid = m.get_open_position(c, "u1", "MINT")["id"]
    r = ct.partial_exit(c, pid, 0.5, "2026-07-05T10:01:00Z",
                        price_fn=lambda mint, http=None: {})   # no price
    assert r is None                                          # booked nothing (was flat-at-entry)
    assert m.get_open_position(c, "u1", "MINT") is not None


def test_brackets_exit_pays_slippage():
    c = _conn()
    ct.on_copy_event(c, _Ev("buy"), 100.0, "2026-07-05T10:00:00Z",
                     fill_fn=lambda *a, **k: (1.0, "jupiter"))   # entry price 1.0
    pid = m.get_open_position(c, "u1", "MINT")["id"]
    qty = m.open_qty(c, pid)
    # price 7.0 -> +600% -> TP fires; liquidity 1000 so slippage is material
    price_map = {"MINT": {"price": 7.0, "liquidity": 1000}}
    closed = evaluate_brackets(c, price_map, "2026-07-05T10:02:00Z")
    assert len(closed) == 1 and closed[0]["exit_reason"] == "take_profit"
    booked = c.execute("SELECT price FROM memecoin_fills WHERE position_id=? AND kind='bracket_exit'",
                       (pid,)).fetchone()[0]
    assert booked < 7.0                                       # slippage applied, not raw mid
