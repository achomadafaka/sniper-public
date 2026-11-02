"""Trusted code provenance is frozen beside the exact JUDGE sheet, never supplied by a model."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from sniper.services import memecoin_desk_judge as J
from test_memecoin_desk_judge import Clock, enter, model_returning, ready_ttl
from test_memecoin_desk_pipeline import databases  # noqa: F401

SHA = "0123456789abcdef" * 2 + "01234567"
SPOOF = "f" * 40


@pytest.mark.parametrize("kind", ["answer", "skip", "error"])
@pytest.mark.parametrize("release_sha", [SHA, None])
def test_saved_result_keeps_trusted_release_and_diagnostics(databases, kind, release_sha):
    ticket = ready_ttl(databases)
    calls, clock = [], Clock()
    model = model_returning(enter, calls=calls, clock=clock,
                            call_error="timeout" if kind == "error" else None)

    def spoof(*args, **kwargs):
        return {**model(*args, **kwargs), "release_sha": SPOOF}

    J.run_once(databases[0], lambda: nullcontext(databases[1]), call_model=spoof,
               system_prompt="SYS", worker="w", now_fn=clock, calls_per_hour=100,
               admit_fn=lambda _: kind != "skip", release_sha=release_sha)
    raw = databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()
    result = json.loads(raw[0])
    assert result["release_sha"] == release_sha
    assert result["age_at_call_s"] >= 0
    assert len(calls) == (0 if kind == "skip" else 1)
    if kind == "skip":
        assert result["call_error"] == "budget_skipped"
    else:
        assert result["sheet_sha256"] and result["sheet_zb64"] and result["system_sha256"]
        assert result["call_error"] == ("timeout" if kind == "error" else None)


def test_legacy_call_overrides_untrusted_model_stamp_with_none(databases):
    ticket = ready_ttl(databases)
    model = model_returning(enter, calls=[], clock=Clock())
    J.run_once(databases[0], lambda: nullcontext(databases[1]),
               call_model=lambda *a, **kw: {**model(*a, **kw), "release_sha": SPOOF},
               system_prompt="SYS", worker="w", now_fn=Clock(), calls_per_hour=100)
    result = databases[0].execute("SELECT result_json FROM desk_judge_calls WHERE ticket_id=?", (ticket,)).fetchone()
    assert json.loads(result[0])["release_sha"] is None


@pytest.mark.parametrize("bad", ["", SHA.upper(), SHA[:-1], SHA + "0", 0, False, [], SHA + "\n"])
def test_invalid_release_refuses_before_claim_or_model(databases, bad):
    ready_ttl(databases)

    def forbidden(*args, **kwargs):
        pytest.fail("invalid identity must refuse before dispatch")

    with pytest.raises(ValueError, match="release_sha"):
        J.run_once(databases[0], forbidden, call_model=forbidden, system_prompt="SYS", worker="w",
                   now_fn=Clock(), calls_per_hour=100, release_sha=bad)
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0


def worker():
    path = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_judge_worker.py"
    spec = importlib.util.spec_from_file_location("jw_release_stamp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned_fixture(tmp_path, monkeypatch):
    from scripts import memecoin_runtime_verify as verifier
    state = tmp_path / "state"
    code = state / ".runtime" / "memecoin" / "releases" / SHA
    code.mkdir(parents=True)
    gitdir = state / ".git" / "worktrees" / SHA
    gitdir.mkdir(parents=True)
    (code / ".git").write_text("gitdir: " + str(gitdir) + "\n")
    (gitdir / "gitdir").write_text(str(code / ".git") + "\n")
    (gitdir / "HEAD").write_text(SHA + "\n")
    (gitdir / "locked").write_text("retained reviewed paper release\n")
    calls = []

    def git(root, *args):
        calls.append((root, args))
        return 0, {("rev-parse", "HEAD"): SHA,
                   ("rev-parse", "--show-toplevel"): str(code),
                   ("rev-parse", "--absolute-git-dir"): str(gitdir)}.get(args, ""), ""

    monkeypatch.setattr(verifier, "_git", git)
    W = worker()
    monkeypatch.setattr(W, "ROOT", code)
    monkeypatch.setattr(W, "state_root", lambda: state)
    return W, code, gitdir, calls


@pytest.mark.parametrize("failure", [None, "unlocked", "wrong_head", "attached_head", "wrong_backlink",
                                     "missing_marker", "corrupt_marker", "checkout", "dirty", "git_error",
                                     "foreign_toplevel", "foreign_gitdir"])
def test_worker_only_stamps_verified_own_pin(tmp_path, monkeypatch, failure):
    from scripts import memecoin_runtime_verify as verifier
    W, code, gitdir, calls = pinned_fixture(tmp_path, monkeypatch)
    if failure == "unlocked":
        (gitdir / "locked").unlink()
    elif failure == "wrong_head":
        (gitdir / "HEAD").write_text(SPOOF + "\n")
    elif failure == "attached_head":
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    elif failure == "wrong_backlink":
        (gitdir / "gitdir").write_text(str(tmp_path / "foreign" / ".git"))
    elif failure == "missing_marker":
        (code / ".git").unlink()
    elif failure == "corrupt_marker":
        (code / ".git").write_bytes(b"\xff")
    elif failure == "checkout":
        monkeypatch.setattr(W, "ROOT", tmp_path)
    elif failure in ("dirty", "git_error", "foreign_toplevel", "foreign_gitdir"):
        original_git = verifier._git

        def altered_git(root, *args):
            if args == ("status", "--porcelain", "--untracked-files=no"):
                if failure == "git_error":
                    return 1, "", "failed"
                if failure == "dirty":
                    return 0, " M tracked.py", ""
            if args == ("rev-parse", "--show-toplevel") and failure == "foreign_toplevel":
                return 0, str(tmp_path / "foreign"), ""
            if args == ("rev-parse", "--absolute-git-dir") and failure == "foreign_gitdir":
                return 0, str(tmp_path / "foreign" / ".git"), ""
            return original_git(root, *args)

        monkeypatch.setattr(verifier, "_git", altered_git)
    # An unrelated funded manifest is deliberately wrong: the worker must never read it.
    manifest = tmp_path / "state" / "data" / "memecoin_deploy.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"sha": SPOOF, "release_path": "/funded"}))
    assert W.pinned_release_sha() == (SHA if failure is None else None)
    if failure is None:
        assert len(calls) == 4 and all(root == code for root, _ in calls)


def test_real_ambient_git_override_cannot_stamp_another_tree(tmp_path, monkeypatch):
    """A dirty retained pin stays unknown when Git sees a clean sibling at the same commit."""
    from scripts.memecoin_runtime_verify import _git
    state = tmp_path / "state"
    state.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=state, check=True, text=True,
                              capture_output=True).stdout.strip()

    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(key, raising=False)
    git("init", "-q")
    (state / "tracked.txt").write_text("same commit\n")
    git("add", "--", "tracked.txt")
    git("-c", "user.name=Offline fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
    sha = git("rev-parse", "HEAD")
    code = state / ".runtime" / "memecoin" / "releases" / sha
    code.parent.mkdir(parents=True)
    git("worktree", "add", "--detach", str(code), sha)
    git("worktree", "lock", "--reason", "offline test pin", str(code))
    gitdir = state / ".git" / "worktrees" / sha
    W = worker()
    monkeypatch.setattr(W, "ROOT", code)
    monkeypatch.setattr(W, "state_root", lambda: state)
    assert W.pinned_release_sha() == sha
    (code / "tracked.txt").write_text("dirty pin\n")
    assert W.pinned_release_sha() is None
    monkeypatch.setenv("GIT_DIR", str(gitdir))
    monkeypatch.setenv("GIT_WORK_TREE", str(state))
    assert _git(code, "rev-parse", "HEAD")[1] == sha
    assert _git(code, "status", "--porcelain", "--untracked-files=no")[:2] == (0, "")
    assert W.pinned_release_sha() is None


def test_worker_passes_startup_identity_unchanged_across_loop(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sniper.persistence import db_core, db_memecoin as ledger
    from sniper.util import state_paths
    W = worker()
    resolved, passed = [], []

    def resolve():
        resolved.append(1)
        assert len(resolved) == 1
        return SHA

    def run_once(*args, **kwargs):
        passed.append(kwargs["release_sha"])
        if len(passed) == 2:
            raise KeyboardInterrupt
        return []

    for key, value in {"MEMECOIN_DESK_JUDGE_ENABLED": "1", "MEMECOIN_DESK_JUDGE_WARM_SPARE": "0",
                       "MEMECOIN_DESK_JUDGE_ASOF": "0", "MEMECOIN_DESK_JUDGE_STRUCTURE": "0",
                       "MEMECOIN_DESK_JUDGE_CONTEXT": "0"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(W, "pinned_release_sha", resolve)
    monkeypatch.setattr(W, "load_env_files", lambda *a: None)
    monkeypatch.setattr(W, "state_root", lambda: tmp_path)
    monkeypatch.setattr(state_paths, "require_state_root_outside_release", lambda: None)
    monkeypatch.setattr(W.signal, "signal", lambda *a: None)
    monkeypatch.setattr(W.time, "sleep", lambda *a: None)
    monkeypatch.setattr(W, "start_dump_cache", lambda: None)
    monkeypatch.setattr(W.store, "connect", lambda *a: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(db_core, "get_connection", lambda *a: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(ledger, "desk_cohort_gates", lambda *a: [])
    monkeypatch.setattr(W.J, "ranker_gate", lambda *a: None)
    monkeypatch.setattr(W.J, "recover", lambda *a, **kw: None)
    monkeypatch.setattr(W.J, "close_orphan_claims", lambda *a: [])
    monkeypatch.setattr(W.J, "run_once", run_once)
    for name in ("standing_pass", "courier_pass", "page_refusals", "note_holds", "note_aged"):
        monkeypatch.setattr(W, name, lambda *a, **kw: [])
    (tmp_path / "ledger.db").touch()
    playbook = tmp_path / "playbook.md"
    playbook.write_text("prompt_version: `desk-judge-v24`\n---\nOffline prompt\n")
    assert W.main(["--desk-db", str(tmp_path / "desk.db"), "--ledger-db", str(tmp_path / "ledger.db"),
                   "--playbook", str(playbook), "--calls-per-hour", "40", "--loop"], env_root=tmp_path) == 0
    assert resolved == [1] and passed == [SHA, SHA]
