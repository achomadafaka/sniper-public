"""prune_old_releases must keep a release a cron line still names (2026-10-29: 561d114 pruned under live desk crons)."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy_memecoin_runtime.sh"


def _function():
    text = SCRIPT.read_text()
    start = text.index("prune_old_releases() {")
    return text[start:text.index("\n}\n", start) + 3]


def test_prune_keeps_cron_referenced_release(tmp_path):
    releases = tmp_path / "releases"
    for i in range(6):  # r0 newest .. r5 oldest
        d = releases / f"r{i}"
        d.mkdir(parents=True)
        os.utime(d, (1_000_000 - i * 100,) * 2)
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "crontab").write_text(f"#!/bin/sh\necho '* * * * * {releases}/r5/scripts/x.sh'\n")
    (shim / "crontab").chmod(0o755)
    (shim / "git").write_text("#!/bin/sh\n[ \"$3\" = worktree ] && [ \"$4\" = remove ] && rm -rf \"${6:-$5}\"\nexit 0\n")
    (shim / "git").chmod(0o755)
    script = (f"RELEASES={releases}; RELEASE={releases}/r0; OLD_RELEASE={releases}/r1; SOURCE_ROOT={tmp_path}; "
              f"MEMECOIN_RELEASE_KEEP=3\n{_function()}\nprune_old_releases\n")
    env = dict(os.environ, PATH=f"{shim}:/usr/bin:/bin")
    subprocess.run(["bash", "-c", script], check=True, env=env)
    left = sorted(p.name for p in releases.iterdir())
    assert left == ["r0", "r1", "r2", "r5"]  # current, previous, one in budget, cron-named r5
