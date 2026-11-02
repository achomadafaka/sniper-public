"""The Solana outcome join reads only the frozen feature file and refuses any other bytes (A2.6)."""
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "research"))
spec = importlib.util.spec_from_file_location("sgsc", ROOT / "scripts" / "research" / "setup_grading_solana_chart.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


def test_the_frozen_file_is_read_and_matches_its_sha():
    rows = G.frozen_rows()
    assert len(rows) == 856 and hashlib.sha256(G.FROZEN.read_bytes()).hexdigest() == G.FROZEN_SHA256


def test_a_changed_file_is_refused(tmp_path):
    bad = tmp_path / "f.jsonl"
    bad.write_bytes(G.FROZEN.read_bytes() + b"\n")
    with pytest.raises(SystemExit, match="REFUSE"):
        G.frozen_rows(bad)


def test_bh_q_values():
    # sorted p 0.01, 0.03, 0.04, 0.5 (m = 4): q = 0.04, 0.0533, 0.0533, 0.5 by the step-up minimum
    assert G.bh_q([0.01, 0.04, 0.03, 0.5]) == pytest.approx([0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.5])
