# tests/test_brain_graph_import_entities.py
import subprocess
from pathlib import Path

from bot.brain_graph import import_entities as E

REPO = Path(__file__).resolve().parents[1]


def _sha_on_main():
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD~1"], capture_output=True,
                          text=True, check=True).stdout.strip()


def test_only_real_commits_become_commit_entities():
    full = _sha_on_main()
    got = E.resolve_commits({full[:8], "deadbee1", "e1f95763"}, REPO)
    assert got == {full[:8]: full}          # a key-id hash and a made-up SHA are not commits


def test_extract_is_exact_and_sectioned():
    text = ("Fixed in `memecoin_positions.opened_recorded_at` on 4663 via MEMECOIN_REAL_CHASE_MAX_PCT, "
            "scripts/research/j7_shadow_read.py:12 and position 66413; the base rate is low.")
    ents = E.extract(text, commits={}, tables={"memecoin_positions"}, handles=set(),
                     knob_ok=lambda name: name == "MEMECOIN_REAL_CHASE_MAX_PCT")
    got = {(e["type"], e["name"], e["section"]) for e in ents}
    assert ("chain", "4663", "memecoin/4663") in got
    assert ("knob", "MEMECOIN_REAL_CHASE_MAX_PCT", "memecoin/gates") in got
    assert ("table", "memecoin_positions", "memecoin") in got
    assert ("column", "memecoin_positions.opened_recorded_at", "memecoin") in got
    assert ("path", "scripts/research/j7_shadow_read.py", "research/studies") in got
    assert ("position", "66413", "memecoin") in got
    assert not any(e["type"] == "chain" and e["name"] == "8453" for e in ents), "'base rate' is not Base"


def test_an_invented_knob_is_not_an_entity():
    ents = E.extract("MEMECOIN_MAX_MCAP_USD caps it", commits={}, tables=set(), handles=set(),
                     knob_ok=lambda name: False)
    assert not [e for e in ents if e["type"] == "knob"]


def test_fact_sections_follow_d6():
    assert E.fact_section("memecoin", "feedback") == "crew/methods"
    assert E.fact_section("other", "project") == "research/studies"
    assert E.fact_section("sports/markets", "finding") == "wallet-analyzer"
    assert E.fact_section("brain/infra", "project") == "infra/runtime"


def test_absent_identifiers_do_not_compile_regex(monkeypatch):
    from bot.brain_graph import import_entities as module
    def fail(*args, **kwargs):
        raise AssertionError('absent identifier compiled regex')
    monkeypatch.setattr(module.re, 'search', fail)
    assert not module._word('completely_absent', 'unrelated prose')


def test_python_filename_is_not_a_table_column():
    from bot.brain_graph import import_entities as module
    out = module.extract('scripts/brain_health.py and brain_health.py with brain_health.run_at',
                         commits={}, tables={'brain_health'}, handles=set(), knob_ok=lambda n: False)
    columns = {e['name'] for e in out if e['type'] == 'column'}
    assert columns == {'brain_health.run_at'}
