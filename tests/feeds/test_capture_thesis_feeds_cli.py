import json
import subprocess
import sys


def test_capture_thesis_feeds_help():
    out = subprocess.run(
        [sys.executable, "scripts/capture_thesis_feeds.py", "--help"],
        text=True, capture_output=True, check=True,
    )
    assert "--source" in out.stdout
    assert "--out" in out.stdout


def test_capture_thesis_feeds_dry_run_emits_plan():
    out = subprocess.run(
        [sys.executable, "scripts/capture_thesis_feeds.py", "--source", "clob_ws"],
        text=True, capture_output=True, check=True,
    )
    plan = json.loads(out.stdout.strip().splitlines()[-1])
    assert plan["source"] == "clob_ws" and plan["dry_run"] is True
