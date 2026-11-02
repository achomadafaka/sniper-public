"""PumpSwap pool metadata store (data/desk/pumpswap/metadata.db) and its causal reader.

Written by scripts/memecoin_desk_ps_metadata_worker.py (Helius research key, paced, capped, off the fill path); read by the
PS as-of snapshot builder. WRITE-ONCE per mint and per pool; a failed or missing read is UNKNOWN, never a default: decimals
or supply that were not read are absent rows, never 0. A row carries when it was received (`metadata_known_at`), the slot
of the read's context, and its source, so a reader can refuse anything not known at its as-of time.
"""
from __future__ import annotations

import re
import sqlite3

DDL = (
    "CREATE TABLE IF NOT EXISTS mints(mint TEXT PRIMARY KEY, decimals INTEGER NOT NULL, supply_atomic TEXT NOT NULL, "
    "token_program TEXT, mint_authority TEXT, freeze_authority TEXT, metadata_known_at REAL NOT NULL, slot INTEGER NOT NULL, "
    "source TEXT NOT NULL, payload_json TEXT)",
    "CREATE TABLE IF NOT EXISTS pool_accounts(pool TEXT PRIMARY KEY, owner TEXT, lamports INTEGER, data_b64 TEXT, "
    "metadata_known_at REAL NOT NULL, slot INTEGER NOT NULL, source TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS failures(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, error TEXT NOT NULL, at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS failures_key ON failures(kind, key)",
    "CREATE TABLE IF NOT EXISTS gaps(at REAL NOT NULL, cause TEXT NOT NULL, detail TEXT)",
    "CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS calls(at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS calls_at ON calls(at)",
)
SOURCE = "helius_research:get_account_info:json_parsed"
_CANON_UINT = re.compile(r"(0|[1-9][0-9]{0,19})")
U64_MAX = 2**64 - 1
OWNER_OF = {"spl-token": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "spl-token-2022": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def connect(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in DDL:
        conn.execute(stmt)
    return conn


def parse_mint_account(result):
    """(row | None, error | None) from a getAccountInfo jsonParsed `result`. Anything that is not a proven SPL mint is an
    error slug, never a partial row: the context slot, the owner (must be the program named in the parse), decimals and a
    canonical uint64 supply must ALL be proven or the whole read is unknown."""
    try:
        value = result["value"]
        slot = result["context"]["slot"]
    except (KeyError, TypeError):
        return None, "malformed"
    if type(slot) is not int or slot < 0:
        return None, "malformed"
    if value is None:
        return None, "account_missing"
    try:
        data = value["data"]
        program = data["program"]
        if program not in OWNER_OF or data["parsed"]["type"] != "mint":
            return None, "not_a_mint"
        if value["owner"] != OWNER_OF[program]:
            return None, "owner_mismatch"
        info = data["parsed"]["info"]
        decimals, supply = info["decimals"], info["supply"]
    except (KeyError, TypeError):
        return None, "malformed"
    if type(decimals) is not int or not 0 <= decimals <= 255:
        return None, "malformed"
    if not isinstance(supply, str) or not _CANON_UINT.fullmatch(supply) or int(supply) > U64_MAX:
        return None, "malformed"
    return {"decimals": decimals, "supply_atomic": supply, "token_program": program, "owner": value["owner"],
            "mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"), "slot": slot}, None


def read_metadata(conn, mint, *, as_of):
    """The mint's metadata if its read receipt is STRICTLY before `as_of` (the cut rule: received_at < as_of); otherwise None (unknown). Never a default."""
    row = conn.execute("SELECT decimals, supply_atomic, token_program, metadata_known_at, slot, source FROM mints WHERE mint=?",
                       (mint,)).fetchone()
    if row is None or row[3] >= as_of:
        return None
    return {"decimals": row[0], "supply_atomic": row[1], "token_program": row[2], "metadata_known_at": row[3],
            "metadata_slot": row[4], "metadata_source": row[5]}


def read_pool_facts(conn, *, mint, pool, as_of):
    """Shared pure LOCAL reader: {"mint": read_metadata(...) | None, "pool_account": {...} | None}. Each part carries its OWN
    metadata_known_at / slot / source and is None (unknown) unless its read receipt is strictly before `as_of`. The pool account
    is a mutable fact: never relabel it with a swap time or the mint's clock, and never substitute it for swap reserves/fees."""
    out = {"mint": read_metadata(conn, mint, as_of=as_of), "pool_account": None}
    row = conn.execute("SELECT owner, lamports, data_b64, metadata_known_at, slot, source FROM pool_accounts WHERE pool=?",
                       (pool,)).fetchone()
    if row is not None and row[3] < as_of:
        out["pool_account"] = {"owner": row[0], "lamports": row[1], "data_b64": row[2], "metadata_known_at": row[3],
                               "metadata_slot": row[4], "metadata_source": row[5]}
    return out
