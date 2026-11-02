"""`maint` was a residual and named no worker.

On 2026-09-06 the runner was restarted twice for a stale sweep. The first named its cause,
`fomo_fetch=28.6s`. The second was `maint=56.3s`, and `maint` is computed as
`cycle - copy - auth` — a residual over ONE try block containing twelve cadence-gated sub-blocks, so
the log could not say which of them spent the 56 seconds.

Each block now stamps its own elapsed seconds under `maint.<name>`. Measurement only: no control
flow is touched, and a block its cadence gate skipped stamps ~0, which is itself the answer.
"""
import ast
import re
from pathlib import Path

import pytest

LOOP = Path("scripts/memecoin_runner_loop.py")

# In source order, each phase name and the required predicates of the next block.
EXPECTED = [
    ("bracket", "contexts"),
    # The pump/capture research pass left this loop for its own thread (2026-09-25), and with it
    # the "pump" sub-phase: the block after `contexts` is now the follow sync.
    ("contexts", "cycle - last_sync > 300"),
    ("follow_sync", "not _FUNDED_ONLY and cycle - last_cleanup > 3600"),
    ("cleanup", "contexts and time.monotonic() - last_reconcile > MEMECOIN_RECONCILE_PERIOD_S"),
    ("reconcile", "_RENT_SWEEP_ENABLED and contexts and cycle - last_rent_sweep > _RENT_SWEEP_PERIOD_S"),
    ("rent_sweep", "_GAS_TOPUP_ENABLED and contexts and cycle - last_gas_topup > _GAS_TOPUP_PERIOD_S"),
    ("gas_topup", "_HOLDINGS_ENABLED and contexts"),
    ("other_holdings", "_EVM_GAS_TOPUP_ENABLED and contexts and cycle - last_evm_gas_topup > _EVM_GAS_TOPUP_PERIOD_S"),
    # The V4 PoolKey seed (2026-09-24) is its own named sub-phase: it reads a SECOND store, so a
    # slow pass there must be attributable on its own rather than inside a neighbour's span.
    ("evm_gas_topup", "cycle - last_v4_key_seed > _V4_KEY_SEED_PERIOD_S"),
    ("v4_key_seed", "not _FUNDED_ONLY and _RESIDUAL_TAIL_ENABLED and contexts"),
    ("residual_tail", "not _FUNDED_ONLY and _POST_TP_ENABLED and contexts and cycle - last_post_tp > _POST_TP_PERIOD_S"),
    ("post_tp", 'not _FUNDED_ONLY and os.getenv("MEMECOIN_SNAPSHOT_ENABLED", "1").strip().lower() in {"1", "true", "yes"}'),
    ("snapshot", None),                    # the last block in the try
]


@pytest.fixture(scope="module")
def lines():
    return LOOP.read_text().split("\n")


def _marks(lines):
    return [(i, m.group(1)) for i, l in enumerate(lines)
            if (m := re.match(r'\s*_mp\("(\w+)"\)\s*$', l))]


def test_every_maintenance_block_stamps_its_own_name(lines):
    assert [name for _i, name in _marks(lines)] == [n for n, _ in EXPECTED]


def _assert_block_labels(lines):
    tree = ast.parse('\n'.join(lines))
    def marker(stmt):
        return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == '_mp')
    maint, = [n for n in ast.walk(tree)
              if isinstance(n, ast.Try) and any(marker(s) for s in n.body)]
    found = [(i, s.value.args[0].value) for i, s in enumerate(maint.body) if marker(s)]
    assert [name for _, name in found] == [name for name, _ in EXPECTED], 'maintenance marker order'
    def terms(node):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return set().union(*(terms(v) for v in node.values))
        return {ast.dump(node)}
    for (idx, name), (_, required) in zip(found, EXPECTED):
        if required is None:
            continue
        following = maint.body[idx + 1]
        assert isinstance(following, ast.If), f'{name} must precede its maintenance block'
        assert terms(ast.parse(required, mode='eval').body) <= terms(following.test), required


def test_each_name_measures_the_block_it_claims(lines):
    """A swapped label sends investigation at the wrong worker; cadence and retirement remain."""
    _assert_block_labels(lines)


@pytest.mark.parametrize('mutation', ['swapped_markers', 'cleanup_cadence', 'cleanup_funded_guard'])
def test_maintenance_check_rejects_wrong_marker_cadence_or_guard(lines, mutation):
    src = '\n'.join(lines)
    if mutation == 'swapped_markers':
        src = src.replace('_mp("follow_sync")', '_mp("placeholder")').replace(
            '_mp("cleanup")', '_mp("follow_sync")').replace('_mp("placeholder")', '_mp("cleanup")')
    elif mutation == 'cleanup_cadence':
        src = src.replace('cycle - last_cleanup > 3600', 'cycle - last_cleanup > 1800')
    else:
        src = src.replace('not _FUNDED_ONLY and cycle - last_cleanup > 3600',
                          'cycle - last_cleanup > 3600')
    with pytest.raises(AssertionError):
        _assert_block_labels(src.split('\n'))


def test_the_marks_do_not_change_control_flow(lines):
    """Every mark is a bare call at the try's own indentation — never inside a branch, never
    conditional, never able to swallow or raise."""
    for idx, name in _marks(lines):
        assert lines[idx] == '            _mp("%s")' % name, lines[idx]


def test_the_helper_records_a_duration_and_advances_the_baseline():
    clock = iter([100.0, 100.5, 103.5, 103.75])
    ph, t = {}, [next(clock)]

    def _mp(name):
        now = next(clock)
        ph["maint." + name] = now - t[0]
        t[0] = now

    _mp("bracket"); _mp("reconcile"); _mp("snapshot")

    assert ph == {"maint.bracket": 0.5, "maint.reconcile": 3.0, "maint.snapshot": 0.25}


def test_the_residual_total_is_kept_so_the_series_stays_comparable(lines):
    """`maint` itself must survive: the existing 30-second report has been emitting it for days and
    a rename would break every comparison across this change."""
    text = "\n".join(lines)
    assert "_ph['maint'] = time.time() - cycle" in text


def test_the_sub_phases_sum_to_about_the_residual(lines):
    """Not asserted numerically at runtime — the marks span the whole try and the residual spans the
    same wall time plus the finish_db_cycle call, so they agree to within that call. This test
    exists to state the relationship a reader will want when the numbers do not add up."""
    baseline = next(i for i, l in enumerate(lines) if l.strip() == "_t_mp = [time.time()]")
    # the maintenance try is the FIRST `try:` after the baseline, not the first in the file
    maint_try = next(i for i, l in enumerate(lines[baseline:], baseline) if l == "        try:")
    last_mark = max(i for i, l in enumerate(lines) if l.strip().startswith("_mp("))
    residual = next(i for i, l in enumerate(lines)
                    if "_ph['maint'] = time.time() - cycle" in l)

    assert baseline < maint_try < last_mark < residual



def test_the_bracket_block_stamps_each_part_in_order(lines):
    """2026-09-27: `maint.bracket` averaged 2.5-3 s a run with no real position open and could not
    say which part. Each part stamps its own name, in source order, right after the call it
    measures; a moved stamp would credit one part's seconds to its neighbour."""
    text = "\n".join(lines)
    start, end = text.index("_tbr = time.time()\n                _pb_kwargs"), text.index('_mp("bracket")')
    block = text[start:end]
    order = [("start_paper_bracket(", "maint.bracket.paper"),
             ("run_slow_exit_sweep(", "maint.bracket.real_sweep"),
             ("check_gas_alert(", "maint.bracket.alerts"),
             ("finish_bracket_cycle(", "maint.bracket.candidate")]
    at = 0
    for call, stamp in order:
        c = block.index(call, at)
        s = block.index('_ph["%s"]' % stamp, c)
        assert all(block.find(other, c, s) < 0 for other, _ in order if other != call), stamp
        at = s

# --- the feed poll skips the backoff instead of standing inside it ---------------------------------

def test_the_loop_poll_passes_a_small_wait_cap_and_the_45s_cap_is_untouched():
    """`fomo_fetch=28.6s` on 2026-09-06 was OUR OWN rate guard backing off after a 429, not the feed
    being slow — `_fetch_s` is measured around `fomo_get` and `fomo_request` sleeps first.

    Shortening that backoff is the ban (2026-08-03: valid token, 403, no feed). Standing inside it
    holds the exit sweep, which shares this loop (2026-09-02: a confirmed stop at -41.5% sold at
    -81.3%). So the loop REFUSES the poll and carries on; the backoff itself is untouched.
    """
    from sniper.settings import memecoin as cfg
    runner = Path("sniper/services/memecoin_runner.py").read_text()

    assert cfg.MEMECOIN_FOMO_POLL_MAX_WAIT_S == 3.0
    assert cfg.MEMECOIN_FOMO_MAX_THROTTLE_WAIT_S == 45.0, \
        "the cap for callers that must wait their turn is unchanged"
    assert "max_wait_s=MEMECOIN_FOMO_POLL_MAX_WAIT_S" in runner


def test_a_refused_poll_does_not_cost_other_callers_their_turn():
    """The property that makes skipping safe: `before_call` refuses WITHOUT ratcheting `_last_call`,
    so a caller that gives up its turn does not push the queue out for everyone behind it."""
    from sniper.feeds import fomo_feed

    guard = fomo_feed._RateGuard() if hasattr(fomo_feed, "_RateGuard") else None
    if guard is None:                       # class name differs; assert the source property instead
        src = Path("sniper/feeds/fomo_feed.py").read_text()
        assert "Refuse WITHOUT ratcheting `_last_call`" in src
        return
    guard.gap_s = 30.0
    guard._last_call = __import__("time").monotonic()
    before = guard._last_call
    with pytest.raises(fomo_feed.FomoThrottleBacklog):
        guard.before_call(max_wait_s=3.0)
    assert guard._last_call == before


def test_the_throttle_refusal_reaches_the_loop_as_a_feed_failure():
    """It must propagate: the loop's own comment promises `EXIT management + telemetry run
    REGARDLESS of feed health`, and that is the whole point of refusing rather than sleeping."""
    loop = LOOP.read_text()
    assert "EXIT management + telemetry run REGARDLESS of feed health" in loop
