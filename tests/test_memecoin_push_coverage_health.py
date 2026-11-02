"""A push socket that reports itself healthy while delivering a fraction of the chain.

MEASURED on 4663, 2026-09-06: a fresh socket delivered 93% of the chain's blocks (590 of 632/min at
0.095s blocks), decayed to 46% within about 35 minutes, and after a restart to 30% within ten.
RECONNECTS STAYED AT ZERO through all three. The connection was perfectly healthy by every
connection-level measure while under a third of the chain was arriving.

COVERAGE IS OUR DELIVERY RATE OVER THE CHAIN'S OWN RATE, both derived from the block numbers and
TIMESTAMPS already in the `newHeads` payloads plus our own clock. No request, and the reference is
the chain's block spacing rather than anything we remember about ourselves.

THESE TESTS CONSTRUCT DELIVERY IN WALL TIME, AND THAT IS THE POINT. The first version of this file
built windows out of block numbers alone, which CANNOT EXPRESS A PAUSE — so every test passed while
the metric read 1.00 on a socket delivering 12% of the chain. A test that cannot represent the
failure mode will always pass, and the arithmetic was downstream of that.

Offline. No sockets, no network.
"""
import pytest

from sniper.services import memecoin_onchain_push as push

BLOCK_S = 0.095                              # 4663: 632 blocks per minute


def _deliver(*, delivered_per_min, block_s=BLOCK_S, every_nth=1, n=600, start=1000):
    """A window where the chain runs at `block_s` and we receive `delivered_per_min` heads.

    `every_nth` models SKIPPING (we get one block in n, promptly). Lowering `delivered_per_min`
    below the chain's rate models PAUSING (we get contiguous blocks, late). The old block-span
    metric could only see the first.
    """
    s = push.PushState()
    gap = 60.0 / float(delivered_per_min)
    for i in range(n):
        block = start + i * every_nth
        s.head(9000.0 + i * gap, block, int(5000 + block * block_s))
    return s


CHAIN_PER_MIN = 60.0 / BLOCK_S               # 631.6


# --- the metric -----------------------------------------------------------------------------------

def test_receiving_every_block_promptly_is_full_coverage():
    """The tolerance is 0.03, not 0.001, and the reason is real rather than sloppy: block timestamps
    are whole SECONDS, so the chain rate derived from them is quantised. At 0.095s blocks that is a
    couple of percent of noise on the reference. It is far below any threshold that matters, but a
    reader comparing coverage numbers between windows should not expect them to agree exactly."""
    assert _deliver(delivered_per_min=CHAIN_PER_MIN).coverage() == pytest.approx(1.0, abs=0.03)


def test_skipping_blocks_is_caught():
    """The only mode the old block-span metric could see."""
    assert _deliver(delivered_per_min=CHAIN_PER_MIN / 8, every_nth=8).coverage() \
        == pytest.approx(0.125, abs=0.01)


@pytest.mark.parametrize("per_min,want", [(293.0, 0.46), (191.0, 0.30), (78.0, 0.12)])
def test_a_pause_is_caught_and_this_is_what_the_old_metric_missed(per_min, want):
    """CONTIGUOUS BLOCKS, DELIVERED LATE. The old metric read 1.00 for every one of these — 293 and
    191/min were the 2026-09-06 degradations it was built for, and 78/min was live in front of us
    while it reported perfect health."""
    assert _deliver(delivered_per_min=per_min).coverage() == pytest.approx(want, abs=0.02)


def test_the_measured_degradations_land_where_the_floor_separates_them():
    """The three live 4663 readings, as fractions of the chain's 632 blocks/min.

    THE MARGINS ARE NOT SYMMETRIC, which is why the floor is 0.6 and not the 0.5 that sits in the
    observed gap. At 0.5 the milder degradation clears by only 3.6 points and a socket at 55% would
    go uncaught — and looking healthy is the whole failure mode.
    """
    assert _deliver(delivered_per_min=590.0).coverage() > push._COVERAGE_FLOOR
    for per_min in (293.0, 191.0, 78.0):
        assert _deliver(delivered_per_min=per_min).coverage() < push._COVERAGE_FLOOR


@pytest.mark.parametrize("payload,want", [
    ({"number": "0x3097710"}, 50951952), ({"number": None}, None),
    ({}, None), (None, None), ({"number": "nothex"}, None), ("not-a-dict", None),
])
def test_the_block_number_is_read_defensively(payload, want):
    assert push.block_number_of(payload) == want


# --- an unknown is never unhealthy ------------------------------------------------------------------

def test_too_small_a_sample_is_unknown_not_unhealthy():
    """UNKNOWN, NEVER UNHEALTHY. A confident percentage from four blocks is worse than silence."""
    assert push.PushState().coverage() is None
    assert _deliver(delivered_per_min=CHAIN_PER_MIN, n=2).coverage() is None
    assert _deliver(delivered_per_min=CHAIN_PER_MIN,
                    n=push._COVERAGE_MIN_SPAN - 1).coverage() is None
    assert _deliver(delivered_per_min=CHAIN_PER_MIN,
                    n=push._COVERAGE_MIN_SPAN * 6).coverage() is not None


def test_a_fast_chain_needs_enough_BLOCK_TIME_not_just_enough_blocks():
    """Block timestamps are whole SECONDS and 4663 makes ~10 blocks a second, so a window can hold
    plenty of blocks and still span too little block time to derive a rate from. Without this the
    divisor is zero or noise and coverage would be a wild number rather than an honest unknown."""
    # 200 blocks at 0.095s spans 19 seconds of block time — over the block floor, under the time one
    assert _deliver(delivered_per_min=CHAIN_PER_MIN, n=200).coverage() is None
    assert _deliver(delivered_per_min=CHAIN_PER_MIN, n=600).coverage() is not None


def test_a_head_missing_either_field_cannot_enter_the_window():
    """Coverage needs both the number and the timestamp; a header with one is still a head."""
    s = push.PushState()
    s.head(0.0, 1000, None)
    s.head(0.0, None, 5000)
    assert s.heads == 2 and s.window == [] and s.coverage() is None


def test_the_window_is_bounded():
    s = _deliver(delivered_per_min=CHAIN_PER_MIN, n=push._COVERAGE_WINDOW_HEADS * 3)
    assert len(s.window) <= push._COVERAGE_WINDOW_HEADS


# --- the sustained-low stamp -------------------------------------------------------------------------

def test_healthy_coverage_leaves_no_low_stamp():
    assert push.coverage_low_since(None, 0.93, 100.0, None) is None


def test_the_stamp_is_set_once_and_held_while_low():
    first = push.coverage_low_since(None, 0.30, 100.0, None)
    assert first == 100.0
    assert push.coverage_low_since(None, 0.20, 500.0, first) == 100.0, \
        "the stamp marks when it STARTED, so the consumer can ask how long"


def test_recovery_clears_the_stamp():
    assert push.coverage_low_since(None, 0.93, 500.0, 100.0) is None


def test_an_unknown_clears_the_stamp_rather_than_accumulating():
    """UNKNOWN IS NOT UNHEALTHY. A restart empties the window, and a fresh process must not inherit
    a kill from a sample it has not taken."""
    assert push.coverage_low_since(None, None, 500.0, 100.0) is None


def test_exactly_at_the_floor_is_healthy():
    assert push.coverage_low_since(None, push._COVERAGE_FLOOR, 100.0, None) is None


# --- the watchdog rule --------------------------------------------------------------------------------

def test_the_watchdog_kills_only_on_a_sustained_low_and_respects_a_cooldown():
    from pathlib import Path

    text = Path("scripts/memecoin_watchdog.sh").read_text()
    assert "kill_degraded_onchain_watcher" in text
    assert "coverage_low_since" in text                      # reads the stamp, not a raw number
    assert 'MEMECOIN_PUSH_COVERAGE_LOW_S:-120' in text       # sustained
    assert 'MEMECOIN_PUSH_KILL_COOLDOWN_S:-900' in text      # cooldown
    assert "lastkill" in text                                # the cooldown marker is persisted
    # UNKNOWN IS NOT UNHEALTHY: an unreadable heartbeat yields an EMPTY reason and cannot kill.
    assert '[ -n "$reason" ] || return 0' in text
    # the kill must run BEFORE the relaunch, or the pass that kills leaves nothing running
    assert text.index('kill_degraded_onchain_watcher "$_chain"') < \
        text.index('ensure_onchain_evm_watcher "$_chain"')


def test_the_push_lane_starts_from_proven_addresses_and_never_asks_fomo():
    """The old bug: the address sync INSERTed behind a query_only handle, so the lane watched
    nobody 2,524 times. The book is now a table read, and an empty one refuses to start."""
    from pathlib import Path

    text = Path("scripts/research/onchain_evm_wallet_watch.py").read_text()
    push_lane = text.split("def run_push(")[1]
    assert "_require_addresses(wconn)" in push_lane           # proven set, before subscribing
    # refreshes are local reads on the watcher's own read-only roster handle (2026-09-29)
    assert "_verified_addresses(roster_conn[0])" in push_lane
    assert "get_readonly_connection(str(DB))" in text.split("def _roster_connection(")[1].split("\ndef ")[0]
    assert "get_readonly_connection()" not in push_lane
    assert "sync_addresses" not in text and "get_profile" not in text


# --- a watcher from a previous release must not survive a deploy ------------------------------------

def test_the_watchdog_stops_a_watcher_from_a_previous_release():
    """DEPLOYED AND INERT (2026-09-06). The liveness match is release-INDEPENDENT on purpose — it
    must recognise a watcher the PREVIOUS release started, or every deploy launches another one,
    which is the seven-watcher leak of 2026-09-02. The same property means an old watcher satisfies
    the check forever: after the coverage fix shipped, the running process was still on 015a2705
    with no `coverage` key in its heartbeat, and it had to be killed by hand.

    Liveness stays release-independent; UPGRADE is a separate question answered against `$ROOT`.
    """
    from pathlib import Path

    text = Path("scripts/memecoin_watchdog.sh").read_text()
    assert "upgrade_onchain_evm_watcher" in text
    assert 'grep -v -F -- "$ROOT/"' in text, "stale means 'not THIS release', matched on $ROOT"
    # order matters: stop the old one, then the degraded check, then the relaunch — a relaunch
    # before the stop would leave the old process owning the socket for another pass.
    assert text.index('upgrade_onchain_evm_watcher "$_chain"') < \
        text.index('kill_degraded_onchain_watcher "$_chain"') < \
        text.index('ensure_onchain_evm_watcher "$_chain"')
    # and the release-independent LIVENESS match is still there, unweakened
    assert '/.runtime/memecoin/releases/' in text
    assert 'ONCHAIN_EVM_MATCH="$ROOT/' not in text


def test_the_upgrade_check_does_not_confuse_chains():
    """4663 carries no `--chain`, so its needle is a PREFIX of every other chain's command line —
    the same collision the liveness check has to defend against."""
    from pathlib import Path

    text = Path("scripts/memecoin_watchdog.sh").read_text()
    upgrade = text.split("upgrade_onchain_evm_watcher() {")[1].split("\n}")[0]
    assert 'grep -v -- "--chain"' in upgrade


def test_the_upgrade_never_stops_another_state_roots_watcher(tmp_path):
    """2026-09-29: "stale" is an older release of THIS state root only. A tmp STATE_ROOT (a test
    running the whole script) must never stop the live watcher of the real one."""
    import shlex
    import subprocess

    text = _watchdog()
    tree = text[text.index("own_release_tree() {"):text.index("solana_watchers() {")]
    upgrade = text[text.index("upgrade_onchain_evm_watcher() {"):]
    upgrade = upgrade[:upgrade.index("\n}\n") + 3]
    watch = "scripts/research/onchain_evm_wallet_watch.py --duration 86400 --chain 56"
    ours = tmp_path / ".runtime/memecoin/releases"
    rows = [f"11 /py {ours}/new1/{watch}", f"22 /py {ours}/old2/{watch}",
            f"33 /py /home/other/.runtime/memecoin/releases/live3/{watch}"]
    harness = "\n".join([
        "set -u", f"STATE_ROOT={shlex.quote(str(tmp_path))}", f"ROOT={shlex.quote(str(ours / 'new1'))}",
        'ONCHAIN_EVM_SCRIPT="scripts/research/onchain_evm_wallet_watch.py"',
        "ps() { printf '%s\\n' " + " ".join(shlex.quote(r) for r in rows) + "; }",
        'kill() { echo "kill $*"; }', "log() { :; }", "sleep() { :; }", tree, upgrade,
        "upgrade_onchain_evm_watcher 56"])
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}, timeout=10)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["kill", "22"], out.stdout


# --- one supervisor per process, and a heartbeat that stopped advancing is dead ---------------------

def _watchdog():
    from pathlib import Path
    return Path("scripts/memecoin_watchdog.sh").read_text()


def test_the_research_cron_no_longer_launches_the_evm_watcher():
    """TWO SUPERVISORS FOR ONE PROCESS (2026-09-06). `onchain_armed_watchdog.sh` launched the EVM
    watcher from the SOURCE TREE while `memecoin_watchdog.sh` launched it release-pinned. Its guard
    normally found the pinned process and skipped, but in a window when nothing was running it
    started an unpinned one — and the pinned watchdog then correctly refused to count a source-tree
    watcher as alive and started one beside it. Two writers, one heartbeat file: the push process
    wrote `coverage` and the poll process overwrote it with a beat that has no such key.

    The Solana watcher followed on 2026-09-28: memecoin_watchdog.sh owns it release-pinned too.
    """
    from pathlib import Path

    research = Path("scripts/research/onchain_armed_watchdog.sh").read_text()
    assert "onchain_evm_wallet_watch.py" not in research
    assert "ensure_onchain_solana_watcher" in Path("scripts/memecoin_watchdog.sh").read_text(), \
        "Solana must still be supervised"
    assert "memecoin_watchdog.sh" in research, "say WHERE the EVM watcher is owned instead"


def test_an_unpinned_watcher_is_stopped_only_beside_a_pinned_one():
    """Narrow on purpose: that pair IS the broken state. With no pinned owner a source-tree watcher
    is somebody running one deliberately, and killing it would be rude rather than corrective."""
    text = _watchdog()
    body = text.split("stop_unpinned_onchain_watcher() {")[1].split("\n}")[0]

    assert '[ -n "$pinned" ] || return 0' in body, "no pinned owner -> do nothing"
    assert '[ -n "$unpinned" ] || return 0' in body
    assert 'grep -v -- "--chain"' in body, "4663's needle is a prefix of every other chain's"
    assert "alert " in body
    # it must run before the relaunch, or the duplicate survives another pass
    assert text.index('stop_unpinned_onchain_watcher "$_chain"') < \
        text.index('ensure_onchain_evm_watcher "$_chain"')


def test_a_heartbeat_that_stopped_advancing_counts_as_dead():
    """COVERAGE CANNOT ANSWER THIS. It is None when no heads arrive, and None is UNKNOWN — so a lane
    delivering nothing is silent by design. Measured 2026-09-06: the socket refused `eth_subscribe`
    episodically, the backoff parked the lane on a poll fallback whose getLogs cannot work on 4663,
    and the heartbeat sat over three minutes stale while the process ran, with nothing saying so."""
    text = _watchdog()

    assert 'MEMECOIN_WATCHER_HEARTBEAT_MAX_AGE_S:-180' in text
    assert "beat_stale:" in text
    assert "coverage_low:" in text
    # 180s must exceed the push lane's fallback-to-poll transition so a healthy handover cannot trip
    from sniper.settings import memecoin as cfg
    assert 180 > cfg.MEMECOIN_EVM_WS_FALLBACK_S


def test_both_kill_reasons_share_one_cooldown_and_one_pid_match():
    """Two reasons, one path — so the cooldown and the process matching cannot diverge between
    them, and a flapping lane cannot be killed twice as often as a degraded one."""
    text = _watchdog()
    body = text.split("kill_degraded_onchain_watcher() {")[1].split("\n}")[0]

    assert body.count("marker") >= 3                    # cooldown marker: path, read, write
    assert body.count("kill $pids") == 1
    assert 'reason="$(' in body and '[ -n "$reason" ] || return 0' in body


def test_an_unreadable_heartbeat_is_not_a_kill():
    """Unreadable is the absent-watcher check's business, not this one's."""
    body = _watchdog().split("kill_degraded_onchain_watcher() {")[1].split("\n}")[0]
    assert "except Exception:" in body and "print('')" in body


# --- two kinds of no, two ladders --------------------------------------------------------------------

def test_the_two_refusal_ladders_are_separate():
    """A compromise number would be wrong for both. A quota refusal returns the same answer for
    HOURS and every retry spends the quota it is waiting on; an episodic `-32603` clears on the next
    attempt and costs nothing on a keyless endpoint, so ten minutes of backoff buys ten minutes of a
    dark lane and saves nothing."""
    policy, transport = [], []
    d = 0.0
    for _ in range(6):
        d = push.next_refusal_backoff(d)
        policy.append(int(d))
    d = 0.0
    for _ in range(6):
        d = push.next_refusal_backoff(d, transport=True)
        transport.append(int(d))

    assert policy == [60, 120, 240, 480, 600, 600]
    assert transport == [5, 10, 20, 30, 30, 30]


@pytest.mark.parametrize("text,want", [
    ("-32603: Internal error", True),
    ("-32000: server error", True),
    ("-32003: daily request limit reached", False),
    ("403: Archive requests require a personal token", False),
    ("over quota", False),
    ("unauthorized", False),
    ("", False),
    (None, False),
])
def test_the_refusal_classifier(text, want):
    assert push.refusal_is_transport(text) is want


def test_policy_wins_a_tie():
    """A message naming a limit or a key is policy even with a transport-shaped code. Retrying fast
    against a quota is the failure that costs most — it spends the thing it is waiting for — so the
    ambiguous case must land on the slow ladder."""
    assert push.refusal_is_transport("-32603: Internal error: over quota") is False
    assert push.refusal_is_transport("-32603: rate limit exceeded") is False


def test_an_unknown_refusal_takes_the_slow_ladder():
    """Fail-closed: a refusal we cannot classify is treated as policy, which wastes time rather than
    quota."""
    assert push.refusal_is_transport("something we have never seen") is False


def test_the_delay_does_not_carry_across_a_change_of_class():
    """A transport blip after a quota refusal must not inherit the quota's ten-minute wait, and a
    quota refusal after a blip must not inherit the blip's five seconds."""
    import inspect

    src = inspect.getsource(push.run_push_stream)
    assert "last_refusal_transport" in src
    assert "refusal_delay = 0.0" in src.split("transport = refusal_is_transport")[1]


def test_the_log_line_names_which_ladder_it_took():
    import inspect

    src = inspect.getsource(push.run_push_stream)
    assert '"transport" if transport else "policy"' in src


def test_the_code_default_floor_is_the_deployed_point_six(monkeypatch):
    """The .env sets 0.6 (operator, 2026-09-06); the code default must match so a fresh deployment
    does not start looser. At 0.5 a socket delivering 55% of the chain would pass as healthy."""
    import importlib
    monkeypatch.delenv("MEMECOIN_PUSH_COVERAGE_FLOOR", raising=False)
    fresh = importlib.reload(push)
    try:
        assert fresh._COVERAGE_FLOOR == 0.6
        assert _deliver(delivered_per_min=0.55 * 632.0).coverage() < fresh._COVERAGE_FLOOR
    finally:
        importlib.reload(push)
