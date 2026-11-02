"""Atomic JSON writes — a SIGKILL mid-write must never leave a truncated store (the runner
dies ~5x/day; a torn .fomo_tokens.json falls back to a spent refresh token = silent auth death)."""
import json
import pytest

from sniper.util.atomic import atomic_write_json


def test_atomic_write_json_roundtrip(tmp_path):
    p = tmp_path / "store.json"
    atomic_write_json(str(p), {"a": 1, "b": [1, 2]})
    assert json.loads(p.read_text()) == {"a": 1, "b": [1, 2]}


def test_atomic_write_json_preserves_old_file_on_serialize_failure(tmp_path):
    p = tmp_path / "store.json"
    atomic_write_json(str(p), {"ok": True})
    with pytest.raises(TypeError):
        atomic_write_json(str(p), {"bad": {1, 2, 3}})   # a set is not JSON-serializable
    assert json.loads(p.read_text()) == {"ok": True}    # original intact, NOT truncated
    assert not any(x.name.startswith(".tmp-") for x in tmp_path.iterdir())   # no temp litter


def test_atomic_write_json_sets_mode(tmp_path):
    p = tmp_path / "secret.json"
    atomic_write_json(str(p), {"t": 1}, mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600
