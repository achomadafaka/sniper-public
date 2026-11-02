"""Unit C: the funded rails decide on the 1s fast lane, not the ~31s bracket pass (2026-07-27).

A trail cannot ratchet on a peak it never sampled. Measured open-position mark gaps were 14.6s
median / 79s max, and 1.4% of consecutive-sample intervals over 6h contained a >=10% move (max
77%) that was therefore invisible to every rail.
"""
import ast

import pytest
import sniper.settings.memecoin as cfg


def _worker_source(src, name):
    worker = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(src, worker)


def _assert_alerts_stay_slow(src):
    tree = ast.parse(src)
    bracket_test = ast.dump(ast.parse('cycle - last_bracket > 15', mode='eval').body)
    slow = next(n for n in ast.walk(tree)
                if isinstance(n, ast.If) and ast.dump(n.test) == bracket_test)
    fast = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == '_fastlane_worker')
    def calls(node):
        return {n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for fn in ('check_kill_switch_alert', 'check_daily_loss_alert', 'check_gas_alert'):
        assert fn in calls(slow), f'{fn} must still run on the slow pass'
        assert fn not in calls(fast), f'{fn} must NOT run every 1s tick'


def test_stop_debounce_is_counted_in_sweeps_so_it_is_coupled_to_the_tick():
    """THE trap this change had to avoid. stop_debounce is a SWEEP COUNT, so its protection is
    (count x tick) seconds. Moving the decision from a ~31s pass to a 1s tick without raising the
    count would have cut stop confirmation from ~62s to ~2s -- a 30x weakening of the guard that
    stops a bad tick market-selling into the wick, on the one rail that sells at a loss."""
    tick = cfg.MEMECOIN_FASTLANE_TICK_S
    hits = cfg.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS
    assert tick <= 1.0, "fast lane must tick at 1s or faster"
    confirm_s = hits * tick
    assert confirm_s >= 4.0, (
        f"stop confirmation is only {confirm_s}s ({hits} hits x {tick}s) — a single bad tick "
        "clears that. Raise MEMECOIN_REAL_STOP_CONFIRM_SWEEPS with the tick.")
    assert confirm_s <= 15.0, (
        f"stop confirmation of {confirm_s}s is slower than the point of a 1s lane")
    assert hits >= 3, "fewer than 3 independent reads is weaker evidence than the old 2x31s"


def test_alerts_still_fire_from_the_slow_pass():
    """Kill-switch / daily-loss / gas pages are once-on-CROSSING transitions, not rails. They must
    keep running on the slow pass even when it no longer decides — and must NOT move to the 1s lane,
    which would re-evaluate them every tick."""
    src = open("scripts/memecoin_runner_loop.py").read()
    _assert_alerts_stay_slow(src)


@pytest.mark.parametrize('alert', ['check_kill_switch_alert', 'check_daily_loss_alert', 'check_gas_alert'])
def test_alert_scope_check_rejects_alerts_added_to_the_fast_worker(alert):
    tree = ast.parse(open('scripts/memecoin_runner_loop.py').read())
    fast = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == '_fastlane_worker')
    fast.body.insert(0, ast.parse(f'{alert}()').body[0])
    with pytest.raises(AssertionError, match='must NOT run'):
        _assert_alerts_stay_slow(ast.unparse(tree))


def test_fast_lane_reuses_its_own_quote_rather_than_refetching():
    """Deciding at 1s must cost no extra quote: the same batched map that moved the ticker is fed
    to the rails via price_fn. Refetching would be 2 calls/s instead of 1."""
    src = open("scripts/memecoin_runner_loop.py").read()
    fast = _worker_source(src, '_fastlane_worker')
    assert "price_fn=(lambda _mints, _pm=pm: _pm)" in fast


def test_swap_mark_worker_uses_only_the_read_rpc_and_a_readonly_main_ledger():
    """The lane writes the TAPE file, and only the tape file.

    It binds each position's pool itself (2026-08-12) instead of waiting a median 276 s for the
    300 s tape pass, so its tape handle is deliberately writable. The MAIN ledger handle stays
    read-only, and the endpoint stays the read RPC: no custody setting reaches this thread.
    """
    src = open("scripts/memecoin_runner_loop.py").read()
    worker = src.split("def _solana_swap_mark_worker():")[1].split(
        "def _fastlane_worker():"
    )[0]
    assert "solana_read_rpc()" in worker
    assert "connect_sol_tape(" in worker
    assert "connect_observer_main" in worker
    assert "MEMECOIN_SOLANA_RPC_URL" not in worker
    assert "get_connection" not in worker


def test_swap_mark_worker_binds_the_pool_without_the_tape_pass():
    src = open("scripts/memecoin_runner_loop.py").read()
    worker = _worker_source(src, "_solana_swap_mark_worker")
    assert "book = build_price_book(live, sol_conn)" in worker
    # The binding must precede the price book that reads it, or the lane stays a pass behind.
    assert worker.index("bind_open_positions(") < worker.index("_price_book(positions)")
    refresh = worker.split("def _refresh():")[1]
    assert refresh.index("bind_open_positions(") < refresh.index("_price_book(live)")


def test_swap_mark_worker_keeps_recent_paper_mints_after_the_real_close():
    src = open("scripts/memecoin_runner_loop.py").read()
    worker = src.split("def _solana_swap_mark_worker():")[1].split(
        "def _fastlane_worker():"
    )[0]
    assert "open_swap_mark_positions(" in worker


def test_slow_paper_sweep_receives_recent_swap_marks():
    src = open("scripts/memecoin_runner_loop.py").read()
    slow = src.split("if cycle - last_bracket > 15:")[1].split(
        "last_bracket = finish_bracket_cycle"
    )[0]
    assert "supplemental_price_map=" in slow
    assert "_paper_swap_marks.snapshot(" in slow


def test_fastlane_pass_uses_the_shared_swap_cache_without_an_extra_fetch():
    src = open("scripts/memecoin_runner_loop.py").read()
    fast = _worker_source(src, '_fastlane_worker')
    assert "swap_mark_cache=_swap_mark_cache" in fast
    assert "mid_observation_state" in fast
    assert "price_fn=(lambda _mints, _pm=pm: _pm)" in fast


def test_every_name_the_runner_loop_uses_is_actually_in_scope():
    """REGRESSION (2026-07-27): MEMECOIN_FASTLANE_DECIDES was imported INSIDE _fastlane_worker but
    also read by the main loop's bracket gate, so that gate raised NameError every cycle — 88 times
    in production. It took the slow-path exit sweep AND the kill-switch / daily-loss / gas alerts
    down with it, because they share that try block.

    Syntax checks and unit tests both passed: the module imports fine and nothing exercised that
    line. A static scope check does catch it, cheaply, for every name in the file."""
    import ast
    import builtins
    src = open("scripts/memecoin_runner_loop.py").read()
    tree = ast.parse(src)

    module_names = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in ast.walk(node):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    module_names.add(t.id)

    # Any ALL_CAPS config-looking name a function READS must resolve somewhere it can see.
    problems = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        local = set()
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    local.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
            elif isinstance(node, ast.arg):
                local.add(node.arg)
        for node in ast.walk(fn):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id.isupper() and node.id.startswith("MEMECOIN_")
                    and node.id not in local and node.id not in module_names):
                problems.append(f"{fn.name}:{node.lineno} reads undefined {node.id}")
    assert not problems, "names read out of scope:\n  " + "\n  ".join(problems)
