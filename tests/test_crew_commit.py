"""A guarded commit preserves foreign staged/working data and holds the lock through hooks."""
import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/crew_commit.sh"


def test_crew_commit(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "--quiet")
    git("config", "user.name", "Crew fixture")
    git("config", "user.email", "crew-fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(tmp_path / ".git/hooks"))
    (tmp_path / "index").mkdir()
    (tmp_path / "owned file.txt").write_text("owned before\n")
    (tmp_path / "foreign.txt").write_text("foreign before\n")
    (tmp_path / "index/dependency_index.json").write_text('{"v":0}\n')
    git("add", "--", "owned file.txt", "foreign.txt", "index/dependency_index.json")
    git("commit", "--quiet", "-m", "baseline")
    (tmp_path / "owned file.txt").write_text("owned after\n")
    (tmp_path / "foreign.txt").write_text("foreign staged\n")
    git("add", "--", "foreign.txt")
    staged = git("diff", "--cached", "--binary")
    head = git("rev-parse", "HEAD")
    assert SCRIPT.is_file(), "crew commit wrapper has not been implemented"
    assert os.access(SCRIPT, os.X_OK), "wrapper must be directly executable"
    refused = subprocess.run([str(SCRIPT), "owned change", "owned file.txt"], cwd=tmp_path,
                             capture_output=True, text=True, timeout=45)
    assert refused.returncode != 0
    assert "foreign.txt" in refused.stderr and "Alpha" in refused.stderr
    assert git("diff", "--cached", "--binary") == staged
    assert git("rev-parse", "HEAD") == head
    assert (tmp_path / "foreign.txt").read_text() == "foreign staged\n"
    git("commit", "--quiet", "-m", "fixture foreign baseline")
    (tmp_path / "foreign.txt").write_text("foreign unstaged\n")
    for bad in (".", "../escape", "*.txt"):
        invalid = subprocess.run([str(SCRIPT), "invalid scope", bad], cwd=tmp_path,
                                 capture_output=True, text=True, timeout=45)
        assert invalid.returncode != 0
        assert git("diff", "--cached", "--name-only") == ""
    hook = tmp_path / ".git/hooks/pre-commit"
    hook.write_text("""#!/bin/sh
if flock -n /home/bigpapi/.cache/sniper-crew-git.lock true; then
    echo 'crew lock was released before the hook' >&2
    exit 1
fi
printf '{"v":1}\\n' > index/dependency_index.json
git add -- index/dependency_index.json
""")
    hook.chmod(0o700)
    committed = subprocess.run([str(SCRIPT), "owned change", "owned file.txt"], cwd=tmp_path,
                               capture_output=True, text=True, timeout=45)
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert committed.stdout.splitlines()[-1] == git("rev-parse", "HEAD")
    assert git("diff", "--cached", "--name-only") == ""
    assert git("show", "HEAD:owned file.txt") == "owned after"
    assert git("show", "HEAD:index/dependency_index.json") == '{"v":1}'
    assert git("status", "--porcelain", "--", "owned file.txt", "index") == ""
    assert git("show", "HEAD:foreign.txt") == "foreign staged"
    assert (tmp_path / "foreign.txt").read_text() == "foreign unstaged\n"
    assert git("status", "--porcelain", "--", "foreign.txt") == "M foreign.txt"
    head = git("rev-parse", "HEAD")
    hook.write_text("#!/bin/sh\nexit 1\n")
    (tmp_path / "owned file.txt").write_text("hook failure\n")
    failed = subprocess.run([str(SCRIPT), "must fail", "owned file.txt"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=45)
    assert failed.returncode != 0 and git("rev-parse", "HEAD") == head
    assert git("diff", "--cached", "--name-only") == ""
    assert (tmp_path / "owned file.txt").read_text() == "hook failure\n"
    assert "your files are unstaged and untouched" in failed.stderr
    assert (tmp_path / "foreign.txt").read_text() == "foreign unstaged\n"
    (tmp_path / "owned file.txt").write_text("owned after\n")
    unchanged = subprocess.run([str(SCRIPT), "no change", "owned file.txt"], cwd=tmp_path,
                               capture_output=True, text=True, timeout=45)
    assert unchanged.returncode != 0 and "no change to commit" in unchanged.stderr
    assert git("diff", "--cached", "--name-only") == ""
    (tmp_path / "owned file.txt").write_text("scope failure\n")
    scope = subprocess.run([str(SCRIPT), "scope mismatch", "owned file.txt", "index/dependency_index.json"],
                           cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert scope.returncode != 0 and git("diff", "--cached", "--name-only") == ""
    assert (tmp_path / "owned file.txt").read_text() == "scope failure\n"
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("keep ignored\n")
    add_failed = subprocess.run([str(SCRIPT), "add must fail", "owned file.txt", "ignored.txt"],
                                cwd=tmp_path, capture_output=True, text=True, timeout=45)
    assert add_failed.returncode != 0 and git("rev-parse", "HEAD") == head
    assert git("diff", "--cached", "--name-only") == ""
    assert (tmp_path / "owned file.txt").read_text() == "scope failure\n"
    assert (tmp_path / "ignored.txt").read_text() == "keep ignored\n"
