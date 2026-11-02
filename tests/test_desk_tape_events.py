"""desk_tape_events: curve value math and the event/forward-return bookkeeping on a synthetic constant-product coin."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "dte", Path(__file__).resolve().parents[1] / "scripts" / "research" / "desk_tape_events.py")
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)


def coin(steps, t0=1000.0):
    """steps: [(dt_s, is_buy, quote_lamports)] on a SOL curve from the standard start."""
    c = E.Coin("M")
    q, b, t = E.INIT_Q, E.INIT_B, t0
    for n, (dt, buy, qa) in enumerate(steps):
        t += dt
        if buy:
            nb = q * b / (q + qa)
            ta, q, b = b - nb, q + qa, nb
        else:
            nq = q - qa
            nb = q * b / nq
            ta, q, b = nb - b, nq, nb
        c.add(("M", t, 100 + n, n, int(buy), qa, ta, "a%d" % (n % 3), q, b, 7e14, 95, 30, None))
    return c


def test_fill_then_immediate_value_is_about_two_fees_and_first_swap_proves_sol_curve():
    c = coin([(1, True, 5_000_000_000)] + [(1, True, 1_000_000)] * 8)
    assert c.first_ok and c.quote_ok
    i = 0
    j = E.fill_index(c, i, False)
    qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
    tokens = c.b[j] * qn / (c.q[j] + qn)
    v = E.value(c, j, tokens, qn)
    assert abs(v - (1 - 0.0125) ** 2) < 2e-3                    # buy fee in, sell fee out; no price change at the fill


def test_nonstandard_start_is_unverified():
    c = E.Coin("X")
    c.add(("X", 1.0, 1, 0, 1, 1_000_000, 10_000_000, "a", 31e9, 1.0e15, 7e14, 95, 30, None))
    assert not c.first_ok


def test_adverse_fill_never_better_than_optimistic():
    c = coin([(1, True, 2_000_000_000)] + [(0.2, True, 300_000_000)] * 12)
    assert E.price(c, E.fill_index(c, 0, True)) >= E.price(c, E.fill_index(c, 0, False))
