"""Shared strategy validation and mutation helpers.

Used by both routes/api_strategies.py and mcp_tools/write_tools.py.
"""
from __future__ import annotations

import json
import re
import sqlite3

from sniper.strategies.registry import list_strategy_plugins

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

VALID_STRATEGY_TYPES = {
    "mirror_all",
    "selective",
    "signal_only",
    "fade",
    *(p.plugin_id for p in list_strategy_plugins()),
}

# Re-export from db.py — single source of truth for valid statuses.
from db import VALID_STRATEGY_STATUSES  # noqa: E402


def normalize_wallet(wallet: str | None) -> str:
    """Normalize wallet to lowercase stripped string."""
    return str(wallet or "").strip().lower()


def validate_wallet(wallet: str | None) -> str:
    """Normalize and validate wallet. Returns normalized wallet or raises ValueError."""
    w = normalize_wallet(wallet)
    if not WALLET_RE.fullmatch(w):
        raise ValueError("wallet must be a valid 0x address")
    return w


def validate_strategy_type(strategy_type: str | None) -> str:
    """Normalize and validate strategy_type. Raises ValueError if invalid."""
    st = str(strategy_type or "").strip().lower()
    if st not in VALID_STRATEGY_TYPES:
        raise ValueError(
            f"strategy_type must be one of: {', '.join(sorted(VALID_STRATEGY_TYPES))}"
        )
    return st


def validate_parameters(parameters) -> dict:
    """Ensure parameters is a dict. Parses JSON strings. Returns {} on failure."""
    if isinstance(parameters, dict):
        return parameters
    if isinstance(parameters, str) and parameters:
        try:
            parsed = json.loads(parameters)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def create_strategy(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    label: str,
    strategy_type: str,
    parameters: dict,
) -> dict:
    """Validate inputs, create strategy in paused state, return row as dict.

    Raises ValueError on invalid inputs.
    """
    wallet = validate_wallet(wallet)
    strategy_type = validate_strategy_type(strategy_type)
    parameters = validate_parameters(parameters)
    label = str(label or "").strip()

    from db import insert_strategy, get_strategy_by_id

    strategy_id = insert_strategy(
        conn,
        wallet=wallet,
        label=label,
        strategy_type=strategy_type,
        parameters=parameters,
        status="paused",
    )

    row = get_strategy_by_id(conn, strategy_id)
    return dict(row) if row else {"id": strategy_id, "status": "paused"}


def activate_strategy(
    conn: sqlite3.Connection,
    strategy_id: int,
) -> dict:
    """Activate a paused strategy. Raises ValueError if not paused or not found."""
    from db import get_strategy_by_id, update_strategy_status

    row = get_strategy_by_id(conn, strategy_id)
    if not row:
        raise ValueError(f"strategy {strategy_id} not found")
    if row["status"] != "paused":
        raise ValueError(
            f"strategy {strategy_id} status is '{row['status']}', must be 'paused' to activate"
        )

    update_strategy_status(conn, strategy_id, "active")

    row = get_strategy_by_id(conn, strategy_id)
    return dict(row) if row else {"id": strategy_id, "status": "active"}
