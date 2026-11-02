"""The Solana on-chain watcher runs from the pinned release (2026-09-28, Alpha).

It feeds funded admission through the on-chain lane and imports sniper/ modules, yet it ran from
the mutable checkout under scripts/research/onchain_armed_watchdog.sh. memecoin_watchdog.sh now owns
it the way it owns the EVM watchers. The real shell function runs here with `ps`, `kill`, `setsid`
and `sleep` substituted.
"""
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "scripts/memecoin_watchdog.sh"
# The state root the rows are written against; the harness maps it onto tmp_path, because the
# watchdog replaces only releases under its own $STATE_ROOT (2026-09-29).
HOME = "/home/u/Sniper"
REL = f"{HOME}/.runtime/memecoin/releases/abc123"
OLD = f"{HOME}/.runtime/memecoin/releases/0ld999"
FOREIGN = "/home/other/Sniper/.runtime/memecoin/releases/f00d42"
SCRIPT = "scripts/research/onchain_wallet_watch.py"


def run(tmp_path, ps_rows, *, ready=None, age=0, dead=()):
    """`ready` is the pinned watcher's data/onchain_watch.ready line; `age` its etimes in seconds;
    `dead` the pids a `kill -0` finds gone (listed by ps, exited before the kill)."""
    source = WATCHDOG.read_text()
    functions = source[source.index('ONCHAIN_SOL_SCRIPT="'):source.index("stop_unpinned_onchain_watcher() {")]
    (tmp_path / "data").mkdir(exist_ok=True)
    if ready is not None:
        (tmp_path / "data" / "onchain_watch.ready").write_text(ready)
    calls = tmp_path / "calls"
    harness = "\n".join([
        "set -u", f"ROOT={shlex.quote(REL.replace(HOME, str(tmp_path)))}",
        f"STATE_ROOT={shlex.quote(str(tmp_path))}",
        "PY=/py; DB_PATH=/state/sniper_tracker.db", f"CALLS={shlex.quote(str(calls))}",
        'log() { printf "log %s\\n" "$*" >> "$CALLS"; }', functions,
        "ps() { case \"$*\" in *etimes*) echo %d ;; *) printf '%%s\\n' %s ;; esac; }" % (
            int(age), " ".join(shlex.quote(r.replace(HOME, str(tmp_path))) for r in ps_rows) or "''"),
        'kill() { if [ "$1" = -0 ]; then case " %s " in *" $2 "*) return 1 ;; esac; return 0; fi; '
        'printf "kill %%s\\n" "$*" >> "$CALLS"; }' % " ".join(str(d) for d in dead),
        'setsid() { printf "launch %s\\n" "$*" >> "$CALLS"; }',
        "sleep() { :; }", "ensure_onchain_solana_watcher", "wait",
    ])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=10)
    assert result.returncode == 0, result.stderr
    lines = [l.replace(str(tmp_path), HOME) for l in calls.read_text().splitlines()] \
        if calls.exists() else []
    log = (tmp_path / "data" / "onchain_armed.log")
    return lines, (log.read_text() if log.exists() else "")


def _row(pid, script, *, py="/py"):
    return f"{pid} {py} {script} --duration 86400"


def test_no_watcher_launches_the_pinned_one_with_the_state_root(tmp_path):
    lines, log = run(tmp_path, [])
    [launch] = [l for l in lines if l.startswith("launch ")]
    assert f"PYTHONPATH={REL} SNIPER_STATE_ROOT={HOME} DB_PATH=/state/sniper_tracker.db" in launch
    assert launch.endswith(f"/py {REL}/{SCRIPT} --duration 86400")
    assert "relaunched armed watcher pid" in log, "monitors wait on this line"


def test_the_pinned_one_running_stops_a_source_tree_copy_beside_it(tmp_path):
    lines, _ = run(tmp_path, [_row(11, f"{REL}/{SCRIPT}"), _row(22, SCRIPT)],
                   ready="11 2026-10-07T21:00:00+00:00")
    assert "kill 22" in lines and not any(l.startswith("launch ") for l in lines)


# THE RESTART GAP (operator order 2026-10-07). On every deploy the new release's watchdog killed the
# older-release watcher, slept 1 s and launched the new one: at 20:52:04.7Z the old one took SIGTERM
# and the new one subscribed seconds later, with no backfill, so a buy in between was seen by neither
# lane. The old watcher now keeps running until the new one has acked every owner (rows dedupe on
# signature, a datagram wake follows only a stored row).

def test_an_older_release_keeps_watching_while_the_new_one_launches(tmp_path):
    """Fails if kill-before-launch comes back: no signal at all on the launching tick."""
    lines, log = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}")], ready="33 2026-10-07T20:00:00+00:00")
    assert any(l.startswith("launch ") for l in lines), lines
    assert not any(l.startswith("kill ") for l in lines), lines
    assert "relaunched armed watcher pid" in log
    assert not (tmp_path / "data" / "onchain_watch.ready").exists(), \
        "a launch clears the line, so a reused pid can never inherit an earlier process's ready"


def test_the_old_watcher_stops_once_the_pinned_one_acked_every_owner(tmp_path):
    lines, _ = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}"), _row(11, f"{REL}/{SCRIPT}")],
                   ready="11 2026-10-07T21:00:00+00:00", age=8)
    assert "kill 33" in lines and not any(l.startswith("launch ") for l in lines), lines


def test_the_old_watcher_stays_while_the_pinned_one_has_not_acked(tmp_path):
    """A new watcher still subscribing, or backing off on a 429, must not cost the old socket."""
    lines, _ = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}"), _row(11, f"{REL}/{SCRIPT}")], age=90)
    assert not any(l.startswith(("kill ", "launch ")) for l in lines), lines
    assert any("waiting" in l and "33" in l for l in lines), "the hold is logged"


def test_a_ready_line_written_by_another_process_does_not_count(tmp_path):
    """The old watcher (pid 33) or a dead earlier launch (99) left the line, not the pinned one."""
    for stale_ready in ("99 2026-10-07T20:00:00+00:00", "33 2026-10-07T20:00:00+00:00", "", "junk"):
        lines, _ = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}"), _row(11, f"{REL}/{SCRIPT}")],
                       ready=stale_ready, age=30)
        assert not any(l.startswith("kill ") for l in lines), (stale_ready, lines)


# AT THE CAP THE NEW ONE GOES (Gamma LOW-2, Alpha GO 2026-10-07). A new watcher still unready at the
# cap is backing off (a quota 429, a refused subscribe); stopping the OLD, fully acked one there made
# the watcher dark, the gap this handover closes. The new one is stopped instead and relaunched beside
# the old one on the next tick with a fresh backoff. Never zero watchers: if the old one is gone too,
# the new one stays.

def test_at_the_cap_the_unready_new_watcher_goes_and_the_old_one_keeps_watching(tmp_path):
    lines, _ = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}"), _row(11, f"{REL}/{SCRIPT}")], age=300)
    assert "kill 11" in lines and "kill 33" not in lines, lines
    assert any("cap" in l for l in lines if l.startswith("log ")), lines


def test_a_partial_watcher_is_not_ready_before_the_cap(tmp_path):
    lines, _ = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}"), _row(11, f"{REL}/{SCRIPT}")],
                   ready="11 partial 23/24", age=90)
    assert not any(l.startswith("kill ") for l in lines), lines


def test_at_the_cap_a_partial_new_watcher_stays_and_the_old_one_goes(tmp_path):
    """An owner refused on every try leaves every new watcher partial; stopping the new one would
    loop forever (Gamma LOW on 252bab9d8). A partial one has a socket, so the old one goes."""
    lines, _ = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}"), _row(11, f"{REL}/{SCRIPT}")],
                   ready="11 partial 23/24", age=300)
    assert "kill 33" in lines and "kill 11" not in lines, lines


def test_at_the_cap_with_the_old_watcher_gone_the_new_one_stays(tmp_path):
    lines, _ = run(tmp_path, [_row(33, f"{OLD}/{SCRIPT}"), _row(11, f"{REL}/{SCRIPT}")], age=300,
                   dead=(33,))
    assert not any(l.startswith("kill ") for l in lines), lines


# THE DAILY END (Gamma LOW-1a). The watcher exits itself after --duration 86400 and cron relaunched
# it up to a minute later. Its same-release replacement now launches beside it ROTATE_LEAD_S before
# that end; the old one then exits on its own, so no kill is involved.

def test_a_watcher_near_its_86400_s_end_gets_a_replacement_beside_it(tmp_path):
    lines, _ = run(tmp_path, [_row(11, f"{REL}/{SCRIPT}")], age=86400 - 180,
                   ready="11 2026-10-06T21:00:00+00:00")
    assert any(l.startswith("launch ") for l in lines) and not any(l.startswith("kill ") for l in lines)
    assert not (tmp_path / "data" / "onchain_watch.ready").exists()


def test_a_watcher_short_of_its_end_gets_no_replacement(tmp_path):
    lines, _ = run(tmp_path, [_row(11, f"{REL}/{SCRIPT}")], age=86400 - 181)
    assert lines == [], lines


def test_a_replacement_already_running_is_not_launched_again(tmp_path):
    lines, _ = run(tmp_path, [_row(11, f"{REL}/{SCRIPT}"), _row(12, f"{REL}/{SCRIPT}")], age=86390)
    assert lines == [], lines


def test_a_release_watcher_of_another_state_root_is_never_signalled(tmp_path):
    """A pytest tmp STATE_ROOT beside the live pinned watcher (2026-09-29): the live one is not an
    older release of THIS root, so it is neither killed nor counted; ours launches beside it."""
    lines, _ = run(tmp_path, [_row(77, f"{FOREIGN}/{SCRIPT}")])
    assert not any(l.startswith("kill ") for l in lines), lines
    assert any(l.startswith("launch ") for l in lines)


def test_a_source_tree_copy_alone_is_left_until_the_pinned_one_runs(tmp_path):
    """The handover overlaps one tick (rows dedupe on signature) rather than going dark."""
    lines, _ = run(tmp_path, [_row(22, SCRIPT)])
    assert not any(l.startswith("kill ") for l in lines)
    assert any(l.startswith("launch ") for l in lines)


def test_a_shell_or_the_evm_watcher_naming_the_script_never_matches(tmp_path):
    rows = [f"44 bash -c tail -f x; {SCRIPT} --duration 86400",
            f"55 /usr/bin/grep -F {SCRIPT} --duration 86400",
            _row(66, f"{REL}/scripts/research/onchain_evm_wallet_watch.py"),
            _row(11, f"{REL}/{SCRIPT}")]
    lines, _ = run(tmp_path, rows)
    assert lines == [], lines


def test_the_watcher_takes_its_state_from_the_environment(tmp_path):
    """Run from the release, the ledger, heartbeat, stage log and .env are the state root's."""
    code = ("import importlib.util as u; s=u.spec_from_file_location('w', %r); m=u.module_from_spec(s);"
            " s.loader.exec_module(m); print(m.DB); print(m.HEARTBEAT); print(m.STAGE_LOG)"
            % str(ROOT / SCRIPT))
    env = {"PATH": "/usr/bin:/bin", "SNIPER_STATE_ROOT": str(tmp_path),
           "DB_PATH": str(tmp_path / "ledger.db"), "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == [str(tmp_path / "ledger.db"),
                                  str(tmp_path / "data/onchain_watch.heartbeat"),
                                  str(tmp_path / "data/onchain_watch_stages.jsonl")]
    bare = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=bare,
                         timeout=60)
    assert out.stdout.split()[0] == str(ROOT / "sniper_tracker.db"), "no env: the repo, as before"
