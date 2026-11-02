"""JUDGE playbook v21 (operator 'strengthen the prompts', Codex review): wording only, output contract unchanged."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "docs" / "superpowers" / "specs"
V20, V21 = SPECS / "2026-10-23-desk-judge-playbook-v20.md", SPECS / "2026-10-23-desk-judge-playbook-v21.md"


def _load(path):
    spec = importlib.util.spec_from_file_location("judge_worker", ROOT / "scripts" / "memecoin_desk_judge_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.load_playbook(str(path))


def _section(text, head):
    start = text.index(head)
    return text[start:text.index("\n## ", start + len(head))]


def test_v21_loads_as_its_own_version():
    system, version = _load(V21)
    assert version == "desk-judge-v21"
    assert "## Order of evidence" in system


def test_output_contract_and_reason_codes_are_byte_identical_to_v20():
    old, new = _load(V20)[0], _load(V21)[0]
    assert _section(new, "## Output (speed rules)") == _section(old, "## Output (speed rules)")
    assert _section(new + "\n## end", "## Reason codes") == _section(old + "\n## end", "## Reason codes")


def test_rate_is_a_diagnostic_and_the_unsupported_claim_is_gone():
    system = " ".join(_load(V21)[0].split())
    assert "no worse results" not in system and "second reader" not in system
    assert "never a quota" in system and "do not skip to hold one" in system


def test_holding_false_is_path_dependent_and_null_support_is_not_a_break():
    system = " ".join(_load(V21)[0].split())
    assert "stays false even after a recovery" in system and "never invent the level" in system
    assert "proves neither healthy structure nor a break" in system and "clean run" not in system
    assert "neither good nor bad news" in system and "never good news" not in system
