"""Chain 4663 is back in the funded on-chain watcher loop (2026-09-28, operator order).

Retired on 2026-09-23 because no 4663 address was verified. On 2026-09-28 the J7 proof run wrote
47 axiom_confirmed 4663 addresses, and the operator ordered the Robinhood fast lane back on.

The 4663 command line carries no `--chain`, so it is a PREFIX of every other chain's command line.
The launch, duplicate-stop and upgrade paths filtered `--chain` lines out for 4663; the
degraded-heartbeat kill did not. The 4663 heartbeat file was last written on 2026-09-16, so the
first tick after the revival read it as stale and would have killed every OTHER chain's watcher
(the 5042 observe-only one today) instead of the 4663 one, which was not running.

These tests run the real upkeep text out of the script with process and probe substitutes.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "memecoin_watchdog.sh"
WATCH = "scripts/research/onchain_evm_wallet_watch.py --duration 86400"


FOREIGN = "/home/other/Sniper/.runtime/memecoin/releases/live9"


def _upkeep(tmp_path, running, *, foreign=(), unpinned=()):
    """Run peripheral_upkeep once with 4663 funded + 5042 observe-only. `running` maps pid to the
    arguments after the script path. Pids in `foreign` run from ANOTHER state root's release (the
    live watchers beside a test), pids in `unpinned` from a source tree. Returns (killed pids,
    launch lines)."""
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    # The real 4663 heartbeat path (no chain suffix), stale: last written 2026-09-16.
    (state / "data" / "onchain_evm_watch.heartbeat").write_text('{"ts": "2026-09-16T09:27:47Z"}')
    # This root's own release: stale and degraded rules only touch $STATE_ROOT's releases.
    root = state / ".runtime/memecoin/releases/current"
    kills, launches, alerts = tmp_path / "kills", tmp_path / "launches", tmp_path / "alerts"
    for f in (kills, launches, alerts):
        f.write_text("")
    rows = "".join("%d /usr/bin/python %s%s%s\\n" % (
        pid, "" if pid in unpinned else f"{FOREIGN if pid in foreign else root}/", WATCH, tail)
        for pid, tail in running.items())
    text = SCRIPT.read_text()
    upkeep = text[text.index("log() {"):text.index("\n_UPKEEP_DONE=0")] + "\nperipheral_upkeep\nwait\n"
    harness = "\n".join([
        "set -u",
        f"ROOT={shlex.quote(str(root))}",
        f"STATE_ROOT={shlex.quote(str(state))}",
        f"KILLS={shlex.quote(str(kills))}; LAUNCHES={shlex.quote(str(launches))}",
        f"ALERTS={shlex.quote(str(alerts))}",
        'WATCHDOG_LOG="$STATE_ROOT/logs/watchdog.log"',
        'PY=fake_python; DB_PATH=""; FOMO_TOKEN_STORE=""',
        'fake_python() {',
        '  if [ "$2" = "--chain" ]; then printf "{\\"status\\":\\"eligible\\"}\\n"; return 0; fi',
        '  case "$2" in',
        '    *send_telegram*) printf "%s\\n" "$3" >> "$ALERTS" ;;',
        '    *coverage_low_since*) printf "beat_stale:1000000\\n" ;;',
        '    *MEMECOIN_EVM_ALLOWLIST*) printf "4663\\n" ;;',
        '    *"if not cfg"*) : ;;',
        '    *MEMECOIN_EVM_OBSERVE_CHAINS*) printf "5042\\n" ;;',
        '  esac; }',
        f'ps() {{ case "$*" in *pid=*) printf "{rows}" ;;',
        f'  *) printf "{rows}" | cut -d" " -f2- ;; esac; }}',
        'kill() { printf "%s\\n" "$*" >> "$KILLS"; }',
        'sleep() { :; }',
        'setsid() { printf "%s\\n" "$*" >> "$LAUNCHES"; }',
        upkeep,
    ])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "command not found" not in result.stderr, result.stderr
    return kills.read_text().split(), launches.read_text().splitlines()


def test_an_eligible_4663_is_launched_without_a_chain_argument(tmp_path):
    _killed, launches = _upkeep(tmp_path, {201: " --chain 5042 --observe-only"})
    fours = [l for l in launches if WATCH.split()[0] in l and "--chain" not in l]
    assert len(fours) == 1, launches


def test_a_stale_4663_heartbeat_never_kills_another_chains_watcher(tmp_path):
    """The prefix collision: before the fix this killed 201 (5042) and 202 (8453)."""
    killed, _ = _upkeep(tmp_path, {201: " --chain 5042 --observe-only", 202: " --chain 8453"})
    assert killed == []


def test_a_stale_4663_heartbeat_kills_only_the_4663_watcher(tmp_path):
    killed, launches = _upkeep(tmp_path, {201: " --chain 5042 --observe-only", 202: " --chain 8453",
                                          203: ""})
    assert killed == ["203"]


def test_a_stale_beat_never_kills_a_live_watcher_of_another_state_root(tmp_path):
    """Gamma H2 (2026-09-29): the beat read is this root's, so a whole-script test whose tmp root
    holds a stale beat must not kill the live watcher of the real root beside it."""
    killed, _ = _upkeep(tmp_path, {203: "", 204: ""}, foreign={204})
    assert killed == ["203"]


def test_another_roots_pinned_watcher_does_not_license_stopping_an_unpinned_one(tmp_path):
    """Gamma M (2026-09-29): "pinned beside unpinned" means THIS root's pinned watcher."""
    killed, _ = _upkeep(tmp_path, {204: "", 205: ""}, foreign={204}, unpinned={205})
    assert killed == []
