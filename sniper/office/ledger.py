"""The Office paper book (spec 2026-10-28 §3.2, plan Task 3 as amended by Beta round 1, Zeta and Gamma).

An isolated ledger DB, data/office/ledger.db, separate from the desk paper book and from the funded runner:
- fills and marks come only from memecoin_desk_ps_paper.buy/sell (PumpSwap, integrity=True, the committed tape);
  PumpSwap is the only venue with fill math, so any other chain is refused, whatever the policy lists;
- every number comes from the OfficeBookPolicy frozen on the order at submit; a later policy never rewrites an
  open position's rails;
- the evaluated book takes the full frozen clip or nothing (no size cuts, Gamma / prereg Am1);
- each fill and exit writes ONE cashflow row with the modeled network+priority cost (EXEC_COST_SOL, the MEASURED MEAN,
  basis 'measured_mean'; the adverse-execution bound B is a separate prereg input) and its evidence;
- the book has explicit audited capital: initial capital and external flows in capital_flows; no implicit capital;
- every fill and exit happens at its scheduled due time, quoted and FX-priced causally AT that time; a watcher that was
  away longer than TICK_GRACE_S records an outage and re-arms the exit with a fresh attempt clock (an entry is
  censored), so a late proof never invents a past fill;
- the submitted network/mint/pool is bound: the pool must be the mint's canonical PumpSwap pool and every quote must
  carry that pool;
- unknown is typed: missing FX or route never fills, never marks, never becomes 0.

No import from the funded runner, its policy, the desk courier or sniper.settings (tests/office/test_ledger.py).
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import time
import math
import sqlite3
from decimal import Decimal, InvalidOperation
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from sniper.office import pods as _pods
from sniper.services import memecoin_desk_ps_paper as _psp
from sniper.services.memecoin_desk_paper import ENTRY_DELAY_S, EXEC_COST_SOL, EXIT_DELAY_S
from sniper.services.memecoin_desk_ps_tape import canonical_pool as _canonical_pool

_ps_estimate = _psp.estimate_sell
_ps_buy, _ps_sell = _psp.entry_buy, _psp.sell          # module attributes so tests can stub the quotes
EXIT_CUT_HOLD_S = 60.0                            # a requested exit keeps its cut this long through tape lag; live replay healed at 26-42 s, 3 of 7 not by 45 s
LIVE_CLOCK_MAX_TICK_AGE_S = 30.0
TICK_GRACE_S = 5.0                                # a due fill or exit more late than this was a watcher outage
COST_BASIS = "measured_mean"
SUPPORTED_CHAINS = ("solana",)                    # memecoin_desk_ps_paper is PumpSwap-only
LAMPORTS = 1_000_000_000
MARK_SOURCE = "ps_paper.sell"
TRANSIENT_FILL = frozenset({"ps_commit_unproven", "ps_capture_unproven", "capture_gap", "no_block_before_as_of", "pool_tip_unproven",
                            "pool_block_cut_unproven", "no_swaps_before_cut", "pool_state_stale"})
# pool_block_cut_unproven is not held: block and swap rows are both read before the cut, so that cut's answer can never change.
EXIT_CUT_HOLD=(TRANSIENT_FILL-{'pool_block_cut_unproven','pool_tip_unproven'})|{'frontier_not_committed','fx_unknown','exit_estimate_unavailable'}
EXIT_REQUEST_MAX_S=120.0                          # fresh cuts cannot chase forever: past this from the request, the terminal path applies
ZERO_QUOTE_REASONS=frozenset({'real_quote_insufficient','dust_or_invalid_sell','no_swaps_before_cut',
                             'no_route'})
BOOK_UNKNOWN_MARK_REASONS=frozenset({'clock_unknown','clock_invalid','fx_unknown',
                                    'mark_unproven','quote_binding_mismatch'})


@dataclass(frozen=True)
class OfficeBookPolicy:
    enabled: bool
    chains: tuple
    clip_usd: float
    rail_bounds: dict
    max_concurrent: int
    daily_loss_usd: float
    fill_model_version: str
    sl_pct: float
    tp_pct: float
    trail_pct: float
    catastrophe_pct: float
    max_hold_s: float
    cap_min_headroom_pct: float
    fx_max_age_s: float
    entry_timeout_s: float

    def frozen(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class SubmitResult:
    order_id: str
    state: str
    reason: str | None = None


@dataclass(frozen=True)
class ExitResult:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class ManageResult:
    filled: int = 0
    censored: int = 0
    closed: int = 0
    marked: int = 0
    unpriced: int = 0
    frontier_not_committed: int = 0


@dataclass(frozen=True)
class Equity:
    cash: float
    known_liq_lower: float | None
    unpriced_count: int
    unpriced_qty: int
    state: Literal["known", "partial", "unknown"]
    fill_model_version: str = ""                  # comma-joined when a book mixes versions (pinning refuses it)
    binding_kind_counts: dict = field(default_factory=dict)
    legs: list = field(default_factory=list)       # causal per-leg evidence; never substitutes a point for unknown


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
    order_id TEXT PRIMARY KEY, book_id TEXT NOT NULL, epoch_id TEXT NOT NULL, book_kind TEXT NOT NULL,
    network_id TEXT NOT NULL, mint TEXT NOT NULL, pool TEXT NOT NULL,
    decision_bundle_hash TEXT NOT NULL, policy_json TEXT NOT NULL, policy_hash TEXT NOT NULL,
    fill_model_version TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, requested_at REAL NOT NULL, due_at REAL,
    filled_at REAL, spend_lamports TEXT, tokens_atomic TEXT, spend_usd REAL, fill_proof TEXT,
    peak_lamports TEXT, exit_requested_at REAL, exit_reason TEXT, exit_requested_by TEXT,
    closed_at REAL, proceeds_lamports TEXT, exit_usd REAL, exit_proof TEXT, costs_usd REAL NOT NULL DEFAULT 0,
    ticker TEXT, decimals INTEGER, metadata_known_at REAL, entry_unit_price_usd REAL, exit_by TEXT,
    epoch_halted_at REAL, post_halt INTEGER);
CREATE TABLE IF NOT EXISTS marks(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, at REAL NOT NULL,
    lamports TEXT, source TEXT NOT NULL, reason TEXT, binding_kind TEXT, quote_proof TEXT);
CREATE INDEX IF NOT EXISTS marks_order_at ON marks(order_id, at);
CREATE INDEX IF NOT EXISTS marks_order_priced_at ON marks(order_id,at DESC,id DESC) WHERE lamports IS NOT NULL;
CREATE TABLE IF NOT EXISTS cashflows(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL,
    at REAL NOT NULL, lamports TEXT NOT NULL, cost_lamports TEXT NOT NULL, cost_basis TEXT NOT NULL,
    cost_evidence TEXT NOT NULL, fx_usd REAL NOT NULL, fx_at REAL NOT NULL, usd REAL NOT NULL, UNIQUE(order_id, kind));
CREATE INDEX IF NOT EXISTS orders_book_state ON orders(book_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS orders_epoch_coin ON orders(epoch_id, book_kind, network_id, mint);
CREATE TABLE IF NOT EXISTS ledger_epochs(epoch_id TEXT PRIMARY KEY, clip_usd REAL NOT NULL, rule_version TEXT NOT NULL,
    opened_at REAL NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epoch_pods_rules(epoch_id TEXT PRIMARY KEY REFERENCES ledger_epochs(epoch_id),
    rule_json TEXT NOT NULL, rule_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS books(book_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL REFERENCES ledger_epochs(epoch_id),
    kind TEXT NOT NULL CHECK(kind IN ('evaluated','discovery')), opened_at REAL NOT NULL, actor TEXT NOT NULL,
    note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS capital_flows(id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
    usd REAL NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, epoch_id TEXT, rule_version TEXT, transfer_id TEXT,
    CHECK(kind NOT LIKE 'transfer%' OR (epoch_id IS NOT NULL AND rule_version IS NOT NULL AND transfer_id IS NOT NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS capital_flows_transfer ON capital_flows(transfer_id, kind);
CREATE TABLE IF NOT EXISTS decision_receipts(action_key TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
    book_id TEXT NOT NULL, bundle_hash TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT, order_id TEXT,
    risk_version TEXT, actor_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS decision_receipts_created_at ON decision_receipts(created_at);
CREATE INDEX IF NOT EXISTS decision_receipts_order ON decision_receipts(order_id);
CREATE TABLE IF NOT EXISTS exit_askers(order_id TEXT NOT NULL, slot TEXT NOT NULL, decision_id TEXT NOT NULL, reason TEXT NOT NULL,
    answers_json TEXT NOT NULL, asked_at REAL NOT NULL, PRIMARY KEY(order_id, slot));
CREATE TRIGGER IF NOT EXISTS exit_askers_no_update BEFORE UPDATE ON exit_askers BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS exit_askers_no_delete BEFORE DELETE ON exit_askers BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE INDEX IF NOT EXISTS orders_closed_at ON orders(closed_at);
CREATE INDEX IF NOT EXISTS orders_bundle ON orders(decision_bundle_hash);
CREATE INDEX IF NOT EXISTS orders_filled_at ON orders(filled_at);
CREATE INDEX IF NOT EXISTS orders_open ON orders(book_id) WHERE state = 'open';
CREATE TRIGGER IF NOT EXISTS decision_receipts_no_update BEFORE UPDATE ON decision_receipts
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER IF NOT EXISTS decision_receipts_no_delete BEFORE DELETE ON decision_receipts
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TABLE IF NOT EXISTS outages(id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL,
    from_at REAL NOT NULL, to_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS epoch_entry_safety(epoch_id TEXT PRIMARY KEY REFERENCES ledger_epochs(epoch_id),
    config_hash TEXT, last_at REAL NOT NULL, last_known_equity_at REAL NOT NULL,
    peak_equity_usd REAL NOT NULL, unknown_since REAL, equity_usd REAL, state TEXT NOT NULL, reason TEXT,
    halt_kind TEXT, halt_reason TEXT, halted_at REAL, halt_actor TEXT,
    last_equity_read_at REAL, drawdown_equity_usd REAL, unpriced_count INTEGER NOT NULL DEFAULT 0,
    unpriced_order_ids_json TEXT NOT NULL DEFAULT '[]', demoted_order_ids_json TEXT NOT NULL DEFAULT '[]',
    demoted_seconds_json TEXT NOT NULL DEFAULT '{}', repromotions INTEGER NOT NULL DEFAULT 0,
    demoted_never_priced_order_ids_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS epoch_leg_safety(epoch_id TEXT NOT NULL,order_id TEXT NOT NULL,
    pause_since REAL,demoted_at REAL,last_at REAL NOT NULL,state TEXT NOT NULL,cause TEXT,
    repromotions INTEGER NOT NULL DEFAULT 0,awaiting_repromotion INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(epoch_id,order_id));
"""


def open_ledger(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    with _write(conn):
        order_columns={r[1] for r in conn.execute('PRAGMA table_info(orders)')}
        for name,kind in (('gate_basis','TEXT'),('exit_fill_kind','TEXT'),('entry_refusals','TEXT'),('exit_estimate_check','TEXT'),('exit_trigger_kind','TEXT'),('exit_cut_fx','REAL'),('exit_cut_fx_at','REAL'),('fill_gate_basis','TEXT'),('frontier_retries','INTEGER NOT NULL DEFAULT 0'),('exit_frontier_retries','INTEGER NOT NULL DEFAULT 0'),('sheet_expires_at','REAL'),('ticker','TEXT'),('decimals','INTEGER'),('metadata_known_at','REAL'),
                          ('entry_unit_price_usd','REAL'),('exit_by','TEXT'),('epoch_halted_at','REAL'),('post_halt','INTEGER'),
                          ('sheet_state','TEXT'),('sheet_class','TEXT'),('sheet_ticket_age_s','REAL'),('sheet_unknown_n','INTEGER'),('sheet_view_hash','TEXT'),
                          ('sells_basis','TEXT'),('sells_n','INTEGER'),('probe_age_s','REAL'),('probe_loss_pct','REAL')):
            if name not in order_columns:
                conn.execute('ALTER TABLE orders ADD COLUMN '+name+' '+kind)
                # Orders that exist when a stratum column is first added predate partial-sheet orders: known sheet, fresh flow. Backfilled ONCE here; a new order carries its stratum from its own insert
                # (submit, same transaction) and an order that does not stays NULL = unstamped, never read as known.
                if name in ('sheet_state','sells_basis'):conn.execute('UPDATE orders SET '+name+'=? WHERE '+name+' IS NULL',('known' if name=='sheet_state' else 'fresh_flow',))
        if 'epoch_id' not in {r[1] for r in conn.execute('PRAGMA table_info(decision_receipts)')}:
            conn.execute('ALTER TABLE decision_receipts ADD COLUMN epoch_id TEXT')
        decision_columns={r[1] for r in conn.execute('PRAGMA table_info(decision_receipts)')}
        for name in ('judgements_hash','decision_label'):
            if name not in decision_columns:conn.execute('ALTER TABLE decision_receipts ADD COLUMN '+name+' TEXT')
        safety_columns={r[1] for r in conn.execute('PRAGMA table_info(epoch_entry_safety)')}
        for name,kind in (('last_equity_read_at','REAL'),('drawdown_equity_usd','REAL'),
            ('unpriced_count','INTEGER NOT NULL DEFAULT 0'),('unpriced_order_ids_json',"TEXT NOT NULL DEFAULT '[]'"),
            ('demoted_order_ids_json',"TEXT NOT NULL DEFAULT '[]'"),('demoted_seconds_json',"TEXT NOT NULL DEFAULT '{}'"),
            ('repromotions','INTEGER NOT NULL DEFAULT 0'),('demoted_never_priced_order_ids_json',"TEXT NOT NULL DEFAULT '[]'")):
            if name not in safety_columns:conn.execute('ALTER TABLE epoch_entry_safety ADD COLUMN '+name+' '+kind)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(marks)")}
        for name in ("binding_kind", "quote_proof"):
            if name not in columns:
                conn.execute("ALTER TABLE marks ADD COLUMN " + name + " TEXT")
        for name, kind in (("mark_path", "TEXT"), ("mark_age_s", "REAL"), ("null_last_at", "REAL"), ("null_n", "INTEGER"), ("reason_branches", "TEXT")):
            if name not in columns:
                conn.execute("ALTER TABLE marks ADD COLUMN " + name + " " + kind)
    return conn


def open_readonly(path: Path) -> sqlite3.Connection:
    """A read-only handle for views and reports (no DDL, no writes): equity(), external_flows(), capital() and plain
    SELECTs on orders / marks / cashflows / capital_flows / books / ledger_epochs / outages."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _num(v):
    try:return type(v) in (int, float) and math.isfinite(v)
    except OverflowError:return False


def _fx(sol_usd, sol_at, at, max_age_s):
    """Causal SOL/USD: an observation at or before `at` and no older than max_age_s, else None (never 0)."""
    ok = _num(sol_usd) and sol_usd > 0 and _num(sol_at)
    return float(sol_usd) if ok and 0 <= at - sol_at <= max_age_s else None


RAILS = ("sl_pct", "tp_pct", "trail_pct", "catastrophe_pct")


def policy_refusal(p: OfficeBookPolicy):
    """None, or why this policy cannot run: every number finite and non-bool in range, every rail inside its frozen
    bound, and no requested guard the ledger cannot enforce (never silently ignored)."""
    for name in ("clip_usd", "daily_loss_usd", "max_hold_s", "fx_max_age_s", "entry_timeout_s") + RAILS:
        v = getattr(p, name)
        if not _num(v) or v < 0 or (name in ("clip_usd", "max_hold_s", "fx_max_age_s", "entry_timeout_s") and v <= 0):
            return "policy_invalid:" + name
    if type(p.max_concurrent) is not int or p.max_concurrent < 1:
        return "policy_invalid:max_concurrent"
    if type(p.enabled) is not bool or not isinstance(p.fill_model_version, str) or not p.fill_model_version:
        return "policy_invalid:enabled_or_version"
    if not _num(p.cap_min_headroom_pct) or p.cap_min_headroom_pct != 0:
        return "unsupported:cap_min_headroom_pct"     # no verdict cap in the Office book yet: refuse, never ignore
    if not isinstance(p.rail_bounds, dict) or any(k not in RAILS for k in p.rail_bounds):
        return "policy_invalid:rail_bounds"
    for name, bound in p.rail_bounds.items():
        lo, hi = bound if isinstance(bound, (list, tuple)) and len(bound) == 2 else (None, None)
        if not (_num(lo) and _num(hi) and lo <= hi):
            return "policy_invalid:rail_bounds"
        if not lo <= getattr(p, name) <= hi:
            return "rail_out_of_bounds:" + name
    return None


def _book(book_id):
    if not isinstance(book_id, str) or not book_id or len(book_id) > 64:
        raise ValueError("book_id_invalid")
    return book_id


def open_epoch(conn, *, epoch_id: str, clip_usd: float, rule_version: str, actor: str, note: str, now: float,
               pods_rule: dict | None = None):
    """An evaluation epoch: its frozen fixed clip and its frozen reallocation rule version (prereg Am3). pods_rule (the
    config/office/pods.json content, version == rule_version) is frozen with it; its evaluated books trade only under
    the pod caps it derives, and without one they refuse (pods_rule_unknown)."""
    if not (isinstance(epoch_id, str) and epoch_id and _num(clip_usd) and clip_usd > 0 and isinstance(rule_version, str)
            and rule_version and actor and note):
        raise ValueError("epoch_invalid")
    if pods_rule is not None:
        _pods.validate_rule(pods_rule)
        if pods_rule["version"] != rule_version:
            raise ValueError("pods_rule_not_epoch_rule")
        frozen = json.dumps(pods_rule, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with _write(conn):
        conn.execute("INSERT INTO ledger_epochs VALUES(?,?,?,?,?,?)", (epoch_id, clip_usd, rule_version, now, actor, note))
        if pods_rule is not None:
            conn.execute("INSERT INTO epoch_pods_rules VALUES(?,?,?)", (epoch_id, frozen, _hash(frozen)))


def open_book(conn, *, book_id: str, epoch_id: str, kind: str, actor: str, note: str, now: float):
    """A pod's book inside one epoch: 'evaluated' (counts toward the Office verdict) or 'discovery' (never does)."""
    _book(book_id)
    if kind not in ("evaluated", "discovery") or not actor or not note:
        raise ValueError("book_invalid")
    with _write(conn):
        if not conn.execute("SELECT 1 FROM ledger_epochs WHERE epoch_id=?", (epoch_id,)).fetchone():
            raise ValueError("epoch_unknown")
        conn.execute("INSERT INTO books VALUES(?,?,?,?,?,?)", (book_id, epoch_id, kind, now, actor, note))


def _book_row(conn, book_id):
    return conn.execute("SELECT b.*, e.clip_usd, e.rule_version FROM books b JOIN ledger_epochs e USING(epoch_id) "
                        "WHERE b.book_id=?", (book_id,)).fetchone()


def fund(conn, *, book_id: str, usd: float, kind: str, actor: str, note: str, now: float):
    """An audited external capital flow into one book: 'initial' once per book, then 'deposit' / 'withdrawal'. Each
    book (a pod, spec §2.6) trades only on its own capital."""
    _book(book_id)
    if kind not in ("initial", "deposit", "withdrawal") or not _num(usd) or usd <= 0 or not actor or not note:
        raise ValueError("capital_flow_invalid")
    if _book_row(conn, book_id) is None:
        raise ValueError("book_unknown")
    with _write(conn):
        has = conn.execute("SELECT COUNT(*) FROM capital_flows WHERE book_id=? AND kind='initial'", (book_id,)).fetchone()[0]
        if (kind == "initial") == bool(has):
            raise ValueError("capital_initial_once")
        conn.execute("INSERT INTO capital_flows(book_id, at, kind, usd, actor, note) VALUES(?,?,?,?,?,?)",
                     (book_id, now, kind, -usd if kind == "withdrawal" else usd, actor, note))


def lookup_transfer(conn, transfer_id: str, *, expect: dict | None = None) -> dict:
    """Read-only (Codex T6 reconcile): {state, legs}. state 'missing' (no row), 'booked' (exactly one transfer_out and
    one transfer_in with equal and opposite amounts, the same epoch and rule, and, when `expect` {from_book, to_book,
    usd, epoch_id, rule_version} is given, equal to it), 'partial' (one leg only) or 'conflict' (anything else).
    Never a write."""
    rows = conn.execute("SELECT book_id, kind, usd, epoch_id, rule_version FROM capital_flows WHERE transfer_id=? "
                        "ORDER BY id", (transfer_id,)).fetchall()
    legs = [dict(zip(("book_id", "kind", "usd", "epoch_id", "rule_version"), r)) for r in rows]
    if not legs:
        return {"state": "missing", "legs": []}
    out = [l for l in legs if l["kind"] == "transfer_out"]
    inn = [l for l in legs if l["kind"] == "transfer_in"]
    if len(legs) == 1 and len(out) + len(inn) == 1:
        return {"state": "partial", "legs": legs}
    if len(legs) != 2 or len(out) != 1 or len(inn) != 1:
        return {"state": "conflict", "legs": legs}
    o, i = out[0], inn[0]
    got = dict(from_book=o["book_id"], to_book=i["book_id"], usd=i["usd"], epoch_id=i["epoch_id"],
               rule_version=i["rule_version"])
    ok = (o["usd"] == -i["usd"] and (o["epoch_id"], o["rule_version"]) == (i["epoch_id"], i["rule_version"])
          and (expect is None or got == expect))
    return {"state": "booked" if ok else "conflict", "legs": legs, **({"transfer": got} if ok else {})}


def transfer(conn, *, transfer_id: str, from_book: str, to_book: str, usd: float, epoch_id: str, rule_version: str,
             actor: str, note: str, now: float, sol_usd=None, sol_at=None, fx_max_age_s: float = 120.0) -> str:
    """A reallocation between two books of one epoch and one kind (the frozen rule decides it; this only books it): one
    paired transfer_out / transfer_in with structured epoch_id and rule_version, so the Office total is unchanged and the
    auditor can check the rule. Refused for another epoch or kind, a rule_version other than the epoch's frozen rule,
    or beyond the source book's free cash at causal FX (its pending and open reserves stay covered).
    transfer_id is the stable effect key (Delta): a replay with the same books, amount, epoch and rule is a no-op that
    returns 'replayed'; the same id with anything else raises transfer_id_conflict. Returns 'booked' or 'replayed'."""
    if not isinstance(transfer_id, str) or not transfer_id:
        raise ValueError("transfer_invalid")
    with _write(conn):
        found = lookup_transfer(conn, transfer_id, expect=dict(from_book=from_book, to_book=to_book, usd=usd,
                                                               epoch_id=epoch_id, rule_version=rule_version))
        if found["state"] == "booked":                # the id decides first: a retry never re-validates into a 2nd effect
            return "replayed"
        if found["state"] != "missing":
            raise ValueError("transfer_id_conflict")
        _book(from_book), _book(to_book)
        if from_book == to_book or not _num(usd) or usd <= 0 or not actor or not note or not epoch_id or not rule_version:
            raise ValueError("transfer_invalid")
        src, dst = _book_row(conn, from_book), _book_row(conn, to_book)
        if src is None or dst is None:
            raise ValueError("book_unknown")
        if not (src["epoch_id"] == dst["epoch_id"] == epoch_id and src["kind"] == dst["kind"]):
            raise ValueError("transfer_across_epoch_or_kind")
        if rule_version != src["rule_version"]:
            raise ValueError("rule_version_not_epoch_rule")
        fx = _fx(sol_usd, sol_at, now, fx_max_age_s)
        if fx is None:
            raise ValueError("fx_unknown")
        if not conn.execute("SELECT 1 FROM capital_flows WHERE book_id=? AND kind='initial'", (to_book,)).fetchone():
            raise ValueError("no_capital:" + to_book)
        if free_cash(conn, fx, book_id=from_book) < usd:
            raise ValueError("insufficient_capital")
        conn.executemany("INSERT INTO capital_flows(book_id, at, kind, usd, actor, note, epoch_id, rule_version, "
                         "transfer_id) VALUES(?,?,?,?,?,?,?,?,?)",
                         [(from_book, now, "transfer_out", -usd, actor, note, epoch_id, rule_version, transfer_id),
                          (to_book, now, "transfer_in", usd, actor, note, epoch_id, rule_version, transfer_id)])
    return "booked"


_IN_BOOK = "(? IS NULL OR book_id = ?)"


def capital(conn, book_id=None):
    return conn.execute("SELECT COALESCE(SUM(usd), 0) FROM capital_flows WHERE " + _IN_BOOK,
                        (book_id, book_id)).fetchone()[0]


def external_flows(conn, book_id=None):
    """Net EXTERNAL capital (initial + deposits - withdrawals) only: transfers net to zero by kind, not by luck."""
    return conn.execute("SELECT COALESCE(SUM(usd), 0) FROM capital_flows WHERE kind IN ('initial','deposit','withdrawal') "
                        "AND " + _IN_BOOK, (book_id, book_id)).fetchone()[0]


def _flows(conn, book_id=None):
    return conn.execute("SELECT COALESCE(SUM(c.usd), 0) FROM cashflows c JOIN orders o USING(order_id) WHERE "
                        "(? IS NULL OR o.book_id = ?)", (book_id, book_id)).fetchone()[0]


COST_LAMPORTS = {k: int(round(EXEC_COST_SOL[k] * LAMPORTS)) for k in ("entry", "exit")}


def free_cash(conn, fx, *, book_id=None, exclude=None):
    """One book's (None = the whole Office's) capital + realized cashflows, minus what is already promised at this FX:
    each pending order's OWN frozen clip plus its entry and exit cost, and each open position's exit cost. No implicit
    borrowing (Zeta a58513e74), and no borrowing between books."""
    cost = (COST_LAMPORTS["entry"] + COST_LAMPORTS["exit"]) / LAMPORTS * fx
    pending = sum(json.loads(r[0])["clip_usd"] + cost for r in conn.execute(
        "SELECT policy_json FROM orders WHERE state='pending' AND order_id IS NOT ? AND " + _IN_BOOK,
        (exclude, book_id, book_id)))
    opened = conn.execute("SELECT COUNT(*) FROM orders WHERE state='open' AND " + _IN_BOOK,
                          (book_id, book_id)).fetchone()[0] * COST_LAMPORTS["exit"] / LAMPORTS * fx
    return capital(conn, book_id) + _flows(conn, book_id) - pending - opened


def _policy(row) -> OfficeBookPolicy:
    d = json.loads(row["policy_json"])
    d["chains"] = tuple(d["chains"])
    return OfficeBookPolicy(**d)


def submit(conn, policy: OfficeBookPolicy, **kw) -> SubmitResult:
    """Idempotent on order_id with the SAME payload; the same id with any other payload or policy is refused. The
    checks and the insert are one BEGIN IMMEDIATE transaction, so two writers cannot both pass the capital and
    concurrency checks."""
    with _write(conn):
        result = _submit(conn, policy, **kw)
        if result.state == 'pending':
            epoch = conn.execute('SELECT epoch_id FROM orders WHERE order_id=?', (kw['order_id'],)).fetchone()
            _record_gate_basis(conn, kw['order_id'], epoch[0], kw['now'])
        return result


def daily_realized_usd(conn, *, book_id, now):
    """The daily-loss halt quantity: cashflows of orders closed since the UTC day start of `now`. The operating view reads
    this same function, so the number shown predicts the halt."""
    day0 = now - now % 86400
    return conn.execute("SELECT COALESCE(SUM(c.usd), 0) FROM cashflows c JOIN orders o USING(order_id) "
                        "WHERE o.book_id=? AND o.state='closed' AND o.closed_at >= ?", (book_id, day0)).fetchone()[0]


STRATUM_KEYS = ('sheet_state', 'sheet_class', 'sheet_ticket_age_s', 'sheet_unknown_n', 'sheet_view_hash', 'sells_basis', 'sells_n', 'probe_age_s', 'probe_loss_pct')


def _write_stratum(conn, order_id, stratum):
    """Which sheet an order rode on (known / partial) and its sells basis, as queryable columns. Called inside submit's transaction; an invalid stratum raises and the order is not created."""
    if (type(stratum) is not dict or set(stratum) != set(STRATUM_KEYS) or stratum['sheet_state'] not in ('known', 'partial')
            or stratum['sells_basis'] not in ('fresh_flow', 'cut_tape', 'sell_probe')
            or any(stratum[k] is not None and not _num(stratum[k]) for k in ('sheet_ticket_age_s', 'sheet_unknown_n', 'sells_n', 'probe_age_s', 'probe_loss_pct'))
            or any(stratum[k] is not None and type(stratum[k]) is not str for k in ('sheet_class', 'sheet_view_hash'))):
        raise ValueError('stratum_invalid')
    conn.execute("UPDATE orders SET sheet_state=?,sheet_class=?,sheet_ticket_age_s=?,sheet_unknown_n=?,sheet_view_hash=?,sells_basis=?,sells_n=?,probe_age_s=?,probe_loss_pct=? WHERE order_id=?",
                 (*[stratum[k] for k in STRATUM_KEYS], order_id))


@contextmanager
def _write(conn):
    """BEGIN IMMEDIATE when this call owns the transaction, a SAVEPOINT when the caller already holds one."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT office_ledger")
        try:
            yield
            conn.execute("RELEASE office_ledger")
        except BaseException:
            conn.execute("ROLLBACK TO office_ledger")
            conn.execute("RELEASE office_ledger")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _submit(conn, policy: OfficeBookPolicy, *, book_id: str, order_id: str, network_id: str, mint: str, pool: str,
            decision_bundle_hash: str, now: float, sol_usd=None, sol_at=None, size_usd: float | None = None,
            metadata: dict | None = None) -> SubmitResult:
    bad = policy_refusal(policy)
    if bad:
        return SubmitResult(order_id, "refused", bad)
    try:
        _book(book_id)
    except ValueError:
        return SubmitResult(order_id, "refused", "book_id_invalid")
    book = _book_row(conn, book_id)
    frozen = policy.frozen()
    prior = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if prior is not None:
        same = (prior["book_id"], prior["network_id"], prior["mint"], prior["pool"], prior["decision_bundle_hash"],
                prior["policy_json"]) == (book_id, network_id, mint, pool, decision_bundle_hash, frozen)
        return (SubmitResult(order_id, prior["state"], prior["reason"]) if same
                else SubmitResult(order_id, "refused", "order_id_conflict"))
    if book is None:
        return SubmitResult(order_id, "refused", "book_unknown")
    if book['kind']=='evaluated':
        safety=evaluation_safety(conn,epoch_id=book['epoch_id'],now=now,sol_usd=sol_usd,sol_at=sol_at)
        if not _entry_gate_open(conn,safety,book['epoch_id'],now,sol_usd,sol_at):
            return SubmitResult(order_id,'refused',safety['reason'])
    if not policy.enabled:
        return SubmitResult(order_id, "refused", "office_book_disabled")
    if book["kind"] == "evaluated" and policy.clip_usd != book["clip_usd"]:
        return SubmitResult(order_id, "refused", "clip_not_epoch_clip")
    if conn.execute("SELECT 1 FROM orders WHERE epoch_id=? AND book_kind=? AND network_id=? AND mint=? LIMIT 1",
                    (book["epoch_id"], book["kind"], network_id, mint)).fetchone():
        return SubmitResult(order_id, "refused", "coin_already_entered")      # one entry per coin across the books
    if network_id not in SUPPORTED_CHAINS or network_id not in policy.chains:
        return SubmitResult(order_id, "refused", "chain_not_supported")
    if size_usd is not None and size_usd != policy.clip_usd:
        return SubmitResult(order_id, "refused", "size_cut_not_allowed")
    try:
        if pool != _canonical_pool(mint):
            return SubmitResult(order_id, "refused", "pool_not_canonical")
    except Exception:
        return SubmitResult(order_id, "refused", "mint_invalid")
    if not conn.execute("SELECT 1 FROM capital_flows WHERE book_id=? AND kind='initial'", (book_id,)).fetchone():
        return SubmitResult(order_id, "refused", "no_capital")
    fx = _fx(sol_usd, sol_at, now, policy.fx_max_age_s)
    if fx is None:
        return SubmitResult(order_id, "refused", "fx_unknown")
    need = policy.clip_usd + (COST_LAMPORTS["entry"] + COST_LAMPORTS["exit"]) / LAMPORTS * fx
    if free_cash(conn, fx, book_id=book_id) < need:
        return SubmitResult(order_id, "refused", "insufficient_capital")
    live = conn.execute("SELECT COUNT(*) FROM orders WHERE book_id=? AND state IN ('pending','open')",
                        (book_id,)).fetchone()[0]
    if live >= policy.max_concurrent:
        return SubmitResult(order_id, "refused", "max_concurrent")
    day0 = now - now % 86400                         # also the pod/office halt windows below
    realized = daily_realized_usd(conn, book_id=book_id, now=now)
    if realized <= -policy.daily_loss_usd:
        return SubmitResult(order_id, "refused", "daily_loss_halt")
    if book["kind"] == "evaluated":                   # pod caps from the epoch's frozen rule, never the caller's policy
        row = conn.execute("SELECT rule_json FROM epoch_pods_rules WHERE epoch_id=?", (book["epoch_id"],)).fetchone()
        if row is None:
            return SubmitResult(order_id, "refused", "pods_rule_unknown")
        books = [r[0] for r in conn.execute("SELECT book_id FROM books WHERE epoch_id=? AND kind='evaluated'",
                                            (book["epoch_id"],))]
        rule = json.loads(row[0])
        try:
            cap = _pods.pod_caps(rule, {b: capital(conn, b) for b in books})[book_id]
        except ValueError:
            return SubmitResult(order_id, "refused", "pods_capital_invalid")
        marks = ",".join("?" * len(books))          # the totals bind on their own, not only through the per-pod sum
        if conn.execute("SELECT COUNT(*) FROM orders WHERE book_id IN (%s) AND state IN ('pending','open')" % marks,
                        books).fetchone()[0] >= rule["max_concurrent_total"]:
            return SubmitResult(order_id, "refused", "office_max_concurrent")
        if conn.execute("SELECT COALESCE(SUM(c.usd), 0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id "
                        "IN (%s) AND o.state='closed' AND o.closed_at >= ?" % marks,
                        (*books, day0)).fetchone()[0] <= -rule["daily_loss_usd_total"]:
            return SubmitResult(order_id, "refused", "office_daily_loss_halt")
        if live >= cap["max_concurrent"]:
            return SubmitResult(order_id, "refused", "pod_max_concurrent")
        if realized <= -cap["daily_loss_usd"]:
            return SubmitResult(order_id, "refused", "pod_daily_loss_halt")
    conn.execute("INSERT INTO orders(order_id, book_id, epoch_id, book_kind, network_id, mint, pool, decision_bundle_hash, "
                 "policy_json, policy_hash, fill_model_version, state, requested_at, due_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)",
                 (order_id, book_id, book["epoch_id"], book["kind"], network_id, mint, pool, decision_bundle_hash,
                  frozen, _hash(frozen),
                  policy.fill_model_version, now, now + ENTRY_DELAY_S))
    data=metadata if type(metadata) is dict else {}
    known_at=data.get('metadata_known_at')
    causal=_num(known_at) and 0<=known_at<=now
    ticker=data.get('ticker')
    decimals=data.get('decimals')
    conn.execute('UPDATE orders SET sheet_expires_at=? WHERE order_id=?',(data.get('sheet_expires_at'),order_id))   # same transaction as the insert
    if data.get('stratum') is not None:_write_stratum(conn,order_id,data['stratum'])             # so a stratum-carrying order cannot exist without its stratum columns
    conn.execute('UPDATE orders SET ticker=?,decimals=?,metadata_known_at=?,post_halt=0 WHERE order_id=?',
                 (ticker if causal and type(ticker) is str and 0<len(ticker)<=64 else None,
                  decimals if causal and type(decimals) is int and 0<=decimals<=18 else None,
                  known_at if causal else None,order_id))
    return SubmitResult(order_id, "pending")


def request_exit(conn, *, order_id: str, reason: str, requested_by: str, now: float,exit_by=None) -> ExitResult:
    """An agent asks; manage() executes after EXIT_DELAY_S. Typed refusal on an unknown or non-open order."""
    if exit_by not in (None,'judge','rails'):raise ValueError('exit_by_invalid')
    with _write(conn):                               # one conditional update: never on a row another writer closed
        conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason=?, exit_requested_by=?, due_at=?,exit_by=? "
                     "WHERE order_id=? AND state='open' AND exit_requested_at IS NULL",
                     (now, reason, requested_by, now + EXIT_DELAY_S,exit_by,order_id))
        row = conn.execute("SELECT state FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        return ExitResult(False, "unknown_order")
    return ExitResult(True) if row["state"] == "open" else ExitResult(False, "not_open")


def _record_cashflow(conn, order_id, kind, at, lamports, fx, fx_at):
    """The ONE debit/credit of a fill or an exit, with the modeled network+priority cost (the measured mean) and its
    evidence."""
    cost = int(round(EXEC_COST_SOL[kind] * LAMPORTS))
    sign = -1 if kind == "entry" else 1
    usd = (sign * lamports - cost) / LAMPORTS * fx
    conn.execute("INSERT INTO cashflows(order_id, kind, at, lamports, cost_lamports, cost_basis, cost_evidence, fx_usd, "
                 "fx_at, usd) VALUES(?,?,?,?,?,?,?,?,?,?)", (order_id, kind, at, str(lamports), str(cost), COST_BASIS,
                                                         EXEC_COST_SOL["source"], fx, fx_at, usd))
    return cost / LAMPORTS * fx


def _outage(conn, row, kind, now):
    conn.execute("INSERT INTO outages(order_id, kind, from_at, to_at) VALUES(?,?,?,?)",
                 (row["order_id"], kind, row["due_at"], now))


def _rail(pol, row, value, now):
    """Pure rails on SOL value vs SOL spent, from the policy frozen on the order."""
    if now - row["filled_at"] >= pol.max_hold_s:
        return "max_hold"
    spend = int(row["spend_lamports"])
    if value is None or spend <= 0:
        return None
    pnl = (value / spend - 1.0) * 100.0
    if pol.catastrophe_pct > 0 and pnl <= -pol.catastrophe_pct:
        return "catastrophe"
    if pol.sl_pct > 0 and pnl <= -pol.sl_pct:
        return "stop"
    if pol.tp_pct > 0 and pnl >= pol.tp_pct:
        return "tp"
    peak = int(row["peak_lamports"] or 0)
    if pol.trail_pct > 0 and peak > spend and value <= peak * (1 - pol.trail_pct / 100.0):
        return "trail"
    return None


CLOCK_MAX_AGE_S = 3600.0                         # the D sheet's offset max age (memecoin_desk_contract.CLOCK_MAX_AGE_S)


def _offset(clock_sample, now):
    """host - corrected seconds from ONE clock_offset.json sample {offset_s, sampled_at (host)}, or None when it is
    missing, non-finite, from the future or older than CLOCK_MAX_AGE_S. Never an implicit 0."""
    off, at = (clock_sample or {}).get("offset_s"), (clock_sample or {}).get("sampled_at")
    if not (_num(off) and _num(at)):
        return None
    return float(off) if 0 <= now - (at - off) <= CLOCK_MAX_AGE_S else None


def manage(conn, policy: OfficeBookPolicy, *, desk_dir: Path, now: float, clock_sample, sol_usd=None,
           sol_at=None, mark_min_interval_s: float | None = None) -> ManageResult:
    """Fill pending orders, mark open ones, fire rails and execute exits. Each order's change is one transaction.
    `policy` is accepted for the caller's audit trail only: every order runs on the policy frozen at its submit.
    Clock domain (Delta, T7): every ledger time (now, due, fills, marks, FX) is corrected UTC. The PumpSwap quote
    helpers read the tape's HOST receipt cut, so a quote time is converted to host (+ offset_s from the ONE injected
    clock sample) only at that call, and the sample is kept in the proof. An unknown offset reads no tape: fills and
    exits re-arm with a fresh attempt clock, marks are recorded unpriced clock_unknown."""
    if mark_min_interval_s is not None and not (_num(mark_min_interval_s) and mark_min_interval_s > 0):
        raise ValueError("mark_min_interval_invalid")      # None, or a finite positive number: never inf/NaN/bool/<=0
    off = _offset(clock_sample, now)
    clock = None if off is None else {"offset_s": off, "sampled_at": float(clock_sample["sampled_at"])}
    n = dict(filled=0, censored=0, closed=0, marked=0, unpriced=0, frontier_not_committed=0)
    n['censored']+=_manage_evaluation_safety(conn,now=now,sol_usd=sol_usd,sol_at=sol_at)
    ids = [r[0] for r in conn.execute("SELECT order_id FROM orders WHERE state IN ('pending','open') ORDER BY requested_at")]
    for order_id in ids:
        with _write(conn):                           # re-read under the write lock: a stale row never refinalizes
            row = conn.execute("SELECT * FROM orders WHERE order_id=? AND state IN ('pending','open')",
                               (order_id,)).fetchone()
            if row is None:
                continue
            pol = _policy(row)
            if row["state"] == "pending":
                _fill(conn, pol, row, desk_dir, now, sol_usd, sol_at, n, clock)
            else:
                _hold(conn, pol, row, desk_dir, now, sol_usd, sol_at, n, clock, mark_min_interval_s)
    n['censored']+=_manage_evaluation_safety(conn,now=now,sol_usd=sol_usd,sol_at=sol_at)
    _check_terminal_estimates(conn, desk_dir, now, clock)
    return ManageResult(**n)


def _quote(fn, desk_dir, mint, amount, at, now, clock, detail=None):
    """One PumpSwap quote at corrected time `at`, read on the tape's host clock; the sample and host times are kept."""
    if clock is None:
        return None, "clock_unknown"
    off = clock["offset_s"]
    # Host clock, as off maps corrected time to it. Kill OFFICE_CAPTURE_LIVE_CLOCK=0 returns to the tick's frozen time.
    # Only for a tick running now: a historical replay keeps its frozen time.
    live_now = time.time if os.environ.get("OFFICE_CAPTURE_LIVE_CLOCK", "1") != "0" and abs(time.time() - (now + off)) <= LIVE_CLOCK_MAX_TICK_AGE_S else None
    if live_now is None and detail is not None:
        detail.append("live_clock_off")
    quote, why = fn(desk_dir, mint, amount, at + off, evaluated_at=now + off, integrity=True, live_now=live_now, detail=detail)
    if quote is not None:
        quote = dict(quote, clock=dict(clock, host_at=at + off, host_evaluated_at=now + off))
    return quote, why


def _entry_refusal(conn, row, why, detail=()):
    """Counts per reason and branch, so a late fill can be explained. Never read to decide."""
    counts = json.loads(row["entry_refusals"]) if row["entry_refusals"] else {}
    for key in (why, *("%s:%s" % (why, branch) for branch in detail)):
        counts[key] = counts.get(key, 0) + 1
    conn.execute("UPDATE orders SET entry_refusals=? WHERE order_id=?", (json.dumps(counts, sort_keys=True), row["order_id"]))


def _fill(conn, pol, row, desk_dir, now, sol_usd, sol_at, n, clock):
    at = row["due_at"]
    if now < at:return
    if now - row["requested_at"] > pol.entry_timeout_s:
        conn.execute("UPDATE orders SET state='censored', reason=? WHERE order_id=?",
                     (row["reason"] or "entry_timeout", row["order_id"]))
        n["censored"] += 1
        return
    if row['book_kind']=='evaluated':
        safety=evaluation_safety(conn,epoch_id=row['epoch_id'],now=now,sol_usd=sol_usd,sol_at=sol_at)
        if safety['state']=='halted':
            n['censored']+=safety.get('censored_n',0)
            return
        if not _entry_gate_open(conn,safety,row['epoch_id'],now,sol_usd,sol_at):
            _entry_refusal(conn,row,safety['reason'])
            conn.execute('UPDATE orders SET reason=?,due_at=? WHERE order_id=?',
                         (safety['reason'],now+ENTRY_DELAY_S,row['order_id']))
            return
        _record_gate_basis(conn,row['order_id'],row['epoch_id'],now,column='fill_gate_basis')
    if now - at > TICK_GRACE_S and row["reason"] == "frontier_not_committed":
        # The watcher was here, retrying this cut: the tape's commit lag ran past the grace (live lag p90 4.5 s, max 6.5 s).
        # Still never a booking in the past: a fresh attempt at a later cut, bounded by entry_timeout_s.
        if row["sheet_expires_at"] is not None and now + ENTRY_DELAY_S > row["sheet_expires_at"]:
            conn.execute("UPDATE orders SET state='censored', reason='sheet_expired' WHERE order_id=?", (row["order_id"],))
            n["censored"] += 1
            return
        _entry_refusal(conn, row, "frontier_retry_expired")
        conn.execute("UPDATE orders SET reason='frontier_retry_expired', frontier_retries=frontier_retries+1, due_at=? WHERE order_id=?", (now + ENTRY_DELAY_S, row["order_id"]))
        return
    if now - at > TICK_GRACE_S:                      # the watcher was away: never book an entry in the past
        _outage(conn, row, "entry", now)
        conn.execute("UPDATE orders SET state='censored', reason='watcher_outage' WHERE order_id=?", (row["order_id"],))
        n["censored"] += 1
        return
    if row["sheet_expires_at"] is not None and at > row["sheet_expires_at"]:   # every path to a fill, so age at fill can't pass the limit
        conn.execute("UPDATE orders SET state='censored', reason='sheet_expired' WHERE order_id=?", (row["order_id"],))
        n["censored"] += 1
        return
    fx = _fx(sol_usd, sol_at, at, pol.fx_max_age_s)
    detail = []
    quote, why = (None, "fx_unknown") if fx is None else _quote(_ps_buy, desk_dir, row["mint"],
                                                                int(pol.clip_usd / fx * LAMPORTS), at, now, clock, detail)
    if quote is not None and (quote.get("pool") != row["pool"] or not str(quote.get("tokens_atomic", "")).isdigit()
                              or int(quote["tokens_atomic"]) <= 0):
        quote, why = None, "quote_binding_mismatch"
    if quote is None:
        _entry_refusal(conn, row, why, detail)
        if why == "frontier_not_committed":
            conn.execute("UPDATE orders SET reason=? WHERE order_id=?", (why, row["order_id"]))
            n["frontier_not_committed"] += 1
        elif why in TRANSIENT_FILL or why in ("fx_unknown", "clock_unknown"):   # retry from a fresh attempt clock
            conn.execute("UPDATE orders SET reason=?, due_at=? WHERE order_id=?", (why, now + ENTRY_DELAY_S, row["order_id"]))
        else:
            conn.execute("UPDATE orders SET state='censored', reason=? WHERE order_id=?", (why, row["order_id"]))
            n["censored"] += 1
        return
    spent = int(quote["spend_lamports"])
    if free_cash(conn, fx, book_id=row["book_id"], exclude=row["order_id"]) < (spent + COST_LAMPORTS["entry"] + COST_LAMPORTS["exit"]) / LAMPORTS * fx:
        conn.execute("UPDATE orders SET state='censored', reason='insufficient_capital' WHERE order_id=?", (row["order_id"],))
        n["censored"] += 1
        return
    cost_usd = _record_cashflow(conn, row["order_id"], "entry", at, spent, fx, sol_at)
    conn.execute("UPDATE orders SET state='open', reason=NULL, due_at=NULL, filled_at=?, spend_lamports=?, "
                 "tokens_atomic=?, spend_usd=?, costs_usd=costs_usd+?, fill_proof=?, peak_lamports=? WHERE order_id=?",
                 (at, str(spent), quote["tokens_atomic"], spent / LAMPORTS * fx, cost_usd,
                  json.dumps(quote, sort_keys=True, default=str), str(spent), row["order_id"]))
    conn.execute('UPDATE orders SET entry_unit_price_usd=? WHERE order_id=?',
                 (_entry_unit_price(spent / LAMPORTS * fx,quote['tokens_atomic'],row['decimals']),row['order_id']))
    n["filled"] += 1


def _entry_unit_price(spend_usd,tokens_atomic,decimals):
    """Approximate display USD per whole token; accounting retains exact atomic TEXT."""
    if type(decimals) is not int or not 0<=decimals<=18:return None
    try:
        price=float(Decimal(str(spend_usd))*Decimal(10)**decimals/Decimal(tokens_atomic))
        return price if math.isfinite(price) and price>0 else None
    except (InvalidOperation,ValueError,OverflowError,ZeroDivisionError):return None


MARK_MAX_AGE_S = 30.                            # a committed cut older than this prices nothing: null, never a stale price (commit lag p90 4.5 s, max 6.5 s)


def _committed_cut(desk_dir, clock):
    """The tape's commit watermark on the ledger's corrected clock, or None when it can't be read, which keeps the requested-cut path."""
    if clock is None:
        return None
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        through = beat["ps_committed_through"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return through - clock["offset_s"] if _num(through) else None


def lagged_row(conn, row):
    return conn.execute("SELECT id, at, reason, lamports, null_last_at FROM marks WHERE order_id=? ORDER BY id DESC LIMIT 1", (row["order_id"],)).fetchone()


def _null_mark(conn, row, last, now, why, detail=None):
    """A run of the same null is one row: first time in at, last time and count kept. detail: the refusal's branch, counted per run."""
    branches = list(detail or ())   # every branch of each attempt; attempts with none add nothing, so never divide these by null_n
    if last is not None and last["lamports"] is None and last["reason"] == why:
        conn.execute("UPDATE marks SET null_last_at=?, null_n=COALESCE(null_n,1)+1 WHERE id=?", (now, last["id"]))
        if branches:
            prior = conn.execute("SELECT reason_branches FROM marks WHERE id=?", (last["id"],)).fetchone()[0]
            counts = json.loads(prior) if prior else {}
            for branch in branches:
                counts[branch] = counts.get(branch, 0) + 1
            conn.execute("UPDATE marks SET reason_branches=? WHERE id=?", (json.dumps(counts, sort_keys=True), last["id"]))
    else:
        conn.execute("INSERT INTO marks(order_id, at, lamports, source, reason, null_last_at, null_n, mark_path, reason_branches) VALUES(?,?,NULL,?,?,?,1,'heartbeat',?)",
                     (row["order_id"], now, MARK_SOURCE, why, now, json.dumps({b: branches.count(b) for b in sorted(set(branches))}) if branches else None))


def _request_exit(conn, row, reason, now):
    conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason=?, exit_requested_by='rails', exit_by='rails', due_at=? WHERE order_id=?",
                 (now, reason, now + EXIT_DELAY_S, row["order_id"]))


ESTIMATE_CHECK_MAX_S = 600.
ESTIMATE_CHECK_LAG_S = 10.


def _check_terminal_estimates(conn, desk_dir, now, clock):
    """Measurement only, nothing reads it to decide: the first strict price after a terminal estimate fill, or none by the bound."""
    for row in conn.execute("SELECT order_id, mint, pool, tokens_atomic, closed_at FROM orders WHERE exit_fill_kind='estimated_terminal' AND exit_estimate_check IS NULL").fetchall():
        cut = now - ESTIMATE_CHECK_LAG_S
        check = None
        if row["closed_at"] < cut <= row["closed_at"] + ESTIMATE_CHECK_MAX_S:
            quote, why = _quote(_ps_sell, desk_dir, row["mint"], int(row["tokens_atomic"]), cut, now, clock)
            if quote is not None and quote.get("pool") == row["pool"] and str(quote.get("lamports", "")).isdigit():
                check = dict(state="known", cut=cut, lamports=quote["lamports"], after_close_s=cut - row["closed_at"])
        if check is None and now - row["closed_at"] > ESTIMATE_CHECK_MAX_S:
            check = dict(state="none_within_bound", bound_s=ESTIMATE_CHECK_MAX_S)
        if check is not None:
            with _write(conn):
                conn.execute("UPDATE orders SET exit_estimate_check=? WHERE order_id=? AND exit_estimate_check IS NULL", (json.dumps(check), row["order_id"]))


def _close_exit(conn, row, at, quote, fx, sol_at, n, kind="strict"):
    """The one close: same cost path for both kinds. A closed row is never read back here, so the labels cannot change. NULL labels are closes before these columns, all strict."""
    got = int(quote["lamports"])
    cost_usd = _record_cashflow(conn, row["order_id"], "exit", at, got, fx, sol_at)
    trigger = "not_rails"
    if row["exit_requested_by"] == "rails":
        fired = conn.execute("SELECT binding_kind FROM marks WHERE order_id=? AND at<=? AND lamports IS NOT NULL ORDER BY at DESC, id DESC LIMIT 1",
                             (row["order_id"], row["exit_requested_at"])).fetchone()
        trigger = "none_priced" if fired is None else fired[0] or "unlabelled"
    conn.execute("UPDATE orders SET state='closed', due_at=NULL, closed_at=?, proceeds_lamports=?, exit_usd=?, "
                 "costs_usd=costs_usd+?, exit_proof=?, exit_fill_kind=?, exit_trigger_kind=? WHERE order_id=?",
                 (at, str(got), got / LAMPORTS * fx, cost_usd, json.dumps(quote, sort_keys=True, default=str), kind,
                  trigger, row["order_id"]))
    n["closed"] += 1


def _hold(conn, pol, row, desk_dir, now, sol_usd, sol_at, n, clock, mark_min_interval_s=None):
    qty = int(row["tokens_atomic"])
    if row["due_at"] is not None and now >= row["due_at"]:
        at = row["due_at"]
        lagged = lagged_row(conn, row)
        attending = (lagged is not None and lagged["lamports"] is None and lagged["reason"] in EXIT_CUT_HOLD|{"pool_block_cut_unproven"}
                     and _num(lagged["null_last_at"]) and now - lagged["null_last_at"] <= TICK_GRACE_S)
        terminal_on = os.environ.get("OFFICE_EXIT_ESTIMATE_FILL", "1") != "0"
        if attending and (now - at > EXIT_CUT_HOLD_S or (terminal_on and now - row["exit_requested_at"] > EXIT_REQUEST_MAX_S)):
            if not terminal_on:   # a fresh later cut, never a past exit
                conn.execute("UPDATE orders SET due_at=?, exit_frontier_retries=exit_frontier_retries+1 WHERE order_id=?", (now + EXIT_DELAY_S, row["order_id"]))
                _null_mark(conn, row, lagged, now, "frontier_retry_expired")
                return
            if now - at <= EXIT_CUT_HOLD_S:   # Alpha: past the request bound a live cut still gets its strict try; a strict close beats an estimate
                strict_fx = _fx(sol_usd, sol_at, at, pol.fx_max_age_s)
                strict_at = sol_at
                if strict_fx is None:
                    strict_at = row["exit_cut_fx_at"]
                    strict_fx = _fx(row["exit_cut_fx"], strict_at, at, pol.fx_max_age_s)
                if strict_fx is not None:
                    quote, _why = _quote(_ps_sell, desk_dir, row["mint"], qty, at, now, clock)
                    if quote is not None and quote.get("pool") == row["pool"] and str(quote.get("lamports", "")).isdigit():
                        _close_exit(conn, row, at, quote, strict_fx, strict_at, n)
                        return
            # Operator order 2026-10-30: the first labelled estimate observed at or after expiry closes it; never a choice among prices.
            fx_now = _fx(sol_usd, sol_at, now, pol.fx_max_age_s)
            if fx_now is None:
                sol_usd, sol_at = row["exit_cut_fx"], row["exit_cut_fx_at"]
                fx_now = _fx(sol_usd, sol_at, now, pol.fx_max_age_s)
            estimate = None
            if fx_now is not None and clock is not None:
                estimate, _why = _ps_estimate(desk_dir, row["mint"], qty, now + clock["offset_s"], observed_at=now + clock["offset_s"])
            if (estimate is not None and estimate.get("pool") == row["pool"] and str(estimate.get("lamports", "")).isdigit()
                    and estimate.get("binding_kind") in ("carried", "estimated")):
                terminal = dict(held_cut=at, hold_expiry_at=min(at + EXIT_CUT_HOLD_S, row["exit_requested_at"] + EXIT_REQUEST_MAX_S), estimate_observed_at=now, hold_retries=row["exit_frontier_retries"],
                                cut_rule="first_estimate_at_or_after_hold_expiry")
                _close_exit(conn, row, now, dict(estimate, terminal=terminal), fx_now, sol_at, n, kind="estimated_terminal")
            else:
                _null_mark(conn, row, lagged, now, "exit_estimate_unavailable")
                n["unpriced"] += 1
            return
        if now - at > TICK_GRACE_S and not attending:   # the watcher was away: re-arm with a fresh clock, no past exit
            _outage(conn, row, "exit", now)
            conn.execute("UPDATE orders SET due_at=? WHERE order_id=?", (now + EXIT_DELAY_S, row["order_id"]))
            return
        fx = _fx(sol_usd, sol_at, at, pol.fx_max_age_s)
        if fx is not None:                            # the sample moves past a held cut: keep the last causal one
            conn.execute("UPDATE orders SET exit_cut_fx=?, exit_cut_fx_at=? WHERE order_id=?", (sol_usd, sol_at, row["order_id"]))
        else:
            sol_usd, sol_at = row["exit_cut_fx"], row["exit_cut_fx_at"]
            fx = _fx(sol_usd, sol_at, at, pol.fx_max_age_s)
        detail = []
        quote, why = (None, "fx_unknown") if fx is None else _quote(_ps_sell, desk_dir, row["mint"], qty, at, now, clock, detail)
        if quote is not None and (quote.get("pool") != row["pool"] or not str(quote.get("lamports", "")).isdigit()):
            quote, why = None, "quote_binding_mismatch"
        if quote is None:                             # tape lag and FX keep the requested cut; anything else re-arms
            _null_mark(conn, row, lagged, now, why, detail)
            if why == "frontier_not_committed":
                n["frontier_not_committed"] += 1
            if why not in EXIT_CUT_HOLD:
                conn.execute("UPDATE orders SET due_at=? WHERE order_id=?", (now + EXIT_DELAY_S, row["order_id"]))
            n["unpriced"] += 1
            return
        _close_exit(conn, row, at, quote, fx, sol_at, n)
        return
    if row["exit_requested_at"] is None and now - row["filled_at"] >= pol.max_hold_s:   # every path, priced or not
        _request_exit(conn, row, "max_hold", now)
        return
    last = conn.execute("SELECT id, at, reason, lamports, null_last_at FROM marks WHERE order_id=? ORDER BY id DESC LIMIT 1",
                        (row["order_id"],)).fetchone()
    # The newest committed cut, so a rail sees the fall as soon as the tape can show it. The mark's time is that cut, its age is
    # recorded, and an exit it fires is requested at now, the observation time, never in the past.
    committed = _committed_cut(desk_dir, clock)
    if committed is not None:
        cut = min(now, committed)
        if now - cut > MARK_MAX_AGE_S:
            _null_mark(conn, row, last, now, "frontier_stale")
            n["unpriced"] += 1
            return
        previous = last["at"] if last is not None and last["lamports"] is not None else None
        if previous is not None and (cut <= previous or (mark_min_interval_s is not None and now - previous < mark_min_interval_s)):
            return
        detail = []
        quote, why = _quote(_ps_sell, desk_dir, row["mint"], qty, cut, now, clock, detail)
        if quote is not None and (quote.get("pool") != row["pool"] or not str(quote.get("lamports", "")).isdigit()):
            quote, why = None, "quote_binding_mismatch"
        if quote is None and why != "frontier_not_committed":
            # Rails must not go blind while a coin is being sold: an estimate, labelled, never a fill.
            estimate, estimate_why = _ps_estimate(desk_dir, row["mint"], qty, cut + clock["offset_s"], observed_at=now + clock["offset_s"])
            if estimate is not None and os.environ.get("OFFICE_MARK_%s" % estimate.get("binding_kind", "").upper(), "1") == "0":
                estimate, estimate_why = None, "estimate_switched_off"   # kill switches OFFICE_MARK_ESTIMATED, OFFICE_MARK_CARRIED; strict is untouched
            if estimate is not None and estimate.get("pool") == row["pool"]:
                estimate["strict_refusal"] = why
                quote, why = estimate, None
            else:
                why = why if estimate is not None else "%s;%s" % (why, estimate_why)
        value = int(quote["lamports"]) if quote is not None else None
        if value is None:
            _null_mark(conn, row, last, now, why, detail)
        else:
            conn.execute("INSERT INTO marks(order_id, at, lamports, source, reason, binding_kind, quote_proof, mark_age_s, mark_path) VALUES(?,?,?,?,?,?,?,?,'heartbeat')",
                         (row["order_id"], cut, str(value), MARK_SOURCE, why, quote.get("binding_kind"), json.dumps(quote, sort_keys=True, default=str), now - cut))
        n["marked" if value is not None else "unpriced"] += 1
        if value is not None and value > int(row["peak_lamports"] or 0):
            conn.execute("UPDATE orders SET peak_lamports=? WHERE order_id=?", (str(value), row["order_id"]))
            row = conn.execute("SELECT * FROM orders WHERE order_id=?", (row["order_id"],)).fetchone()
        if row["exit_requested_at"] is None:
            reason = _rail(pol, row, value, now)
            if reason is not None:
                _request_exit(conn, row, reason, now)
        return
    pending_frontier = last is not None and last["reason"] == "frontier_not_committed"
    at = last["at"] if pending_frontier else now
    if pending_frontier and now - at > TICK_GRACE_S:
        conn.execute("INSERT INTO marks(order_id, at, lamports, source, reason) VALUES(?,?,NULL,?,?)",
                     (row["order_id"], now, MARK_SOURCE, "frontier_retry_expired"))
        n["unpriced"] += 1
        return
    if not pending_frontier and mark_min_interval_s is not None:
        if last is not None and 0 <= now - last["at"] < mark_min_interval_s:
            return
    quote, why = _quote(_ps_sell, desk_dir, row["mint"], qty, at, now, clock)
    if why == "frontier_not_committed":
        n["frontier_not_committed"] += 1
    if quote is not None and (quote.get("pool") != row["pool"] or not str(quote.get("lamports", "")).isdigit()):
        quote, why = None, "quote_binding_mismatch"
    value = int(quote["lamports"]) if quote is not None else None
    conn.execute("INSERT INTO marks(order_id, at, lamports, source, reason, binding_kind, quote_proof, mark_path) "
                 "VALUES(?,?,?,?,?,?,?,'requested_cut')",
                 (row["order_id"], at, None if value is None else str(value), MARK_SOURCE, why,
                  quote.get("binding_kind") if quote is not None else None,
                  json.dumps(quote, sort_keys=True, default=str) if quote is not None else None))
    n["marked" if value is not None else "unpriced"] += 1
    if value is not None and value > int(row["peak_lamports"] or 0):
        conn.execute("UPDATE orders SET peak_lamports=? WHERE order_id=?", (str(value), row["order_id"]))
        row = conn.execute("SELECT * FROM orders WHERE order_id=?", (row["order_id"],)).fetchone()
    if row["exit_requested_at"] is None:
        reason = _rail(pol, row, value, now)
        if reason is not None:
            conn.execute("UPDATE orders SET exit_requested_at=?, exit_reason=?, exit_requested_by='rails', exit_by='rails', due_at=? "
                         "WHERE order_id=?", (now, reason, now + EXIT_DELAY_S, row["order_id"]))


GATE_MARK_MAX_AGE_S = 30.


def _entry_gate_open(conn, safety, epoch_id, now, sol_usd, sol_at):
    """Admission only. The kill rule's own reading stays proven-only: drawdown and the 24 h clock never see this. Kill switch OFFICE_GATE_ESTIMATES=0."""
    if safety['state'] in ('known', 'partial_lower_bound', 'not_configured'):
        return True
    if (safety['state'] == 'halted' or safety.get('reason') != 'epoch_equity_unknown' or epoch_id not in LABELLED_MARK_EPOCHS
            or os.environ.get('OFFICE_GATE_ESTIMATES', '1') == '0'):
        return False
    eq = equity(conn, epoch_id=epoch_id, kind='evaluated', now=now, mark_max_age_s=GATE_MARK_MAX_AGE_S, sol_usd=sol_usd, sol_at=sol_at, require_proven='labelled')
    return eq.state == 'known' and all(leg['cause'] == 'known' for leg in eq.legs)


def _record_gate_basis(conn, order_id, epoch_id, now, column='gate_basis'):
    # mark_kind is the newest mark at any age: reads filter on mark_age_s.
    rows = conn.execute("SELECT o.order_id,m.binding_kind,m.at FROM orders o LEFT JOIN marks m ON m.id=(SELECT id FROM marks WHERE order_id=o.order_id AND at<=? ORDER BY at DESC,id DESC LIMIT 1) "
                        "WHERE o.epoch_id=? AND o.state='open'", (now, epoch_id)).fetchall()
    conn.execute('UPDATE orders SET '+column+'=? WHERE order_id=?', (json.dumps([dict(order_id=r[0], mark_kind=r[1], mark_age_s=None if r[2] is None else now - r[2]) for r in rows]), order_id))


LABELLED_MARK_EPOCHS = frozenset({'office-v2-explore-1', 'office-v2-explore-gpt-1'})   # Alpha 2026-10-30; a frozen evaluation stays proven-only


def _labelled_mark(mark, pool, now):
    """A carried or estimated mark that says what it is and, aged from its cut to `now`, still sits inside its bounds.
    Ages start from the producer's at-cut figures: the at-observation ones already include commit lag."""
    try:
        quote = json.loads(mark["quote_proof"])
        proof = quote["proof"]
        elapsed, swap, carried = now - mark["at"], proof["newest_swap_age_s"], proof["carried_age_s"]
        return (mark["binding_kind"] in ("carried", "estimated") and quote.get("binding_kind") == mark["binding_kind"]
                and quote["lamports"] == mark["lamports"] and quote["pool"] == pool
                and _num(elapsed) and elapsed >= 0
                and _num(swap) and swap >= 0 and swap + elapsed <= _psp.ESTIMATE_TIP_MAX_AGE_S
                and ((mark["binding_kind"] == "estimated" and carried is None)
                     or (mark["binding_kind"] == "carried" and _num(carried) and carried >= 0 and carried + elapsed <= _psp.ESTIMATE_CARRY_MAX_AGE_S)))
    except (TypeError, ValueError, KeyError, IndexError):
        return False


def _mark_accepted(mark, pool, require, now):
    return _proven_mark(mark, pool) or (require == "labelled" and _labelled_mark(mark, pool, now))


def _proven_mark(mark, pool):
    try:
        quote = json.loads(mark["quote_proof"])
        clock, proof = quote["clock"], quote["proof"]
        host_at = clock["host_at"]
        return (mark["binding_kind"] in {"create_pool", "curve_derived", "curve_derived_unchecked", "intake_canonical_pda"}
                and quote.get("binding_kind") == mark["binding_kind"]
                and _num(clock["offset_s"]) and _num(host_at)
                and abs(host_at - (mark["at"] + clock["offset_s"])) <= 1e-6
                and quote["lamports"] == mark["lamports"] and quote["pool"] == pool
                and _num(proof["committed_through"]) and proof["committed_through"] >= host_at)
    except (TypeError, ValueError, KeyError, IndexError):
        return False


EVAL_EPOCHS=('office-paper-eval-1','office-v2-explore-1','office-v2-explore-gpt-1')   # epochs whose kill rule (drawdown, unknown time) this ledger enforces


def _evaluation_config(conn,epoch_id):
    epoch=conn.execute('SELECT opened_at,note FROM ledger_epochs WHERE epoch_id=?',(epoch_id,)).fetchone()
    if epoch is None:raise ValueError('epoch_unknown')
    try:doc=json.loads(epoch['note'])
    except (ValueError,TypeError):doc=None
    cfg=doc.get('paper_eval') if type(doc) is dict else None
    if cfg is None and epoch_id not in EVAL_EPOCHS:return epoch,None
    if (type(cfg) is not dict or set(cfg)!={'version','drawdown_usd','unknown_max_s','mark_max_age_s',
                                          'clock_skew_tolerance_s','leg_stale_demote_s'}
            or cfg.get('version')!=epoch_id or epoch_id not in EVAL_EPOCHS
            or any(not _num(cfg.get(k)) or not 0<cfg[k]<=ceiling for k,ceiling in
                   (('drawdown_usd',200.),('unknown_max_s',86400.),('mark_max_age_s',60.)))
            or not _num(cfg.get('clock_skew_tolerance_s')) or cfg['clock_skew_tolerance_s']<0
            or not _num(cfg.get('leg_stale_demote_s')) or cfg['leg_stale_demote_s']<=0):
        raise ValueError('epoch_config_unknown')
    return epoch,cfg


_SAFETY_FIELDS=('epoch_id','config_hash','last_at','last_known_equity_at','peak_equity_usd','unknown_since',
                'equity_usd','state','reason','halt_kind','halt_reason','halted_at','halt_actor',
                'last_equity_read_at','drawdown_equity_usd','unpriced_count','unpriced_order_ids_json',
                'demoted_order_ids_json','demoted_seconds_json','repromotions','demoted_never_priced_order_ids_json')


def _public_safety(row):
    row=dict(row)
    out=dict(row)
    for key in ('unpriced_order_ids','demoted_order_ids','demoted_seconds','demoted_never_priced_order_ids'):
        raw=row.get(key+'_json')
        try:out[key]=json.loads(raw) if raw is not None else None
        except (ValueError,TypeError):out[key]=None
    return out


def _save_safety(conn,row):
    conn.execute('INSERT INTO epoch_entry_safety('+','.join(_SAFETY_FIELDS)+') VALUES('+','.join('?' for _ in _SAFETY_FIELDS)+') '
        'ON CONFLICT(epoch_id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in _SAFETY_FIELDS[1:]),
        tuple(row[k] for k in _SAFETY_FIELDS))
    return _public_safety(row)


def _halt_safety(conn,row,*,kind,reason,now,actor):
    if row['halted_at'] is not None:return _public_safety(row)
    row.update(state='halted',reason=reason,halt_kind=kind,halt_reason=reason,
               halted_at=now,halt_actor=actor,last_at=max(row['last_at'],now))
    _save_safety(conn,row)
    cancelled=conn.execute("UPDATE orders SET state='censored',reason=?,epoch_halted_at=? "
        "WHERE epoch_id=? AND book_kind='evaluated' AND state='pending'",(reason,now,row['epoch_id'])).rowcount
    conn.execute("UPDATE orders SET epoch_halted_at=?,post_halt=1 WHERE epoch_id=? "
                 "AND book_kind='evaluated' AND state='open'",(now,row['epoch_id']))
    return dict(_public_safety(row),censored_n=cancelled)


def _entry_equity_snapshot(conn,eq,*,epoch_id,now,cfg):
    """Am5/7 entry and halt bases are distinct. Clock/FX faults never zero a leg."""
    if not _num(eq.cash):raise ValueError('equity_cash_unknown')
    if not eq.legs:
        known=eq.state=='known' and _num(eq.cash) and _num(eq.known_liq_lower)
        value=eq.cash+eq.known_liq_lower if known else None
        if known and not _num(value):known=False;value=None
        state='known' if known else eq.state if eq.state in ('partial','unknown') else 'unknown'
        return dict(state=state,reason=None if known else 'epoch_equity_unknown',
            equity_usd=value,drawdown_equity_usd=value,unpriced_count=eq.unpriced_count,
            unpriced_order_ids_json='[]',demoted_order_ids_json='[]',demoted_seconds_json='{}',repromotions=0,
            demoted_never_priced_order_ids_json='[]')
    entries=[];halts=[];unpriced=[];demoted=[];never_priced=[];seconds={};reason=None
    for leg in eq.legs:
        oid=leg['order_id'];cause=leg['cause']
        prior=conn.execute('SELECT * FROM epoch_leg_safety WHERE epoch_id=? AND order_id=?',(epoch_id,oid)).fetchone()
        old={} if prior is None else dict(prior)
        count=old.get('repromotions',0);awaiting=old.get('awaiting_repromotion',0)
        since=None;demoted_at=None;state=cause
        if cause=='known':
            if awaiting:count+=1;awaiting=0
            entry=halt=leg['value_usd']
        elif cause=='quote_failure':
            unpriced.append(oid);entry=halt=leg['zero_bound_usd']
        elif cause=='leg_pause':
            unpriced.append(oid)
            since=old.get('pause_since') if old.get('state') in ('paused','demoted_stale','demoted_never_priced') else None
            if since is None:since=leg.get('pause_since_hint') if _num(leg.get('pause_since_hint')) else now
            since=min(since,now)
            if now-since>cfg['leg_stale_demote_s']:
                state='demoted_stale';awaiting=1
                demoted_at=old.get('demoted_at') if old.get('state') in ('demoted_stale','demoted_never_priced') else None
                if demoted_at is None:demoted_at=now
                demoted.append(oid);seconds[oid]=max(0,now-demoted_at)
                entry=leg['zero_bound_usd'];halt=leg['last_priced_value_usd']
                if halt is None:
                    # Neutral entry cost is a labeled halt-only proxy, NEVER a mark or reported NET point.
                    halt=leg.get('entry_cost_usd')
                    if _num(halt):
                        state='demoted_never_priced';never_priced.append(oid)
                    else:entry=halt=None;reason='epoch_equity_unknown'
            else:
                state='paused';entry=halt=None
                reason=reason or 'epoch_mark_stale'
        else:
            unpriced.append(oid);entry=halt=None
            reason='fx_unknown' if leg.get('reason')=='fx_unknown' else 'epoch_equity_unknown'
        entries.append(entry);halts.append(halt)
        conn.execute('INSERT INTO epoch_leg_safety(epoch_id,order_id,pause_since,demoted_at,last_at,state,cause,repromotions,awaiting_repromotion) '
            'VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(epoch_id,order_id) DO UPDATE SET pause_since=excluded.pause_since,'
            'demoted_at=excluded.demoted_at,last_at=excluded.last_at,state=excluded.state,cause=excluded.cause,'
            'repromotions=excluded.repromotions,awaiting_repromotion=excluded.awaiting_repromotion',
            (epoch_id,oid,since,demoted_at,now,state,leg.get('reason'),count,awaiting))
    bounded=all(_num(v) for v in entries)
    halt_known=all(_num(v) for v in halts)
    count=conn.execute('SELECT COALESCE(SUM(repromotions),0) FROM epoch_leg_safety WHERE epoch_id=?',(epoch_id,)).fetchone()[0]
    return dict(state=('partial_lower_bound' if unpriced else 'known') if bounded else eq.state,
        reason=None if bounded else reason or 'epoch_equity_unknown',
        equity_usd=eq.cash+sum(entries) if bounded else None,
        drawdown_equity_usd=eq.cash+sum(halts) if halt_known else None,
        unpriced_count=len(unpriced),unpriced_order_ids_json=json.dumps(unpriced),
        demoted_order_ids_json=json.dumps(demoted),demoted_seconds_json=json.dumps(seconds),repromotions=count,
        demoted_never_priced_order_ids_json=json.dumps(never_priced))


def evaluation_safety(conn,*,epoch_id,now,sol_usd=None,sol_at=None):
    """Typed paper entry gate; never values unknown equity as zero or disables exits.

    The no-read clock starts at epoch open; a dead watcher cannot reset it with a
    fresh sample on restart. Accepted lower bounds reset no-read, never peak. Transient pauses clear;
    material-config, drawdown and auditor/operator halts never clear in this epoch.
    """
    if not _num(now):return {'state':'unknown','reason':'epoch_clock_unknown'}
    with _write(conn):
        found=conn.execute('SELECT * FROM epoch_entry_safety WHERE epoch_id=?',(epoch_id,)).fetchone()
        row=None if found is None else dict(found)
        config_error=False
        try:epoch,cfg=_evaluation_config(conn,epoch_id)
        except ValueError:
            epoch=conn.execute('SELECT opened_at FROM ledger_epochs WHERE epoch_id=?',(epoch_id,)).fetchone()
            if epoch is None:return {'state':'unknown','reason':'epoch_unknown'}
            cfg=None;config_error=True
        if cfg is None and row is None and not config_error:return {'state':'not_configured','reason':None}
        initial=conn.execute("SELECT COALESCE(SUM(c.usd),0) FROM capital_flows c JOIN books b USING(book_id) "
            "WHERE b.epoch_id=? AND b.kind='evaluated' AND c.kind='initial' AND c.at<=?",(epoch_id,now)).fetchone()[0]
        config_hash=None if cfg is None else _hash(json.dumps(cfg,sort_keys=True,separators=(',',':'),allow_nan=False))
        if row is None:
            row=dict(epoch_id=epoch_id,config_hash=config_hash,last_at=epoch['opened_at'],
                last_known_equity_at=epoch['opened_at'],peak_equity_usd=initial,unknown_since=None,
                equity_usd=None,state='unknown',reason=None,halt_kind=None,halt_reason=None,halted_at=None,halt_actor=None,
                last_equity_read_at=epoch['opened_at'],drawdown_equity_usd=None,unpriced_count=0,
                unpriced_order_ids_json='[]',demoted_order_ids_json='[]',demoted_seconds_json='{}',repromotions=0,
                demoted_never_priced_order_ids_json='[]')
        if row.get('last_equity_read_at') is None:row['last_equity_read_at']=row['last_known_equity_at']
        if row['halted_at'] is not None:return _public_safety(row)
        if config_error or cfg is None or row['config_hash']!=config_hash:
            return _halt_safety(conn,row,kind='config_drift',reason='epoch_config_drift',now=now,actor='safety')
        if row['last_at']-now>cfg['clock_skew_tolerance_s']:
            row.update(state='unknown',reason='epoch_clock_regressed',equity_usd=None,
                       unknown_since=row['last_equity_read_at'],drawdown_equity_usd=None)
            return _save_safety(conn,row)
        now=max(now,row['last_at'])   # bounded same-host observation reordering, not a change to the Judge cut
        if now-row['last_equity_read_at']>cfg['unknown_max_s']:
            return _halt_safety(conn,row,kind='unknown_equity',reason='epoch_equity_unknown_timeout',now=now,actor='safety')
        try:
            eq=equity(conn,epoch_id=epoch_id,kind='evaluated',now=now,mark_max_age_s=cfg['mark_max_age_s'],
                      sol_usd=sol_usd,sol_at=sol_at,require_proven=True)
            snapshot=_entry_equity_snapshot(conn,eq,epoch_id=epoch_id,now=now,cfg=cfg)
        except (ValueError,TypeError):
            snapshot=dict(state='unknown',equity_usd=None,drawdown_equity_usd=None,reason='epoch_equity_unknown')
        row.update(snapshot,last_at=now)
        if row['state'] in ('known','partial_lower_bound'):
            row.update(last_equity_read_at=now,unknown_since=None)
            if row['state']=='known':
                row.update(last_known_equity_at=now,peak_equity_usd=max(row['peak_equity_usd'],initial,row['equity_usd']))
            halt_value=row['drawdown_equity_usd']
            if halt_value is not None and row['peak_equity_usd']-halt_value>=cfg['drawdown_usd']:
                return _halt_safety(conn,row,kind='drawdown',reason='epoch_drawdown_halt',now=now,actor='safety')
        else:row['unknown_since']=row['last_equity_read_at']
        return _save_safety(conn,row)


def halt_evaluation_entries(conn,*,epoch_id,reason,actor,now,kind='manual'):
    """Trusted operator/auditor latch. No unhalt API; a retry is a new epoch."""
    if (kind not in ('manual','zero_tolerance','funded_boundary') or type(reason) is not str or not 1<=len(reason)<=160
            or type(actor) is not str or not actor or not _num(now)):raise ValueError('epoch_halt_invalid')
    with _write(conn):
        row=evaluation_safety(conn,epoch_id=epoch_id,now=now)
        if row['state']=='not_configured' or 'epoch_id' not in row:raise ValueError('epoch_config_unknown')
        return _halt_safety(conn,row,kind=kind,reason='epoch_'+kind+'_halt:'+reason,now=now,actor=actor)


def evaluation_safety_state(conn,*,epoch_id):
    """Read-only latch for Beta's host status mapping; no equity sample or mutation."""
    try:row=conn.execute('SELECT * FROM epoch_entry_safety WHERE epoch_id=?',(epoch_id,)).fetchone()
    except sqlite3.Error:return {'state':'unknown','reason':'epoch_safety_unavailable'}
    return _public_safety(row) if row is not None else {'state':'unknown','reason':'epoch_safety_not_sampled'}


def _manage_evaluation_safety(conn,*,now,sol_usd,sol_at):
    epochs=conn.execute("SELECT epoch_id FROM ledger_epochs WHERE epoch_id IN ('office-paper-eval-1','office-v2-explore-1') OR "
        "EXISTS (SELECT 1 FROM epoch_entry_safety s WHERE s.epoch_id=ledger_epochs.epoch_id) OR "
        "CASE WHEN json_valid(note) THEN json_type(note,'$.paper_eval') IS NOT NULL ELSE 0 END").fetchall()
    return sum(evaluation_safety(conn,epoch_id=r[0],now=now,sol_usd=sol_usd,sol_at=sol_at).get('censored_n',0) for r in epochs)


def equity(conn, *, now: float, mark_max_age_s: float, sol_usd=None, sol_at=None, book_id=None, epoch_id=None,
           kind: str = "evaluated", require_proven: bool = False) -> Equity:
    """As of `now` (an explicit as-of, never defaulted): cash = audited capital + cashflows dated at or before it; open
    inventory = orders filled at or before it and not closed by it. Open inventory is valued only by the latest exact sell mark at
    or before `now` (finite, non-negative age within mark_max_age_s), net of the modeled exit network cost once (a
    zero-value bag is a negative liability, never clamped to 0), at causal FX; a stale, future, missing or unroutable
    mark is unpriced. state: known (nothing unpriced), partial (some open inventory valued, some not), unknown (open
    inventory and none of it valued, or no FX). Scope: one book_id (a pod, descriptive), or the books of one epoch_id
    and kind; the Office verdict (prereg estimand) is epoch_id with kind 'evaluated', and discovery money never
    enters it."""
    if book_id is not None:
        books = [book_id]
    elif epoch_id is not None and kind in ("evaluated", "discovery"):
        books = [r[0] for r in conn.execute("SELECT book_id FROM books WHERE epoch_id=? AND kind=?", (epoch_id, kind))]
    else:
        raise ValueError("equity_scope_required")
    if not _num(now):
        raise ValueError("equity_as_of_required")    # the as-of is explicit; nothing here defaults to the current time
    # Every input is capped at the as-of (Zeta reporting cut 2026-10-29): capital flows and cashflows dated after it,
    # and orders filled after it, do not exist yet; an order closed after it is still open inventory.
    cash = sum(conn.execute("SELECT COALESCE(SUM(usd), 0) FROM capital_flows WHERE book_id=? AND at<=?", (b, now)).fetchone()[0]
               + conn.execute("SELECT COALESCE(SUM(c.usd), 0) FROM cashflows c JOIN orders o USING(order_id) "
                              "WHERE o.book_id=? AND c.at<=?", (b, now)).fetchone()[0] for b in books)
    marks = ",".join("?" * len(books)) or "NULL"
    versions = ",".join(sorted(r[0] for r in conn.execute("SELECT DISTINCT fill_model_version FROM orders "
                                                          "WHERE spend_lamports IS NOT NULL AND book_id IN (%s)" % marks,
                                                          books)))
    fx = _fx(sol_usd, sol_at, now, 120.0)
    exit_cost = int(round(EXEC_COST_SOL["exit"] * LAMPORTS))
    liq, unpriced, unpriced_qty, priced = 0.0, 0, 0, 0
    binding_counts = {};legs=[]
    for row in conn.execute("SELECT order_id, tokens_atomic, pool,filled_at,spend_usd FROM orders WHERE filled_at IS NOT NULL AND filled_at<=? "
                            "AND (closed_at IS NULL OR closed_at>?) AND book_id IN (%s)" % marks, (now, now, *books)):
        m = conn.execute("SELECT at, lamports, binding_kind, quote_proof,reason FROM marks WHERE order_id=? AND at<=? ORDER BY at DESC, id DESC LIMIT 1",
                         (row["order_id"], now)).fetchone()
        fresh_at=m is not None and _num(m['at']) and 0<=now-m['at']<=mark_max_age_s
        fresh=fresh_at and m['lamports'] is not None
        why=None if m is None else m['reason']
        cause='known';pause_hint=None
        if fx is None:cause='book_unknown';why='fx_unknown'
        elif m is not None and m['lamports'] is None and why in BOOK_UNKNOWN_MARK_REASONS:
            cause='book_unknown'
        elif m is not None and m['lamports'] is not None and require_proven and not _mark_accepted(m,row['pool'],require_proven,now):
            cause='book_unknown';why='mark_unproven'
        elif not fresh_at:
            cause='leg_pause';why='mark_stale' if m is not None else 'mark_missing'
            pause_hint=m['at']+mark_max_age_s if m is not None and _num(m['at']) else row['filled_at']
        elif m['lamports'] is None:
            if why in ZERO_QUOTE_REASONS:
                cause='quote_failure'
            else:cause='leg_pause';pause_hint=m['at']
        last_value=None
        if cause=='known':last_value=(int(m['lamports'])-exit_cost)/LAMPORTS*fx
        elif cause=='leg_pause' and fx is not None:
            last=conn.execute('SELECT at,lamports,binding_kind,quote_proof FROM marks INDEXED BY marks_order_priced_at '
                'WHERE order_id=? AND at<=? AND lamports IS NOT NULL ORDER BY at DESC,id DESC LIMIT 1',
                (row['order_id'],now)).fetchone()
            if last is not None and (not require_proven or _mark_accepted(last,row['pool'],require_proven,now)):
                last_value=(int(last['lamports'])-exit_cost)/LAMPORTS*fx
        legs.append(dict(order_id=row['order_id'],cause=cause,reason=why,pause_since_hint=pause_hint,
                         value_usd=last_value if cause=='known' else None,last_priced_value_usd=last_value,
                         zero_bound_usd=None if fx is None else -exit_cost/LAMPORTS*fx,entry_cost_usd=row['spend_usd']))
        if fx is None or not fresh or (require_proven and not _mark_accepted(m, row["pool"], require_proven, now)):
            unpriced += 1
            unpriced_qty += int(row["tokens_atomic"])
        else:
            priced += 1
            binding = m["binding_kind"] or "unknown"
            binding_counts[binding] = binding_counts.get(binding, 0) + 1
            liq += (int(m["lamports"]) - exit_cost) / LAMPORTS * fx
    if unpriced == 0:
        return Equity(cash, liq, 0, 0, "known", versions, binding_counts,legs)
    if priced == 0:
        return Equity(cash, None, unpriced, unpriced_qty, "unknown", versions, binding_counts,legs)
    return Equity(cash, liq, unpriced, unpriced_qty, "partial", versions, binding_counts,legs)
