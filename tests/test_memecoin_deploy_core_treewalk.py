"""Every test that reads the whole tree runs on every deploy (2026-10-15, Gamma's full run).

The deploy gate runs its core list, the changed tests and the tests that import a changed module. A
test that walks tests/, sniper/ or scripts/ (rglob, os.walk, glob, walk_packages), or asserts a
property of every file without naming one, can break on a change anywhere, and neither rule ever
selects it. Those tests are in MEMECOIN_CORE_TESTS; this file keeps the list complete."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_memecoin_runtime.sh"
WALKS = re.compile(r"\.rglob\(|os\.walk\(|walk_packages\(|\bglob\.glob\(|\.glob\(")
# Tests that check every file of a kind without walking for it by one of the calls above.
NO_FILE_NAMED = ("tests/test_source_guard_helper.py", "tests/test_tests_refuse_real_dns.py",
                 "tests/test_wallet_map_readability.py")


def _core():
    """MEMECOIN_CORE_TESTS as bash itself expands it."""
    text = SCRIPT.read_text()
    start = text.index("MEMECOIN_CORE_TESTS=(")
    body = text[start:text.index(")\n", start) + 1]
    out = subprocess.run(["bash", "-c", body + '\nprintf "%s\\n" "${MEMECOIN_CORE_TESTS[@]}"'],
                         capture_output=True, text=True, check=True).stdout
    return out.split()


def test_every_core_entry_is_a_test_file_in_the_tree():
    core = _core()
    assert len(core) == len(set(core))
    for path in core:
        assert re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", path), path
        assert (ROOT / path).is_file(), path


def test_every_tree_walking_test_is_in_the_core():
    core = set(_core())
    walking = {"tests/" + p.name for p in sorted((ROOT / "tests").glob("test_*.py"))
               if WALKS.search(p.read_text(errors="replace"))}
    assert walking - core == set()
    assert set(NO_FILE_NAMED) <= core


def test_the_unscoped_fallback_runs_the_core_too():
    text = SCRIPT.read_text()
    assert ("find tests -maxdepth 1 -name 'test_memecoin*.py' -print;\n"
            "      printf '%s\\n' \"${MEMECOIN_CORE_TESTS[@]}\"; } | sort -u)") in text
