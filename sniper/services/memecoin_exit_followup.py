"""Funded exit price tape, part B: whole-bag quotes of the EXITED quantity after every funded exit.

Spec docs/superpowers/specs/2026-09-30-memecoin-exit-price-tape-design.md §4 and §4a (EVM build,
2026-10-01). Measurement only: nothing here reads into a decision, an inventory or a rail.

ITS OWN PROCESS (scripts/memecoin_exit_tape_follow.py). The main thread reads new funded full exits
from the ledger on a READ-ONLY handle and copies each fill, with its attempt, into the tape file.
One worker thread per EVM chain owns that chain's `ExactLane` (the refusal-capture lane: our own
V4/V3/V2 quoters on one research host, a token bucket, a hold on any limit) and its own connection
to the tape file. The worker finds the exit's route once, then SELLS the exited quantity on it at
138 slots: every 2 s to 120 s, then every 10 s to 900 s.

A SLOT IS QUOTED OR SAID TO BE MISSING. Slot k may be quoted from its due time until the next slot's
due time; after that it is a `skipped` row, never interpolated. A held host writes `held` rows. A
limit doubles the next hold, 2 s to 300 s, and a good quote resets it.

THE HOSTS ARE RESEARCH HOSTS. A host whose budget any non-research role uses is refused (the capture
lane's derived set, research variables excluded), and so is a host that answers eth_chainId with
another chain. 56 reads the operator's keyed dRPC account (Alpha 2026-10-02), the account the BSC
tape's hop seed also reads: every read there, the identity check included, takes the account's one
cross-process pacer (1/1.5 s, shared with every other user), so each quote row records its read
budget and that minimum spacing.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

SLOTS = tuple(range(2, 121, 2)) + tuple(range(130, 901, 10))
LAST_WINDOW_S = 10
EXCLUDED_KINDS = ("entry", "derisk")
# chain: (env var holding the research URL, literal default, requests per second)
HOSTS = {
    "4663": ("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", None, 2.5),
    "56": ("MEMECOIN_RESEARCH_DRPC_RPC_URL_56", None, 1.5),
    "8453": (None, "https://base.gateway.tenderly.co", 2.0),
}
BLOCKED = {"5042": "blocked: both Arc hosts are taken by other roles"}
URL_OVERRIDE = "MEMECOIN_EXIT_TAPE_READ_URL_%s"
HOLD_MIN_S, HOLD_MAX_S = 2.0, 300.0
EXIT_EVIDENCE_SEAMS = ("execution_quote", "exit_audit", "exit_decision")

SCHEMA = """
CREATE TABLE IF NOT EXISTS exit_fills (
    fill_id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    network_id TEXT,
    mint TEXT,
    kind TEXT,
    fraction REAL,
    price REAL,
    qty REAL,
    notional REAL,
    created_at TEXT,
    closed_recorded_at TEXT,
    exit_reason TEXT,
    tx_sig TEXT,
    submit_quote_price REAL,
    attempt_id INTEGER,
    attempted_at TEXT,
    attempt_status TEXT,
    attempt_reason TEXT,
    chain_block INTEGER,
    chain_time TEXT,
    chain_time_status TEXT,
    copied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exit_followups (
    fill_id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    network_id TEXT,
    mint TEXT,
    status TEXT NOT NULL,
    anchor_at TEXT,
    entry_notional REAL,
    qty_atomic TEXT,
    qty_basis TEXT,
    token_decimals INTEGER,
    exit_pool TEXT,
    entry_pool TEXT,
    route_json TEXT,
    route_label TEXT,
    route_basis TEXT,
    provider TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_exit_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id INTEGER NOT NULL,
    position_id INTEGER,
    network_id TEXT,
    slot_s INTEGER NOT NULL,
    status TEXT NOT NULL,
    observed_at TEXT,
    age_s REAL,
    quote_output_atomic TEXT,
    quote_asset_usd REAL,
    value_usd REAL,
    http_status TEXT,
    latency_ms REAL,
    read_budget TEXT,
    read_pace_s REAL,
    UNIQUE(fill_id, slot_s)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path):
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(post_exit_quotes)")}
    for column, kind in (("read_budget", "TEXT"), ("read_pace_s", "REAL")):   # tapes before 2026-10-02
        if column not in have:
            try:
                conn.execute("ALTER TABLE post_exit_quotes ADD COLUMN %s %s" % (column, kind))
            except sqlite3.OperationalError as exc:      # another connection added it first
                if "duplicate column" not in str(exc):
                    raise
    return conn


def _wall():
    epoch = time.time()
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(), epoch


def _epoch(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def window_end(index):
    """The last instant (seconds after the anchor) slot `index` may still be quoted."""
    return SLOTS[index + 1] if index + 1 < len(SLOTS) else SLOTS[index] + LAST_WINDOW_S


# --- hosts ---------------------------------------------------------------------------------------

def resolve_hosts(env, *, http=None):
    """`(usable, pending, refused)`: `{chain: (url, rps)}` twice and `{chain: why}`."""
    from sniper.services import memecoin_evm_exact_capture as ex
    others = {k: v for k, v in env.items()
              if "RESEARCH" not in str(k) and not str(k).startswith("MEMECOIN_EXIT_TAPE_")}
    taken = ex.taken_budgets(others)
    from sniper.services.memecoin_research_pacer import drpc_paced_http
    http = drpc_paced_http(http or ex._default_http())
    usable, pending, refused = {}, {}, dict(BLOCKED)
    for chain, (var, default, rps) in sorted(HOSTS.items()):
        url = str(env.get(URL_OVERRIDE % chain) or (env.get(var) if var else "") or default or "").strip()
        host = (urlsplit(url).hostname or "").lower() if url else ""
        if not host:
            refused[chain] = "no research host set"
            continue
        if ex.budget(url) in taken:
            refused[chain] = "domain %s is used by another role" % ex.budget(url)
            continue
        verdict, got, status = ex._identity(http, url, chain)
        if verdict is None:
            pending[chain] = (url, rps)
        elif verdict:
            usable[chain] = (url, rps)
        else:
            refused[chain] = "host %s answered chain %s (status %s)" % (host, got, status)
    for chain, why in sorted(refused.items()):
        logger.warning("exit tape follow: chain %s refused: %s", chain, why)
    return usable, pending, refused


def build_lane(chain, url, rps, *, pending=False, http=None, clock=time.monotonic, sleep=time.sleep,
               keys_fn=None):
    """One chain's ExactLane: its own host, bucket and hold, starting at the 2 s hold."""
    from sniper.services.memecoin_evm_exact_capture import ExactLane, _default_http, budget
    if str(chain) == "4663" and http is None:
        # The 4663 research key rotates across its spare slots on a quota answer (operator
        # 2026-10-02); an override URL (MEMECOIN_EXIT_TAPE_READ_URL_4663) is no slot and passes.
        from sniper.execution.research_rotation_4663 import Rotation
        http = Rotation().default_seam()
    if budget(url) == "drpc.org (keyed)":
        from sniper.services.memecoin_research_pacer import drpc_paced_http
        http = drpc_paced_http(http or _default_http())
    urls = {str(chain): url}
    return ExactLane({} if pending else urls, pending=urls if pending else None, rps=rps,
                     yield_s=HOLD_MIN_S, clip_usd=20.0, http=http, keys_fn=keys_fn, clock=clock,
                     sleep=sleep)


# --- ledger side (main thread) -------------------------------------------------------------------

def _meta(conn, key, value=None):
    if value is None:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))
    conn.commit()
    return value


def init_cursor(tape, ledger):
    """First start: the newest fill, no backfill (a past exit's horizon is gone)."""
    cur = _meta(tape, "fill_cursor")
    if cur is not None:
        return int(cur)
    top = ledger.execute("SELECT COALESCE(MAX(id), 0) FROM memecoin_fills").fetchone()[0]
    _meta(tape, "fill_cursor", int(top))
    return int(top)


def new_exits(ledger, cursor, limit=200):
    """Funded full exits after `cursor`, each with its attempt. Two index-backed reads."""
    rows = [dict(r) for r in ledger.execute(
        "SELECT f.id AS fill_id, f.position_id, p.network_id, p.mint, f.kind, f.fraction, f.price,"
        " f.qty, f.notional, f.created_at, p.closed_recorded_at, p.exit_reason, f.tx_sig,"
        " f.quote_price AS submit_quote_price, p.entry_notional, p.executed_pool,"
        " p.executed_pool_source"
        " FROM memecoin_fills f JOIN memecoin_positions p ON p.id = f.position_id"
        " WHERE f.id > ? AND p.execution_mode = 'real' AND f.kind NOT IN (?, ?)"
        " AND f.tx_sig IS NOT NULL ORDER BY f.id LIMIT ?",
        (int(cursor), *EXCLUDED_KINDS, int(limit)))]
    sigs = [r["tx_sig"] for r in rows]
    attempts = {}
    if sigs:
        for a in ledger.execute(
                "SELECT id, sig, attempted_at, status, exit_reason FROM memecoin_exit_execution_attempts"
                " WHERE sig IN (%s)" % ",".join("?" * len(sigs)), sigs):
            attempts[a["sig"]] = a
    for r in rows:
        a = attempts.get(r["tx_sig"])
        r.update(attempt_id=a["id"] if a else None, attempted_at=a["attempted_at"] if a else None,
                 attempt_status=a["status"] if a else None,
                 attempt_reason=a["exit_reason"] if a else None)
    return rows


def exit_pool(ledger, position_id):
    """The V4 pool id our exit quoted, from the newest route-series evidence row, or None."""
    row = ledger.execute(
        "SELECT quote_evidence_json FROM memecoin_route_series WHERE position_id = ?"
        " AND seam IN (?, ?, ?) AND quote_evidence_json IS NOT NULL"
        " ORDER BY observed_at DESC LIMIT 1", (int(position_id), *EXIT_EVIDENCE_SEAMS)).fetchone()
    try:
        route = (json.loads(row[0]) or {}).get("route") or {} if row else {}
    except (TypeError, ValueError):
        return None
    pool = route.get("pool_id") if route.get("protocol") == "uniswap_v4" else None
    return str(pool).lower() if pool else None


def confirmed_amount(tape, position_id, attempted_at):
    """The A-lite confirmation's exact balance for this attempt, or None."""
    if not attempted_at:
        return None
    try:
        row = tape.execute(
            "SELECT amount_atomic FROM exit_confirmations WHERE position_id = ? AND decision_at = ?"
            " AND amount_atomic IS NOT NULL ORDER BY id DESC LIMIT 1",
            (int(position_id), attempted_at)).fetchone()
    except sqlite3.OperationalError:            # no confirmations table yet: A-lite never ran
        return None
    return row[0] if row else None


def record_exit(tape, ledger, row, *, lanes, refused, now_iso):
    """Copy one exit and open its follow-up. Returns the follow-up dict for a lane, else None."""
    tape.execute(
        "INSERT OR IGNORE INTO exit_fills(fill_id, position_id, network_id, mint, kind, fraction,"
        " price, qty, notional, created_at, closed_recorded_at, exit_reason, tx_sig,"
        " submit_quote_price, attempt_id, attempted_at, attempt_status, attempt_reason, copied_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(row[k] for k in ("fill_id", "position_id", "network_id", "mint", "kind", "fraction",
                               "price", "qty", "notional", "created_at", "closed_recorded_at",
                               "exit_reason", "tx_sig", "submit_quote_price", "attempt_id",
                               "attempted_at", "attempt_status", "attempt_reason")) + (now_iso,))
    chain = str(row["network_id"])
    if chain in lanes:
        status = "pending"
    elif chain == "solana":
        status = "no_lane"                      # stage 3: B1 on the research Helius key
    else:
        status = refused.get(chain, "no_lane")
    entry_pool = (str(row["executed_pool"]).lower()
                  if row.get("executed_pool") and "v4" in str(row.get("executed_pool_source") or "")
                  else None)
    amount = confirmed_amount(tape, row["position_id"], row.get("attempted_at"))
    mint = str(row["mint"] or "")
    follow = {
        "fill_id": row["fill_id"], "position_id": row["position_id"], "network_id": chain,
        # base58 is case-sensitive: only an EVM address is lowercased
        "mint": mint if chain == "solana" else mint.lower(), "status": status, "tx_sig": row["tx_sig"],
        "anchor_at": row["closed_recorded_at"] or row["created_at"],
        "entry_notional": row.get("entry_notional"), "qty": row["qty"],
        "qty_atomic": amount, "qty_basis": "confirmation" if amount else None,
        "exit_pool": (exit_pool(ledger, row["position_id"])
                      if status == "pending" and chain != "solana" else None),
        "entry_pool": entry_pool,
    }
    save_followup(tape, follow, now_iso)
    return follow if status == "pending" else None


FOLLOW_COLUMNS = ("fill_id", "position_id", "network_id", "mint", "status", "anchor_at",
                  "entry_notional", "qty_atomic", "qty_basis", "token_decimals", "exit_pool",
                  "entry_pool", "route_json", "route_label", "route_basis", "provider")


def save_followup(tape, f, now_iso):
    row = dict(f)
    row["route_json"] = json.dumps(f["route"], sort_keys=True) if f.get("route") else None
    tape.execute(
        "INSERT OR REPLACE INTO exit_followups(%s, updated_at) VALUES(%s)"
        % (",".join(FOLLOW_COLUMNS), ",".join("?" * (len(FOLLOW_COLUMNS) + 1))),
        tuple(row.get(k) for k in FOLLOW_COLUMNS) + (now_iso,))
    tape.commit()


def resumable(tape, chains):
    """Follow-ups a restart must pick up again: pending or active on a chain with a lane."""
    out = []
    for r in tape.execute(
            "SELECT f.*, e.qty, e.tx_sig FROM exit_followups f JOIN exit_fills e USING(fill_id)"
            " WHERE f.status IN ('pending', 'active')"):
        f = dict(r)
        if f["network_id"] not in chains:
            continue
        f["route"] = json.loads(f["route_json"]) if f.get("route_json") else None
        done = {s for (s,) in tape.execute(
            "SELECT slot_s FROM post_exit_quotes WHERE fill_id=?", (f["fill_id"],))}
        f["next_index"] = next((i for i, s in enumerate(SLOTS) if s not in done), len(SLOTS))
        out.append(f)
    return out


# --- one chain (worker thread) -------------------------------------------------------------------

class ChainWorker:
    """One chain's follow-ups on its own thread, lane and tape connection."""

    def __init__(self, chain, lane, tape_path, *, wall=_wall):
        self.chain = str(chain)
        self.lane, self.tape_path, self.wall = lane, tape_path, wall
        self.inbox = queue.SimpleQueue()
        self.active = {}
        self.errors, self.last_pass, self.quotes = 0, None, 0
        self.limits = self.held_rows = 0     # host limits seen, slots written `held`
        self.read_budget, self.read_pace_s = self._read_pace(lane)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="exit-tape-%s" % chain, daemon=True)

    def _read_pace(self, lane):
        """`(budget, minimum spacing s)` of this worker's reads, written on every quote row. A keyed
        dRPC lane is paced by the account's shared pacer, whatever its own rate."""
        from sniper.services.memecoin_evm_exact_capture import budget
        from sniper.services.memecoin_research_pacer import DRPC_GAP_S
        host = lane.hosts[self.chain]
        rate = getattr(host, "rate", None)
        pace = 1.0 / rate if rate else None
        if budget(host.url) == "drpc.org (keyed)":
            pace = max(pace or 0.0, DRPC_GAP_S)
        return budget(host.url), pace

    def submit(self, follow):
        self.inbox.put(dict(follow))

    def stats(self):
        """Per chain, for the heartbeat. `limits` and `hold_s` tell another 4663 research reader
        whether this key is being leaned on right now."""
        return {"alive": self.thread.is_alive(), "active": len(self.active), "quotes": self.quotes,
                "limits": self.limits, "held_rows": self.held_rows,
                "hold_s": self.lane.hosts[self.chain].yield_s, "errors": self.errors,
                "last_pass": self.last_pass, "hosts": self.lane.stats()}

    def _after_reads(self, holds_before, ok):
        """EVERY read path backs off: a NEW limit doubles the next hold (2 s to 300 s); a read
        that answered resets it."""
        host = self.lane.hosts[self.chain]
        new = host.counts["held"] - holds_before
        if new > 0:
            self.limits += new
            host.yield_s = min(HOLD_MAX_S, max(HOLD_MIN_S, host.yield_s * 2.0))
        elif ok:
            host.yield_s = HOLD_MIN_S

    # Writes go through these two so a test can read the same rows the worker wrote.
    def _write_quote(self, conn, f, slot, status, *, observed_at=None, age_s=None, out=None,
                     native=None, value=None, state=None):
        state = state or {}
        conn.execute(
            "INSERT OR IGNORE INTO post_exit_quotes(fill_id, position_id, network_id, slot_s, status,"
            " observed_at, age_s, quote_output_atomic, quote_asset_usd, value_usd, http_status,"
            " latency_ms, read_budget, read_pace_s) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["fill_id"], f["position_id"], self.chain, slot, status, observed_at, age_s,
             None if out is None else str(int(out)), native, value,
             None if state.get("status") is None else str(state.get("status")), state.get("ms"),
             self.read_budget, self.read_pace_s))
        conn.commit()

    def _resolve(self, conn, f, now_iso):
        """Route and exited quantity, once. False: try again next pass (host held or busy)."""
        lane, chain = self.lane, self.chain
        state = {}
        holds = lane.hosts[chain].counts["held"]
        try:
            return self._resolve_reads(conn, f, now_iso, state)
        finally:
            self._after_reads(holds, not state.get("failed"))

    def _resolve_reads(self, conn, f, now_iso, state):
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        from sniper.services.memecoin_evm_exact_capture import PROVIDER, route_label
        lane, chain = self.lane, self.chain
        if not f.get("qty_atomic"):
            dec = lane._decimals_of(chain, f["mint"], state)
            if dec is None:
                if not state.get("failed"):
                    f["status"] = "no_decimals"
                    save_followup(conn, f, now_iso)
                return False
            f["token_decimals"] = dec
            f["qty_atomic"] = str(int(Decimal(repr(float(f["qty"]))) * (Decimal(10) ** dec)))
            f["qty_basis"] = "fill_qty"
        native = lane.native_usd(chain)
        if native is None:
            return False
        clip = float(f.get("entry_notional") or lane.clip_usd)
        amount_in = int(clip / native * 10 ** int(CHAIN_QUOTE[chain]["decimals"]))
        # A known V4 pool asks the V4 quoter only (at most 8 pools): on the metered 4663 host a full
        # V4+V3+V2 scan costs ~28 calls and the first five slots. Nothing found there: full scan.
        kinds = ("v4",) if (f.get("exit_pool") or f.get("entry_pool")) else None
        found = lane._scan(chain, f["mint"], amount_in, state, kinds)
        if not found and kinds and not state.get("failed"):
            found = lane._scan(chain, f["mint"], amount_in, state)
        if state.get("failed"):
            logger.info("exit tape follow %s: route scan for fill %s held (%s); retried next pass",
                        chain, f["fill_id"], state.get("failed"))
            return False
        if not found:
            f["status"] = "no_route"
            save_followup(conn, f, now_iso)
            return False
        best = min(found, key=lambda x: -x[0])

        def pool_of(item):
            return str(item[2].get("pool") or "").lower() if item[1] == "v4" else None
        pick, basis = best, "scan_best"
        for wanted, label in ((f.get("exit_pool"), "exit_pool"), (f.get("entry_pool"), "entry_pool")):
            hit = next((x for x in found if wanted and pool_of(x) == wanted), None)
            if hit is not None:
                pick, basis = hit, label
                break
        f.update(route=pick[2], route_label=route_label(pick[2]), route_basis=basis,
                 provider=PROVIDER[pick[1]], status="active")
        save_followup(conn, f, now_iso)
        return True

    def _chain_time(self, conn, f):
        """The fill's block and block time on the research host, after the horizon. Best effort."""
        from sniper.services.memecoin_evm_exact_capture import _post
        state = {}
        holds = self.lane.hosts[self.chain].counts["held"]
        http = self.lane._rpc_http(self.chain, state)
        url = self.lane.hosts[self.chain].url
        tx = conn.execute("SELECT tx_sig FROM exit_fills WHERE fill_id=?", (f["fill_id"],)).fetchone()
        try:
            _s, receipt, _h = _post(http, url, "eth_getTransactionReceipt", [tx[0]])
            block = int(str((receipt.get("result") or {}).get("blockNumber")), 16)
            _s, blk, _h = _post(http, url, "eth_getBlockByNumber", [hex(block), False])
            ts = int(str((blk.get("result") or {}).get("timestamp")), 16)
            conn.execute("UPDATE exit_fills SET chain_block=?, chain_time=?, chain_time_status='ok'"
                         " WHERE fill_id=?", (block, datetime.fromtimestamp(ts, tz=timezone.utc)
                                              .isoformat(), f["fill_id"]))
        except Exception as exc:
            conn.execute("UPDATE exit_fills SET chain_time_status=? WHERE fill_id=?",
                         ("failed:%s" % (state.get("failed") or type(exc).__name__), f["fill_id"]))
        self._after_reads(holds, not state.get("failed"))
        conn.commit()

    def _quote_slot(self, conn, f, slot, age, now_iso):
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        holds = self.lane.hosts[self.chain].counts["held"]
        state = {}
        out = self.lane._quote(self.chain, f["route"], int(f["qty_atomic"]), "sell", state)
        self._after_reads(holds, not state.get("failed"))
        if state.get("failed"):
            self.held_rows += 1
            self._write_quote(conn, f, slot, "held", observed_at=now_iso, age_s=age, state=state)
            return
        if not out:
            self._write_quote(conn, f, slot, "no_route", observed_at=now_iso, age_s=age, state=state)
            return
        native = self.lane.native_usd(self.chain)
        value = (int(out) / 10 ** int(CHAIN_QUOTE[self.chain]["decimals"]) * native
                 if native is not None else None)
        self._write_quote(conn, f, slot, "ok" if native is not None else "price_unknown",
                          observed_at=now_iso, age_s=age, out=out, native=native, value=value,
                          state=state)
        self.quotes += 1

    def run_once(self, conn):
        self.lane.retry_pending()
        while True:
            try:
                f = self.inbox.get_nowait()
            except queue.Empty:
                break
            f.setdefault("next_index", 0)
            self.active[f["fill_id"]] = f
        if not self.lane._ready(self.chain):
            return
        last = len(SLOTS) - 1
        for fid, f in list(self.active.items()):
            now_iso, now = self.wall()
            age = now - _epoch(f["anchor_at"])
            # A horizon already gone (a long outage) buys no route scan: its slots are all skipped.
            if f.get("route") is None and age <= window_end(last):
                if not self._resolve(conn, f, now_iso):
                    if f["status"] != "pending":        # no_route / no_decimals: final
                        self.active.pop(fid, None)
                    continue
                now_iso, now = self.wall()
                age = now - _epoch(f["anchor_at"])
            i = f["next_index"]
            while i < len(SLOTS) and age > window_end(i):
                self._write_quote(conn, f, SLOTS[i], "skipped")
                i += 1
            if i < len(SLOTS) and age >= SLOTS[i] and f.get("route") is not None:
                self._quote_slot(conn, f, SLOTS[i], age, now_iso)
                i += 1
            f["next_index"] = i
            if i >= len(SLOTS):
                self._chain_time(conn, f)
                f["status"] = "done" if f.get("route") is not None else "missed"
                save_followup(conn, f, self.wall()[0])
                self.active.pop(fid, None)
        self.last_pass = self.wall()[0]

    def _run(self):
        conn = connect(self.tape_path)
        while not self._stop.is_set():
            try:
                self.run_once(conn)
            except Exception:
                self.errors += 1
                logger.exception("exit tape follow %s: pass failed; the thread continues", self.chain)
            self._stop.wait(0.25)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()


# --- Solana, stage 3b: B1 pool state on the paid research Helius key -----------------------------
# Spec §4 (B1) and §4b. Only a LIVE-VALIDATED venue is valued: PumpSwap (event reserves = raw vault
# pre-balances 156/156, e2e 135/135 within 2 points), with a SOL or USDC quote. The pump curve and
# Raydium CP stay `unpriced` until their live reads are checked against same-slot events, and so does
# any other venue, a multi-leg exit and a PumpSwap pool quoted in another token. An unpriced exit is a
# follow-up row with its venue in route_label: censored and counted, never dropped.
# FLAT-BOOK EXEMPT (operator order, 2026-10-01): the research Helius gate does not apply here.
from sniper.execution.pumpswap_buy import (  # noqa: E402
    AMM_PROGRAM as PUMP_AMM, POOL_ACCOUNT_DISCRIMINATOR, POOL_VIRTUAL_QUOTE_OFFSET, parse_pool_account)

SOLANA_KNOB = "MEMECOIN_EXIT_TAPE_SOLANA_ENABLED"
SOLANA_HOST_VAR = "MEMECOIN_RESEARCH_HELIUS_RPC_URL"
SOLANA_RPS = 2.0
SOLANA_RESOLVE_RETRY_S = 1.0
SOLANA_RESOLVE_ERRORS_MAX = 3            # node errors on one exit's resolve, then `unpriced: tx_unreadable`
PUMP_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_CP = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_DECIMALS = {WSOL: 9, USDC: 6}
AMM_SELL_DISC = bytes([62, 47, 55, 10, 165, 3, 220, 42])
SELL_EVENT_U64 = ("base", "limit", "user_base", "user_quote", "pool_base", "pool_quote", "quote",
                  "lp_bps", "lp_fee", "proto_bps", "proto_fee", "quote_x", "user_quote_amount")


def resolve_solana_host(env):
    """`(url, None)` or `(None, why)`. Off unless SOLANA_KNOB is 1. The research key must not be a key
    any other role uses (compared by key, sniper.execution.solana_rpc_pool.key_id: the funded pool is
    on the same provider domain, so a domain rule would refuse every Helius key)."""
    if str(env.get(SOLANA_KNOB) or "").strip() != "1":
        return None, "off: %s is not 1" % SOLANA_KNOB
    url = str(env.get(URL_OVERRIDE % "solana") or env.get(SOLANA_HOST_VAR) or "").strip()
    if not url:
        return None, "no %s" % SOLANA_HOST_VAR
    from sniper.execution.solana_rpc_pool import key_id
    mine = key_id(url)
    for name, value in env.items():
        if "RESEARCH" in str(name) or str(name).startswith("MEMECOIN_EXIT_TAPE_"):
            continue
        for part in str(value).replace(";", ",").split(","):
            part = part.strip()
            if part.startswith("http") and key_id(part) == mine:
                return None, "refused: the key is also in %s (a funded or other role)" % name
    return url, None


class Held(Exception):
    """The host limited or dropped the read: the slot is `held`, never a price."""


def _solana_http(url, body, timeout=10.0):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


class SolanaReader:
    """One research Helius host: a fixed request rate and a hold on any limit (2 s doubling to 300 s,
    reset by a read that answered). Owned by the Solana worker thread only."""

    def __init__(self, url, rps, *, http=None, clock=time.monotonic, sleep=time.sleep):
        self.url, self.interval = url, 1.0 / float(rps)
        self.http, self.clock, self.sleep = http or _solana_http, clock, sleep
        self.yield_s, self.held_until, self.next_at = HOLD_MIN_S, 0.0, 0.0
        self.counts = {"calls": 0, "held": 0, "errors": 0}

    def retry_pending(self):
        pass

    def _ready(self, _chain=None):
        return self.clock() >= self.held_until

    def call(self, method, params):
        if not self._ready():
            raise Held("hold")
        wait = self.next_at - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.next_at = self.clock() + self.interval
        self.counts["calls"] += 1
        try:
            status, reply = self.http(self.url, {"jsonrpc": "2.0", "id": 1, "method": method,
                                                 "params": params})
        except Exception as exc:                # transport: a hold, like a limit
            self._hold()
            raise Held("transport:%s" % type(exc).__name__) from None
        if status == 429 or int(status or 0) >= 500:
            self._hold()
            raise Held("http_%s" % status)
        if status != 200 or not isinstance(reply, dict) or reply.get("error") is not None:
            self.counts["errors"] += 1
            raise RuntimeError("%s: http %s %s" % (method, status, (reply or {}).get("error")))
        self.yield_s = HOLD_MIN_S
        return reply.get("result")

    def _hold(self):
        self.counts["held"] += 1
        self.held_until = self.clock() + self.yield_s
        self.yield_s = min(HOLD_MAX_S, max(HOLD_MIN_S, self.yield_s * 2.0))


class SolUsd:
    """USD per SOL: a plain Binance GET with a 60 s cache (the EVM lanes' native-rate rule). None when
    unread; a failed read is retried after 10 s."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.value, self.until = clock, None, 0.0

    def __call__(self):
        if self.clock() < self.until:
            return self.value
        import urllib.request
        try:
            with urllib.request.urlopen("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
                                        timeout=5) as resp:
                self.value = float(json.loads(resp.read())["price"])
            self.until = self.clock() + 60.0
        except Exception:
            logger.warning("exit tape follow solana: SOL/USD unread", exc_info=True)
            self.value, self.until = None, self.clock() + 10.0
        return self.value


def _tx_keys(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    keys = [k if isinstance(k, str) else (k or {}).get("pubkey") for k in msg.get("accountKeys") or []]
    loaded = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
    return keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def our_sell_events(tx):
    """Our PumpSwap SellEvents in `tx` (the signer's own, logged inside the AMM program): each a dict of
    SELL_EVENT_U64 plus pool and creator_bps."""
    import base64
    import re
    from solders.pubkey import Pubkey
    keys = _tx_keys(tx)
    user = keys[0] if keys else None
    invoke, done = re.compile(r"^Program (\S+) invoke \["), re.compile(r"^Program (\S+) (?:success|failed:)")
    stack, out = [], []
    for line in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        hit = invoke.match(line)
        if hit:
            stack.append(hit.group(1))
            continue
        if line.startswith("Program data: ") and stack and stack[-1] == PUMP_AMM:
            try:
                raw = base64.b64decode(line[len("Program data: "):])
            except ValueError:
                continue
            if raw[:8] != AMM_SELL_DISC or len(raw) < 360 or str(Pubkey.from_bytes(raw[152:184])) != user:
                continue
            e = {k: int.from_bytes(raw[16 + 8 * i:24 + 8 * i], "little") for i, k in enumerate(SELL_EVENT_U64)}
            e.update(pool=str(Pubkey.from_bytes(raw[120:152])),
                     creator_bps=int.from_bytes(raw[344:352], "little"))
            out.append(e)
            continue
        hit = done.match(line)
        if hit and stack:
            if stack[-1] == hit.group(1):
                stack.pop()
            elif hit.group(1) in stack:
                stack = stack[:stack.index(hit.group(1))]
    return out


def _token_amount(account):
    import base64
    raw = base64.b64decode(account["data"][0])
    return int.from_bytes(raw[64:72], "little")


class SolanaWorker(ChainWorker):
    """Solana follow-ups: the exit's route from our own exit tx (one read), then each slot one
    getMultipleAccounts of the pool and its two vaults, valued by PumpSwap's own math."""

    def __init__(self, reader, tape_path, *, sol_usd_fn=None, wall=_wall):
        super().__init__("solana", reader, tape_path, wall=wall)
        self.sol_usd = sol_usd_fn or SolUsd()

    def _read_pace(self, reader):
        from sniper.services.memecoin_evm_exact_capture import budget
        return budget(reader.url), reader.interval

    def stats(self):
        return {"alive": self.thread.is_alive(), "active": len(self.active), "quotes": self.quotes,
                "limits": self.limits, "held_rows": self.held_rows, "hold_s": self.lane.yield_s,
                "errors": self.errors, "last_pass": self.last_pass, "reads": dict(self.lane.counts)}

    def _unpriced(self, conn, f, label, now_iso):
        f.update(status="unpriced", route_label=label)
        save_followup(conn, f, now_iso)
        return False

    def _resolve(self, conn, f, now_iso):
        _iso, now = self.wall()
        if now < f.get("_retry_at", 0.0):
            return False
        try:
            tx = self.lane.call("getTransaction", [f["tx_sig"], {
                "encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
            if tx is None:                      # not visible on this node yet
                f["_retry_at"] = now + SOLANA_RESOLVE_RETRY_S
                return False
            if tx.get("blockTime"):
                conn.execute("UPDATE exit_fills SET chain_block=?, chain_time=?, chain_time_status='ok'"
                             " WHERE fill_id=?", (tx.get("slot"), datetime.fromtimestamp(
                                 int(tx["blockTime"]), tz=timezone.utc).isoformat(), f["fill_id"]))
                conn.commit()
            events = our_sell_events(tx)
            if len(events) > 1:
                return self._unpriced(conn, f, "multi_leg", now_iso)
            if not events:
                keys = set(_tx_keys(tx))
                label = ("pump_curve" if PUMP_CURVE in keys else "raydium_cp" if RAYDIUM_CP in keys
                         else "other_venue")
                return self._unpriced(conn, f, label, now_iso)
            (e,) = events
            got = (self.lane.call("getMultipleAccounts", [[e["pool"]], {
                "encoding": "base64", "commitment": "confirmed"}]) or {}).get("value") or [None]
        except Held:
            self.limits += 1
            return False
        except Exception as exc:
            self.errors += 1
            f["_errors"] = f.get("_errors", 0) + 1
            if f["_errors"] >= SOLANA_RESOLVE_ERRORS_MAX:     # never retried to the horizon unnamed
                logger.warning("exit tape follow solana: fill %s unreadable: %s", f["fill_id"], exc)
                return self._unpriced(conn, f, "tx_unreadable", now_iso)
            logger.exception("exit tape follow solana: resolve of fill %s failed; retried", f["fill_id"])
            f["_retry_at"] = now + SOLANA_RESOLVE_RETRY_S
            return False
        if not got[0] or got[0].get("owner") != PUMP_AMM:
            return self._unpriced(conn, f, "pumpswap_pool_unread", now_iso)
        pool = parse_pool_account(got[0]["data"][0])
        if pool["base_mint"] != f["mint"]:
            return self._unpriced(conn, f, "pumpswap_mint_mismatch", now_iso)
        if pool["quote_mint"] not in QUOTE_DECIMALS:
            return self._unpriced(conn, f, "pumpswap_quote_other", now_iso)
        f.update(route={"venue": "pumpswap", "pool": e["pool"], "base_vault": pool["pool_base_token_account"],
                        "quote_vault": pool["pool_quote_token_account"], "quote_mint": pool["quote_mint"],
                        "fee_bps": e["lp_bps"] + e["proto_bps"] + e["creator_bps"]},
                 qty_atomic=str(e["base"]), qty_basis="exit_event", exit_pool=e["pool"],
                 route_label="pumpswap", route_basis="exit_tx", provider="b1_pool_state", status="active")
        save_followup(conn, f, now_iso)
        return True

    def _quote_slot(self, conn, f, slot, age, now_iso):
        r = f["route"]
        started = time.monotonic()
        try:
            got = (self.lane.call("getMultipleAccounts", [[r["pool"], r["base_vault"], r["quote_vault"]], {
                "encoding": "base64", "commitment": "confirmed"}]) or {}).get("value") or []
        except Held:
            self.limits += 1
            self.held_rows += 1
            self._write_quote(conn, f, slot, "held", observed_at=now_iso, age_s=age)
            return
        except Exception as exc:
            self.errors += 1
            self._write_quote(conn, f, slot, "rpc_error", observed_at=now_iso, age_s=age,
                              state={"status": type(exc).__name__})
            return
        state = {"ms": (time.monotonic() - started) * 1000.0}
        if len(got) != 3 or not all(got):
            self._write_quote(conn, f, slot, "no_route", observed_at=now_iso, age_s=age, state=state)
            return
        qty = int(f["qty_atomic"])
        quote = _token_amount(got[2]) + parse_pool_account(got[0]["data"][0])["virtual_quote"]
        base = _token_amount(got[1])
        if quote <= 0:
            state["status"] = "effective_quote_nonpositive"
            self._write_quote(conn, f, slot, "no_route", observed_at=now_iso, age_s=age, state=state)
            return
        gross = quote * qty // (base + qty) if base + qty > 0 else 0
        out = gross - gross * int(r["fee_bps"]) // 10_000
        usd = 1.0 if r["quote_mint"] == USDC else self.sol_usd()
        value = out / 10 ** QUOTE_DECIMALS[r["quote_mint"]] * usd if usd is not None else None
        self._write_quote(conn, f, slot, "ok" if usd is not None else "price_unknown", observed_at=now_iso,
                          age_s=age, out=out, native=usd, value=value, state=state)
        self.quotes += 1

    def _chain_time(self, conn, f):
        """The block time came with the exit tx at resolve; nothing to read after the horizon."""
