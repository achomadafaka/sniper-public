import json
import subprocess
import sys


def test_batch_cli_help():
    out = subprocess.run(
        [sys.executable, "scripts/run_winning_wallet_thesis_batch.py", "--help"],
        text=True, capture_output=True, check=True,
    )
    assert "--candidates" in out.stdout
    assert "--max-wallets" in out.stdout


def test_batch_cli_dry_run_counts(tmp_path):
    cand = tmp_path / "c.json"
    cand.write_text('[{"wallet": "0x1"}, {"wallet": "0x2"}]')
    out = subprocess.run(
        [sys.executable, "scripts/run_winning_wallet_thesis_batch.py",
         "--candidates", str(cand)],
        text=True, capture_output=True, check=True,
    )
    counts = json.loads(out.stdout.strip().splitlines()[-1])
    assert counts["done"] == 2 and counts["seen"] == 2
