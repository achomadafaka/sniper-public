"""A deploy must not SIGTERM a runner that has an approved, unexecuted funded exit.

TITAN 48565, 2026-09-02: the rail confirmed a stop at -41.5% and allowed the sale at 18:51:19; a
deploy killed the runner at 18:53:02 with that sale unexecuted; it finally sold at -81.3% at
18:55:27. $27.80 of a $58.08 loss happened after we had already decided to sell.
"""
import sqlite3

import pytest

from scripts.memecoin_deploy_exit_drain_gate import approved_unexecuted_exits


def _db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, ticker TEXT, "
              "network_id TEXT, execution_mode TEXT, closed_at TEXT)")
    c.execute("CREATE TABLE memecoin_exit_trigger_audit (position_id INTEGER, reason TEXT, "
              "allowed INTEGER, checked_at TEXT)")
    c.execute("CREATE TABLE memecoin_exit_execution_attempts (position_id INTEGER, "
              "attempted_at TEXT, status TEXT)")
    return c


def _pos(c, pid, *, closed=None, mode="real"):
    c.execute("INSERT INTO memecoin_positions VALUES (?,?,?,?,?)",
              (pid, "TITAN", "4663", mode, closed))


def _audit(c, pid, reason="b_stop", allowed=1, at="2026-09-02T18:51:19"):
    c.execute("INSERT INTO memecoin_exit_trigger_audit VALUES (?,?,?,?)", (pid, reason, allowed, at))


def test_an_approved_exit_with_no_execution_blocks():
    c = _db(); _pos(c, 48565); _audit(c, 48565)
    got = approved_unexecuted_exits(c)
    assert len(got) == 1 and got[0][0] == 48565


def test_an_executed_approval_does_not_block():
    c = _db(); _pos(c, 48565); _audit(c, 48565)
    c.execute("INSERT INTO memecoin_exit_execution_attempts VALUES (?,?,?)",
              (48565, "2026-09-02T18:51:26", "closed"))
    assert approved_unexecuted_exits(c) == []


def test_an_attempt_BEFORE_the_approval_does_not_count():
    """An earlier attempt acted on an earlier decision. It says nothing about this one."""
    c = _db(); _pos(c, 48565); _audit(c, 48565, at="2026-09-02T18:51:19")
    c.execute("INSERT INTO memecoin_exit_execution_attempts VALUES (?,?,?)",
              (48565, "2026-09-02T18:40:00", "closed"))
    assert len(approved_unexecuted_exits(c)) == 1


def test_a_closed_position_does_not_block():
    c = _db(); _pos(c, 48565, closed="2026-09-02T18:55:27"); _audit(c, 48565)
    assert approved_unexecuted_exits(c) == []


def test_a_paper_position_does_not_block():
    c = _db(); _pos(c, 48565, mode="paper"); _audit(c, 48565)
    assert approved_unexecuted_exits(c) == []


def test_a_REFUSED_rail_does_not_block():
    """allowed=0 is the rail deciding NOT to sell. There is nothing to drain."""
    c = _db(); _pos(c, 48565); _audit(c, 48565, allowed=0)
    assert approved_unexecuted_exits(c) == []


@pytest.mark.parametrize("reason", ["b_stop", "catastrophe"])
def test_both_LOSS_rails_block(reason):
    c = _db(); _pos(c, 48565); _audit(c, 48565, reason=reason)
    assert len(approved_unexecuted_exits(c)) == 1


def test_a_take_profit_does_NOT_block_a_deploy():
    """A delayed take-profit costs opportunity, not principal. Blocking releases on one would
    stall deploys for a state that is not losing money."""
    c = _db(); _pos(c, 48565); _audit(c, 48565, reason="real_tp")
    assert approved_unexecuted_exits(c) == []


def test_a_missing_table_does_not_block_a_deploy():
    """This gate must never be the reason a release cannot proceed on an older schema."""
    c = sqlite3.connect(":memory:")
    assert approved_unexecuted_exits(c) == []


def test_it_reports_the_position_so_the_operator_can_see_WHAT_is_draining():
    c = _db(); _pos(c, 48565); _audit(c, 48565)
    pid, ticker, net, reason, when = approved_unexecuted_exits(c)[0]
    assert (ticker, net, reason) == ("TITAN", "4663", "b_stop")
    assert when == "2026-09-02T18:51:19"


# --- every kill site must be guarded, not just the cutover ---------------------------------------
#
# The deploy stops the runner in FIVE places (cluster-gate activation, rollover, band policy,
# timecap lift, and the ordinary cutover). A gate on one lane is not a gate — that is the same
# shape as the shared-gate lesson: fix a shared gate, check all lanes.


def _deploy_source():
    import pathlib
    return pathlib.Path("scripts/deploy_memecoin_runtime.sh").read_text()


def _runner_lookup_starts(src):
    """Cover each actual stop loop and its direct or provider/legacy PID lookup shape."""
    lines = [line.strip() for line in src]
    stops = [i for i, line in enumerate(lines) if line == 'for OLD_PID in $RUNNER_PIDS; do']
    assert stops, "no runner kill sites found; the guard's anchor has moved"
    assert len(stops) == sum(line.startswith('kill "$OLD_PID"') for line in lines)
    for stop in stops:
        if lines[stop - 1] == 'fi':
            start = stop - 6
            assert lines[start] == 'if [ -n "$PROVIDER_ENV" ] || [ "$SHYFT_RUNTIME" -eq 1 ]; then'
            assert lines[start + 1].startswith('RUNNER_PIDS="$(provider_runner_pids ')
            assert lines[start + 2] == 'else'
            legacy = start + 3
        else:
            start = legacy = stop - 2
        assert lines[legacy].startswith('RUNNER_PIDS="$($PY "$SOURCE_ROOT/scripts/memecoin_runtime_processes.py"')
        assert lines[legacy + 1].startswith('--expected ')
        assert lines[stop + 1].startswith('kill "$OLD_PID"')
        yield stop, start


def _assert_runner_stops_drained(src):
    import re
    for stop, lookup in _runner_lookup_starts(src):
        # The drain must directly precede the whole lookup, outside both provider branches.
        # Its error must propagate; an older drain or a swallowed failure cannot satisfy this.
        assert re.fullmatch(r'wait_for_exit_drain "[^"]+"', src[lookup - 1].strip()), (
            f"runner kill at line {stop+1} stops the process without draining an approved exit")


def test_every_runner_kill_site_drains_first():
    _assert_runner_stops_drained(_deploy_source().splitlines())


@pytest.mark.parametrize('site', range(5))
@pytest.mark.parametrize('mutation', ['remove', 'ignore_failure'])
def test_each_applicable_drain_is_required_even_with_older_drains(site, mutation):
    src = _deploy_source().splitlines()
    stops = list(_runner_lookup_starts(src))
    assert len(stops) == 5, 'normal runner stop inventory changed; review every new stop'
    drain = stops[site][1] - 1
    if mutation == 'remove':
        del src[drain]
    else:
        src[drain] += ' || true'
    with pytest.raises(AssertionError, match='without draining an approved exit'):
        _assert_runner_stops_drained(src)


def test_the_drain_fails_rather_than_stepping_over_a_stuck_exit():
    """A bounded wait that gives up silently is worse than no gate: it would look guarded."""
    src = _deploy_source()
    body = src[src.index("wait_for_exit_drain() {"):]
    body = body[:body.index("\n}\n")]
    # The TIMEOUT branch specifically must fail. Asserting merely that the body contains `fail`
    # passes on a mutant that turns the timeout into `return 0`, because the readback error above
    # it also fails — so anchor on the timeout message itself.
    assert 'fail "$_stuck funded position(s) hold an approved but unexecuted exit' in body, (
        "the drain must FAIL when it times out, never return quietly — a bounded wait that gives "
        "up silently is worse than no gate, because it looks guarded")


# --- a bag that CANNOT sell must not block every deploy forever (2026-09-04) ------------------

def _attempt(c, pid, *, at, status):
    c.execute("INSERT INTO memecoin_exit_execution_attempts VALUES (?,?,?)", (pid, at, status))


def test_a_proven_stuck_bag_does_not_block():
    """FROGE 50786: b_stop approved 20:03:33, last attempt 20:01:40 and terminal `stuck`.

    The runner RE-APPROVES every sweep, so the approval keeps advancing past the newest attempt.
    Any position whose sell reverts on chain therefore blocked EVERY deploy forever -- which is
    exactly when a fix is most wanted. FROGE's sell reverts because its only route to native runs
    through a hooked pool that refuses us; no retry and no smaller size changes that.
    """
    c = _db()
    _pos(c, 50786)
    _audit(c, 50786, at="2026-09-04T20:03:33")
    _attempt(c, 50786, at="2026-09-04T20:01:40", status="stuck")   # OLDER than the approval
    assert approved_unexecuted_exits(c) == []


def test_a_decision_with_no_attempt_at_all_still_blocks():
    """The TITAN 48565 case this gate exists for: decided, never executed."""
    c = _db()
    _pos(c, 48565)
    _audit(c, 48565, at="2026-09-02T18:51:19")
    assert len(approved_unexecuted_exits(c)) == 1


def test_a_non_terminal_attempt_still_blocks():
    """Mid-exit: the executor is working on it, so the runner must not be stopped."""
    c = _db()
    _pos(c, 50786)
    _audit(c, 50786, at="2026-09-04T20:03:33")
    _attempt(c, 50786, at="2026-09-04T20:01:40", status="submitted")
    assert len(approved_unexecuted_exits(c)) == 1
