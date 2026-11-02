"""A test process never resolves DB_PATH to the funded ledger, in an xdist worker too.

FOUND 2026-09-27 by the ledger write-lock sampler: every pytest-xdist worker took the WAL write
lock of the real sniper_tracker.db exactly once (1,578 workers, 1,578 holds, 22:17-00:17Z).

THE CHAIN. The controller runs conftest first: it creates a throwaway SNIPER_STATE_ROOT and points
DB_PATH at `<root>/test.db`, and the workers inherit both. In the worker,
`_neutralise_operator_env` then pops every key the operator's `.env` names, and `.env` names
DB_PATH. `_isolate_state_root_from_operator_env` returned early on the inherited root and never put
DB_PATH back. conftest then imports `sniper.web.server`, whose module body runs
`init_db(get_connection())` on the relative default "sniper_tracker.db". That is DDL on the funded
ledger from every worker, and any test that resolved DB_PATH read or wrote the real ledger.

Only in the shared checkout: the release tree has no `.env`, so nothing is popped and the deploy
gate never saw it. The first test reproduces the worker's condition without needing a `.env`.
"""
import os
import pathlib

import pytest

_ROOT_CONFTEST = pathlib.Path(__file__).resolve().parent / "conftest.py"
_LEDGER = (_ROOT_CONFTEST.parents[1] / "sniper_tracker.db").resolve()


@pytest.fixture
def root_conftest(request):
    """The loaded tests/conftest.py, from pytest's own plugin registry. `import conftest` is
    ambiguous (tests/wallet_protocol has its own) and some tests drop it from sys.modules."""
    for plugin in request.config.pluginmanager.get_plugins():
        f = getattr(plugin, "__file__", None)
        if f and pathlib.Path(f).resolve() == _ROOT_CONFTEST:
            return plugin
    raise AssertionError("tests/conftest.py is not registered in this process")


def test_an_inherited_state_root_still_gets_a_throwaway_db_path(monkeypatch, tmp_path, root_conftest):
    # The worker, exactly: the root came from the controller and DB_PATH was popped.
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DB_PATH", raising=False)
    root_conftest._isolate_state_root_from_operator_env()
    assert os.environ.get("DB_PATH") == str(tmp_path / "test.db")
    assert os.environ["SNIPER_STATE_ROOT"] == str(tmp_path), "a given root is kept, not replaced"


def test_a_fresh_process_gets_a_throwaway_root_and_db_path(monkeypatch, root_conftest):
    monkeypatch.delenv("SNIPER_STATE_ROOT", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    root_conftest._isolate_state_root_from_operator_env()
    root = os.environ["SNIPER_STATE_ROOT"]
    assert os.environ["DB_PATH"] == os.path.join(root, "test.db")
    assert pathlib.Path(os.environ["DB_PATH"]).resolve() != _LEDGER


def test_an_explicit_db_path_is_kept(monkeypatch, tmp_path, root_conftest):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chosen.db"))
    root_conftest._isolate_state_root_from_operator_env()
    assert os.environ["DB_PATH"] == str(tmp_path / "chosen.db")


def test_this_process_does_not_resolve_the_funded_ledger():
    """In place, in whichever process runs it: controller, `-n0`, or an xdist worker."""
    from sniper.persistence.db_core import get_db_path
    assert pathlib.Path(get_db_path()).resolve() != _LEDGER
