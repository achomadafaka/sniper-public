"""A restart is not a deploy.

On 2026-08-26 the web was restarted to clear a stale settings snapshot and came back on a release
73 commits old: fresh ENV, old CODE. The single-contract-writer fix was reported live and was
absent from the tree being served, and so was the unreadable-USDC fix whose whole point was to
protect the web path after a restart. The procedure to actually deploy the web existed only inside
one plan document's step, which is why hands reached for the watchdog instead.
"""
from __future__ import annotations

import pathlib
import os
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path("scripts/deploy_web_runtime.sh")
CHECKS = pathlib.Path("scripts/post_deploy_checks.sh")


def _body():
    return SCRIPT.read_text()


def test_the_script_exists_and_is_executable():
    """The finding was that this procedure lived in prose. Prose does not run."""
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "a deploy procedure must be runnable, not readable"


def test_it_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_it_refuses_a_sha_that_is_not_a_full_lowercase_hash():
    for bad in ("HEAD", "0b1f9251", "0B1F9251C74AF34A7C2A76C5D813C0D35A0C8542"):
        out = subprocess.run(["bash", str(SCRIPT), bad], capture_output=True, text=True)
        assert out.returncode != 0, f"{bad} must be refused"


def test_a_dry_run_changes_nothing():
    sha = subprocess.run(["git", "rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()
    before = pathlib.Path(".runtime/web/current").resolve(strict=False)
    out = subprocess.run(["bash", str(SCRIPT), sha, "--dry-run"], capture_output=True, text=True)
    assert out.returncode == 0 and "dry run" in out.stdout
    assert pathlib.Path(".runtime/web/current").resolve(strict=False) == before


@pytest.mark.parametrize("use_state,override", [(True, False), (True, True), (False, False)])
def test_web_runtime_uses_shared_state_unless_explicitly_overridden(tmp_path, use_state, override):
    repo = pathlib.Path.cwd().resolve()
    sha = subprocess.check_output(["git", "rev-parse", "origin/main"], text=True).strip()
    state, custom = tmp_path / "shared state", tmp_path / "custom web"
    env = dict(os.environ, SNIPER_SOURCE_ROOT=str(repo))
    env.pop("SNIPER_STATE_ROOT", None)
    env.pop("SNIPER_WEB_RUNTIME", None)
    if use_state:
        env["SNIPER_STATE_ROOT"] = str(state)
    if override:
        env["SNIPER_WEB_RUNTIME"] = str(custom)
    expected = custom if override else (state if use_state else repo) / ".runtime" / "web"
    result = subprocess.run(["bash", str(SCRIPT.resolve()), sha, "--dry-run"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "would materialize " + str(expected / "releases" / sha) in result.stdout
    assert not state.exists() and not custom.exists()


@pytest.mark.parametrize("case", ["default", "local", "no_remote", "missing", "dirty", "short"])
def test_web_local_commit_opt_in_preserves_source_guards(tmp_path, case):
    from test_memecoin_runtime_deploy import _run, _setup

    source, _state, _runtime, env, _sha = _setup(tmp_path)
    (source / "tracked.txt").write_text("local reviewed change\n")
    _run(["git", "commit", "-qam", "local only"], source)
    sha = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()
    if case == "no_remote":
        _run(["git", "update-ref", "-d", "refs/remotes/origin/main"], source)
    elif case == "missing":
        sha = "f" * 40
    elif case == "dirty":
        (source / "tracked.txt").write_text("unreviewed tracked change\n")
    elif case == "short":
        sha = sha[:8]
    web = tmp_path / "web"
    args = ["bash", str(SCRIPT.resolve()), sha, "--dry-run"]
    if case != "default":
        args.append("--allow-local-commit")
    result = _run(args, source, env | {"SNIPER_WEB_RUNTIME": str(web)}, False)
    if case in {"local", "no_remote"}:
        assert result.returncode == 0, result.stderr
        assert "dry run" in result.stdout
    else:
        assert result.returncode != 0
        expected = {"default": "not reachable from origin/main", "missing": "commit does not exist",
                    "dirty": "tracked changes", "short": "full lowercase"}
        assert expected[case] in result.stderr
    assert not web.exists()


# ------------------------------------------------------------------ ordering is the safety

def test_the_candidate_is_smoke_tested_before_anything_is_flipped():
    """A syntax error must be caught while the OLD release is still serving."""
    body = _body()
    assert body.index("import smoke test") < body.index("ATOMIC FLIP")


def test_the_rollback_trap_is_armed_before_the_flip():
    body = _body()
    assert body.index("trap rollback EXIT") < body.index('mv -Tf "$CURRENT.next.$$"')


def test_the_flip_precedes_the_cycle():
    """The supervisor must bring the service back THROUGH the new symlink, so the flip has to
    already have happened when the old process dies."""
    body = _body()
    assert body.index("SWITCHED=1") < body.index("$CYCLE\" -eq 1 ]; then")


def test_the_symlink_is_replaced_atomically():
    """`ln` then `mv -Tf` is one rename; a reader never sees the pointer absent."""
    body = _body()
    assert 'ln -s "$RELEASE" "$CURRENT.next.$$"' in body
    assert 'mv -Tf "$CURRENT.next.$$" "$CURRENT"' in body


# ------------------------------------------------------------------ proving it afterwards

def test_it_proves_the_deploy_from_production_not_from_branch_state():
    body = _body()
    assert 'readlink -f "$CURRENT"' in body
    assert "never from branch state" in body


def test_it_also_checks_the_serving_process_started_after_the_flip():
    """THE HALF THAT WOULD HAVE CAUGHT THE ORIGINAL MISS. argv points at the SYMLINK, so resolving
    `current` says what a NEWLY started process picks up — a process running from before the flip
    still holds whatever it imported at its own start. Either fact alone lies."""
    body = _body()
    assert "FLIPPED_AT" in body
    assert 'STARTED" -ge "$FLIPPED_AT' in body
    assert "started before the flip" in body


def test_it_fails_loudly_when_the_service_does_not_come_back():
    assert "web did not come back after the cycle" in _body()


# ------------------------------------------------------------------ the drift warning

def _drift_block():
    body = CHECKS.read_text()
    return body[body.index("Runtime release drift"):body.index("# \u2500\u2500 Summary")]


def test_post_deploy_warns_on_release_drift_and_never_fails_on_it():
    """Lag is sometimes deliberate, so this may not block. Silent lag is what must end."""
    block = _drift_block()
    assert "WARN" in block
    assert "((FAIL++" not in block, "release drift must not fail the gate"
    assert "exit 1" not in block


def test_the_drift_check_enumerates_every_runtime_tree_rather_than_naming_two():
    """REGRESSION on my own checker. The first version named `web` and `memecoin` — a two-item
    maintained list, which is the same shape the check exists to catch. There are FIVE trees
    (hyperliquid, memecoin, opportunity, v6, web); a readdir replaces the list with the fact."""
    block = _drift_block()
    assert "for TREE_DIR in .runtime/*/" in block
    for named in ('"web"', "'web'", '"memecoin"', "'memecoin'"):
        assert named not in block, f"the check must not hard-code {named}"


def test_live_detection_matches_the_symlink_as_well_as_the_resolved_release():
    """A process launched through `current` carries the SYMLINK in argv, so matching only the
    resolved release path reads a live tree as dead — measured: the web read as 0 live processes
    while it was serving."""
    block = _drift_block()
    assert "LIVE_SYM" in block and "/current" in block


def test_a_dead_stale_tree_is_reported_as_latent_rather_than_as_a_warning():
    """Severity split, the same one the funded reconciliation uses: two LIVE trees on different
    releases is the active defect; a dead tree that is behind is latent until someone launches it."""
    block = _drift_block()
    assert "latent" in block
    assert "no live process" in block


def test_the_drift_warning_names_the_missing_commits_not_just_a_count():
    """A bare count is ignorable. The commits are what make it actionable."""
    block = _drift_block()
    assert "git log --oneline" in block
    assert "a restart is not a deploy" in block


def test_the_warning_tells_the_reader_how_to_verify_from_production():
    block = _drift_block()
    assert "readlink -f .runtime/<tree>/current" in block
    assert "started AFTER the flip" in block


def test_the_drift_check_runs_against_the_live_symlinks():
    """Executes the real enumeration rather than asserting on its source."""
    runtime = pathlib.Path(".runtime")
    if not runtime.is_dir():
        pytest.skip("no runtime trees on this host")
    trees = [d for d in runtime.iterdir() if (d / "current").exists()]
    assert trees, "no runtime tree carries a current pointer"
    for tree in trees:
        sha = (tree / "current").resolve().name
        out = subprocess.run(["git", "rev-list", "--count", f"{sha}..origin/main"],
                             capture_output=True, text=True)
        assert out.returncode == 0, f"{tree.name} points at an unknown SHA {sha}"


# ------------------------------------------------------------------ which processes are the web

def _helper():
    """The two helper lines, verbatim from the script."""
    lines = [l for l in _body().splitlines() if l.startswith(("WEB_ARGV_RE=", "web_pids()"))]
    assert len(lines) == 2, lines
    return "\n".join(lines)


def _web_pids():
    out = subprocess.run(["bash", "-c", 'ENTRY="sniper/web/server.py"\n' + _helper() + "\nweb_pids"],
                         capture_output=True, text=True, check=True).stdout
    return {int(p) for p in out.split()}


LIVE_ARGV = ("/home/bigpapi/.pyenv/versions/3.13.0/bin/python3 "
             "/home/bigpapi/Desktop/Sniper/.runtime/web/current/sniper/web/server.py")


@pytest.mark.parametrize("argv,is_web", [
    (LIVE_ARGV, True),                                      # the live web, 2026-09-27
    ("/usr/bin/python3.13 /srv/tree/sniper/web/server.py", True),
    ("/bin/bash -c source snapshot.sh && until readlink x/sniper/web/server.py; do sleep 15; done",
     False),                                                # a harness session waiter
    ("grep -F sniper/web/server.py logs/web.log", False),
    ("tail -f /home/bigpapi/Desktop/Sniper/.runtime/web/current/sniper/web/server.py", False),
    ('/usr/bin/python3 -c "import ast; ast.parse(open(\'x/sniper/web/server.py\').read())"', False),
    ("/usr/bin/python3 -m sniper.web.server", False),       # not how the unit runs it
    (LIVE_ARGV + " --extra", False),
])
def test_the_web_pattern_matches_the_unit_argv_and_nothing_that_names_it(argv, is_web):
    """sniper-web.service ExecStart (both the user and system unit drop-ins, 2026-09-27) is exactly
    LIVE_ARGV. The old bare `pgrep -f` on the entry matched every row here."""
    pattern = subprocess.run(["bash", "-c", 'ENTRY="sniper/web/server.py"\n' + _helper()
                              + '\nprintf %s "$WEB_ARGV_RE"'], capture_output=True, text=True,
                             check=True).stdout
    matched = subprocess.run(["grep", "-Eq", "--", pattern], input=argv, text=True).returncode == 0
    assert matched is is_web, (argv, pattern)


def test_the_real_helper_finds_a_web_shaped_process_and_skips_the_decoys(tmp_path):
    """Real processes, real pgrep. Decoys NAME the entry the way today's killed waiters did."""
    entry = tmp_path / "tree" / "sniper" / "web" / "server.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("import time\ntime.sleep(30)\n")
    procs = {}
    try:
        procs["web"] = subprocess.Popen([sys.executable, str(entry)])
        procs["bash_waiter"] = subprocess.Popen(["bash", "-c", f"sleep 30; : {entry}"])
        procs["python_c"] = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep(30)  # {entry}"])
        procs["tail"] = subprocess.Popen(["tail", "-f", str(entry)], stdout=subprocess.DEVNULL)
        procs["grep"] = subprocess.Popen(["grep", "-F", "sniper/web/server.py"],
                                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        import time as _t
        _t.sleep(0.3)
        found = _web_pids()
        assert procs["web"].pid in found
        for name in ("bash_waiter", "python_c", "tail", "grep"):
            assert procs[name].pid not in found, name
    finally:
        for p in procs.values():
            p.kill()
            p.wait()


def test_all_three_sites_use_the_one_helper():
    body = _body()
    assert body.count("$(web_pids)") == 3
    assert "pgrep -f \"$ENTRY\"" not in body


# ------------------------------------------------------------------ release retention

def _prune_fn():
    body = _body()
    start = body.index("prune_old_releases() {")
    return body[start:body.index("\n}\n", start) + 3]


def test_prune_runs_only_after_success():
    body = _body()
    assert body.index("trap - EXIT") < body.index("prune_old_releases || true")


def test_prune_keeps_current_previous_and_newest_others(tmp_path):
    """121 web releases held 27 GB (2026-09-28). Current and previous must survive even when they
    are the OLDEST releases on disk, which is the case the budget alone would get wrong."""
    import os
    import time
    src = tmp_path / "src"
    src.mkdir()
    run = lambda *a, **k: subprocess.run(a, check=True, capture_output=True, text=True, **k)
    run("git", "init", "-q", str(src))
    run("git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", "x")
    releases = tmp_path / "releases"
    releases.mkdir()
    names = [f"r{i}" for i in range(8)]                 # r0 oldest ... r7 newest
    now = time.time()
    for i, n in enumerate(names):
        run("git", "-C", str(src), "worktree", "add", "-q", "--detach", str(releases / n))
        os.utime(releases / n, (now - 1000 + i, now - 1000 + i))
    current = tmp_path / "current"
    current.symlink_to(releases / "r0")                  # oldest on disk
    script = ("set -euo pipefail\n" + _prune_fn() + "\nprune_old_releases\n")
    env = dict(os.environ, SOURCE_ROOT=str(src), CURRENT=str(current),
               OLD_RELEASE=str(releases / "r1"), RELEASES=str(releases), WEB_RELEASE_KEEP="5")
    subprocess.run(["bash", "-c", "SOURCE_ROOT=$SOURCE_ROOT CURRENT=$CURRENT OLD_RELEASE=$OLD_RELEASE "
                    "RELEASES=$RELEASES\n" + script], env=env, check=True)
    left = sorted(p.name for p in releases.iterdir())
    assert left == ["r0", "r1", "r5", "r6", "r7"], left
