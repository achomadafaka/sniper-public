from pathlib import Path


def test_standalone_watchdog_identity_is_exact():
    source = Path("scripts/hl_altshort_signal_watchdog.sh").read_text(encoding="utf-8")
    manifest = next(line for line in source.splitlines() if line.startswith("ARM_MANIFEST="))
    match = next(line for line in source.splitlines() if line.startswith("MATCH="))
    launch = next(line for line in source.splitlines() if line.startswith("LAUNCH="))
    assert "standalone_forward_arm.v3.json" in manifest
    for required in (
        "run_hl_altshort_signal_paper.py", "--db-path", "$DB",
        "--arm-manifest", "$ARM_MANIFEST",
    ):
        assert required in match
        assert required in launch
