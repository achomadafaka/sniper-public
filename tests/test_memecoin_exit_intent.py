

# --- exit floor sanity (UNIFROG 27097, 2026-08-05) -----------------------------

from sniper.services.memecoin_exit_intent import (  # noqa: E402
    EXIT_FLOOR_RATIO_LIMIT,
    exit_floor_is_plausible,
)


def test_floor_rejects_the_unifrog_case():
    """~$6 bag, floor signed at ~$0.02 because the quote was taken inside a corrupted window."""
    assert exit_floor_is_plausible(0.02, 6.14) is False


def test_floor_accepts_an_ordinary_slippage_haircut():
    # KIO's real de-risk: $48.52 floor against a $49.68 fill — a genuine, protective floor.
    assert exit_floor_is_plausible(48.52, 49.68) is True
    # even a brutal-but-real 50% haircut is allowed; this guard is not a slippage policy
    assert exit_floor_is_plausible(3.07, 6.14) is True


def test_floor_boundary_is_inclusive():
    reference = 20.0
    assert exit_floor_is_plausible(reference / EXIT_FLOOR_RATIO_LIMIT, reference) is True
    assert exit_floor_is_plausible(reference / EXIT_FLOOR_RATIO_LIMIT - 1e-9, reference) is False


def test_floor_allows_when_there_is_no_usable_reference():
    """No reference means we cannot judge. Refusing every exit we cannot price strands positions."""
    for reference in (None, 0, -1, "nonsense"):
        assert exit_floor_is_plausible(0.02, reference) is True


def test_floor_allows_a_malformed_floor_rather_than_stranding():
    for floor in (None, "nonsense"):
        assert exit_floor_is_plausible(floor, 6.14) is True


def test_floor_is_disabled_by_a_degenerate_limit():
    assert exit_floor_is_plausible(0.02, 6.14, limit=1.0) is True


def test_unifrog_floor_conversion_matches_the_ledger():
    """End-to-end with the numbers actually written to disk on 2026-08-05.

    `min_output_atomic` is verbatim from memecoin_exit_intents.intent_json for position 27097;
    CHAIN_QUOTE['4663'] is WETH at 18 decimals, so the bridge resolves the signed floor to the
    $0.02 that was really received — and the trusted reference is the bag at the last ACCEPTED
    tape mark (16:01:27), not the corrupted one the quote was taken from.
    """
    min_output_atomic = 7_984_416_699_723
    floor_usd = (min_output_atomic / 10 ** 18) * 2500.0          # WETH 18dp x ETH/USD
    reference_usd = 24398.175953732978 * 0.0002514992            # bag at the trusted mid
    assert round(floor_usd, 2) == 0.02
    assert round(reference_usd, 2) == 6.14
    assert exit_floor_is_plausible(floor_usd, reference_usd) is False


# --- the shared four-gate guard ------------------------------------------------

from sniper.services.memecoin_exit_intent import exit_floor_refused  # noqa: E402


class _Intent:
    min_output_atomic = 20_000


def test_floor_guard_is_opt_in_when_either_seam_is_missing():
    """Unwired callers must behave exactly as before."""
    assert exit_floor_refused(_Intent(), {}, {}, 100.0) is False
    assert exit_floor_refused(_Intent(), {}, {}, 100.0,
                              floor_usd_fn=lambda i, q: 0.02) is False
    assert exit_floor_refused(_Intent(), {}, {}, 100.0,
                              reference_value_usd_fn=lambda p, n: 6.14) is False


def test_floor_guard_refuses_an_implausible_floor():
    assert exit_floor_refused(
        _Intent(), {}, {}, 24398.18,
        floor_usd_fn=lambda i, q: 0.02,
        reference_value_usd_fn=lambda p, n: 6.14) is True


def test_floor_guard_values_only_the_quantity_being_sold():
    """A de-risk sells `fraction` of the bag. Valuing the WHOLE bag would refuse legitimate
    small partials — a 4% de-risk against a full-bag reference reads as 25x."""
    bag, mid = 1000.0, 1.0
    fraction = 0.04
    floor = bag * fraction * mid * 0.95          # a normal 5% slippage floor on the partial
    # correct: reference is the sold quantity
    assert exit_floor_refused(
        _Intent(), {}, {}, bag * fraction,
        floor_usd_fn=lambda i, q: floor,
        reference_value_usd_fn=lambda p, n: n * mid) is False
    # wrong: reference is the whole bag -> the same honest floor is refused
    assert exit_floor_refused(
        _Intent(), {}, {}, bag,
        floor_usd_fn=lambda i, q: floor,
        reference_value_usd_fn=lambda p, n: n * mid) is True


def test_floor_guard_allows_when_a_seam_raises():
    def boom(*_a, **_k):
        raise RuntimeError("price source down")

    assert exit_floor_refused(
        _Intent(), {}, {}, 100.0,
        floor_usd_fn=boom,
        reference_value_usd_fn=lambda p, n: 6.14) is False


def test_every_pre_sign_gate_routes_through_the_shared_guard():
    """All four funded exit gates — Solana close/de-risk, EVM close/de-risk — must be protected.
    A path guarded on one chain and bare on another is the bug this consolidates away."""
    import inspect

    from sniper.execution import evm_exit_executor, solana_exit_executor
    for module, fns in ((solana_exit_executor, ("execute_exit", "execute_partial_exit")),
                        (evm_exit_executor, ("execute_exit_evm", "execute_partial_exit_evm",
                                             "replace_exit_evm"))):
        for name in fns:
            fn = getattr(module, name)
            params = inspect.signature(fn).parameters
            assert "floor_usd_fn" in params, f"{module.__name__}.{name}"
            assert "reference_value_usd_fn" in params, f"{module.__name__}.{name}"
            assert "exit_floor_refused" in inspect.getsource(fn), f"{module.__name__}.{name}"


def test_every_runner_seam_passes_the_guard_to_its_executor():
    """The executors accepting the seams is not enough — the runner must actually pass them.

    Caught a real defect: the Solana close had the seams in scope and never handed them over, so
    that path looked wired and was bare.
    """
    import ast

    expected = {
        "sniper/execution/solana_real_seams.py": {"execute_exit", "execute_partial_exit"},
        "sniper/execution/evm_real_seams.py": {"execute_exit_evm", "execute_partial_exit_evm",
                                               "replace_exit_evm"},
    }
    for path, executors in expected.items():
        tree = ast.parse(open(path).read())
        seen = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            nested = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
            if not {"_floor_usd_fn", "_reference_value_usd_fn"} <= nested:
                continue
            for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name) and n.func.id in executors]:
                kwargs = {k.arg for k in call.keywords}
                assert {"floor_usd_fn", "reference_value_usd_fn"} <= kwargs, \
                    f"{path}:{node.name} calls {call.func.id} without the floor seams"
                seen.add(call.func.id)
        assert seen == executors, f"{path}: unguarded executor calls {executors - seen}"
