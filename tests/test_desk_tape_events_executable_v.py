"""STRUCT_BREAK / ONE_SWAP_DROP use the executable value V (prereg c74aeaa7b), not the marginal price (CORTEX-root witness)."""
import importlib.util
import math
from pathlib import Path

spec = importlib.util.spec_from_file_location("dte", Path(__file__).resolve().parents[1] / "scripts" / "research" / "desk_tape_events.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def coin():
    c = m.Coin("fixture")
    for i in range(311):          # constant spot, sells only; the sell fee jumps from 1% to 20% on the last swap
        c.add(("fixture", float(i), i, 0, False, 1_000_000, 1_000_000, "seller", 30_000_000_000,
               1_000_000_000_000_000, 1_000_000_000_000_000, 100 if i < 310 else 2000, 0, m.SOL_QUOTES[-1]))
    return c


def test_fee_only_drop_trips_the_executable_indicators_and_not_the_spot_fallback():
    c, noise, qn, tokens = coin(), .001, m.CLIP_SOL * 1e9 * .99, 1e12
    drop = math.log(m.value(c, 309, tokens, qn) / m.value(c, 310, tokens, qn))
    assert drop >= 2 * noise
    ex = m.indicators(c, 310, noise, tokens, qn)
    assert ex["STRUCT_BREAK"][2] is True and ex["ONE_SWAP_DROP"][2] is True
    sp = m.indicators(c, 310, noise)                                   # no qty/fees: marginal-price fallback, documented as not the contract
    assert sp["STRUCT_BREAK"][2] is False and sp["ONE_SWAP_DROP"][2] is False
