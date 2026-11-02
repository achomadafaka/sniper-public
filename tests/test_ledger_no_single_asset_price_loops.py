from __future__ import annotations

import ast
from pathlib import Path


LEDGER_ROOT = Path("sniper/analysis/wallet_protocol/ledger")


def test_ledger_subtree_has_no_single_asset_prices_history_endpoint_calls() -> None:
    offenders: list[str] = []
    for path in sorted(LEDGER_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if "/prices-history" in node.value and "/batch-prices-history" not in node.value:
                offenders.append(f"{path}:{node.lineno}")

    assert offenders == []
