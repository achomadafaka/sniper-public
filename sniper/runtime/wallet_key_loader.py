"""Shared wallet private-key loading utilities.

Single source of truth for key path resolution:
- `WALLET_KEY_FILE` env var takes precedence.
- fallback: `~/.openclaw/.wallet_key`
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict


DEFAULT_WALLET_KEY_FILE = "~/.openclaw/.wallet_key"


def resolve_wallet_key_file() -> Path:
    configured = str(os.getenv("WALLET_KEY_FILE", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(DEFAULT_WALLET_KEY_FILE).expanduser().resolve()


def _normalize_wallet(wallet: str | None) -> str:
    return str(wallet or "").strip().lower()


def load_private_key(*, expected_wallet: str | None = None) -> Dict[str, Any]:
    """Load private key from resolved key file and return signer metadata.

    Raises RuntimeError on any validation failure.
    """
    path = resolve_wallet_key_file()
    if not path.exists():
        raise RuntimeError(f"Wallet key file not found: {path}")

    key = path.read_text().strip()
    if not key:
        raise RuntimeError(f"Wallet key file is empty: {path}")
    if not key.startswith("0x"):
        key = "0x" + key

    try:
        from eth_account import Account  # lazy import
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "eth-account is required for private-key validation (pip install eth-account)"
        ) from exc

    try:
        account = Account.from_key(key)
    except Exception as exc:
        raise RuntimeError("Invalid private key format") from exc

    signer = _normalize_wallet(account.address)
    target = _normalize_wallet(expected_wallet)
    if target and signer != target:
        raise RuntimeError(
            f"Signer mismatch: key derives to {account.address}, expected {expected_wallet}"
        )

    return {
        "private_key": key,
        "signer": signer,
        "path": str(path),
    }


def startup_validate_key(*, require_key: bool) -> Dict[str, Any]:
    """Best-effort startup validation for armed live redemption mode."""
    if not require_key:
        return {"ok": True, "required": False}
    loaded = load_private_key(expected_wallet=None)
    return {
        "ok": True,
        "required": True,
        "signer": loaded["signer"],
        "path": loaded["path"],
    }

