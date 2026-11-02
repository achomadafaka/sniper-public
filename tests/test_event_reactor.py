from sniper.research.leadlag import event_reactor as E


def test_classify_trade_detects_liquidation_and_continuation_side():
    liq = {"side": "A", "liquidation": {"liquidatedUser": "0x1", "method": "market"}}
    is_liq, side = E.classify_trade(liq)
    assert is_liq and side == "short"          # forced sell (ask-side taker) -> continue short
    assert E.classify_trade({"side": "B", "px": "1"}) == (False, None)
    assert E.classify_trade({"side": "B", "liquidation": None}) == (False, None)


def test_detect_dislocation_requires_k_sigma_and_floor():
    assert E.detect_dislocation(10.0, vol_bps=5.0) is None        # below floor + below k*vol
    t = E.detect_dislocation(60.0, vol_bps=5.0)                    # 60 > max(25, 20)
    assert t and t.side == "long" and t.kind == "dislocation"
    assert E.detect_dislocation(-60.0, vol_bps=5.0).side == "short"
    assert E.detect_dislocation(30.0, vol_bps=10.0) is None        # 30 < k*vol=40


def test_exit_plan_targets_clear_the_fee_and_scale_with_move():
    small = E.exit_plan(E.Trigger("dislocation", "long", 30.0), vol_bps=5.0)
    big = E.exit_plan(E.Trigger("dislocation", "long", 200.0), vol_bps=20.0)
    assert small["tp_bps"] >= 22.5            # always clears ~2.5x round-trip fee
    assert big["tp_bps"] > small["tp_bps"]    # scales with the event size


def test_executable_pnl_is_buy_ask_sell_bid_with_fee():
    # long: buy ask 100, sell bid 101 -> 100bps - 9bps fee
    assert round(E.executable_pnl_bps("long", 99.9, 100.0, 101.0, 101.1), 1) == round(100 - 9, 1)
    # short: sell bid 100, buy ask 99 -> 100bps - 9bps
    assert round(E.executable_pnl_bps("short", 100.0, 100.1, 98.9, 99.0), 1) == round(100 - 9, 1)
    # flat mid still loses the fee (no spread here)
    assert E.executable_pnl_bps("long", 100.0, 100.0, 100.0, 100.0) < 0
