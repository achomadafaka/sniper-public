"""Independent witnesses for the meaning of data supplied to the desk models."""
from sniper.services import memecoin_desk_risk_live as R
from sniper.services import memecoin_desk_structure as S


def trade(t, price):
    return {"received_at": t, "slot": int(t), "signature": str(t), "ordinal": 0,
            "is_buy": True, "usd": 10.0, "price_usd": price, "actor": str(t)}


def test_healthy_rise_can_have_no_support_pivot():
    rows = [trade(t, 1 + t / 1000) for t in range(1, 300)]
    st = S.structure(rows, as_of=300, cut_slot=400)
    assert st["windows"]["60s"]["trend"]["label"] == "up"
    assert st["swings"]["support_below_pct"] is None


def test_historical_failed_hold_can_end_above_breakout_reference():
    rows = [trade(250, 1.0), trade(290, 1.1), trade(292, .99), trade(295, 1.2)]
    st = S.structure(rows, as_of=300, cut_slot=400)
    assert st["breakout"]["60s"]["broke"] is True
    assert st["breakout"]["60s"]["holding"] is False
    assert st["price_usd"] > 1.0


def test_risk_does_not_instruct_missing_support_is_a_break():
    assert "support_below_pct missing or" not in R.SYSTEM_PROMPT
