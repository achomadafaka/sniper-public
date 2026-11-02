"""Tests for the algo-discovery run-dir artifact store + resume manifest."""
from __future__ import annotations

from sniper.analysis.algo_discovery.artifacts import RunDir, hash_obj


def test_manifest_skip_when_unchanged(tmp_path):
    rd = RunDir(tmp_path / "run")
    assert rd.should_run("04_wallet_rule", "abc") is True
    rd.write_artifact("04_wallet_rule", {"x": 1}, input_hash="abc")
    assert rd.should_run("04_wallet_rule", "abc") is False   # cached
    assert rd.should_run("04_wallet_rule", "def") is True    # input changed
    assert rd.read_artifact("04_wallet_rule") == {"x": 1}


def test_force_always_runs(tmp_path):
    rd = RunDir(tmp_path / "run")
    rd.write_artifact("01_x", {"a": 1}, input_hash="h")
    assert rd.should_run("01_x", "h") is False
    rd2 = RunDir(tmp_path / "run", force=True)
    assert rd2.should_run("01_x", "h") is True


def test_manifest_persists_across_instances(tmp_path):
    rd = RunDir(tmp_path / "run")
    rd.write_artifact("02_y", {"b": 2}, input_hash="h2")
    rd2 = RunDir(tmp_path / "run")  # fresh instance reloads manifest
    assert rd2.should_run("02_y", "h2") is False
    assert rd2.read_artifact("02_y") == {"b": 2}


def test_mark_failed_recorded(tmp_path):
    rd = RunDir(tmp_path / "run")
    rd.mark_failed("03_z", "boom")
    assert rd.manifest()["03_z"]["status"] == "failed"
    assert "boom" in rd.manifest()["03_z"]["error"]
    # a failed stage re-runs
    assert rd.should_run("03_z", "anyhash") is True


def test_hash_obj_stable_and_order_insensitive():
    assert hash_obj({"a": 1, "b": 2}) == hash_obj({"b": 2, "a": 1})
    assert hash_obj({"a": 1}) != hash_obj({"a": 2})
