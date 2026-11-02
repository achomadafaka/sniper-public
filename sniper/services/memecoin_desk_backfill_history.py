"""Helius G1 creator history + G2 coin age (spec 2026-10-27-helius-history-backfill-design.md; Alpha order 2026-10-28). STAGE 1: nothing here is read by JUDGE or any funded path.

The transport is injected (status, body) = transport(method, params) and every request goes through the credit ledger: reserve (on disk) -> send -> classify -> commit or
mark_ambiguous. G2: the FIRST successful tx touching a mint is fetched in full mode (limit 1, one 10-credit page) and the pump `create` instruction binding that mint is decoded; a first tx
that is not that creation, a failed, unreadable or empty answer is typed unknown, never a creation time. G1: the creator wallet's txs are scanned in full mode to the wallet's start; launches
are the successful pump create instructions it signed. A count exists ONLY for a complete scan (pagination reached the wallet start, no unreadable tx, no stop, within the page cap)."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import base58

from sniper.services import memecoin_desk_backfill_ledger as L

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
METHOD = "getTransactionsForAddress"
METHOD_FULL = METHOD + ":full"                       # ledger cost key: documented 10 credits per 100 txs returned (ceil), one 100-tx page = 10
PAGE_LIMIT = 100
CREATE_DISCS = frozenset(hashlib.sha256(("global:%s" % n).encode()).digest()[:8] for n in ("create", "create_v2"))
CREATOR_PAGE_CAP = 50                                  # 5,000 txs; more is unknown, never a partial count
FINALITY_MARGIN_S = 60                                 # a scan can only count launches that finished this long before it ran
WITNESS = "create_instruction_in_first_tx"
_GAP = {"month_cap": "credits_exhausted", "run_cap": "credits_exhausted", "quota_stop": "max_usage", "cost_bound_exceeded": "max_usage", "cycle_expired": "no_cycle",
        "no_cycle_attestation": "no_cycle", "unknown_method_cost": "no_cycle", "credits_invalid": "no_cycle"}
DDL = """
create table if not exists coin_creation(mint text primary key, state text not null check(state in ('known','unknown')), reason text, signature text, slot integer, block_time integer,
  witness text, creator text, payer text, fetched_at real not null, clock_offset_s real);
create table if not exists creator_scans(creator text primary key, state text not null check(state in ('complete','capped','unknown')), reason text, pages integer, txs integer,
  launches integer, fetched_at real not null, covered_slot integer, sort_order text not null default 'asc', clock_offset_s real);
create table if not exists creator_scan_log(creator text not null, fetched_at real not null, state text not null, reason text, pages integer, covered_slot integer,
  sort_order text not null, clock_offset_s real, primary key(creator, fetched_at));
create table if not exists creator_launches(creator text not null, mint text not null, signature text not null, slot integer not null, block_time integer not null,
  seen_at real not null default 0, primary key(creator, mint));
create table if not exists g12_via(kind text not null check(kind in ('coin','scan')), key text not null, fetched_at real not null, via text not null check(via in ('nightly','inline')),
  primary key(kind, key, fetched_at));
create table if not exists g12_runs(run_id integer primary key autoincrement, mode text not null, day text, started_at real not null, finished_at real, run_cap integer, run_spent integer, outcome text);
create table if not exists g12_targets(day text not null, kind text not null check(kind in ('coin','creator')), key text not null, mint text, state text not null default 'pending',
  gap text, frozen_at real not null, priority integer not null default 9, primary key(day, kind, key));
"""


def _host_offset():
    try:
        from sniper.util.clock import offset_s
        return offset_s()
    except Exception:                                          # an unreadable offset file is NULL (unknown), never 0
        return None


def connect(path, busy_timeout=30):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=busy_timeout)
    con.execute("pragma journal_mode=wal")
    con.executescript(DDL)
    con.commit()                                             # NO ALTER of the nightly tables: an older release (the pinned nightly) inserts into them POSITIONALLY and would fail on an extra column; `via` lives in the sibling g12_via
    return con


def freeze_targets(con, day, targets, *, now):
    """targets: (kind, key, mint|None[, priority]). Insert-or-ignore: a re-write never resets a finished target nor moves its frozen_at (the denominator is fixed when first written)."""
    for t in targets:
        kind, key, mint = t[:3]
        con.execute("insert or ignore into g12_targets(day, kind, key, mint, frozen_at, priority) values(?,?,?,?,?,?)", (day, kind, key, mint, now, t[3] if len(t) > 3 else 9))
    con.commit()


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _keys(tx):
    return tx["transaction"]["message"]["accountKeys"]


def _borsh_creator(raw):
    """creator pubkey argument of pump create/create_v2: 8-byte discriminator, three borsh strings (name, symbol, uri), then the 32-byte creator. None if the layout does not parse."""
    i = 8
    for _ in range(3):
        if len(raw) < i + 4:
            return None
        i += 4 + int.from_bytes(raw[i:i + 4], "little")
    return base58.b58encode(raw[i:i + 32]).decode() if len(raw) >= i + 32 else None


def _create_instructions(tx):
    """[(accounts, creator_arg)] for every pump create/create_v2 instruction of a readable tx (top level and inner). The launch's creator is the instruction's creator ARGUMENT, not the fee
    payer or the signing `user` (measured 2026-10-28: 2 of 20 launches had a creator that is not in the instruction accounts at all). Raises on a malformed tx."""
    msg = tx["transaction"]["message"]
    ixs = list(msg["instructions"])
    for inner in (tx["meta"].get("innerInstructions") or []):
        ixs.extend(inner.get("instructions") or [])
    out = []
    for ix in ixs:
        if ix.get("programId") != PUMP or not isinstance(ix.get("data"), str):
            continue
        raw = base58.b58decode(ix["data"])
        if raw[:8] in CREATE_DISCS:
            out.append(([a if isinstance(a, str) else a["pubkey"] for a in ix["accounts"]], _borsh_creator(raw)))
    return out


def _readable(tx):
    return isinstance(tx, dict) and isinstance(tx.get("meta"), dict) and isinstance(tx.get("transaction"), dict) and isinstance(_int(tx.get("slot")), int)


def decode_creator_page(result, creator):
    """-> (launches, token, unreadable). A launch = successful tx, signed by `creator`, with a pump create instruction (mint = first account). Failed txs are excluded; a malformed
    tx counts as unreadable (the scan is then not complete), never as 'no launch'."""
    launches, bad = [], 0
    for tx in result["data"]:
        try:
            if not _readable(tx) or _int(tx.get("blockTime")) is None:
                raise ValueError
            if tx["meta"].get("err") is not None:
                continue
            for accs, arg in _create_instructions(tx):
                if arg == creator:
                    launches.append(dict(mint=accs[0], signature=tx["transaction"]["signatures"][0], slot=tx["slot"], block_time=tx["blockTime"]))
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            bad += 1
    return launches, result.get("paginationToken"), bad


def decode_coin_first(result, mint):
    """-> (row dict | None, reason). The first tx must be successful, readable, carry a pump create instruction whose first account is `mint`, and a usable slot and blockTime."""
    data = result["data"]
    if not data:
        return None, "no_history"
    tx = data[0]
    try:
        if not _readable(tx):
            return None, "unreadable_tx" if not isinstance(tx, dict) or tx.get("meta") is None or tx.get("transaction") is None else "slot_unusable"
        if tx["meta"].get("err") is not None:
            return None, "first_tx_not_creation"
        creates = _create_instructions(tx)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None, "unreadable_tx"
    mine = [arg for a, arg in creates if a and a[0] == mint]
    if not mine:
        return None, "first_tx_not_creation"
    if _int(tx.get("blockTime")) is None:
        return None, "block_time_unusable"
    return dict(signature=tx["transaction"]["signatures"][0], slot=tx["slot"], block_time=tx["blockTime"], creator=mine[0], payer=_keys(tx)[0]["pubkey"]), None


class Fetcher:
    def __init__(self, con, ledger, transport, *, clock=time.time, creator_page_cap=CREATOR_PAGE_CAP, offset=None, rate_retry=True, call_timeout=None, via="nightly"):
        self.con, self.ledger, self.transport, self.clock, self.page_cap = con, ledger, transport, clock, creator_page_cap
        self.offset = offset or _host_offset                  # sampled host-minus-corrected offset stored beside every fetched_at (Gamma: the leak check compares on corrected UTC)
        self.via = via                                        # 'nightly' | 'inline': stored beside every fetched_at so a reader can stratify
        self.deadline = None                                  # absolute clock time after which no new request is reserved (inline intake use)
        self.rate_retry = rate_retry                          # nightly: bounded charged retries on a plain 429; inline: NONE (the contract is no retry)
        self.call_timeout = call_timeout                      # inline: a request is only STARTED if this much of the deadline is left, and is told min(call_timeout, remaining AFTER the reserve);
                                                              # the transport timeout is a socket-idle timeout, so the deadline is a SOFT wall bound (wall_ms is logged by the caller)
        self.stopped = None
        self.ambiguous_streak = 0

    def _call(self, address, config, page):
        """-> ('ok', result) | ('gap', reason). Reserve before the send; ambiguous when the send may have been processed; quota and caps stop the whole fetcher."""
        while True:
            if self.stopped:
                return "gap", self.stopped
            remaining = None
            if self.deadline is not None:
                remaining = self.deadline - self.clock()
                if remaining <= 0 or (self.call_timeout is not None and remaining < self.call_timeout):
                    return "gap", "deadline"                  # checked BEFORE the reserve: nothing is charged for a request that is not sent, and none starts that could overrun
            try:
                attempt = self.ledger.reserve(METHOD_FULL, page=page)
            except L.Refusal as exc:
                gap = _GAP.get(exc.code, "credits_exhausted")
                self.stopped = gap
                return "gap", gap
            if self.deadline is not None:
                remaining = self.deadline - self.clock()           # re-read AFTER the reserve: its SQLite wait is part of the budget
                if remaining <= 0.05:
                    self.ledger.commit(attempt, actual=0)          # reserved but never sent: settled at 0 credits, nothing is owed for it
                    return "gap", "deadline"
            try:
                req = {"address": address, "config": config}
                if self.call_timeout is not None:
                    req["timeout"] = min(self.call_timeout, remaining) if remaining is not None else self.call_timeout
                status, body = self.transport(METHOD, req)
            except Exception:
                self.ledger.mark_ambiguous(attempt)
                self.ambiguous_streak += 1
                if self.ambiguous_streak >= 3:
                    self.stopped = "transport_error"
                return "gap", "transport_error"
            kind = L.classify(status, body)
            if kind == "quota":
                self.ledger.commit(attempt)
                self.ledger.record_stop("max_usage")
                self.stopped = "max_usage"
                return "gap", "max_usage"
            if kind == "rate":
                self.ledger.commit(attempt)
                if self.rate_retry and self.ledger.rate_retry_allowed(page):
                    continue
                return "gap", "rate_limited"
            if kind in ("http_error", "malformed"):
                self.ledger.mark_ambiguous(attempt)
                self.ambiguous_streak += 1
                if self.ambiguous_streak >= 3:
                    self.stopped = "transport_error"
                return "gap", "http_error" if kind == "http_error" else "unreadable_tx"
            self.ledger.commit(attempt)
            self.ambiguous_streak = 0
            if kind == "rpc_error":
                return "gap", "rpc_error"
            return "ok", json.loads(body)["result"]

    # ---- G2
    def fetch_coin(self, mint):
        row = self.con.execute("select state from coin_creation where mint=?", (mint,)).fetchone()
        if row and row[0] == "known":
            return "known"                                     # final: never fetched or charged twice
        cfg = {"sortOrder": "asc", "limit": 1, "transactionDetails": "full", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
               "filters": {"status": "succeeded"}}
        kind, res = self._call(mint, cfg, "coin:" + mint)
        now = self.clock()
        if kind == "gap":
            self._coin(mint, "unknown", res, None, now)
            return res
        try:
            fact, reason = decode_coin_first(res, mint)
        except (KeyError, TypeError):
            fact, reason = None, "unreadable_tx"
        if fact is None:
            self._coin(mint, "unknown", reason, None, now)
            return reason
        self._coin(mint, "known", None, fact, now)
        return "known"

    def _coin(self, mint, state, reason, fact, now):
        f = fact or {}
        with self.con:                                         # ONE failure-atomic transaction: the fact and its provenance land together or not at all (a later caller commit cannot make half of it durable)
            self.con.execute("insert or replace into coin_creation values(?,?,?,?,?,?,?,?,?,?,?)",
                             (mint, state, reason, f.get("signature"), f.get("slot"), f.get("block_time"), WITNESS if fact else None, f.get("creator"), f.get("payer"), now, self.offset()))
            self.con.execute("insert or replace into g12_via values('coin',?,?,?)", (mint, now, self.via))

    # ---- G1
    def fetch_creator(self, creator):
        row = self.con.execute("select state, reason, pages from creator_scans where creator=?", (creator,)).fetchone()
        if row and row[0] == "complete":
            return "complete"
        if row and row[1] == "pagination_incomplete" and row[2] >= self.page_cap:
            return "pagination_incomplete"                      # the same cap would stop at the same page: do not pay for it again
        token, pages, txs, unreadable, launches = None, 0, 0, 0, []
        self.covered = None
        while True:
            if pages >= self.page_cap:
                return self._scan(creator, "unreadable_tx" if unreadable else "pagination_incomplete", pages, txs, launches)     # a tx we could not read hides history: never a capped lower bound
            cfg = {"sortOrder": "asc", "limit": PAGE_LIMIT, "transactionDetails": "full", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                   "filters": {"status": "succeeded"}}
            if token:
                cfg["paginationToken"] = token
            kind, res = self._call(creator, cfg, "creator:%s:%d" % (creator, pages))
            if kind == "gap":
                return self._scan(creator, res, pages, txs, launches)
            try:
                page_launches, token, bad = decode_creator_page(res, creator)
            except (KeyError, TypeError):
                return self._scan(creator, "unreadable_tx", pages, txs, launches)
            pages += 1
            txs += len(res["data"])
            slots = [t["slot"] for t in res["data"] if isinstance(t, dict) and _int(t.get("slot"))]
            if slots:
                self.covered = max(slots + [self.covered or 0])
            unreadable += bad
            launches.extend(page_launches)
            if not token:
                return self._scan(creator, "unreadable_tx" if unreadable else None, pages, txs, launches)

    def _scan(self, creator, gap, pages, txs, launches):
        now = self.clock()
        state = "complete" if gap is None else "capped" if gap == "pagination_incomplete" else "unknown"       # a capped scan keeps its launches: 'at least k' is a valid lower bound
        if state == "unknown":
            launches = []                                                                             # unreadable tx, stop, transport: no partial count is kept as evidence
        cov = getattr(self, "covered", None)
        off = self.offset()
        with self.con:                                         # launches, scan row, scan log and via are ONE failure-atomic transaction
            for l in launches:
                self.con.execute("insert or ignore into creator_launches values(?,?,?,?,?,?)", (creator, l["mint"], l["signature"], l["slot"], l["block_time"], now))   # seen_at = first scan that saw it
            self.con.execute("insert or replace into creator_scans values(?,?,?,?,?,?,?,?,?,?)", (creator, state, gap, pages, txs, len(launches), now, cov, SORT_ORDER, off))
            self.con.execute("insert or ignore into creator_scan_log values(?,?,?,?,?,?,?,?)", (creator, now, state, gap, pages, cov, SORT_ORDER, off))
            self.con.execute("insert or replace into g12_via values('scan',?,?,?)", (creator, now, self.via))
        return gap or "complete"


CREATOR_SOURCE = "create_ix_arg"
SORT_ORDER = "asc"                                       # every scan walks the wallet from its start; "covered through slot" is only valid for this order


def _valid_scan(con, creator, coin_block_time, decision_at):
    """-> (row | None, reason). The earliest scan that is complete/capped, ran >= FINALITY_MARGIN_S after the coin and (live) at or before the decision."""
    rows = con.execute("select fetched_at, state, pages, covered_slot, sort_order from creator_scan_log where creator=? order by fetched_at", (creator,)).fetchall()
    if not rows:
        return None, "no_scan"
    usable = [r for r in rows if r[1] in ("complete", "capped")]
    if not usable:
        return None, "unknown_scan"
    timely = [r for r in usable if r[0] >= coin_block_time + FINALITY_MARGIN_S]
    if not timely:
        return None, "scan_before_coin"
    ok = [r for r in timely if decision_at is None or r[0] <= decision_at]
    if not ok:
        return None, "rescan_after_decision"
    return ok[0], None


def prior_launches_why(con, creator, *, coin_slot, coin_block_time, decision_at=None):
    """Why prior_launches is None: no_scan | unknown_scan | scan_before_coin | rescan_after_decision (a scan exists but only after the decision); None when a count exists."""
    return _valid_scan(con, creator, coin_block_time, decision_at)[1]


def prior_launches(con, creator, *, coin_slot, coin_block_time, decision_at=None):
    """-> None | {lb, complete, pages, source}. `lb` = this creator's launches with slot < coin_slot that THE CHOSEN SCAN had seen (seen_at <= its fetched_at: a later scan's finds never leak into an
    earlier decision): a LOWER BOUND (a launch funded by another payer is not in the wallet's history). `complete` = the wallet history was scanned through the coin's slot (whole scan complete,
    or an ASCENDING capped scan that already passed the coin's slot; a scan of any other order never claims it). 'none seen' (lb 0) means something only when complete. None (unknown stratum) per
    prior_launches_why: no scan, unknown scan (unreadable tx, quota stop, transport), scan < 60 s after the coin, or (live) only fetched after the decision."""
    row, _ = _valid_scan(con, creator, coin_block_time, decision_at)
    if row is None:
        return None
    fetched_at, state, pages, covered, order = row
    lb = con.execute("select count(*) from creator_launches where creator=? and slot < ? and seen_at <= ?", (creator, coin_slot, fetched_at)).fetchone()[0]
    complete = state == "complete" or (order == "asc" and covered is not None and coin_slot <= covered)
    return {"lb": lb, "complete": complete, "pages": pages, "source": CREATOR_SOURCE}
