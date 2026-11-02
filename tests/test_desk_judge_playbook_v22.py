"""Playbook v22 (watch-and-trigger): every answer shape it teaches parses under a standing cohort."""
import importlib.util
import re
from pathlib import Path

from test_desk_watch_v22_contract import STANDING
from test_memecoin_desk_contract import api, BOUNDS, TIMING_CAPS

ROOT = Path(__file__).resolve().parents[1]
V22 = ROOT / "docs" / "superpowers" / "specs" / "2026-10-23-desk-judge-playbook-v22.md"


def _system():
    spec = importlib.util.spec_from_file_location("jw", ROOT / "scripts" / "memecoin_desk_judge_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_playbook(str(V22))


def test_v22_loads_and_teaches_the_new_shape():
    system, version = _system()
    assert version == "desk-judge-v22"
    flat = " ".join(system.split())
    assert '"p":P' in flat and "px_hold" in flat and "px_up" in flat and "## Position plan" in flat
    assert "`bu` (buy USD" not in flat                               # no bar conditions: no executor for them


def test_every_example_answer_parses_under_a_standing_cohort():
    system, _ = _system()
    answers = re.findall(r"Answer: `(\{.*?\})`", system)
    assert answers
    for raw in answers:
        k = re.search(r'"k":"([^"]+)"', raw).group(1)
        out = api().validate_verdict(raw, opportunity_id=k, bounds={**BOUNDS, "tp_pct": (10, 200), "sl_pct": (10, 40)},
                                     timing_caps=TIMING_CAPS, elapsed_ms=1, standing=STANDING, bars_available=True)
        assert out["action"] in ("ENTER", "SKIP") and out["reason"] != "verdict_keys", (raw, out)
