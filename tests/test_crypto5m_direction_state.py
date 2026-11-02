"""Memory/confirmation layer: confirm a direction only when it PERSISTS, agrees across horizons, and the
tape order flow agrees — so the scalper commits on sustained moves, not micro-wiggles."""
from sniper.algos.polymarket_crypto5m.direction_state import DirectionState


def _thesis(side, conv=0.6, ofi_vote=1):
    sign = 1 if side == "Up" else (-1 if side == "Down" else 0)
    return {"side": side, "conviction": conv, "score": 0.6 * sign,
            "votes": {"order_flow": (ofi_vote if side else None)}}


def _desk(ret15=0.0006, ret30=0.0005):
    return {"ret_15s": ret15, "ret_30s": ret30}


def test_sustained_move_confirms_after_persist_ticks():
    ds = DirectionState(persist_ticks=3, enter_conviction=0.5)
    r1 = ds.update(_thesis("Up"), _desk(), 100)
    r2 = ds.update(_thesis("Up"), _desk(), 102)
    assert not r1["confirmed"] and not r2["confirmed"]      # not yet persistent
    r3 = ds.update(_thesis("Up"), _desk(), 104)
    assert r3["confirmed"] and r3["confirmed_side"] == "Up" and r3["persistence_ticks"] >= 3


def test_wiggle_never_confirms():
    ds = DirectionState(persist_ticks=3)
    sides = ["Up", "Down", "Up", "Down", "Up"]
    res = [ds.update(_thesis(s), _desk(ret15=(0.0006 if s == "Up" else -0.0006),
                                       ret30=(0.0005 if s == "Up" else -0.0005)), 100 + 2 * i)
           for i, s in enumerate(sides)]
    assert not any(r["confirmed"] for r in res)             # alternating sign never persists


def test_single_spike_does_not_confirm():
    ds = DirectionState(persist_ticks=3)
    ds.update(_thesis(None), _desk(), 100)
    r = ds.update(_thesis("Up", conv=0.9), _desk(), 102)    # one strong tick is not memory
    assert not r["confirmed"] and r["persistence_ticks"] == 1


def test_horizon_disagreement_blocks_confirmation():
    ds = DirectionState(persist_ticks=2)
    # read Up persists, but the long horizon (ret_30s) points Down -> not a real trend
    ds.update(_thesis("Up"), _desk(ret15=0.0006, ret30=-0.0005), 100)
    r = ds.update(_thesis("Up"), _desk(ret15=0.0006, ret30=-0.0005), 102)
    assert not r["confirmed"] and r["horizons_agree"] is False


def test_order_flow_disagreement_blocks_confirmation():
    ds = DirectionState(persist_ticks=2)
    ds.update(_thesis("Up", ofi_vote=-1), _desk(), 100)
    r = ds.update(_thesis("Up", ofi_vote=-1), _desk(), 102)  # tape flow opposes the read
    assert not r["confirmed"] and r["flow_agree"] is False


def test_weak_conviction_blocks_confirmation():
    ds = DirectionState(persist_ticks=2, enter_conviction=0.5)
    ds.update(_thesis("Up", conv=0.2), _desk(), 100)
    r = ds.update(_thesis("Up", conv=0.2), _desk(), 102)
    assert not r["confirmed"]
