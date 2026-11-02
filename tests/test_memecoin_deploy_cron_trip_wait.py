"""A cron-handoff deploy trips the entry gate just before the cron tick (2026-09-28, Alpha).

The 06:36/06:42/06:47Z deploys each stood entries down about 46-52 s: the trip landed early in the
minute, the launch was handed to cron about 4 s later, and cron fires only at the next minute.
The deploy now waits, BEFORE the trip, until the second of the minute is in [48, 52].
"""
import subprocess
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "deploy_memecoin_runtime.sh").read_text()
FUNC = SCRIPT[SCRIPT.index("# CRON_TRIP_WAIT_BEGIN"):SCRIPT.index("# CRON_TRIP_WAIT_END")]


def _waits(seconds):
    body = FUNC + "\n" + "\n".join('cron_trip_wait_s "%s"' % s for s in seconds)
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", body], capture_output=True, text=True,
                         check=True).stdout.split()
    return [int(x) for x in out]


def test_every_second_of_the_minute_trips_inside_the_window():
    seconds = ["%02d" % s for s in range(60)]           # date +%S pads: "08" must not read as octal
    for s, wait in zip(range(60), _waits(seconds)):
        assert 0 <= wait <= 59, (s, wait)
        assert 48 <= (s + wait) % 60 <= 52, (s, wait)
        assert (wait == 0) == (48 <= s <= 52), (s, wait)


def test_the_wait_is_before_the_trip_and_only_on_the_cron_handoff_path():
    gate = SCRIPT.index('if [ "$LAUNCH_VIA_CRON" -eq 1 ] && [ "${SNIPER_DEPLOY_NO_PROCESS:-0}" != "1" ]; then\n  TRIP_SECOND=')
    sleep = SCRIPT.index('sleep "$TRIP_WAIT_S"')
    trip = SCRIPT.index('step "entry_gate_trip"\n')
    kill = SCRIPT.index("trip_kill_switch(source='runtime-deploy'")
    assert gate < sleep < trip < kill
    assert SCRIPT.count('sleep "$TRIP_WAIT_S"') == 1
    assert 'step "cron_aligned_trip_wait second=$TRIP_SECOND wait_s=$TRIP_WAIT_S"' in SCRIPT
