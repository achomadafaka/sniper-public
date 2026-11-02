"""The conftest guards that keep one test's leftovers out of the next (operator order 2026-10-02,
"fix all failing tests"). Each check here fails if its guard stops working; the guards themselves
live in tests/_isolation_guards.py so they can be called directly, not only as fixtures."""
import importlib
import os

from pathlib import Path

from _isolation_guards import env_roots, environ_restored, main_checkout_root, settings_restored


def test_a_worktree_names_its_main_checkout(tmp_path):
    # Run from a git worktree, the operator's .env is in the MAIN checkout, not in this tree; the
    # load guard only knew this tree, so a research script's hard-coded main-checkout load leaked
    # live keys (an FMP API key showed up in a failure message, 2026-10-02).
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    wt = tmp_path / "wt1"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: %s\n" % (main / ".git" / "worktrees" / "wt1"))
    assert main_checkout_root(wt) == main.resolve()
    assert main_checkout_root(main) == main.resolve()           # the main checkout is its own


def test_a_relative_gitdir_resolves_against_the_worktree_not_the_cwd(tmp_path, monkeypatch):
    # git writes a RELATIVE gitdir under worktree.useRelativePaths (Gamma LOW2, 2026-10-02).
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    wt = tmp_path / "trees" / "wt1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../../main/.git/worktrees/wt1\n")
    monkeypatch.chdir(main)
    assert main_checkout_root(wt) == main.resolve()


def test_a_load_of_the_main_checkout_env_is_refused_from_any_tree():
    from sniper.util.envfiles import load_env_files
    roots = env_roots(Path(__file__).resolve().parents[1])
    assert roots >= {Path(__file__).resolve().parents[1]}
    for root in roots:
        assert load_env_files(str(root)) == []


def test_settings_come_back_after_direct_writes_reloads_and_in_place_edits(monkeypatch):
    from sniper.services import memecoin_entry_knobs as knobs
    from sniper.settings import memecoin as cfg
    name = next(n for n in knobs.KNOBS if hasattr(cfg, n))
    dict_name = next(k for k, v in vars(cfg).items() if isinstance(v, dict) and not k.startswith("_"))
    before, before_dict = getattr(cfg, name), dict(getattr(cfg, dict_name))
    monkeypatch.setenv("MEMECOIN_REAL_HISTORY_DAYS", "7")
    with settings_restored():
        setattr(cfg, name, object())                              # a direct write
        getattr(cfg, dict_name)["__leak__"] = 1                   # an in-place edit
        cfg.__dict__["ADDED_BY_A_TEST"] = 1                        # a new name
        importlib.reload(cfg)                                      # a reload from the env of the moment
        assert cfg.MEMECOIN_REAL_HISTORY_DAYS == 7.0
        knobs._DEFAULTS["poisoned"] = 1
    assert getattr(cfg, name) is before
    assert getattr(cfg, dict_name) == before_dict
    assert not hasattr(cfg, "ADDED_BY_A_TEST") and cfg.MEMECOIN_REAL_HISTORY_DAYS == 0.0
    assert "poisoned" not in knobs._DEFAULTS


def test_an_in_place_edit_of_a_nested_container_comes_back(monkeypatch):
    # Gamma LOW1 (2026-10-02): a shallow snapshot missed cfg.X["4663"].append(...).
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "NESTED_FOR_TEST", {"4663": ["a"]}, raising=False)
    with settings_restored():
        cfg.NESTED_FOR_TEST["4663"].append("leak")
    assert cfg.NESTED_FOR_TEST == {"4663": ["a"]}


def test_the_environment_comes_back_after_direct_writes():
    before = dict(os.environ)
    with environ_restored():
        os.environ["SNIPER_TEST_LEAK"] = "1"
        os.environ.pop(next(iter(before)), None)
    assert dict(os.environ) == before


def test_the_restore_wraps_monkeypatch(request, monkeypatch):
    # Set up before monkeypatch, torn down after it: every monkeypatch undo runs before the restore.
    names = request.fixturenames
    assert names.index("_a_isolation_restore") < names.index("monkeypatch")
