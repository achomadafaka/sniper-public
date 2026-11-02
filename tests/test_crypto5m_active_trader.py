"""Disciplined R3-bound taker: enter ONLY on dual conviction (momentum AND order-flow agree), at a sane
price, HOLD TO SETTLEMENT; lock big wins; exit early only when the conviction FLIPS."""
from sniper.algos.polymarket_crypto5m import active_trader as AT
from sniper.algos.polymarket_crypto5m.active_trader import Position


def _book(up_ask=None, up_bid=None, down_ask=None, down_bid=None):
    return {"up_ask": up_ask, "up_bid": up_bid, "down_ask": down_ask, "down_bid": down_bid}


def _d(**kw):
    # seconds_left within the late-entry window (the engine only enters when the direction has firmed up)
    args = dict(prob_up=0.60, book=_book(up_ask=0.55, down_ask=0.42), position=None,
                seconds_left=60, now_ts=1, momentum_bps=+15.0, momentum_agreement=1.0, book_imbalance=+0.30)
    args.update(kw)
    return AT.decide(**args)


def test_waits_when_too_early():
    assert AT.decide(prob_up=0.60, book=_book(up_ask=0.55, down_ask=0.42), position=None, seconds_left=200,
                     now_ts=1, momentum_bps=+15.0, momentum_agreement=1.0, book_imbalance=+0.30)["action"] == "wait"


def test_enters_only_on_dual_conviction():
    a = _d()                                              # momentum + order-flow both favor Up
    assert a["action"] == "enter" and a["side"] == "Up" and a["price"] == 0.55
    assert "BOTH favor Up" in a["reason"] and "hold to settlement" in a["reason"]


def test_no_entry_when_only_one_signal():
    # order-flow neutral (below threshold) -> not dual conviction -> wait
    assert _d(book_imbalance=0.10)["action"] == "wait"
    # momentum and order-flow conflict -> no agreed side -> wait
    assert _d(momentum_bps=+15.0, book_imbalance=-0.30)["action"] == "wait"


def test_no_entry_without_any_signals():
    a = _d(momentum_bps=None, book_imbalance=None)
    assert a["action"] == "wait" and "no dual conviction" in a["reason"]


def test_incoherent_trend_is_not_conviction():
    # momentum favors Up but scales DISAGREE (agreement 0.5) -> momentum ignored -> no dual conviction
    a = _d(momentum_agreement=0.5)
    assert a["action"] == "wait"


def test_skips_near_certain_ask():
    a = _d(book=_book(up_ask=0.95, down_ask=0.06))
    assert a["action"] == "wait" and "unusable" in a["reason"]


def test_holds_to_settlement_when_conviction_intact():
    pos = Position("Up", 0.55, 1, 18.0, entry_prob=0.60)
    a = AT.decide(prob_up=0.66, book=_book(up_bid=0.62, up_ask=0.64), position=pos, seconds_left=120,
                  now_ts=100, momentum_bps=+15.0, momentum_agreement=1.0, book_imbalance=+0.30)
    assert a["action"] == "hold" and "settlement" in a["reason"]


def test_take_profit_near_certain():
    pos = Position("Up", 0.55, 1, 18.0, entry_prob=0.60)
    a = AT.decide(prob_up=0.99, book=_book(up_bid=0.95, up_ask=0.97), position=pos, seconds_left=60,
                  now_ts=100, momentum_bps=+15.0, momentum_agreement=1.0, book_imbalance=+0.30)
    assert a["action"] == "exit" and a["kind"] == "take_profit" and a["price"] == 0.95


def test_holds_to_settlement_even_on_adverse_move():
    # PURE hold-to-settlement: no whipsaw stop, no conviction-flip exit — only near-certain TP exits early.
    pos = Position("Up", 0.55, 1, 18.0, entry_prob=0.60)
    # conviction flipped against us AND price dropped -> still HOLD (no stop; R3 rides to the close)
    a = AT.decide(prob_up=0.40, book=_book(up_bid=0.45, up_ask=0.47), position=pos, seconds_left=120,
                  now_ts=100, momentum_bps=-15.0, momentum_agreement=1.0, book_imbalance=-0.30)
    assert a["action"] == "hold"


def test_settlement_and_exit_pnl():
    pos = Position(side="Up", entry_price=0.50, entry_ts=1, shares=20.0)
    assert AT.settle_pnl(pos, "Up")["pnl"] == 10.0
    assert AT.settle_pnl(pos, "Down")["pnl"] == -10.0
    assert AT.exit_pnl(pos, 0.80)["pnl"] == 6.0
