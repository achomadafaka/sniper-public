import importlib.util
from pathlib import Path

import pytest

from brain_graph_fake_store import FakeStore

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("legacy_import", ROOT / "scripts/brain_legacy_memory_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_changed_files_append_episodes_without_facts_or_status_writes(tmp_path):
    mod, store = module(), FakeStore()
    (tmp_path / "MEMORY.md").write_text("index")
    file = tmp_path / "finding.md"
    file.write_text("body one")
    assert mod.run(store, tmp_path, secret_values=set()) == {
        "files": 1, "imported": 1, "unchanged": 0, "skipped_index": 1}
    assert mod.run(store, tmp_path, secret_values=set())["unchanged"] == 1
    file.write_text("body two")
    assert mod.run(store, tmp_path, secret_values=set())["imported"] == 1
    assert len(store.rows["brain_episodes"]) == 2 and store.rows["brain_facts"] == []
    assert {e["kind"] for e in store.rows["brain_episodes"]} == {"legacy_memory"}


def test_redacts_and_splits_long_files_then_resumes_missing_parts(tmp_path):
    mod, store = module(), FakeStore()
    key = "AKIAZZZZZZZZZZZZZZZZ"
    (tmp_path / "x.md").write_text(("evidence " * 450) + key)
    mod.run(store, tmp_path, secret_values={key})
    episodes = store.rows["brain_episodes"]
    assert len(episodes) == 3 and all(len(e["body"]) <= 1800 for e in episodes)
    assert key not in "".join(e["body"] for e in episodes)
    episodes.pop()
    assert mod.run(store, tmp_path, secret_values={key})["imported"] == 1
    assert len(episodes) == 3
    assert mod.run(store, tmp_path, secret_values={key})["unchanged"] == 1


def test_missing_directory_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        module().run(FakeStore(), tmp_path / "missing", secret_values=set())
