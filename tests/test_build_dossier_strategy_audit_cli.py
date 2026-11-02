"""Offline CLI tests for cross-wallet dossier strategy audit."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_runs_bare_without_pythonpath():
    """Regression: this is a CLI (`python3 scripts/build_dossier_strategy_audit.py ...`); it must
    self-bootstrap sys.path so `from sniper...` (a module-top import, above argparse) resolves when
    run bare. It did not -> ModuleNotFoundError before --help could even print. Run it the bare way
    (clean env, no PYTHONPATH); --help must reach argparse and exit 0, no import crash."""
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "build_dossier_strategy_audit.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, str(script), "--help"], cwd=str(root), env=env,
                       capture_output=True, text=True, timeout=60)
    assert "ModuleNotFoundError" not in r.stderr, r.stderr
    assert r.returncode == 0, r.stderr


def _write_export(root: Path, wallet: str, *, with_thesis: bool = True) -> Path:
    export_dir = root / wallet
    export_dir.mkdir()
    grain_key = {
        "category": "sports",
        "market_type": "binary",
        "event_family": "sports_head_to_head",
    }
    tables = {
        "dossier_meta": [
            {"key": "wallet", "value": wallet},
            {"key": "schema_version", "value": "v4_thesis_lifecycle"},
        ],
        "result_semantics": [
            {"grain_id": "g1", "grain_key": json.dumps(grain_key, sort_keys=True)}
        ],
        "thesis_candidates": [
            {
                "thesis_id": f"{wallet}-t1",
                "grain_id": "g1",
                "mechanism": "prediction_driven",
                "status": "candidate",
                "thesis_text": "Candidate evidence",
                "supporting_metrics": json.dumps({}),
                "required_test_data": json.dumps(["price_history"]),
                "simulation_plan": json.dumps({}),
                "source_schema_version": "v4_thesis_lifecycle",
            }
        ]
        if with_thesis
        else [],
        "mechanism_evidence": [],
        "data_gaps": [],
    }
    for name, rows in tables.items():
        (export_dir / f"{name}.json").write_text(
            json.dumps(rows, sort_keys=True),
            encoding="utf-8",
        )
    return export_dir


def test_build_dossier_strategy_audit_cli_writes_audit_json(tmp_path: Path) -> None:
    from scripts.build_dossier_strategy_audit import main

    d1 = _write_export(tmp_path, "0x1")
    d2 = _write_export(tmp_path, "0x2")
    out = tmp_path / "audit.json"

    rc = main(["--dossier-json", str(d1), "--dossier-json", str(d2), "--out-json", str(out)])

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["surface_version"] == "strategy_pattern_audit_dossier_v1"
    assert len(payload["theses"]) == 1


def test_build_dossier_strategy_audit_cli_writes_validation_json(tmp_path: Path) -> None:
    from scripts.build_dossier_strategy_audit import main

    d1 = _write_export(tmp_path, "0x1")
    d2 = _write_export(tmp_path, "0x2")
    audit_out = tmp_path / "audit.json"
    workflow_out = tmp_path / "workflow.json"

    rc = main([
        "--dossier-json", str(d1),
        "--dossier-json", str(d2),
        "--out-json", str(audit_out),
        "--out-validation-json", str(workflow_out),
        "--dataset-inventory", "price_history",
    ])

    assert rc == 0
    payload = json.loads(workflow_out.read_text(encoding="utf-8"))
    assert payload["surface_version"] == "strategy_thesis_validation_workflow_v1"
    assert payload["validation_plans"][0]["status"] == "simulation_ready"
