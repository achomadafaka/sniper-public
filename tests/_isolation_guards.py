"""Per-test isolation guards used by tests/conftest.py (operator order 2026-10-02, "fix all failing
tests"). Plain functions and context managers, so tests/test_conftest_isolation_guards.py can prove
each one directly.

THE MEASUREMENT. Beta's 251-file memecoin set failed 19-47 tests under xdist, a different set each
run, every one passing alone. On this host the same files passed serially in list order and in three
xdist runs, and failed 275 tests serially in REVERSED file order: a polluting test that sorts after
its victim breaks it whenever xdist happens to run it first on the same worker."""
import contextlib
import copy
import os
from pathlib import Path


def main_checkout_root(repo_root):
    """The checkout that holds the operator's .env. A git worktree's `.git` is a FILE naming
    <main>/.git/worktrees/<name>; anywhere else the repo root is its own main checkout."""
    root = Path(repo_root).resolve()
    dotgit = root / ".git"
    try:
        if dotgit.is_file():
            # Relative under git's worktree.useRelativePaths: relative to the worktree, not the cwd.
            gitdir = (root / dotgit.read_text().split("gitdir:", 1)[1].strip()).resolve()
            if gitdir.parent.name == "worktrees":
                return gitdir.parent.parent.parent.resolve()
    except (OSError, IndexError):
        pass
    return root


def env_roots(repo_root):
    """Every root whose .env / .fomo.env is the operator's: this tree and its main checkout. Run from
    a worktree, research scripts that hard-code /home/bigpapi/Desktop/Sniper loaded the MAIN
    checkout's .env into os.environ, past a guard that knew only this tree (an FMP API key showed up
    in a failure message, 2026-10-02)."""
    return {Path(repo_root).resolve(), main_checkout_root(repo_root)}


@contextlib.contextmanager
def settings_restored():
    """Leave `sniper.settings.memecoin` exactly as it was. monkeypatch undoes only what went
    through monkeypatch; three other writers outlived their test:
    - `memecoin_entry_knobs.apply_overrides` writes every registry knob straight onto the module,
      from a "pristine" capture (`_DEFAULTS`) taken once per process. A capture taken while a test
      had a knob patched made the patched value the default for good: a 0.5% source-buy mcap floor
      and MEMECOIN_REAL_GLOBAL_FEED_ENTRIES=False stayed on for every later test.
    - `importlib.reload` of the module rebinds every attribute from the environment of that moment.
    - in-place edits of a module-level dict, list or set.
    Restores identities, removes added names, puts container contents back in place (other modules
    hold the same objects), and starts and ends with an empty knob capture. Contents are snapshotted
    deep, so an edit inside a nested container comes back too; the nested objects come back as
    copies, not the same identities (no current setting nests one). ~0.4 ms."""
    from sniper.services import memecoin_entry_knobs as knobs
    from sniper.settings import memecoin as cfg
    before = {k: v for k, v in vars(cfg).items() if not k.startswith("__")}
    contents = {k: copy.deepcopy(v) for k, v in before.items() if isinstance(v, (dict, list, set))}
    knobs._DEFAULTS.clear()
    try:
        yield
    finally:
        knobs._DEFAULTS.clear()
        now = vars(cfg)
        for name in [k for k in now if not k.startswith("__") and k not in before]:
            cfg.__dict__.pop(name, None)
        for name, value in before.items():
            if now.get(name, _MISSING) is not value:
                setattr(cfg, name, value)
            if name in contents and value != contents[name]:
                if isinstance(value, list):
                    value[:] = contents[name]
                else:
                    value.clear()
                    value.update(contents[name])


_MISSING = object()
_PYTEST_OWN = ("PYTEST_CURRENT_TEST",)


@contextlib.contextmanager
def environ_restored():
    """Leave os.environ exactly as it was: a direct `os.environ[...] =`, a `load_env_files` /
    `load_dotenv` into the process, or an import that loads one, otherwise outlives its test and
    reaches every later test in the process. pytest's own variable is left to pytest."""
    before = {k: v for k, v in os.environ.items() if k not in _PYTEST_OWN}
    try:
        yield
    finally:
        for key in [k for k in os.environ if k not in before and k not in _PYTEST_OWN]:
            del os.environ[key]
        for key, value in before.items():
            if os.environ.get(key) != value:
                os.environ[key] = value
