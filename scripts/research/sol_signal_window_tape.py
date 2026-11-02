#!/usr/bin/env python3
"""Solana signer window tape: who traded each signal's coin around the source slot, and at what pool price.

Spec docs/superpowers/specs/2026-10-01-memecoin-solana-signer-window-tape-design.md (approved by the
operator 2026-10-02; slice 1 in its build section). For each Solana buy signal whose origin carries the
source signature, once the window has closed, read the mint's succeeded transactions from S - 150 to
S + 4500 slots in the spec's order (entry dense, leaders 5 + 5 pages, then one page per 2 s / 5 s / 30 s
bucket) and store one row per on-curve owner whose balance of the coin moved (`ticks`) and one per pool
side of the coin (`pool_swaps`). Overlapping windows on a mint are read once: `coverage` keeps what each
mint already has, with its bucket width (0 = every transaction).

Research key only (`funded_exit_truth.rpc`: refuses a funded key, paces through the key's SHARED pacer),
and only while the Solana book is flat (checked before every call). Read-only on the ledger; writes only
--db. One pass per run under a lock file. Not on any fill path. The sample is coins our sources bought,
never a census of the chain.

    python3 scripts/research/sol_signal_window_tape.py --since 2026-10-02T00:00:00+00:00 [--db PATH]
    python3 scripts/research/sol_signal_window_tape.py            # later runs keep the stored --since
"""
import argparse
import base64
import contextlib
import fcntl
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sniper.util.state_paths import state_root  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
PUMP_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_EVENT = bytes([189, 219, 127, 211, 78, 230, 97, 238])
EVENT_CPI = bytes.fromhex("e445a52e51cb9a1d")         # anchor emit_cpi! prefix on a self-invoke
# program -> (venue, constant product: the vault reserves are the price). DAMM v2 pools can carry a
# price range, so their vaults are not the price either (brain 680ba272: DLMM/CLMM median 4.40x off).
VENUES = {"pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": ("pump_amm", 1),
          "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": ("raydium_amm_v4", 1),
          "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": ("raydium_cpmm", 1),
          "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": ("meteora_damm_v1", 1),
          "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": ("meteora_damm_v2", 0),
          "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": ("raydium_clmm", 0),
          "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": ("raydium_launchlab", 0),
          "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": ("meteora_dlmm", 0),
          "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": ("meteora_dbc", 0),
          "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": ("orca_whirlpool", 0)}

WINDOW_END = 4500
DUE_MARGIN = 32
ENTRY_SAFETY_PAGES = 300          # the spec gives the entry segment no cap; this only stops a runaway
LEADER_PAGES = 5                  # each end of the leaders segment
PAGE = 100
SLOT_TRIES = 3
RESOLVE_AFTER_S = 120             # a signature seconds old may not be visible yet
MAX_CALLS = 600                   # per run: about 11 minutes at the key's 1.1 s gap
SIG = r"([1-9A-HJ-NP-Za-km-z]{64,90})"
ORIGINS = (re.compile(r"^slotlane:" + SIG), re.compile(r"^onchain:" + SIG),
           re.compile(r"^swap:raybot:" + SIG + ":"), re.compile(r"^pump:" + SIG + ":"))


class ReadStop(Exception):
    """A read that ends the run: quota, HTTP error, timeout, the book opening, or the call budget."""


def on_curve(owner):
    from solders.pubkey import Pubkey
    try:
        return Pubkey.from_string(owner).is_on_curve()
    except (ValueError, TypeError):
        return False


# --- one transaction ---------------------------------------------------------------------------------

def _keys(tx):
    msg = tx["transaction"]["message"]
    keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg.get("accountKeys") or []]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def _instructions(tx, keys):
    """(program, [account addresses], data, inner) for every top-level and inner instruction, in either
    encoding (json gives indices, jsonParsed gives addresses; parsed system/token ones have none)."""
    msg = tx["transaction"]["message"]
    groups = [(False, msg.get("instructions") or [])]
    groups += [(True, inner.get("instructions") or []) for inner in (tx["meta"].get("innerInstructions") or [])]
    for inner, ixs in groups:
        for ix in ixs:
            try:
                program = ix.get("programId") or keys[ix["programIdIndex"]]
                accounts = [a if isinstance(a, str) else keys[a] for a in ix.get("accounts") or []]
            except (KeyError, IndexError, TypeError):
                continue
            yield program, accounts, ix.get("data"), inner


def _moved(tx, keys):
    """{token account: [owner, mint, pre, post, decimals]} for every token account whose balance changed."""
    rows = {}
    meta = tx["meta"]
    for side, slot in (("preTokenBalances", 2), ("postTokenBalances", 3)):
        for b in meta.get(side) or []:
            i = b.get("accountIndex")
            if i is None or i >= len(keys):
                continue
            rec = rows.setdefault(keys[i], [b.get("owner"), b.get("mint"), 0, 0, None])
            rec[slot] = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            rec[4] = (b.get("uiTokenAmount") or {}).get("decimals")
    return {a: r for a, r in rows.items() if r[2] != r[3]}


def _curve_events(tx, keys, mint):
    """The pump curve TradeEvents on `mint`, from `Program data:` logs or an emit_cpi self-invoke. Only
    the fixed head is read (mint .. creator_fee): older events are short (brain 9241badf)."""
    from sniper.research.memecoin_opportunity.decoders import BorshReader
    import base58
    raws = []
    for line in tx["meta"].get("logMessages") or []:
        if line.startswith("Program data: "):
            with contextlib.suppress(ValueError):
                raws.append(base64.b64decode(line[len("Program data: "):]))
    for program, _accounts, data, _inner in _instructions(tx, keys):
        if program == PUMP_CURVE and data:
            with contextlib.suppress(ValueError):
                raw = base58.b58decode(data)
                if raw[:8] == EVENT_CPI:
                    raws.append(raw[8:])
    out, seen = [], set()
    for raw in raws:
        if raw[:8] != TRADE_EVENT or raw in seen:
            continue
        seen.add(raw)
        try:
            r = BorshReader(raw[8:])
            e = {"mint": r.pubkey(), "sol": r.u64(), "tokens": r.u64(), "is_buy": r.bool(), "user": r.pubkey(),
                 "ts": r.i64(), "vsol": r.u64(), "vtok": r.u64(), "rsol": r.u64(), "rtok": r.u64(),
                 "fee_recipient": r.pubkey(), "fee_bps": r.u64(), "fee": r.u64(), "creator": r.pubkey(),
                 "creator_fee_bps": r.u64()}
        except (ValueError, IndexError):
            continue
        if e["mint"] == mint:
            out.append(e)
    return out


def _price(coin, cdec, quote, qdec):
    if not coin or not quote or cdec is None or qdec is None:
        return None
    return (abs(quote) / 10 ** qdec) / (abs(coin) / 10 ** cdec)


def parse_tx(tx, mint):
    """(ticks, pool_swaps) for one transaction on `mint`; ([], []) for a failed or unreadable one.

    A tick is an ON-CURVE owner whose balance of the coin moved (a pool vault, a curve PDA or a router
    authority is off-curve and never a trader; a Squads vault is a PDA and drops out too). Its kind is
    `liquidity` when its coin and quote tokens all moved the same way, else `swap` when the transaction
    has a pool side of the coin, else `transfer`.
    A pool side is the pump curve's TradeEvent, or a coin vault paired with ITS OWN quote vault: the one
    opposite-moving account of the same owner inside the same instruction (a pool authority can own the
    vaults of several pools in one route, so pairing by owner alone can take another pool's dust).
    The quote is whatever that pool pairs the coin with: usually WSOL, USDC or USDT, but a coin/coin
    pool stores its price in the OTHER token's units (Gamma LOW 2). `quote_mint` says which; a reader
    converting with SOL/USD keeps only WSOL rows, or prices the other token first."""
    meta = tx.get("meta") if isinstance(tx, dict) else None
    if not meta or meta.get("err") is not None or "transaction" not in tx:
        return [], []
    keys = _keys(tx)
    moved = _moved(tx, keys)
    cdec = next((r[4] for r in moved.values() if r[1] == mint), None)
    base = {"signature": tx["transaction"]["signatures"][0], "slot": tx.get("slot"),
            "tx_index": tx.get("transactionIndex"), "block_time": tx.get("blockTime")}
    swaps, taken = [], set()
    for i, e in enumerate(_curve_events(tx, keys, mint)):
        coin = -e["tokens"] if e["is_buy"] else e["tokens"]
        vault = next((a for a, r in moved.items() if r[1] == mint and r[3] - r[2] == coin and not on_curve(r[0])), None)
        taken.add(vault)
        # Several trades in one transaction (a bundler buying for several wallets) move the curve vault by
        # their sum, so no event matches it; each keeps its own row by its event index.
        swaps.append(dict(base, venue="pump_curve", program=PUMP_CURVE, source="trade_event",
                          coin_vault=vault or "trade_event:%d" % i,
                          quote_vault=None, pool_owner=moved[vault][0] if vault else None, quote_mint=WSOL,
                          quote_decimals=9, coin_decimals=cdec if cdec is not None else 6, coin_delta=coin,
                          quote_delta=e["sol"] if e["is_buy"] else -e["sol"], coin_reserve_after=e["vtok"],
                          quote_reserve_after=e["vsol"], cp=1, fee_bps=e["fee_bps"] + e["creator_fee_bps"],
                          price=_price(e["tokens"], cdec if cdec is not None else 6, e["sol"], 9)))
    best = {}
    for program, accounts, _data, inner in _instructions(tx, keys):
        for coin_vault in accounts:
            rec = moved.get(coin_vault)
            if not rec or rec[1] != mint or on_curve(rec[0]) or coin_vault in taken:
                continue
            sign = rec[3] - rec[2]
            quotes = [a for a in dict.fromkeys(accounts) if a != coin_vault and a in moved and moved[a][0] == rec[0]
                      and moved[a][1] != mint and (moved[a][3] - moved[a][2]) * sign < 0]
            if len(quotes) != 1:
                continue
            # Prefer the AMM's own instruction, then the narrowest one: a router's instruction lists
            # every account it passes through.
            rank = (program not in VENUES, len(accounts))
            if coin_vault not in best or rank < best[coin_vault][0]:
                best[coin_vault] = (rank, program, quotes[0])
    for coin_vault, (_rank, program, quote_vault) in best.items():
        c, q = moved[coin_vault], moved[quote_vault]
        venue, cp = VENUES.get(program, ("program:" + program, 0))
        swaps.append(dict(base, venue=venue, program=program, source="vaults", coin_vault=coin_vault,
                          quote_vault=quote_vault, pool_owner=c[0], quote_mint=q[1], quote_decimals=q[4],
                          coin_decimals=c[4], coin_delta=c[3] - c[2], quote_delta=q[3] - q[2],
                          coin_reserve_after=c[3], quote_reserve_after=q[3], cp=cp, fee_bps=None,
                          price=_price(c[3] - c[2], c[4], q[3] - q[2], q[4])))
    owners = {}
    for r in moved.values():
        if r[1] == mint and r[0] and on_curve(r[0]):
            o = owners.setdefault(r[0], [0, 0, r[4]])
            o[0] += r[2]
            o[1] += r[3]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    ticks = []
    for owner, (p0, p1, dec) in owners.items():
        if p1 == p0:
            continue
        lam = None                                    # diagnostic only: never a price
        if owner in keys:
            i = keys.index(owner)
            if i < len(pre) and i < len(post):
                lam = post[i] - pre[i] + (int(meta.get("fee") or 0) if i == 0 else 0)
                lam += sum(r[3] - r[2] for r in moved.values() if r[0] == owner and r[1] == WSOL)
        # A liquidity add or withdrawal moves the coin and the quote the SAME way for its owner; a trade
        # moves some quote the other way (or only native SOL, which is not a token row).
        quotes = [r[3] - r[2] for r in moved.values() if r[0] == owner and r[1] in (WSOL, USDC, USDT)]
        lp = bool(quotes) and all(q * (p1 - p0) > 0 for q in quotes)
        ticks.append(dict(base, owner=owner, delta=p1 - p0, pre=p0, post=p1, decimals=dec,
                          kind="liquidity" if lp else "swap" if swaps else "transfer",
                          fee_payer=keys[0] if keys else None, own_sol_lamports=lam))
    return ticks, swaps


# --- the window ----------------------------------------------------------------------------------------

@dataclass
class Seg:
    name: str
    lo: int
    hi: int
    bucket: int


def plan(s, landing=None):
    """The spec's read order for a source slot `s`; a landing past S + 30 stretches the entry segment to
    it plus 5 slots, and the later segments start after it."""
    entry_hi = max(s + 30, landing + 5) if landing is not None else s + 30
    segs = [Seg("entry", s - 20, entry_hi, 0), Seg("leaders", s - 150, s - 21, 0)]
    for name, lo, hi, bucket in (("minute", s + 31, s + 150, 5), ("path", s + 151, s + 750, 12),
                                 ("tail", s + 751, s + WINDOW_END, 75)):
        lo = max(lo, entry_hi + 1)
        if lo <= hi:
            segs.append(Seg(name, lo, hi, bucket))
    return segs


def uncovered(cov, lo, hi, bucket):
    """[(a, b)] of [lo, hi] not covered by a read at `bucket` or finer (0 = every transaction)."""
    out = [(lo, hi)]
    for a, b, width in sorted(cov):
        if width > bucket:
            continue
        nxt = []
        for x, y in out:
            if b < x or a > y:
                nxt.append((x, y))
                continue
            if x < a:
                nxt.append((x, a - 1))
            if b < y:
                nxt.append((b + 1, y))
        out = nxt
    return out


@dataclass
class Read:
    txs: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    covered: list = field(default_factory=list)
    truncated: bool = False


def read_range(rpc, mint, lo, hi, *, bucket=0, max_pages=None, order="asc", flat_fn=lambda: True):
    """Read [lo, hi] of `mint`. Dense (bucket 0): every transaction, following the token, at most
    `max_pages`; a cut read covers only the slots it finished (its last slot may be partial). Sampled:
    one page a bucket; a page that ends inside its bucket jumps to the next bucket's first slot, and a
    page that reaches a later bucket continues. The book is checked before every call."""
    out = Read()
    # `cursor` is the filter's open end; a token is valid only for the filter it came from, so a page
    # that continues keeps the filter and moves `pos`, the slot whose bucket the next page belongs to.
    cursor, token = (lo if order == "asc" else hi), None
    pos = cursor
    while True:
        if max_pages is not None and len(out.pages) >= max_pages:
            last = out.pages[-1]["last_slot"]
            out.truncated = True
            span = (lo, last - 1) if order == "asc" else (last + 1, hi)
            out.covered = [span + (0,)] if span[0] <= span[1] else []
            return out
        if not flat_fn():
            raise ReadStop("solana book open mid-read")
        flt = {"status": "succeeded",
               "slot": {"gte": cursor, "lte": hi} if order == "asc" else {"gte": lo, "lte": cursor}}
        opts = {"transactionDetails": "full", "sortOrder": order, "limit": PAGE, "encoding": "json",
                "maxSupportedTransactionVersion": 1, "filters": flt}
        if token:
            opts["paginationToken"] = token
        res = rpc("getTransactionsForAddress", [mint, opts])
        if (not isinstance(res, dict) or "error" in res
                or not isinstance(res.get("data"), list)
                or any(not isinstance(tx, dict) or not isinstance(tx.get("slot"), int)
                       for tx in res.get("data", []))):
            raise ReadStop("invalid transaction page: coverage unknown")
        page = res["data"]
        # MORE PAGES = A CONTINUATION TOKEN, whatever the row count (R2, Codex review 2026-10-06):
        # the provider may answer fewer rows than the limit and still hold more, and a short page
        # read as the end recorded the whole range covered. A token that repeats, or comes with an
        # empty page, does not advance: coverage unknown, like a malformed page.
        nxt = res.get("paginationToken") or None
        if nxt is not None and (nxt == token or not page):
            raise ReadStop("pagination did not advance: coverage unknown")
        out.txs.extend(page)
        full = nxt is not None
        out.pages.append({"lo": flt["slot"]["gte"], "hi": flt["slot"]["lte"], "order": order,
                          "first_slot": page[0]["slot"] if page else None,
                          "last_slot": page[-1]["slot"] if page else None, "n": len(page), "full": int(full)})
        if not full:
            out.covered = [(lo, hi, bucket)]
            return out
        last = page[-1]["slot"]
        if bucket and order == "asc":
            bucket_end = lo + ((pos - lo) // bucket + 1) * bucket - 1
            if last <= bucket_end:
                cursor = pos = bucket_end + 1
                token = None
                if cursor > hi:
                    out.covered = [(lo, hi, bucket)]
                    return out
                continue
            pos = last
        token = res["paginationToken"]


# --- the ledger ----------------------------------------------------------------------------------------

def signals_from_ledger(led, since):
    """({sig:mint: signal}, out_of_scope count) for Solana buy attempts decided at or after `since`."""
    sigs, out = {}, 0
    for aid, origin, mint, uid, handle, decided, fill_id, tx_sig in led.execute(
            "SELECT attempt_id, origin_event_id, mint, source_user_id, source_handle, decision_started_at, fill_id, "
            "tx_sig FROM memecoin_entry_attempts WHERE network_id='solana' AND side='buy' AND decision_started_at>=? "
            "ORDER BY decision_started_at, attempt_id", (since,)):
        src = next((m.group(1) for m in (p.match(str(origin or "")) for p in ORIGINS) if m), None)
        if not src or not mint:
            out += 1
            continue
        s = sigs.setdefault(src + ":" + mint, {"signal_key": src + ":" + mint, "source_sig": src, "mint": mint,
                                               "source_user_id": uid, "source_handle": handle,
                                               "first_decided_at": decided, "attempts": [], "landing_sig": None})
        s["attempts"].append(aid)
        if fill_id is not None and tx_sig and not s["landing_sig"]:
            s["landing_sig"] = tx_sig
    return sigs, out


def solana_book_open(led):
    """Not flat by the shared research-read rule (db_memecoin.solana_book_flat), or a reserved entry
    with no network (a legacy row is Solana until it says otherwise). An error reads as open."""
    from sniper.persistence.db_memecoin import solana_book_flat
    try:
        return not solana_book_flat(led) or led.execute(
            "SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' AND network_id IS NULL").fetchone()[0] > 0
    except sqlite3.Error:
        return True


# --- storage and the job ---------------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS signals(signal_key TEXT PRIMARY KEY, source_sig TEXT NOT NULL, mint TEXT NOT NULL,
  source_user_id TEXT, source_handle TEXT, first_decided_at TEXT NOT NULL, attempts_json TEXT NOT NULL,
  s_slot INTEGER, landing_sig TEXT, landing_slot INTEGER, status TEXT NOT NULL, slot_tries INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0, read_at TEXT);
CREATE TABLE IF NOT EXISTS pages(signal_key TEXT, mint TEXT, segment TEXT, sort TEXT, lo INTEGER, hi INTEGER,
  bucket INTEGER, first_slot INTEGER, last_slot INTEGER, n INTEGER, full INTEGER, read_at TEXT);
CREATE TABLE IF NOT EXISTS segments_done(signal_key TEXT NOT NULL, segment TEXT NOT NULL, done_at TEXT,
  PRIMARY KEY(signal_key, segment));
CREATE TABLE IF NOT EXISTS coverage(mint TEXT NOT NULL, lo INTEGER NOT NULL, hi INTEGER NOT NULL,
  bucket INTEGER NOT NULL, signal_key TEXT, segment TEXT);
CREATE INDEX IF NOT EXISTS coverage_mint ON coverage(mint);
CREATE TABLE IF NOT EXISTS ticks(signature TEXT NOT NULL, mint TEXT NOT NULL, owner TEXT NOT NULL, delta TEXT,
  pre TEXT, post TEXT, decimals INTEGER, kind TEXT, fee_payer TEXT, slot INTEGER, tx_index INTEGER, block_time INTEGER,
  own_sol_lamports INTEGER, UNIQUE(signature, mint, owner));
CREATE INDEX IF NOT EXISTS ticks_mint_slot ON ticks(mint, slot, tx_index);
CREATE INDEX IF NOT EXISTS ticks_owner ON ticks(owner);
CREATE TABLE IF NOT EXISTS pool_swaps(signature TEXT NOT NULL, mint TEXT NOT NULL, venue TEXT, program TEXT,
  source TEXT, coin_vault TEXT NOT NULL, quote_vault TEXT, pool_owner TEXT, quote_mint TEXT, quote_decimals INTEGER,
  coin_decimals INTEGER, coin_delta TEXT, quote_delta TEXT, coin_reserve_after TEXT, quote_reserve_after TEXT,
  price REAL, cp INTEGER, fee_bps INTEGER, slot INTEGER, tx_index INTEGER, block_time INTEGER,
  UNIQUE(signature, mint, coin_vault));
CREATE INDEX IF NOT EXISTS pool_swaps_mint_slot ON pool_swaps(mint, slot, tx_index);
CREATE TABLE IF NOT EXISTS runs(at TEXT NOT NULL, since TEXT, new_signals INTEGER, read INTEGER, waiting INTEGER,
  out_of_scope INTEGER, calls INTEGER, why TEXT);
"""
TICK_COLS = ("signature", "mint", "owner", "delta", "pre", "post", "decimals", "kind", "fee_payer", "slot",
             "tx_index", "block_time", "own_sol_lamports")
SWAP_COLS = ("signature", "mint", "venue", "program", "source", "coin_vault", "quote_vault", "pool_owner",
             "quote_mint", "quote_decimals", "coin_decimals", "coin_delta", "quote_delta", "coin_reserve_after",
             "quote_reserve_after", "price", "cp", "fee_bps", "slot", "tx_index", "block_time")
BIG = {"delta", "pre", "post", "coin_delta", "quote_delta", "coin_reserve_after", "quote_reserve_after"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _store(db, signal_key, mint, segment, read):
    for tx in read.txs:
        ticks, swaps = parse_tx(tx, mint)
        for t in ticks:
            t["mint"] = mint
            db.execute("INSERT OR IGNORE INTO ticks (%s) VALUES (%s)" % (",".join(TICK_COLS), ",".join("?" * len(TICK_COLS))),
                       [str(t[c]) if c in BIG and t[c] is not None else t[c] for c in TICK_COLS])
        for s in swaps:
            s["mint"] = mint
            db.execute("INSERT OR IGNORE INTO pool_swaps (%s) VALUES (%s)" % (",".join(SWAP_COLS), ",".join("?" * len(SWAP_COLS))),
                       [str(s[c]) if c in BIG and s[c] is not None else s[c] for c in SWAP_COLS])
    at = _now_iso()
    for p in read.pages:
        db.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (signal_key, mint, segment, p["order"], p["lo"], p["hi"], read.covered[0][2] if read.covered else None,
                    p["first_slot"], p["last_slot"], p["n"], p["full"], at))
    for lo, hi, bucket in read.covered:
        db.execute("INSERT INTO coverage VALUES (?,?,?,?,?,?)", (mint, lo, hi, bucket, signal_key, segment))
    db.commit()


def read_window(db, rpc, sig, *, flat_fn):
    """Read one closed window, segment by segment, skipping what the mint already has and any segment
    this signal finished on an earlier run (the leaders' cut middle is by design, never re-read). True
    when a segment was cut by a cap other than the leaders' one."""
    mint, key, truncated = sig["mint"], sig["signal_key"], False

    def cov():
        return [tuple(r) for r in db.execute("SELECT lo, hi, bucket FROM coverage WHERE mint=?", (mint,))]
    done = {r[0] for r in db.execute("SELECT segment FROM segments_done WHERE signal_key=?", (key,))}
    for seg in plan(sig["s_slot"], sig["landing_slot"]):
        if seg.name in done:
            continue
        for lo, hi in uncovered(cov(), seg.lo, seg.hi, seg.bucket):
            if seg.name == "leaders":
                r = read_range(rpc, mint, lo, hi, max_pages=LEADER_PAGES, flat_fn=flat_fn)
                _store(db, key, mint, seg.name, r)
                if r.truncated:
                    for a, b in uncovered(cov(), lo, hi, 0):
                        _store(db, key, mint, seg.name,
                               read_range(rpc, mint, a, b, max_pages=LEADER_PAGES, order="desc", flat_fn=flat_fn))
            else:
                r = read_range(rpc, mint, lo, hi, bucket=seg.bucket,
                               max_pages=ENTRY_SAFETY_PAGES if seg.name == "entry" else None, flat_fn=flat_fn)
                _store(db, key, mint, seg.name, r)
                if r.truncated:                       # kept at once: a later run may finish the window
                    truncated = True
                    db.execute("UPDATE signals SET truncated=1 WHERE signal_key=?", (key,))
        db.execute("INSERT OR IGNORE INTO segments_done VALUES (?,?,?)", (key, seg.name, _now_iso()))
        db.commit()
    return truncated


def _counted(rpc, budget):
    used = [0]

    def call(method, params):
        if used[0] >= budget:
            raise ReadStop("call budget %d spent" % budget)
        used[0] += 1
        return rpc(method, params)
    call.used = used
    return call


def _research_rpc():
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import funded_exit_truth as fet           # the research key: refuses a funded key, shared pacer

    def call(method, params):
        try:
            return fet.rpc(method, params)
        except fet.Stop as exc:
            raise ReadStop(str(exc)[:160])
        except (TimeoutError, OSError) as exc:
            raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
    return call


def run(ledger_path, db_path, *, rpc=None, since=None, now=None, max_calls=MAX_CALLS):
    now = now or datetime.now(timezone.utc)
    db = sqlite3.connect(db_path, timeout=30)
    db.executescript(DDL)
    stored = (db.execute("SELECT v FROM meta WHERE k='since'").fetchone() or [None])[0]
    if since is None:
        if stored is None:
            raise SystemExit("the first run needs --since (an ISO time); later runs keep it")
        since = stored
    elif stored is None:
        db.execute("INSERT INTO meta VALUES ('since', ?)", (since,))
    led = sqlite3.connect("file:%s?mode=ro" % ledger_path, uri=True, timeout=30)
    led.execute("PRAGMA query_only=1")
    report = {"since": since, "new": 0, "read": 0, "waiting": 0, "calls": 0, "why": "done"}
    try:
        found, report["out_of_scope"] = signals_from_ledger(led, since)
        for s in found.values():
            cur = db.execute("INSERT OR IGNORE INTO signals (signal_key, source_sig, mint, source_user_id, source_handle, "
                             "first_decided_at, attempts_json, landing_sig, status) VALUES (?,?,?,?,?,?,?,?,'pending')",
                             (s["signal_key"], s["source_sig"], s["mint"], s["source_user_id"], s["source_handle"],
                              s["first_decided_at"], json.dumps(s["attempts"]), s["landing_sig"]))
            report["new"] += cur.rowcount
            if not cur.rowcount:
                db.execute("UPDATE signals SET attempts_json=?, landing_sig=COALESCE(landing_sig, ?) WHERE signal_key=?",
                           (json.dumps(s["attempts"]), s["landing_sig"], s["signal_key"]))
        db.commit()
        if solana_book_open(led):
            report["why"] = "solana book open"
            return report
        call = _counted(rpc or _research_rpc(), max_calls)
        flat_fn = lambda: not solana_book_open(led)        # noqa: E731
        try:
            _resolve(db, call, now, flat_fn)
            if not flat_fn():
                raise ReadStop("solana book open mid-read")
            tip = int(call("getSlot", [{"commitment": "confirmed"}]))
            rows = db.execute("SELECT signal_key, mint, s_slot, landing_slot FROM signals WHERE status='pending' "
                              "AND s_slot IS NOT NULL ORDER BY first_decided_at").fetchall()
            for key, mint, s_slot, landing in rows:
                if tip <= s_slot + WINDOW_END + DUE_MARGIN:
                    report["waiting"] += 1
                    continue
                before = call.used[0]
                read_window(db, call, {"signal_key": key, "mint": mint, "s_slot": s_slot,
                                       "landing_slot": landing}, flat_fn=flat_fn)
                db.execute("UPDATE signals SET status=CASE truncated WHEN 1 THEN 'truncated' ELSE 'read' END, "
                           "calls=calls+?, read_at=? WHERE signal_key=?", (call.used[0] - before, _now_iso(), key))
                db.commit()
                report["read"] += 1
        except ReadStop as exc:
            report["why"] = str(exc)
        report["calls"] = call.used[0]
        return report
    finally:
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", (_now_iso(), since, report["new"], report["read"],
                   report["waiting"], report.get("out_of_scope"), report["calls"], report["why"]))
        db.commit()
        db.close()
        led.close()


def _resolve(db, call, now, flat_fn=lambda: True):
    """S and our landing slot, 256 signatures a getSignatureStatuses call, the book checked before each.
    A source signature not found on SLOT_TRIES runs is `unresolved`. A landing recorded after S resolved
    is looked up on later runs (Gamma LOW 3); one not found yet stays NULL and is asked again."""
    cutoff = now.timestamp() - RESOLVE_AFTER_S
    rows = [r for r in db.execute("SELECT signal_key, source_sig, landing_sig, first_decided_at, slot_tries, s_slot "
                                  "FROM signals WHERE status='pending' AND (s_slot IS NULL OR "
                                  "(landing_sig IS NOT NULL AND landing_slot IS NULL))")
            if datetime.fromisoformat(r[3].replace("Z", "+00:00")).timestamp() <= cutoff]
    sigs = list(dict.fromkeys([r[1] for r in rows if r[5] is None] + [r[2] for r in rows if r[2]]))
    slots = {}
    for i in range(0, len(sigs), 256):
        if not flat_fn():
            raise ReadStop("solana book open mid-read")
        chunk = sigs[i:i + 256]
        res = call("getSignatureStatuses", [chunk, {"searchTransactionHistory": True}]) or {}
        for sig, st in zip(chunk, res.get("value") or []):
            if st and st.get("slot") is not None and not st.get("err"):
                slots[sig] = int(st["slot"])
    for key, src, landing, _at, tries, s_slot in rows:
        if s_slot is not None:
            if landing in slots:
                db.execute("UPDATE signals SET landing_slot=? WHERE signal_key=?", (slots[landing], key))
        elif src in slots:
            db.execute("UPDATE signals SET s_slot=?, landing_slot=? WHERE signal_key=?",
                       (slots[src], slots.get(landing), key))
        else:
            db.execute("UPDATE signals SET slot_tries=?, status=? WHERE signal_key=?",
                       (tries + 1, "unresolved" if tries + 1 >= SLOT_TRIES else "pending", key))
    db.commit()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root = state_root()
    p.add_argument("--ledger", default=str(root / "sniper_tracker.db"))
    p.add_argument("--db", default=str(root / "data" / "research_signal_windows_solana.db"))
    p.add_argument("--since", help="ISO time; signals decided from then on (first run only)")
    p.add_argument("--max-calls", type=int, default=MAX_CALLS)
    a = p.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    load_env_files(str(root))
    with open(str(a.db) + ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another run holds the lock")
            return 0
        print(json.dumps(run(a.ledger, a.db, since=a.since, max_calls=a.max_calls)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
