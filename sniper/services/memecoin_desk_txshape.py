"""TX SHAPE: a hash-sampled record of how swap transactions are ROUTED, for measuring terminal attention (observe-only tape).

Retail terminals route swaps through their own router program and/or pay a small fee to a recurring account inside the same
transaction; bots pay tips. The universe worker sees the whole transaction (account keys incl. loaded addresses, top-level and inner
instructions) and stores none of it. This module turns one decoded transaction into a small row; the worker writes it to
data/desk/txshape/<day>.db for a hash-sampled share of swap transactions (MEMECOIN_DESK_TXSHAPE_SAMPLE_PCT), after every funded hook, on its
own sqlite handle. Nothing here trades, gates or names a terminal: a program or fee account is a CLUSTER until an anchor (a known wallet's
transaction) names it. Knob 0 = the whole path is skipped.

Evidence rule for calling a program or fee recipient a terminal CANDIDATE (applied offline by candidates()): seen in >= MIN_TXS sampled
transactions from >= MIN_PAYERS distinct fee payers across >= MIN_HOURS distinct UTC hours; the public Jito tip accounts and the pump
protocol/creator vault accounts are excluded as recipients; core programs are excluded as routers.

Absent / zero / unknown: a transaction with no router program and no fee transfer is 'direct/unknown', never retail and never a bot; a
failed decode is a counted skip, never a row.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import time
from pathlib import Path

VERSION = "txshape-v1"
MIN_TXS, MIN_PAYERS, MIN_HOURS = 50, 20, 3          # the evidence rule (Alpha-approved 2026-10-23)
MAX_TRANSFERS = 8                                    # per tx: a swap with more transfers is not a fee pattern worth the bytes
MAX_FEE_LAMPORTS = 5 * 10**9                         # a System transfer above 5 SOL is a funding move, not a fee
RETENTION_DAYS = 3                                   # day files older than this are deleted by the writer
COMMIT_EVERY_S = 2.0

SYSTEM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
CORE_PROGRAMS = frozenset({
    SYSTEM, COMPUTE_BUDGET,
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",           # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",           # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",          # Associated Token Account
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",           # Memo v2
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",           # Memo v1
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",           # Pump bonding curve program
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",           # PumpSwap AMM
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",           # Pump fee program (sniper/execution/pump_native_exit.py FEE_PROGRAM)
})
# The eight published Jito tip accounts (Jito docs): a transfer to one of these is a TIP (bots/MEV), never a terminal fee.
JITO_TIP_ACCOUNTS = frozenset({
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5", "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY", "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh", "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL", "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
})

DDL = ("CREATE TABLE IF NOT EXISTS txshape(signature TEXT PRIMARY KEY, slot INTEGER, tx_index INTEGER, received_at REAL, venue TEXT, "
       "payer TEXT, top_json TEXT, inner_json TEXT, xfers_json TEXT, cu_price INTEGER, cu_limit INTEGER, n_top INTEGER, n_inner INTEGER, "
       "version TEXT)")


def hash_sampled(signature, pct):
    """Deterministic share of transactions by signature hash (never by outcome): the same tx is in or out on every run."""
    return pct > 0 and int(hashlib.sha256(signature.encode()).hexdigest()[:8], 16) % 100 < pct


def shape(keys, top, inner, *, venue):
    """keys = account keys as base58 strings (loaded addresses included, in index order); top = [(program_id_index, accounts bytes/list,
    data bytes)] top-level; inner = [(parent_ix_index, [(program_id_index, accounts, data)])]. Returns the row dict, or None when an index is
    out of range (counted by the caller as a decode skip)."""
    n = len(keys)
    try:
        payer = keys[0]
        top_programs, cu_price, cu_limit, xfers = [], None, None, []
        inner_programs = set()
        n_inner = 0

        def read(program, accounts, data):
            nonlocal cu_price, cu_limit
            if program == COMPUTE_BUDGET and data:
                if data[0] == 3 and len(data) >= 9:
                    cu_price = struct.unpack("<Q", bytes(data[1:9]))[0]
                elif data[0] == 2 and len(data) >= 5:
                    cu_limit = struct.unpack("<I", bytes(data[1:5]))[0]
            elif program == SYSTEM and len(data) == 12 and struct.unpack("<I", bytes(data[:4]))[0] == 2 and len(accounts) >= 2:
                lamports = struct.unpack("<Q", bytes(data[4:12]))[0]
                if 0 < lamports <= MAX_FEE_LAMPORTS and len(xfers) < MAX_TRANSFERS:
                    xfers.append([keys[accounts[1]], lamports])
        for pi, accounts, data in top:
            if pi >= n:
                return None
            program = keys[pi]
            if program not in CORE_PROGRAMS and (not top_programs or top_programs[-1] != program):
                top_programs.append(program)
            read(program, accounts, data)
        for _parent, ixs in inner:
            for pi, accounts, data in ixs:
                if pi >= n:
                    return None
                n_inner += 1
                program = keys[pi]
                if program not in CORE_PROGRAMS:
                    inner_programs.add(program)
                read(program, accounts, data)
    except (IndexError, struct.error, TypeError):
        return None
    return {"venue": venue, "payer": payer, "top": top_programs, "inner": sorted(inner_programs), "xfers": xfers,
            "cu_price": cu_price, "cu_limit": cu_limit, "n_top": len(top), "n_inner": n_inner}


def utc_day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class Writer:
    """Own sqlite handle per UTC day, batched commit, every failure swallowed into a counter (the tape must never touch the worker's other
    files or its commit path). Old day files are deleted at a day roll."""

    def __init__(self, base, pct):
        self.dir = Path(base) / "txshape"
        self.pct = int(pct)
        self.conn, self.day = None, None
        self.last_commit = 0.0
        self.stats = {"sampled": 0, "written": 0, "skipped_decode": 0, "errors": 0, "pending": 0}

    def _open(self, now):
        day = utc_day(now)
        if self.day == day and self.conn is not None:
            return self.conn
        self.close()
        self.dir.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(self.dir / ("%s.db" % day)), timeout=0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(DDL)
        c.commit()
        self.conn, self.day = c, day
        self._retention(now)
        return c

    def _retention(self, now):
        cutoff = utc_day(now - RETENTION_DAYS * 86400)
        for p in self.dir.glob("*.db"):
            if p.stem < cutoff:
                for suffix in ("", "-wal", "-shm"):
                    try:
                        Path(str(p) + suffix).unlink()
                    except OSError:
                        pass

    def add(self, signature, slot, tx_index, received_at, row, now):
        """Write one sampled tx row; never raises."""
        self.stats["sampled"] += 1
        if row is None:
            self.stats["skipped_decode"] += 1
            return False
        try:
            c = self._open(now)
            c.execute("INSERT OR IGNORE INTO txshape VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (signature, slot, tx_index, received_at, row["venue"], row["payer"], json.dumps(row["top"]), json.dumps(row["inner"]),
                       json.dumps(row["xfers"]), row["cu_price"], row["cu_limit"], row["n_top"], row["n_inner"], VERSION))
            self.stats["written"] += 1
            self.stats["pending"] += 1
            if now - self.last_commit >= COMMIT_EVERY_S:
                self.commit(now)
            return True
        except (sqlite3.Error, OSError):
            self.stats["errors"] += 1
            self.close()
            return False

    def commit(self, now):
        try:
            if self.conn is not None:
                self.conn.commit()
                self.stats["pending"] = 0
        except (sqlite3.Error, OSError):
            self.stats["errors"] += 1
            self.close()
        self.last_commit = now

    def close(self):
        if self.conn is not None:
            try:
                self.conn.commit()
                self.conn.close()
            except (sqlite3.Error, OSError):
                pass
        self.conn, self.day = None, None


def candidates(rows, *, min_txs=MIN_TXS, min_payers=MIN_PAYERS, min_hours=MIN_HOURS):
    """The evidence table. rows = [(received_at, payer, top_programs list, inner_programs list, xfers [[dest, lamports]...])]. Returns a list
    sorted by tx count of {kind 'router_program'|'fee_recipient', id, txs, payers, hours, median_lamports (fee recipients), qualifies}. Tip accounts and
    core programs never appear."""
    stats = {}

    def touch(kind, ident, at, payer, lamports=None):
        s = stats.setdefault((kind, ident), {"txs": 0, "payers": set(), "hours": set(), "lamports": []})
        s["txs"] += 1
        s["payers"].add(payer)
        s["hours"].add(int(at // 3600))
        if lamports is not None:
            s["lamports"].append(lamports)
    for at, payer, top, inner, xfers in rows:
        for p in set(top) | set(inner):
            if p not in CORE_PROGRAMS:
                touch("router_program", p, at, payer)
        for dest, lamports in xfers:
            if dest not in JITO_TIP_ACCOUNTS and dest != payer:
                touch("fee_recipient", dest, at, payer, lamports)
    out = []
    for (kind, ident), s in stats.items():
        lam = sorted(s["lamports"])
        out.append({"kind": kind, "id": ident, "txs": s["txs"], "payers": len(s["payers"]), "hours": len(s["hours"]),
                    "median_lamports": lam[len(lam) // 2] if lam else None,
                    "qualifies": s["txs"] >= min_txs and len(s["payers"]) >= min_payers and len(s["hours"]) >= min_hours})
    return sorted(out, key=lambda r: -r["txs"])
