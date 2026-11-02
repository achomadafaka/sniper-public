"""Audit that the new wallet-analyzer path imports no legacy modules.

Targets must not import:
  analyze_users, stream_low_buys, db_discovery, bot_detector, classify_accounts

Scanned:
  scripts/run_wallet_protocol.py
  scripts/run_wallet_protocol_batch.py
  sniper/analysis/wallet_protocol/**/*.py

Exit codes:
  0: clean
  1: at least one legacy import found
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys


LEGACY_TOP_LEVEL = {
    "analyze_users",
    "stream_low_buys",
    "db_discovery",
    "bot_detector",
    "classify_accounts",
}


def collect_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def target_paths(repo: Path) -> list[Path]:
    paths = [
        repo / "scripts/run_wallet_protocol.py",
        repo / "scripts/run_wallet_protocol_batch.py",
    ]
    wallet_protocol = repo / "sniper/analysis/wallet_protocol"
    if wallet_protocol.is_dir():
        paths.extend(sorted(wallet_protocol.rglob("*.py")))
    return paths


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    violations: list[tuple[str, set[str]]] = []

    for path in target_paths(repo):
        if not path.is_file():
            continue
        offending = collect_imports(path) & LEGACY_TOP_LEVEL
        if offending:
            violations.append((str(path.relative_to(repo)), offending))

    if violations:
        print("FAIL - legacy imports detected:")
        for path, names in violations:
            print(f"  {path}: {sorted(names)}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
