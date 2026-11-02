from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import sniper.research.memecoin_opportunity as opportunity


PACKAGE_ROOT = Path("sniper/research/memecoin_opportunity")
FORBIDDEN_PREFIXES = (
    "aiohttp",
    "httpx",
    "requests",
    "solana",
    "web3",
    "websocket",
    "sniper.execution",
    "sniper.services.memecoin_v6",
    "sniper.services.memecoin_real",
    "sniper.services.memecoin_sender",
    "sniper.services.memecoin_signer",
)

# `radar-adaptive-v3` runs the V6 position state machine over the ISOLATED Radar paper ledger, so
# it imports the calculator. That is not V6 admission and not funded authority, which is what the
# v6 prefix above exists to keep out. These two modules are exempt from the prefix and pay for the
# exemption in `test_the_permitted_v6_modules_are_provably_pure` below — if either ever reaches
# network or funded execution, that test fails and the exemption dies with it. Nothing else under
# `sniper.services.memecoin_v6*` may be imported here.
PURE_V6_MODULES = (
    "sniper.services.memecoin_v6_engine",
    "sniper.services.memecoin_v6_policy_v3",
)


def _imported_modules(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.module:
        return (node.module,)
    return ()


def test_opportunity_package_has_no_network_funded_or_v6_imports():
    violations = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            for imported in _imported_modules(node):
                if imported in PURE_V6_MODULES:
                    continue
                if imported.startswith(FORBIDDEN_PREFIXES):
                    violations.append((str(path), imported))

    assert violations == []


def test_the_permitted_v6_modules_are_provably_pure():
    """The exemption above is only safe while it is TRUE, so prove it every run.

    Walk everything the two permitted modules reach, not just their own import lines — a leak one
    hop away is still a leak. Import-time side effects are covered separately by the no-side-effect
    test; this one is about reachability.
    """
    network_or_funded = tuple(p for p in FORBIDDEN_PREFIXES
                              if not p.startswith("sniper.services.memecoin_v6"))
    seen: set[str] = set()
    queue = list(PURE_V6_MODULES)
    leaks = []
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        path = Path(module.replace(".", "/") + ".py")
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            for imported in _imported_modules(node):
                if imported.startswith(network_or_funded):
                    leaks.append((module, imported))
                elif imported.startswith("sniper."):
                    queue.append(imported)

    assert leaks == [], "a permitted v6 module reached network or funded execution"
    assert len(seen) > len(PURE_V6_MODULES), "the walk did not resolve any module"


def test_package_root_exports_only_stable_domain_and_pure_evaluation_contracts():
    assert opportunity.__all__ == (
        "CanonicalEvent",
        "ChainOrder",
        "ConfirmationState",
        "EventKind",
        "NetworkId",
        "RawEvent",
        "VenueId",
        "crossed_landmarks",
        "evaluate_competing_outcome",
        "materialize_landmarks",
    )
    assert not hasattr(opportunity, "connect")
    assert not hasattr(opportunity, "append_raw_event")


def test_import_has_no_database_logging_or_environment_side_effect(tmp_path):
    script = """
import logging
import os
from pathlib import Path

root = Path(os.environ["OPPORTUNITY_TEST_ROOT"])
before_handlers = tuple(logging.getLogger().handlers)
before_files = tuple(root.rglob("*"))
import sniper.research.memecoin_opportunity
after_handlers = tuple(logging.getLogger().handlers)
after_files = tuple(root.rglob("*"))
assert after_handlers == before_handlers
assert after_files == before_files
assert not (root / "data" / "memecoin_opportunity.db").exists()
"""
    env = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONPATH": str(Path.cwd()),
        "OPPORTUNITY_TEST_ROOT": str(tmp_path),
        "SNIPER_STATE_ROOT": str(tmp_path),
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
