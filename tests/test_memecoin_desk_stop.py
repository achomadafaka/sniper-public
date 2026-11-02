"""Desk STOP: dry run by default; ordered JUDGE first; exact argv identity; producers only on request;
never the funded runner; prints the .env and cron lines; a fake /proc never signals (own sleep children)."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "memecoin_desk_stop.sh"


def _proc(root, pid, argv):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def _run(tmp_path, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                          env=dict(os.environ, DESK_STOP_PROC_ROOT=str(tmp_path / "proc"), STOP_WAIT_S="1"))


def _table(tmp_path, children):
    rel = "/x/.runtime/memecoin/releases/abc/scripts/"
    judge, assembler, universe, runner, editor = children
    _proc(tmp_path / "proc", judge.pid, ["/usr/bin/python3", rel + "memecoin_desk_judge_worker.py", "--loop"])
    _proc(tmp_path / "proc", assembler.pid, ["/home/u/.pyenv/bin/python3.13", rel + "memecoin_desk_assembler.py"])
    _proc(tmp_path / "proc", universe.pid, ["/usr/bin/python3", rel + "memecoin_desk_universe_worker.py"])
    _proc(tmp_path / "proc", runner.pid, ["/usr/bin/python3", rel + "memecoin_runner_loop.py"])
    _proc(tmp_path / "proc", editor.pid, ["vim", rel + "memecoin_desk_judge_worker.py"])


def _children(n):
    return [subprocess.Popen(["sleep", "30"]) for _ in range(n)]


def test_dry_run_lists_the_order_and_never_the_runner(tmp_path):
    kids = _children(5)
    try:
        _table(tmp_path, kids)
        out = _run(tmp_path).stdout
        assert "dry run" in out
        judge_at, asm_at = out.index("memecoin_desk_judge_worker.py: pid"), out.index("memecoin_desk_assembler.py: pid")
        assert judge_at < asm_at                                                  # JUDGE first
        assert "pid %d" % kids[0].pid in out and "pid %d" % kids[4].pid not in out   # the editor never
        assert "memecoin_runner_loop" not in out and "universe_worker.py: pid" not in out
        assert "MEMECOIN_DESK_JUDGE_ENABLED=0" in out and "memecoin_desk_judge_watchdog.sh" in out
        assert "MEMECOIN_DESK_UNIVERSE_ENABLED=0" not in out
        assert "queued desk intents may still fill for up to 30 s" in out
        assert "runner restart with MEMECOIN_DESK_ENABLED=0" in out
    finally:
        for k in kids:
            k.kill()
            k.wait()


def test_confirm_on_a_fake_table_signals_nothing_and_producers_need_the_flag(tmp_path):
    kids = _children(5)
    try:
        _table(tmp_path, kids)
        out = _run(tmp_path, "--confirm", "--include-producers").stdout
        assert "--confirm" in out and "memecoin_desk_universe_worker.py: pid %d" % kids[2].pid in out
        assert "MEMECOIN_DESK_UNIVERSE_ENABLED=0" in out and "memecoin_desk_x_producer_watchdog.sh" in out
        for k in kids:
            try:
                k.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
            assert k.returncode is None, "a fake process table must never signal"
    finally:
        for k in kids:
            k.kill()
            k.wait()


def test_an_unknown_argument_refuses(tmp_path):
    assert _run(tmp_path, "--yes").returncode == 2


def test_fomo_collector_is_listed_in_dry_run_with_cron_only_off_switch(tmp_path):
    _proc(tmp_path / "proc", 987654321,
          ["/usr/bin/python3", "/release/scripts/memecoin_desk_fomo_intake.py", "--headed", "--loop"])
    result = _run(tmp_path)
    assert result.returncode == 0
    assert "memecoin_desk_fomo_intake.py: pid 987654321" in result.stdout
    assert "memecoin_desk_fomo_intake_watchdog.sh" in result.stdout
    assert "FOMO_INTAKE_ENABLED" not in result.stdout
