"""Every deferred import on the funded path must actually resolve.

WHY THIS EXISTS. On 2026-08-29 a new module was created at an existing module's path and `cat >`
overwrote it. The deployed runner then threw

    ImportError: cannot import name 'post_tp_pass' from 'sniper.services.memecoin_post_tp_followup'

on every pass. Nothing caught it before deploy: the import sits INSIDE a function body, so it is
invisible to the import graph, to a linter following module-level imports, and to every test that
does not happen to execute that branch. A lazy import is a runtime dependency with no compile-time
witness.

The runner defers imports deliberately and correctly — startup cost, optional subsystems, circular
edges — so the fix is not to hoist them. It is to check them.
"""
import ast
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The funded money path plus the workers it starts. Widening this is welcome; these are the files
# where a dead import costs something.
FUNDED = [
    "scripts/memecoin_runner_loop.py",
    "sniper/services/memecoin_real_runner.py",
    "sniper/services/memecoin_risk.py",
    "sniper/services/memecoin_exits.py",
]


def _deferred_imports(path):
    """(module, name, lineno) for every `from X import a, b` nested inside a function."""
    tree = ast.parse(path.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module and inner.level == 0:
                if not inner.module.startswith("sniper"):
                    continue          # third-party absence is a packaging problem, not ours
                for alias in inner.names:
                    out.append((inner.module, alias.name, inner.lineno))
    return out


@pytest.mark.parametrize("rel", FUNDED)
def test_every_deferred_sniper_import_resolves(rel):
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    broken = []
    for module, name, lineno in _deferred_imports(path):
        try:
            mod = importlib.import_module(module)
        except Exception as exc:
            broken.append(f"{rel}:{lineno} cannot import module {module} ({exc})")
            continue
        if hasattr(mod, name):
            continue
        # `from package import submodule` binds a MODULE, and hasattr cannot see it until the
        # submodule is loaded. Not a defect — checking the attribute alone reported every one of
        # these as broken on the first run.
        try:
            importlib.import_module(f"{module}.{name}")
        except Exception:
            broken.append(f"{rel}:{lineno} {module} has no {name!r}")
    assert not broken, "deferred imports that would raise at runtime:\n  " + "\n  ".join(broken)


def test_the_guard_actually_looks_at_something():
    """A scanner that found nothing would pass every file silently — the vacuous-guard shape."""
    found = sum(len(_deferred_imports(ROOT / rel)) for rel in FUNDED if (ROOT / rel).exists())
    assert found > 20, f"expected many deferred imports on the funded path, found {found}"


def test_the_guard_catches_a_name_that_does_not_exist(tmp_path):
    """Proved in the advertised direction rather than assumed."""
    bad = tmp_path / "x.py"
    bad.write_text("def f():\n    from sniper.services.memecoin_real_runner import "
                   "no_such_function_at_all\n")
    module, name, _ = _deferred_imports(bad)[0]
    mod = importlib.import_module(module)
    assert not hasattr(mod, name), "the fixture must name something genuinely absent"
