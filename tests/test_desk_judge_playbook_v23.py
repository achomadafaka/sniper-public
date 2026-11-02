"""Playbook v23: every example parses under a forecast cohort; the grid it teaches is the contract's grid."""
import importlib.util
import re
from pathlib import Path

from sniper.services import memecoin_desk_contract as C
from test_desk_watch_v22_contract import STANDING
from test_memecoin_desk_contract import api, BOUNDS, TIMING_CAPS

ROOT = Path(__file__).resolve().parents[1]
V23 = ROOT / "docs" / "superpowers" / "specs" / "2026-10-23-desk-judge-playbook-v23.md"


def _system():
    spec = importlib.util.spec_from_file_location("jw23", ROOT / "scripts" / "memecoin_desk_judge_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_playbook(str(V23))


def test_v23_loads_with_its_version():
    system, version = _system()
    assert version == "desk-judge-v23"
    flat = " ".join(system.split())
    assert '"p":P,"f":F}' in flat and "## Re-entry" in flat and "structure.qualify" in flat


def test_taught_grid_is_the_contract_grid():
    flat = " ".join(_system()[0].split())
    for values in (C.FORECAST_U, C.FORECAST_D, C.FORECAST_H):
        assert ", ".join(map(str, values)) in flat
    assert all("`%s`" % s in flat for s in C.FORECAST_SCENARIOS)


def test_every_example_answer_parses_with_its_forecast():
    answers = re.findall(r"Answer: `(\{.*?\})`", _system()[0])
    assert len(answers) == 3
    for raw in answers:
        k = re.search(r'"k":"([^"]+)"', raw).group(1)
        out = api().validate_verdict(raw, opportunity_id=k, bounds={**BOUNDS, "tp_pct": (10, 200), "sl_pct": (10, 40)},
                                     timing_caps=TIMING_CAPS, elapsed_ms=1, standing=STANDING, bars_available=True,
                                     forecast=True)
        assert out["action"] in ("ENTER", "SKIP") and out["reason"] not in ("verdict_keys", "forecast_out_of_range"), (raw, out)
        assert out["forecast"]["u"] in C.FORECAST_U
