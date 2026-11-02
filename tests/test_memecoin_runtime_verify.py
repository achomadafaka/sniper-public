import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.memecoin_runtime_processes import find_memecoin_runners
from scripts import memecoin_runtime_verify as runtime_verify
from scripts.memecoin_runtime_verify import (
    _default_config_validator, load_manifest, verify_release, verify_sidecar,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "release"
    repo.mkdir()
    _run("git", "init", "-q", cwd=repo)
    _run("git", "config", "user.email", "test@example.com", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    (repo / "tracked.py").write_text("x = 1\n")
    # A pinned release must be able to serve the cluster gate if it is ever activated, so
    # the verifier requires the worker to be present. Ship it in the fixture release too.
    scripts = repo / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "memecoin_cluster_gate_worker.py").write_text("# pinned cluster worker\n")
    lifi = repo / "sniper" / "execution"
    lifi.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "sniper/execution/lifi_4663_manifest.json", lifi)
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-qm", "init", cwd=repo)
    return repo, _run("git", "rev-parse", "HEAD", cwd=repo)


def _state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / ".fomo.env").write_text("MEMECOIN_REAL_ENABLED=0\n")
    (state / "sniper_tracker.db").write_bytes(b"")
    # The deploy gate exports a shared DB_PATH; verifier fixtures must read only their own ledger.
    monkeypatch.setenv("DB_PATH", str(state / "sniper_tracker.db"))
    return state


def _manifest(repo, state, sha):
    value = json.loads((repo / "sniper/execution/lifi_4663_manifest.json").read_text())
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return {"sha": sha, "release_path": str(repo.resolve()),
            "state_root": str(state.resolve()), "deployed_at": "2026-07-11T00:00:00Z",
            "lifi_manifest_sha256": hashlib.sha256(canonical.encode()).hexdigest()}


def test_healthy_release_requires_exact_clean_sha_and_external_state(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path); state = _state(tmp_path, monkeypatch)
    result = verify_release(repo, state, _manifest(repo, state, sha), config_validator=lambda *_: None)
    assert result.ok and result.sha == sha
    assert result.code_root == str(repo.resolve()) and result.state_root == str(state.resolve())


def test_provider_stage_is_bound_to_evidence_and_does_not_load_a_signer(tmp_path, monkeypatch):
    from test_shyft_migration_preflight import allocation_record, candidate_env
    from scripts.research import shyft_migration_preflight as p
    from datetime import datetime, timezone
    state = _state(tmp_path, monkeypatch)
    (state / ".env").write_text("MEMECOIN_SLOT_LANE_RABBIT_TAPE=0\n"
                                "MEMECOIN_SOLANA_KEYPAIR=data/solana_keypair.json\n"
                                "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT=35\n")
    staged = tmp_path / "stage.env"
    staged.write_text("".join(f"{k}={v}\n" for k, v in candidate_env().items()))
    staged.chmod(0o600)
    record = allocation_record()
    now = datetime.now(timezone.utc).isoformat()
    record["allocations"][0]["verified_at"] = now
    record["capabilities"]["observed_at"] = now
    allocation = tmp_path / "allocation.json"
    allocation.write_text(json.dumps(record))
    report = p.check_preflight(candidate_env(), record)
    report.update(source_env_sha256=p.file_digest(state / ".env"),
                  fomo_env_sha256=p.file_digest(state / ".fomo.env"),
                  staged_env_sha256=p.file_digest(staged), allocation_record=str(allocation),
                  allocation_record_sha256=p.file_digest(allocation))
    proof = tmp_path / "preflight.json"
    proof.write_text(json.dumps(report))
    assert callable(getattr(runtime_verify, "provider_stage_errors", None)), "stage verifier missing"
    assert runtime_verify.provider_stage_errors(state, staged, proof, inherited_env={}) == []
    allocation.write_text(json.dumps(record | {"research_active": True}))
    assert "allocation_record_changed" in runtime_verify.provider_stage_errors(
        state, staged, proof, inherited_env={})
    allocation.write_text(json.dumps(record))
    assert "provider_shell_override" in runtime_verify.provider_stage_errors(
        state, staged, proof, inherited_env={"MEMECOIN_FUNDED_ONLY": "0"})
    staged.chmod(0o644)
    assert "provider_stage_not_private" in runtime_verify.provider_stage_errors(
        state, staged, proof, inherited_env={})


def test_shyft_watcher_needs_current_pid_full_ack_and_both_fresh_heads():
    now, mono = time.time(), time.monotonic()
    expected = "/state/.runtime/memecoin/releases/sha/scripts/research/onchain_wallet_watch.py"
    old = "/state/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py"
    beat = {"pid": 123, "provider": "shyft", "ts": now, "connected": True,
            "subs": 2, "subs_desired": 2, "subs_in_flight": 0,
            "shyft_confirmed": {"feeds": {host: {
                "connected": True, "requested_owners": ["a", "b"],
                "acknowledged_owners": ["a", "b"], "head_slot": 42,
                "head_monotonic": mono - .1}
                for host in ("https://grpc.ams.shyft.to", "https://grpc.fra.shyft.to")}}}
    assert callable(getattr(runtime_verify, "shyft_watcher_errors", None)), "watcher checker missing"
    run = lambda b=beat, ready="123", rows=((123, expected),), overlap=False: (
        runtime_verify.shyft_watcher_errors(expected, b, ready, rows,
                                            now=now, monotonic=mono, allow_overlap=overlap))
    assert run() == []
    assert run(rows=((123, expected), (124, old)), overlap=True) == []
    assert "shyft_watcher_not_sole" in run(rows=((123, expected), (124, old)))
    assert "shyft_watcher_not_ready" in run(ready="123 partial")
    assert "shyft_watcher_wrong_release" in run(rows=((123, old),))
    wrong = json.loads(json.dumps(beat))
    wrong["shyft_confirmed"]["feeds"]["https://grpc.fra.shyft.to"]["acknowledged_owners"] = ["a"]
    assert "shyft_watcher_ack_incomplete" in run(b=wrong)
    wrong = json.loads(json.dumps(beat))
    wrong["shyft_confirmed"]["feeds"]["https://grpc.fra.shyft.to"]["head_monotonic"] = mono - 3
    assert "shyft_watcher_head_stale" in run(b=wrong)


def test_missing_or_malformed_manifest_fails_closed(tmp_path, monkeypatch):
    repo, _sha = _repo(tmp_path); state = _state(tmp_path, monkeypatch)
    assert not verify_release(repo, state, None, config_validator=lambda *_: None).ok
    path = tmp_path / "bad.json"; path.write_text("not json")
    assert load_manifest(path) is None


def test_rollback_requires_exact_old_processes_fresh_sweep_and_full_watcher_ack():
    now = time.time()
    runner, watcher = "/old/scripts/memecoin_runner_loop.py", "/old/scripts/research/onchain_wallet_watch.py"
    heartbeat = {"ts": now, "bracket_ts": now, "auth_ok": True}
    beat = {"ts": now, "connected": True, "subs": 2, "subs_desired": 2, "subs_in_flight": 0}
    assert callable(getattr(runtime_verify, "provider_rollback_errors", None)), "rollback proof missing"
    run = lambda h=heartbeat, w=beat, r=((123, runner),), ready="124": runtime_verify.provider_rollback_errors(
        runner, watcher, h, w, ready, r, [(124, watcher)], now - 1, now=now)
    assert run() == []
    assert "rollback_runner_wrong_release" in run(r=((123, "/new/runner.py"),))
    assert "rollback_runner_not_ready" in run(h=heartbeat | {"bracket_ts": now - 2})
    assert "rollback_watcher_ack_incomplete" in run(w=beat | {"subs": 1})
    assert "rollback_watcher_not_ready" in run(ready="124 partial")


@pytest.mark.parametrize("owners", [[{"a": 1}], ["a", "a"], [None]])
def test_watcher_malformed_or_duplicate_ack_is_a_safe_refusal(owners):
    beat = {"pid": 123, "provider": "shyft", "ts": time.time(), "connected": True,
            "subs": len(owners), "subs_desired": len(owners), "subs_in_flight": 0,
            "shyft_confirmed": {"feeds": {host: {
                "connected": True, "requested_owners": owners,
                "acknowledged_owners": owners, "head_slot": 42,
                "head_monotonic": time.monotonic()}
                for host in ("https://grpc.ams.shyft.to", "https://grpc.fra.shyft.to")}}}
    assert "shyft_watcher_ack_incomplete" in runtime_verify.shyft_watcher_errors(
        "/expected.py", beat, "123", [(123, "/expected.py")])


def test_primary_handover_selects_new_pid_even_when_old_same_release_is_first():
    beat = {"pid": 124, "provider": "shyft", "ts": 100, "connected": True,
            "subs": 1, "subs_desired": 1, "subs_in_flight": 0,
            "shyft_confirmed": {"handover": {"phase": "primary"}, "feeds": {
                "grpc.ams.shyft.to": {"connected": True, "requested_owners": ["a"],
                    "acknowledged_owners": ["a"], "head_slot": 42, "head_monotonic": 99.9}}}}
    run = lambda b=beat, ready="124 primary 100": runtime_verify.shyft_watcher_errors(
        "/expected.py", b, ready, [(123, "/expected.py"), (124, "/expected.py")],
        now=100, monotonic=100, allow_overlap=True, primary_only=True, candidate_pid=124)
    assert run() == []
    assert "shyft_watcher_not_ready" in run(ready="123 primary 100")
    assert "shyft_watcher_wrong_generation" in run(b=beat | {"pid": 123})
    stale = json.loads(json.dumps(beat))
    stale['shyft_confirmed']['feeds']['grpc.ams.shyft.to']['head_monotonic'] = 97
    assert "shyft_watcher_head_stale" in run(b=stale)
    assert "shyft_watcher_not_ready" in runtime_verify.shyft_watcher_errors(
        "/expected.py", beat, "124 primary 100", [(124, "/expected.py")], now=100, monotonic=100)


def test_process_start_time_uses_field_after_parenthesized_command(monkeypatch):
    real = Path.read_text
    def read(path, *args, **kwargs):
        if str(path) == '/proc/123/stat':
            return '123 (python (worker)) ' + ' '.join(['S'] + ['0'] * 18 + ['birth'] + ['0'] * 4)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    assert runtime_verify._process_start_time(123) == 'birth'


def test_watcher_handover_record_binds_birth_path_and_state(monkeypatch):
    script = '/state/.runtime/memecoin/releases/new/scripts/research/onchain_wallet_watch.py'
    old = '/state/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py'
    monkeypatch.setattr(runtime_verify, '_watcher_rows', lambda state: [(123, old), (124, script)])
    monkeypatch.setattr(runtime_verify, '_process_start_time', lambda pid: str(pid + 1))
    record = runtime_verify.watcher_handover_record('/state/.runtime/memecoin/releases/new', '/state', 124)
    assert record['candidate'] == [124, script, '125']
    assert record['old'] == [[123, old, '124']]


def test_retire_exact_old_identity_uses_pidfd_and_preserves_reused_or_foreign_pid(monkeypatch):
    old = '/state/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py'
    foreign = '/other/scripts/research/onchain_wallet_watch.py'
    rows = [(123, old), (125, foreign)]
    monkeypatch.setattr(runtime_verify, '_watcher_rows', lambda state: rows)
    monkeypatch.setattr(runtime_verify, '_process_start_time', lambda pid: 'birth')
    sent, closed = [], []
    monkeypatch.setattr(runtime_verify.os, 'pidfd_open', lambda pid: pid + 1000)
    monkeypatch.setattr(runtime_verify.os, 'close', closed.append)
    import signal
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda fd, sig: sent.append((fd, sig)))
    assert runtime_verify.retire_old_watchers('/state', {'old': [[123, old, 'birth']]})
    assert sent == [(1123, signal.SIGTERM)] and closed == [1123]
    sent.clear()
    assert not runtime_verify.retire_old_watchers('/state', {'old': [[123, old, 'reused']]})
    assert not runtime_verify.retire_old_watchers('/state', {'old': [[125, foreign, 'birth']]})
    assert not sent


def test_old_retirement_failure_is_not_read_as_gone(monkeypatch):
    old = '/state/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py'
    monkeypatch.setattr(runtime_verify, '_watcher_rows', lambda state: [(123, old)])
    monkeypatch.setattr(runtime_verify, '_process_start_time', lambda pid: 'birth')
    monkeypatch.setattr(runtime_verify.os, 'pidfd_open', lambda pid: (_ for _ in ()).throw(PermissionError()))
    assert not runtime_verify.retire_old_watchers('/state', {'old': [[123, old, 'birth']]})


def test_rh_capture_and_stop_share_identity_guard(monkeypatch):
    suffix = 'scripts/memecoin_rh_feed_shadow_loop.py'
    path = '/state/.runtime/memecoin/releases/old/' + suffix
    monkeypatch.setattr(runtime_verify, '_owned_script_rows', lambda *args: [(123, path)])
    monkeypatch.setattr(runtime_verify, '_process_start_time', lambda pid: 'birth')
    record = runtime_verify.owned_script_record('/state', suffix, expected_root='/state/.runtime/memecoin/releases/old')
    assert record == {'suffix': suffix, 'old': [[123, path, 'birth']]}
    with pytest.raises(ValueError):
        runtime_verify.owned_script_record('/state', suffix, expected_root='/state/.runtime/memecoin/releases/new')
    sent = []
    monkeypatch.setattr(runtime_verify.os, 'pidfd_open', lambda pid: 100)
    monkeypatch.setattr(runtime_verify.os, 'close', lambda fd: None)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda fd, sig: sent.append(fd))
    assert runtime_verify.retire_old_watchers('/state', record, script_suffix=suffix)
    assert sent == [100]


def test_abort_candidate_requires_captured_old_owner_still_alive(monkeypatch):
    old = '/state/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py'
    candidate = '/state/.runtime/memecoin/releases/new/scripts/research/onchain_wallet_watch.py'
    rows = [(123, old), (124, candidate)]
    monkeypatch.setattr(runtime_verify, '_watcher_rows', lambda state: rows)
    monkeypatch.setattr(runtime_verify, '_process_start_time', lambda pid: str(pid))
    stopped = []
    monkeypatch.setattr(runtime_verify, 'retire_old_watchers', lambda state, record, **kw: stopped.extend(record['old']) or True)
    record = {'old': [[123, old, '123']], 'candidate': [124, candidate, '124']}
    assert runtime_verify.abort_watcher_handover('/state/.runtime/memecoin/releases/new', '/state', record)
    assert stopped == [[124, candidate, '124']]
    rows.pop(0); stopped.clear()
    assert not runtime_verify.abort_watcher_handover('/state/.runtime/memecoin/releases/new', '/state', record)
    assert not stopped


@pytest.mark.parametrize('candidate_root', ['new', 'rolled-back'])
def test_absent_handover_candidate_is_already_aborted_with_exact_old_survivor(monkeypatch, candidate_root):
    old = '/state/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py'
    candidate = f'/state/.runtime/memecoin/releases/{candidate_root}/scripts/research/onchain_wallet_watch.py'
    monkeypatch.setattr(runtime_verify, '_watcher_rows', lambda state: [(123, old)])
    monkeypatch.setattr(runtime_verify, '_process_start_time', lambda pid: '123' if pid == 123 else None)
    real_stat = Path.stat
    def stat(path, *args, **kwargs):
        if str(path) == '/proc/124':
            raise FileNotFoundError()
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(runtime_verify, 'retire_old_watchers',
                        lambda *args, **kwargs: pytest.fail('absent candidate must not stop the survivor'))
    record = {'old': [[123, old, '123']], 'candidate': [124, candidate, '124']}
    assert runtime_verify.abort_watcher_handover('/state/.runtime/memecoin/releases/new', '/state', record)


@pytest.mark.parametrize('unknown', ['candidate_stat', 'candidate_birth', 'old_birth', 'old_record_birth',
                                     'old_missing', 'foreign_candidate', 'foreign_survivor'])
def test_absent_handover_recovery_holds_unreadable_or_unowned_identity(monkeypatch, unknown):
    old = ('/foreign/scripts/research/onchain_wallet_watch.py' if unknown == 'foreign_survivor'
           else '/state/.runtime/memecoin/releases/old/scripts/research/onchain_wallet_watch.py')
    candidate = ('/foreign/scripts/research/onchain_wallet_watch.py' if unknown == 'foreign_candidate'
                 else '/state/.runtime/memecoin/releases/new/scripts/research/onchain_wallet_watch.py')
    rows = [] if unknown == 'old_missing' else [(123, old)]
    if unknown == 'candidate_birth':
        rows.append((124, candidate))
    monkeypatch.setattr(runtime_verify, '_watcher_rows', lambda state: rows)
    monkeypatch.setattr(runtime_verify, '_process_start_time',
                        lambda pid: '123' if pid == 123 and unknown not in ('old_birth', 'old_record_birth') else None)
    real_stat = Path.stat
    def stat(path, *args, **kwargs):
        if str(path) == '/proc/124':
            if unknown == 'candidate_stat':
                raise PermissionError()
            if unknown == 'candidate_birth':
                return object()  # The PID exists even though its identity cannot be read.
            raise FileNotFoundError()
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(runtime_verify, 'retire_old_watchers',
                        lambda *args, **kwargs: pytest.fail('unknown identity must remain untouched'))
    record = {'old': [[123, old, None if unknown == 'old_record_birth' else '123']],
              'candidate': [124, candidate, '124']}
    assert not runtime_verify.abort_watcher_handover('/state/.runtime/memecoin/releases/new', '/state', record)


def test_release_recalculates_lifi_manifest_hash_and_chain(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    state = _state(tmp_path, monkeypatch)
    manifest = _manifest(repo, state, sha)

    assert runtime_verify.verify_lifi_manifest(manifest, repo) == []
    manifest["lifi_manifest_sha256"] = "0" * 64
    assert runtime_verify.verify_lifi_manifest(manifest, repo) == ["LI.FI manifest hash mismatch"]

    manifest.pop("lifi_manifest_sha256")
    assert runtime_verify.verify_lifi_manifest(manifest, repo) == [
        "deploy manifest LI.FI hash is not 64 lowercase hex"]


def test_path_sha_dirty_and_missing_state_each_fail(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path); state = _state(tmp_path, monkeypatch)
    wrong_path = _manifest(repo, state, sha); wrong_path["release_path"] = str(tmp_path / "else")
    assert "manifest release path mismatch" in verify_release(
        repo, state, wrong_path, config_validator=lambda *_: None,
    ).errors
    wrong_sha = _manifest(repo, state, "0" * 40)
    assert "release HEAD does not match manifest SHA" in verify_release(
        repo, state, wrong_sha, config_validator=lambda *_: None,
    ).errors
    (repo / "tracked.py").write_text("dirty\n")
    assert "release has tracked changes" in verify_release(
        repo, state, _manifest(repo, state, sha), config_validator=lambda *_: None,
    ).errors
    _run("git", "checkout", "--", "tracked.py", cwd=repo)
    (state / ".fomo.env").unlink()
    result = verify_release(repo, state, _manifest(repo, state, sha), config_validator=lambda *_: None)
    assert any(".fomo.env" in error for error in result.errors)


def test_config_validation_failure_and_state_inside_release_fail(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path); state = _state(tmp_path, monkeypatch)

    def invalid(*_args):
        raise ValueError("MEMECOIN_REAL_MAX_HOLD_S must be > 0")

    result = verify_release(repo, state, _manifest(repo, state, sha), config_validator=invalid)
    assert any("MAX_HOLD" in error for error in result.errors)
    inside = repo / "state"; (inside / "data").mkdir(parents=True); (inside / "logs").mkdir()
    (inside / ".fomo.env").write_text("x=1\n"); (inside / "sniper_tracker.db").write_bytes(b"")
    result = verify_release(repo, inside, _manifest(repo, inside, sha), config_validator=lambda *_: None)
    assert "mutable state path is inside release" in result.errors


def test_default_config_validator_targets_candidate_manifest_and_private_socket(
        tmp_path, monkeypatch):
    repo, _sha = _repo(tmp_path)
    state = _state(tmp_path, monkeypatch)
    candidate = state / "data" / ".memecoin_deploy.candidate.123"
    candidate.write_text("{}")
    captured = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(*args, **kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(subprocess, "run", run)
    _default_config_validator(repo, state, candidate)
    env = captured["env"]
    assert env["DB_PATH"] == str(state / "sniper_tracker.db")
    assert env["SNIPER_MEMECOIN_DEPLOY_MANIFEST"] == str(candidate)
    assert env["SNIPER_TPU_SOCKET_PATH_OVERRIDE"].startswith(str(state / "data"))
    assert env["SNIPER_TPU_SOCKET_PATH_OVERRIDE"] != str(
        state / "data" / "memecoin-tpu-sidecar.sock")


def _sidecar_artifact(tmp_path, code_root, sha, *, overrides=None, mode=0o555, symlink=False):
    art = tmp_path / "artifacts" / sha
    art.mkdir(parents=True, exist_ok=True)
    binary = art / "memecoin-tpu-sidecar"
    if binary.exists():
        binary.chmod(0o755)                              # re-writable across repeated helper calls
    binary.write_bytes(b"BINARY-BYTES")
    binary.chmod(mode)
    bin_path = binary
    if symlink:
        link = art / "memecoin-tpu-sidecar.link"
        link.symlink_to(binary)
        bin_path = link
    lockdir = code_root / "rust" / "solana-tpu-sidecar"
    lockdir.mkdir(parents=True, exist_ok=True)
    (lockdir / "Cargo.lock").write_bytes(b"LOCK-BYTES")
    obj = {
        "path": str(bin_path),
        "sha256": hashlib.sha256(b"BINARY-BYTES").hexdigest(),
        "source_sha": sha,
        "cargo_lock_sha256": hashlib.sha256(b"LOCK-BYTES").hexdigest(),
        "target": "x86_64-unknown-linux-gnu",
        "build_image_digest": "sha256:c1e5f19e773b7878c3f7a805dd00a495e747acbdc76fb2337a4ebf0418896b33",
    }
    if overrides:
        obj.update(overrides)
    return obj


def test_valid_sidecar_object_verifies_inside_release(tmp_path, monkeypatch):
    repo, sha = _repo(tmp_path)
    state = _state(tmp_path, monkeypatch)
    manifest = _manifest(repo, state, sha)
    manifest["sidecar"] = _sidecar_artifact(tmp_path, repo, sha)
    result = verify_release(repo, state, manifest, config_validator=lambda *_: None)
    assert result.ok, result.errors


def test_sidecar_missing_object_and_metadata_fail(tmp_path):
    repo, sha = _repo(tmp_path)
    assert verify_sidecar(None, repo, sha) == ["sidecar manifest object missing or invalid"]
    obj = _sidecar_artifact(tmp_path, repo, sha, overrides={"sha256": "nothex"})
    assert any("sha256" in e for e in verify_sidecar(obj, repo, sha))
    obj2 = _sidecar_artifact(tmp_path, repo, sha, overrides={"target": "aarch64"})
    assert any("target" in e for e in verify_sidecar(obj2, repo, sha))
    obj3 = _sidecar_artifact(tmp_path, repo, sha, overrides={"build_image_digest": "latest"})
    assert any("digest" in e for e in verify_sidecar(obj3, repo, sha))


def test_sidecar_wrong_source_sha_hash_and_lock_fail(tmp_path):
    repo, sha = _repo(tmp_path)
    assert any("source_sha does not match" in e
               for e in verify_sidecar(_sidecar_artifact(tmp_path, repo, sha,
                                       overrides={"source_sha": "b" * 40}), repo, sha))
    assert any("binary hash mismatch" in e
               for e in verify_sidecar(_sidecar_artifact(tmp_path, repo, sha,
                                       overrides={"sha256": "0" * 64}), repo, sha))
    assert any("Cargo.lock hash mismatch" in e
               for e in verify_sidecar(_sidecar_artifact(tmp_path, repo, sha,
                                       overrides={"cargo_lock_sha256": "0" * 64}), repo, sha))


def test_sidecar_symlink_any_writable_and_path_outside_artifacts_fail(tmp_path):
    repo, sha = _repo(tmp_path)
    assert any("symlink" in e
               for e in verify_sidecar(_sidecar_artifact(tmp_path, repo, sha, symlink=True), repo, sha))
    assert any("writable" in e
               for e in verify_sidecar(_sidecar_artifact(tmp_path, repo, sha, mode=0o777), repo, sha))
    assert any("writable" in e
               for e in verify_sidecar(_sidecar_artifact(tmp_path, repo, sha, mode=0o755), repo, sha))
    # a binary outside artifacts/<sha> (place it directly under tmp_path) is rejected
    stray = tmp_path / "memecoin-tpu-sidecar"
    stray.write_bytes(b"BINARY-BYTES")
    stray.chmod(0o555)
    (repo / "rust" / "solana-tpu-sidecar").mkdir(parents=True, exist_ok=True)
    (repo / "rust" / "solana-tpu-sidecar" / "Cargo.lock").write_bytes(b"LOCK-BYTES")
    obj = {"path": str(stray), "sha256": hashlib.sha256(b"BINARY-BYTES").hexdigest(),
           "source_sha": sha, "cargo_lock_sha256": hashlib.sha256(b"LOCK-BYTES").hexdigest(),
           "target": "x86_64-unknown-linux-gnu", "build_image_digest": "sha256:abc"}
    assert any("outside artifacts" in e for e in verify_sidecar(obj, repo, sha))


def test_sidecar_path_requires_exact_artifacts_sha_layout_and_binary_name(tmp_path):
    repo, sha = _repo(tmp_path)
    obj = _sidecar_artifact(tmp_path, repo, sha)
    nested = tmp_path / "artifacts" / "wrong" / sha / "renamed-sidecar"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"BINARY-BYTES")
    nested.chmod(0o555)
    obj["path"] = str(nested)
    assert any("outside artifacts" in e for e in verify_sidecar(obj, repo, sha))


def test_watchdog_matches_absolute_release_runner_and_never_mutable_source_literal():
    text = Path("scripts/memecoin_watchdog.sh").read_text()
    assert 'MATCH="$ROOT/scripts/memecoin_runner_loop.py"' in text
    assert "ROOT=/home/bigpapi/Desktop/Sniper" not in text
    assert "memecoin_runtime_verify.py" in text
    assert "memecoin_runtime_processes.py" in text
    assert "foreign runner" in text
    assert "multiple pinned runners" in text
    assert "repairing pinned pidfile" in text
    assert "runner_launch_health" in text
    assert 'STARTUP_TIMEOUT_S="${MEMECOIN_WATCHDOG_STARTUP_TIMEOUT_S:-90}"' in text
    assert '_launch_deadline=$((LAUNCH_STARTED + STARTUP_TIMEOUT_S))' in text
    assert "seq 1 30" not in text
    assert text.index("runner launch verified") < text.index("pinned runner launched")
    # THE ON-CHAIN WATCHER MATCH: release-INDEPENDENT but release-REQUIRED (2026-09-02).
    #
    # This assertion used to demand `ONCHAIN_EVM_MATCH="$ROOT/..."`. `$ROOT` is the RELEASE dir, so
    # the pattern carried the release SHA and stopped matching the watcher the PREVIOUS release had
    # started -- every deploy launched another one. Seven were running on 2026-09-02, one per deploy
    # that day.
    #
    # The guard's INTENT is preserved and is why this is not simply relaxed to a bare path: a
    # watcher started from the mutable SOURCE tree must never satisfy the check, or the watchdog
    # declines to launch the pinned one and the funded host runs an unpinned supervisor -- the same
    # class as the "foreign runner" the runner match above exists to catch. So the match requires
    # the releases directory (any release, not this one) AND anchors on argument position, because a
    # bare substring over `ps` output is satisfied by any command line that merely mentions the path.
    #
    # ONE WATCHER PER CHAIN (2026-09-06). The argument string is now built per chain, so the
    # assertions below check the SHAPE the intent needs rather than one frozen line. 4663 keeps
    # the exact string it has always had, with no `--chain`, because the liveness check is a
    # string match and a changed 4663 string would stop matching the previous release's watcher --
    # which is the seven-watcher leak, arriving again through a new door.
    assert 'ONCHAIN_EVM_SCRIPT="scripts/research/onchain_evm_wallet_watch.py"' in text
    assert 'args="$ONCHAIN_EVM_SCRIPT --duration 86400"' in text          # 4663: unchanged
    assert 'args="$ONCHAIN_EVM_SCRIPT --duration 86400 --chain $chain"' in text
    assert '/.runtime/memecoin/releases/' in text        # release REQUIRED
    assert 'ONCHAIN_EVM_MATCH="$ROOT/' not in text       # but never this release only
    assert "ps -eww -o args=" in text                    # -ww: ps truncates to terminal width
    # Anchored on argument position, wide enough to carry `--chain <id>` and, since the Arc
    # observe-only lane (2026-09-16), a trailing `--observe-only` at field 7.
    assert "awk '{print $2\" \"$3\" \"$4\" \"$5\" \"$6\" \"$7}'" in text
    # The 4663 needle is a PREFIX of every other chain's line: 4663 must not be satisfied by a
    # running per-chain watcher, or it goes unsupervised while the check reads as healthy.
    assert 'grep -v -- "--chain"' in text
    assert '"$ROOT/$ONCHAIN_EVM_SCRIPT" "$@"' in text    # LAUNCH stays release-pinned
    # 4663 WAS RETIRED 2026-09-23 and REVIVED 2026-09-28 (operator order): it now goes through
    # the same eligibility probe loop as every other chain, never its own launch block.
    assert "ensure_onchain_evm_watcher 4663" not in text
    assert '[ "$_chain" != "4663" ]' not in text
    assert text.index("ensure_onchain_evm_watcher") < text.index('RUNNER_ROWS=')


def test_watchdog_does_not_reverify_a_healthy_pinned_runner(tmp_path):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    watchdog = scripts / "memecoin_watchdog.sh"
    shutil.copy2(Path("scripts/memecoin_watchdog.sh"), watchdog)
    runner = scripts / "memecoin_runner_loop.py"
    runner.write_text("import time\ntime.sleep(60)\n")
    for name in (
        "memecoin_runtime_verify.py",
        "memecoin_runtime_processes.py",
        "memecoin_watchdog_escalate.py",
    ):
        (scripts / name).write_text("# handled by fake python\n")

    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / ".fomo.env").write_text("MEMECOIN_REAL_ENABLED=0\n")
    (state / "data" / "memecoin_deploy.json").write_text("{}\n")
    verify_marker = tmp_path / "verify-called"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  */memecoin_runtime_verify.py) touch \"$TEST_VERIFY_MARKER\"; exit 1 ;;\n"
        "  */memecoin_runtime_processes.py)\n"
        "    printf '%s\\tpinned\\t%s\\n' \"$TEST_RUNNER_PID\" \"${3:-}\"\n"
        "    exit 0 ;;\n"
        "  */memecoin_watchdog_escalate.py) exit 0 ;;\n"
        "  -c) printf 'ok\\n'; exit 0 ;;\n"
        f"  *) exec {sys.executable} \"$@\" ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    process = subprocess.Popen([sys.executable, str(runner)])
    try:
        env = os.environ | {
            "SNIPER_STATE_ROOT": str(state),
            "SNIPER_PYTHON": str(fake_python),
            "MEMECOIN_PIDFILE": str(state / "data" / "memecoin_runner.pid"),
            "TEST_RUNNER_PID": str(process.pid),
            "TEST_VERIFY_MARKER": str(verify_marker),
        }
        result = subprocess.run(
            ["bash", str(watchdog)], env=env, capture_output=True, text=True,
            timeout=10,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert not verify_marker.exists()
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_watchdog_runner_survives_parent_session_teardown(tmp_path):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    watchdog = scripts / "memecoin_watchdog.sh"
    shutil.copy2(Path("scripts/memecoin_watchdog.sh"), watchdog)
    runner = scripts / "memecoin_runner_loop.py"
    runner.write_text("import time\ntime.sleep(60)\n")
    for name in ("memecoin_runtime_verify.py", "memecoin_runtime_processes.py"):
        (scripts / name).write_text("# handled by fake python\n")

    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / ".fomo.env").write_text("MEMECOIN_REAL_ENABLED=0\n")
    (state / "data" / "memecoin_deploy.json").write_text("{}\n")
    pidfile = state / "data" / "memecoin_runner.pid"
    verify_marker = tmp_path / "verify-called"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  */memecoin_runtime_verify.py) touch \"$TEST_VERIFY_MARKER\"; exit 0 ;;\n"
        "  */memecoin_runtime_processes.py)\n"
        "    pid=$(cat \"$MEMECOIN_PIDFILE\" 2>/dev/null || true)\n"
        "    if [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then\n"
        "      printf '%s\\tpinned\\t%s\\n' \"$pid\" \"${3:-}\"\n"
        "    fi\n"
        "    exit 0 ;;\n"
        "  -c) exit 0 ;;\n"
        f"  *) exec {sys.executable} \"$@\" ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    # Drop every state-derived path from the parent env before overriding SNIPER_STATE_ROOT.
    # `configure_memecoin_state_env()` runs at IMPORT of the module under test and setdefaults
    # ABSOLUTE paths (under whatever state root the suite booted with) into os.environ. Inherited
    # here they win over the watchdog's `${VAR:-$STATE_ROOT/...}` defaults, so the watchdog logged
    # into a directory this test never created and died — visible only when import order put the
    # pollution first, i.e. as an order-dependent failure under xdist.
    inherited = {k: v for k, v in os.environ.items()
                 if k not in {"DB_PATH", "MEMECOIN_RUN_LOCK", "MEMECOIN_HEARTBEAT_PATH",
                              "MEMECOIN_FEED_SNAPSHOT_PATH", "SNIPER_CLOCK_OFFSET_FILE",
                              "FOMO_TOKEN_STORE", "MEMECOIN_KILL_SWITCH_FILE",
                              "MEMECOIN_SOLANA_KEYPAIR", "MEMECOIN_EVM_KEYPAIR",
                              "MEMECOIN_PIDFILE", "MEMECOIN_RUNNER_LOG",
                              "MEMECOIN_WATCHDOG_LOG"}}
    env = inherited | {
        "SNIPER_STATE_ROOT": str(state),
        "SNIPER_PYTHON": str(fake_python),
        "MEMECOIN_PIDFILE": str(pidfile),
        "MEMECOIN_WATCHDOG_STARTUP_TIMEOUT_S": "3",
        "TEST_VERIFY_MARKER": str(verify_marker),
    }
    process = subprocess.Popen(
        ["bash", str(watchdog)], env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    child_pid = None
    try:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert verify_marker.exists()
        child_pid = int(pidfile.read_text())
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # detached child already left the completed watchdog's process group
        time.sleep(0.2)
        os.kill(child_pid, 0)
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_process_discovery_classifies_relative_old_runner_as_foreign(tmp_path):
    old_root = tmp_path / "old"
    pinned_root = tmp_path / "release"
    (old_root / "scripts").mkdir(parents=True)
    (pinned_root / "scripts").mkdir(parents=True)
    old_script = old_root / "scripts" / "memecoin_runner_loop.py"
    old_script.write_text("import time; time.sleep(30)\n")
    expected = pinned_root / "scripts" / "memecoin_runner_loop.py"
    expected.write_text("# pinned\n")
    process = subprocess.Popen(
        [sys.executable, "scripts/memecoin_runner_loop.py"], cwd=old_root,
    )
    try:
        for _ in range(20):
            rows = find_memecoin_runners(expected)
            row = next((item for item in rows if item.pid == process.pid), None)
            if row is not None:
                break
            time.sleep(0.05)
        assert row is not None
        assert row.script_path == old_script.resolve()
        assert row.is_pinned is False
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_process_discovery_does_not_treat_expected_argument_as_a_runner(tmp_path):
    proc = tmp_path / "proc"
    helper = proc / "101"
    runner = proc / "102"
    helper.mkdir(parents=True); runner.mkdir()
    work = tmp_path / "work"; (work / "scripts").mkdir(parents=True)
    expected = tmp_path / "release" / "scripts" / "memecoin_runner_loop.py"
    helper.joinpath("cmdline").write_bytes(
        b"/usr/bin/python3\0scripts/memecoin_runtime_processes.py\0--expected\0"
        + str(expected).encode() + b"\0"
    )
    runner.joinpath("cmdline").write_bytes(
        b"/usr/bin/python3\0-u\0scripts/memecoin_runner_loop.py\0"
    )
    helper.joinpath("cwd").symlink_to(work)
    runner.joinpath("cwd").symlink_to(work)

    rows = find_memecoin_runners(expected, proc_root=proc)

    assert [row.pid for row in rows] == [102]


@pytest.mark.parametrize("explicit_manifest", [False, True])
def test_relative_cli_manifest_remains_absolute_after_child_changes_directory(
        tmp_path, monkeypatch, explicit_manifest):
    repo, sha = _repo(tmp_path)
    state = _state(tmp_path, monkeypatch)
    candidate = state / "data" / "memecoin_deploy.json"
    candidate.write_text(json.dumps(_manifest(repo, state, sha)))
    monkeypatch.chdir(state)
    argv = ["memecoin_runtime_verify.py", "--code-root", "../release", "--state-root", "."]
    if explicit_manifest:
        argv += ["--manifest", "data/memecoin_deploy.json"]
    monkeypatch.setattr(sys, "argv", argv)
    original_run = subprocess.run
    child_paths = []

    def run(command, **kwargs):
        if command[:2] == [sys.executable, "-c"]:
            child_paths.append(kwargs["env"]["SNIPER_MEMECOIN_DEPLOY_MANIFEST"])
            # The child reads only the fixture manifest; it does not load funded configuration.
            command = [sys.executable, "-c", (
                "import json, os; "
                "p = os.environ['SNIPER_MEMECOIN_DEPLOY_MANIFEST']; "
                "assert os.path.isabs(p), p; "
                "assert json.load(open(p))['sha'] == " + repr(sha))]
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as result:
        runtime_verify.main()
    assert result.value.code == 0
    assert child_paths == [str(candidate.resolve())]


@pytest.mark.parametrize("foreign_state", ["missing", "corrupt", "locked"])
def test_fixture_verification_ignores_inherited_gate_ledger(tmp_path, monkeypatch, foreign_state):
    import sqlite3
    foreign = tmp_path / "gate.db"
    lock = None
    if foreign_state == "corrupt":
        foreign.write_bytes(b"not a SQLite database")
    elif foreign_state == "locked":
        lock = sqlite3.connect(foreign)
        lock.execute("CREATE TABLE foreign_state(value)")
        lock.commit()
        lock.execute("BEGIN EXCLUSIVE")
    monkeypatch.setenv("DB_PATH", str(foreign))
    try:
        repo, _sha = _repo(tmp_path)
        state = _state(tmp_path, monkeypatch)
        assert runtime_verify.source_mirror_capability_errors(repo, state) == []
        with monkeypatch.context() as inherited:
            inherited.setenv("DB_PATH", str(foreign))
            assert runtime_verify.source_mirror_capability_errors(repo, state) == [
                "source_mirror_capability_state_unreadable"]
    finally:
        if lock is not None:
            lock.close()
