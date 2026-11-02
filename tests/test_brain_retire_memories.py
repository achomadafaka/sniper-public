import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("retire", ROOT / "scripts/brain_retire_memories.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows():
    return [{"memory_id": "1", "name": "note", "prior_status": "active",
             "prior_superseded_by": "older-pointer", "fact_id": "f1"}]


def test_only_active_imported_nonuser_memories_are_retired():
    memories = [{"id": str(i), "name": name, "status": status, "type": kind}
                for i, name, status, kind in [(1, "session/a", "active", "project"),
                 (2, "missing", "active", "project"), (3, "user-note", "active", "user"),
                 (4, "session/HANDOFF", "active", "project"), (5, "old", "superseded", "project")]]
    episodes = [{"id": "e1", "source_ref": "memory/a.md#abc"},
                {"id": "e2", "source_ref": "supabase:memories/user-note#abc"},
                {"id": "e3", "source_ref": "memory/HANDOFF.md#abc"}]
    facts = [{"id": "f-late", "episode_ids": ["e1"], "recorded_at": "2"},
             {"id": "f-first", "episode_ids": ["e1", "e2", "e3"], "recorded_at": "1"}]
    assert [(r["memory_id"], r["fact_id"]) for r in module().plan_retire(memories, episodes, facts)] == [("1", "f-first")]


def test_manifest_precedes_writes_and_rollback_preserves_old_pointer(tmp_path):
    mod, calls = module(), []
    manifest = tmp_path / "manifest.json"
    def request(method, path, body=None, headers=None, timeout=10):
        assert json.loads(manifest.read_text())["rows"] == rows()
        calls.append((method, path, body))
        return [{"id": "1"}]
    assert mod.apply(request, rows(), manifest) == {"patched": 1, "skipped": 0}
    assert "status=eq.active" in calls[0][1]
    assert calls[0][2] == {"status": "superseded", "superseded_by": "f1"}
    assert mod.rollback(request, manifest) == {"restored": 1, "skipped": 0}
    assert "superseded_by=eq.f1" in calls[1][1]
    assert calls[1][2] == {"status": "active", "superseded_by": "older-pointer"}


def test_failed_patch_keeps_manifest_and_existing_manifest_cannot_be_overwritten(tmp_path):
    mod, manifest = module(), tmp_path / "manifest.json"
    def fail(*a, **k):
        raise RuntimeError("offline")
    with pytest.raises(RuntimeError):
        mod.apply(fail, rows(), manifest)
    assert json.loads(manifest.read_text())["rows"] == rows()
    with pytest.raises(FileExistsError):
        mod.apply(fail, [], manifest)


def test_concurrent_state_change_is_not_reported_as_patched(tmp_path):
    mod = module()
    request = lambda *a, **k: []
    assert mod.apply(request, rows(), tmp_path / "manifest.json") == {"patched": 0, "skipped": 1}


def test_direct_domain_records_stay_active_even_when_imported():
    names = ['market_boosts_current', 'country:US', 'flag:market-risk', 'session/project-note']
    memories = [dict(id=str(i), name=name, status='active', type='project')
                for i, name in enumerate(names)]
    episodes = [dict(id=f'e{i}', source_ref=f'supabase:memories/{name}#abc')
                for i, name in enumerate(names)]
    facts = [dict(id=f'f{i}', episode_ids=[f'e{i}'], recorded_at='1') for i in range(len(names))]
    plan = module().plan_retire(memories, episodes, facts)
    assert [r['name'] for r in plan] == ['session/project-note']
    assert all(row['status'] == 'active' for row in memories)
