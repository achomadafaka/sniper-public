from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass(frozen=True)
class LedgerPaths:
    root: Path
    wallet: str
    wallet_path: Path
    market_data_path: Path
    index_path: Path


def normalize_wallet(wallet: str) -> str:
    value = str(wallet or "").strip()
    if not _WALLET_RE.fullmatch(value):
        raise ValueError(f"invalid wallet address: {wallet!r}")
    return value.lower()


def ledger_paths(root: str | Path, wallet: str) -> LedgerPaths:
    root_path = Path(root)
    normalized_wallet = normalize_wallet(wallet)
    return LedgerPaths(
        root=root_path,
        wallet=normalized_wallet,
        wallet_path=root_path / f"{normalized_wallet}.duckdb",
        market_data_path=root_path / "market_data.duckdb",
        index_path=root_path / "index.duckdb",
    )
