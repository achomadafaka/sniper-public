"""Run the real roster-drift block with local process, heartbeat and count substitutes.

The block exists because `load_watchlist()` runs once inside `run()`, outside the reconnect loop.
An address confirmed after a start waits for the next restart, which is up to 24 hours away.
Measured on 2026-09-23: 18 addresses became confirmed at 08:01:42Z and reached the socket only at
the 09:42:53Z start.
"""
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/research/onchain_armed_watchdog.sh"


def run_drift(tmp_path, *, pid_age, subs, desired, now=1000):
    """Drive the real block. `pid_age` is the raw `watcher_pid_age` line ('' for no process).
    Calls on the same tmp_path share data/, so a drift first seen on one tick persists to the next."""
    source = SCRIPT.read_text()
    block = source[source.index('BEAT="$REPO/data/onchain_watch.heartbeat"'):]
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "onchain_watch.heartbeat").write_text(subs)
    kills = tmp_path / "kills"
    harness = "\n".join([
        f"REPO={shlex.quote(str(tmp_path))}",
        f"KILLS={shlex.quote(str(kills))}",
        block.replace("roster_drift_restart\n", "", 1),
        # Substitutes come AFTER the block so they replace the real definitions.
        f"watcher_pid_age() {{ printf '%s' {shlex.quote(pid_age)}; }}",
        f"desired_addresses() {{ printf '%s' {shlex.quote(desired)}; }}",
        f"now_s() {{ printf '%s' {int(now)}; }}",
        'kill() { printf "%s\\n" "$*" >> "$KILLS"; }',
        "roster_drift_restart",
    ])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=10)
    assert result.returncode == 0, result.stderr
    log = data / "onchain_armed.log"
    return (kills.read_text() if kills.exists() else "",
            log.read_text() if log.exists() else "")


def test_a_drift_that_persists_past_the_grace_restarts_the_watcher(tmp_path):
    beat = '{"ts": "x", "subs": 2}'
    kills, log = run_drift(tmp_path, pid_age="4242 6000", subs=beat, desired="20", now=1000)
    assert kills == "" and log == "", "the first sighting only starts the clock"
    kills, _ = run_drift(tmp_path, pid_age="4242 6000", subs=beat, desired="20", now=1179)
    assert kills == ""
    kills, log = run_drift(tmp_path, pid_age="4242 6000", subs=beat, desired="20", now=1180)
    assert kills == "4242\n"
    assert "roster drift: subs=2 desired=20 for 180s, restarting" in log
    assert not (tmp_path / "data" / "onchain_watch.drift_since").exists()


def test_a_drift_the_watcher_closes_itself_never_kills(tmp_path):
    """A roster restore arms 30 wallets; the in-process refresh subscribes them within ~60 s."""
    run_drift(tmp_path, pid_age="4242 6000", subs='{"ts": "x", "subs": 15}', desired="45", now=1000)
    kills, _ = run_drift(tmp_path, pid_age="4242 6000", subs='{"ts": "x", "subs": 45}',
                         desired="45", now=1120)
    assert kills == "" and not (tmp_path / "data" / "onchain_watch.drift_since").exists()
    kills, _ = run_drift(tmp_path, pid_age="4242 6000", subs='{"ts": "x", "subs": 2}',
                         desired="20", now=5000)
    assert kills == "", "a new drift starts a new clock"


def test_a_drift_recorded_against_another_process_starts_over(tmp_path):
    run_drift(tmp_path, pid_age="4242 6000", subs='{"ts": "x", "subs": 2}', desired="20", now=1000)
    kills, _ = run_drift(tmp_path, pid_age="5151 700", subs='{"ts": "x", "subs": 2}',
                         desired="20", now=9000)
    assert kills == ""
    assert (tmp_path / "data" / "onchain_watch.drift_since").read_text().split() == ["5151", "9000"]


def test_an_equal_count_leaves_the_watcher_alone(tmp_path):
    kills, log = run_drift(tmp_path, pid_age="4242 6000", subs='{"ts": "x", "subs": 20}',
                           desired="20")
    assert kills == ""
    assert log == ""


def test_a_negative_difference_never_kills(tmp_path):
    """Fewer desired than subscribed means an unsubscribe is still in flight. A dead subscription
    is free; a restart to drop it is not."""
    kills, _ = run_drift(tmp_path, pid_age="4242 6000", subs='{"ts": "x", "subs": 20}',
                         desired="2")
    assert kills == ""


def test_a_watcher_younger_than_the_floor_is_never_killed(tmp_path):
    """A watcher still subscribing reports a low `subs` by design. Killing it for that is a loop."""
    kills, log = run_drift(tmp_path, pid_age="4242 599", subs='{"ts": "x", "subs": 2}',
                           desired="20")
    assert kills == ""
    assert log == ""


def test_no_running_watcher_does_nothing(tmp_path):
    """The relaunch block above owns an absent process."""
    kills, _ = run_drift(tmp_path, pid_age="", subs='{"ts": "x", "subs": 2}', desired="20")
    assert kills == ""


def test_an_unreadable_heartbeat_is_not_a_kill(tmp_path):
    """No number is UNKNOWN, never 'zero subscriptions'."""
    kills, _ = run_drift(tmp_path, pid_age="4242 6000", subs="not json", desired="20")
    assert kills == ""


def test_a_failed_address_count_is_not_a_kill(tmp_path):
    """An empty count is a failed read, not an empty book."""
    kills, _ = run_drift(tmp_path, pid_age="4242 6000", subs='{"ts": "x", "subs": 2}',
                         desired="")
    assert kills == ""


def test_the_process_match_is_anchored_on_argument_position():
    """A bare substring match over `ps` output is satisfied by this script itself, by the awk that
    reads it, and by any editor with the file open."""
    body = SCRIPT.read_text().split("watcher_pid_age() {")[1].split("\n}")[0]
    assert '$5 == "--duration"' in body and '$6 == "86400"' in body
    assert "pkill" not in SCRIPT.read_text(), "kill by pid; pkill -f kills the shell that names it"


def test_the_real_count_is_the_watchers_desired_set_from_any_cwd(tmp_path):
    """Every test above SUBSTITUTES desired_addresses, so none could see that cron runs it from
    $HOME with a relative DB_PATH (it read an empty file there and the restart never fired from
    09-23 to 09-26). This runs the REAL function from a foreign cwd, through the watcher's own
    --desired-count: verified addresses of profiles armed on solana, nothing else."""
    import sqlite3
    import sys
    from sniper.persistence import db_memecoin as m
    repo = tmp_path / "repo"
    repo.mkdir()
    # The count runs the PINNED script (2026-09-28): .runtime/memecoin/current stands in for it.
    (repo / ".runtime" / "memecoin").mkdir(parents=True)
    (repo / ".runtime" / "memecoin" / "current").symlink_to(ROOT)
    db = sqlite3.connect(repo / "sniper_tracker.db")
    db.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(db)
    for uid, approved, chains in (("a", 1, "solana"), ("b", 1, "solana"), ("c", 0, None),
                                  ("d", 1, "56")):
        db.execute("INSERT INTO memecoin_wallets (source_user_id, handle, real_approved, "
                   "real_chains, paused, excluded) VALUES (?,?,?,?,0,0)", (uid, uid, approved, chains))
    for uid, addr, status in (("a", "A" * 44, "verified"), ("b", "B" * 44, "verified"),
                              ("b", "E" * 44, "candidate"), ("c", "C" * 44, "verified"),
                              ("d", "D" * 44, "verified")):
        m.record_address_evidence(db, source_user_id=uid, network_id="solana", address=addr,
                                  status=status, evidence_kind="t", now_iso="2026-09-27T00:00:00Z",
                                  actor="t")
    db.commit()
    db.close()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "sniper_tracker.db").write_bytes(b"")      # the 0-byte decoy cron's $HOME holds
    source = SCRIPT.read_text()
    func = source[source.index("desired_addresses() {"):source.index("now_s() {")]
    harness = "\n".join([f"REPO={shlex.quote(str(repo))}", f"PY={shlex.quote(sys.executable)}",
                         func, "desired_addresses"])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            cwd=elsewhere, env={"PATH": "/usr/bin:/bin"}, timeout=60)
    assert result.stdout.strip() == "2", (result.stdout, result.stderr)


def test_this_script_no_longer_launches_the_watcher():
    """One supervisor per process (2026-09-28): memecoin_watchdog.sh launches it release-pinned.
    Two supervisors produced two watchers when the EVM watcher had them (2026-09-06)."""
    text = SCRIPT.read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "nohup" not in code and "pgrep" not in code
    assert "roster_drift_restart" in code


def _real_pid_age(tmp_path, ps_lines):
    """The real watcher_pid_age over a substituted `ps`."""
    source = SCRIPT.read_text()
    block = source[source.index('BEAT="$REPO/data/onchain_watch.heartbeat"'):]
    harness = "\n".join([
        f"REPO={shlex.quote(str(tmp_path))}",
        block.replace("roster_drift_restart\n", "", 1),
        "ps() { printf '%s\\n' " + " ".join(shlex.quote(l) for l in ps_lines) + "; }",
        "watcher_pid_age",
    ])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_two_watchers_alive_is_a_release_handover_never_a_drift_kill(tmp_path):
    """During a deploy the old watcher keeps running until the new one acks every owner, and both
    write one heartbeat file. A drift read then mixes the new one's partial `subs` with the OLD
    pid's age, and its kill would end the only subscribed socket. Two watchers: no candidate."""
    old = "111 7200 /py /h/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py --duration 86400"
    new = "222 30 /py /h/.runtime/memecoin/releases/new/scripts/research/onchain_wallet_watch.py --duration 86400"
    assert _real_pid_age(tmp_path, [old]) == "111 7200"
    assert _real_pid_age(tmp_path, [old, new]) == ""
