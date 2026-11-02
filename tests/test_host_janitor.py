import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location("host_janitor", Path(__file__).resolve().parents[1] / "scripts/host_janitor.py")
janitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(janitor)


def test_release_rule_spares_referenced_new_and_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor, "RUNTIME", tmp_path)
    releases = tmp_path / "comp" / "releases"
    now = 1_000_000_000
    for i in range(8):  # r0 newest .. r7 oldest, all past the age floor
        d = releases / f"r{i}"
        d.mkdir(parents=True)
        os.utime(d, (now - (i + 1) * 10 * 86400,) * 2)
    os.symlink(releases / "r6", tmp_path / "comp" / "current")
    refs = os.path.realpath(tmp_path / "comp" / "current")
    out = {d.name for d, _ in janitor.stale_releases(keep=5, min_age_d=7, now=now, refs=refs)}
    assert out == {"r5", "r7"}  # r0-r4 newest kept, r6 is current


def test_branch_rule_deletes_only_idle_merged_and_flags_old_unmerged(tmp_path):
    import subprocess

    def g(*a, date=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        if date:
            env.update(GIT_AUTHOR_DATE=f"@{date} +0000", GIT_COMMITTER_DATE=f"@{date} +0000")
        return subprocess.run(["git", *a], cwd=tmp_path, env=env, check=True, capture_output=True, text=True).stdout

    now, day = 1_800_000_000, 86400
    g("init", "-q", "-b", "main")
    g("commit", "-q", "--allow-empty", "-m", "base", date=now - 10 * day)
    g("branch", "merged-old")
    g("commit", "-q", "--allow-empty", "-m", "fresh", date=now - 3600)
    g("branch", "merged-fresh")  # merged but only an hour old: may be a just-created work branch
    for name, age in (("unmerged-old", 5 * day), ("unmerged-new", day)):
        g("checkout", "-q", "-b", name, "main")
        g("commit", "-q", "--allow-empty", "-m", name, date=now - age)
    g("checkout", "-q", "main")
    delete, flag = janitor.stale_branches("main", idle_h=48, flag_d=3, now=now, cwd=tmp_path)
    assert delete == ["merged-old"]
    assert [b for b, _ in flag] == ["unmerged-old"]


def test_tmp_rule_takes_only_idle_crew_trees_nobody_is_in(tmp_path):
    now = 1_000_000_000
    for name, age_h in (("zeta-old", 20), ("zeta-new", 1), ("other-old", 20), ("beta-busy", 20), ("codex-daemon-1000", 20), ("zeta-worktree", 20)):
        d = tmp_path / name
        d.mkdir()
        os.utime(d, (now - age_h * 3600,) * 2)
    (tmp_path / "gamma-file").write_text("x")
    (tmp_path / "zeta-worktree" / ".git").write_text("gitdir: x")
    os.utime(tmp_path / "zeta-worktree", (now - 20 * 3600,) * 2)
    busy = tmp_path / "beta-busy"
    cwd = os.getcwd()
    os.chdir(busy)
    try:
        out = {p.name for p, _ in janitor.stale_tmp_trees(tmp_path, 12, now)}
    finally:
        os.chdir(cwd)
    assert out == {"zeta-old"}
