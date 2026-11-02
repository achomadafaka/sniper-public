#!/usr/bin/env python3
"""Read-only forward recorder of ONE wallet's Solana buys, with an exact-route capture per unit.

Operator order 2026-10-02 (via Alpha) for Gamma's pre-registered notdecu size-tier test (gamma
episode ee10f5f6): a trial arm cannot observe him (J7 does not deliver his buys), so this watches the
chain. UNIT = his first buy of `--min-usd` (300) or more on a coin after T0 (the recorder's first start,
kept in its DB). Each unit starts one exact-route capture (memecoin_refusal_capture: a buy quote at
once, then a sell quote of that bag every MEMECOIN_CAPTURE_CADENCE_S) under gate `forward_<label>`.
Gamma grades the captures; this process prints COUNTS only (no returns), per the prereg.

How it sees him: programSubscribe on Token and Token-2022 filtered to his token accounts (memcmp
owner@32), never logsSubscribe on his owner, which carries 19.5k frames an hour of other signers'
failures (trap ff51c53c; measured 16 notifications an hour on his accounts, 2026-09-27). A rise on a
coin account is resolved on the research key (getSignaturesForAddress on that token account, then
getTransaction) through the key's SHARED pacer, under an hourly call cap that is logged when hit.
Size is the runner's own valuation (memecoin_source_valuation.value_source_buy); when it cannot value
the receipt, dump_forensics.trade_of, and `basis` says which.

Outside the flat-book rule by the operator's ruling (2026-10-02, load is tiny). Nothing here is on a
fill path: it imports no runner code, writes no ledger table, wakes nothing. Jupiter quotes spend the
MEASUREMENT key (declare_measurement_process) and yield on a 429, as the refusal capture does.

    python3 scripts/research/wallet_forward_recorder.py --owner 4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9 --label notdecu
"""
import argparse
import json
import logging
import queue
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
from sniper.services.memecoin_slot_signal import (  # noqa: E402
    TOKEN_2022_PROGRAM, TOKEN_PROGRAM, _NOT_A_COIN, _routed_subscribe, parse_token_notification, ws_url_from_http)

logger = logging.getLogger("wallet_forward_recorder")
TRIES = 5                      # resolve passes before a rise whose signature never appears is dropped
CALLS_PER_HOUR = 200
SIG_PAGE = 25                  # signatures a page on his token account
SIG_PAGES = 4                  # pages back before a rise is left for the next pass
PING_S = 30.0
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

BUYS_TABLE = """CREATE TABLE IF NOT EXISTS buys(signature TEXT NOT NULL, mint TEXT NOT NULL, account TEXT,
  slot INTEGER, block_time REAL, notified_slot INTEGER, resolved_at TEXT, token_delta TEXT, size_usd REAL,
  basis TEXT, unit INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (signature, mint))"""
BUYS_INDEX = "CREATE INDEX IF NOT EXISTS buys_mint ON buys(mint, unit)"
DDL = BUYS_TABLE + ";\n" + BUYS_INDEX + """;
CREATE TABLE IF NOT EXISTS fwd_meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS dropped(account TEXT, mint TEXT, slot INTEGER, signature TEXT, why TEXT, at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS dropped_once ON dropped(signature, why);
CREATE TABLE IF NOT EXISTS cap_hits(hour INTEGER PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS gaps(from_at TEXT, to_at TEXT, why TEXT);
"""


def iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


class CapHit(Exception):
    pass


class CallCap:
    """At most `per_hour` research-key calls in each clock hour."""

    def __init__(self, per_hour, *, clock=time.time):
        self.per_hour, self.clock, self.hour, self.used = int(per_hour), clock, None, 0

    def take(self):
        hour = int(self.clock() // 3600)
        if hour != self.hour:
            self.hour, self.used = hour, 0
        if self.used >= self.per_hour:
            return False
        self.used += 1
        return True


def _migrate_buys_key(db):
    """Codex W4 (2026-10-06): `buys` was keyed on the signature alone, so a second mint acquired in the
    same transaction was silently ignored, and which one survived depended on notification order. The
    key is now (signature, mint); an old table is rebuilt once, rows unchanged."""
    cols = db.execute("PRAGMA table_info(buys)").fetchall()
    if not cols or any(c[1] == "mint" and c[5] for c in cols):
        return
    db.commit()
    db.execute("BEGIN")                  # one transaction: a crash leaves the old table, never neither
    try:
        db.execute("ALTER TABLE buys RENAME TO buys_v1")
        db.execute(BUYS_TABLE)
        db.execute("INSERT INTO buys SELECT * FROM buys_v1")
        db.execute("DROP TABLE buys_v1")  # takes the old index with it
        db.execute(BUYS_INDEX)
    except Exception:
        db.rollback()
        raise
    db.commit()


class Recorder:
    def __init__(self, db, owner, *, rpc, sol_usd_fn, start_fn, cap, now_fn=time.time, min_usd=300.0, t0=None):
        self.db, self.owner, self.key, self.sol_usd_fn, self.start_fn = db, owner, rpc, sol_usd_fn, start_fn
        self.cap, self.now_fn, self.min_usd = cap, now_fn, float(min_usd)
        db.executescript(DDL)
        _migrate_buys_key(db)
        # Rows before this stamp were recorded under the old contract (no durable capture refusals, FIFO
        # resolution), so a reader can split the cohort at the repair instead of pooling the two.
        db.execute("INSERT OR IGNORE INTO fwd_meta VALUES ('contract_v2_at', ?)", (iso(now_fn()),))
        db.commit()
        row = db.execute("SELECT v FROM fwd_meta WHERE k='t0'").fetchone()
        if row is None:
            db.execute("INSERT INTO fwd_meta VALUES ('t0', ?)", (str(t0 if t0 is not None else now_fn()),))
            db.commit()
            row = db.execute("SELECT v FROM fwd_meta WHERE k='t0'").fetchone()
        self.t0 = float(row[0])
        self.amounts, self.pending = {}, []

    def on_frame(self, frame):
        """Queue a rise on one of his coin accounts. A first sight is queued too: its previous
        amount is unknown, so the transaction decides. Returns True when queued."""
        n = parse_token_notification(frame)
        if not n or n["owner"] != self.owner or n["mint"] in _NOT_A_COIN:
            return False
        prev = self.amounts.get(n["pubkey"])
        self.amounts[n["pubkey"]] = n["amount"]
        if (prev is None and n["amount"] == 0) or (prev is not None and n["amount"] <= prev):
            return False                         # a new empty account, a fall, or no change
        if any(c["account"] == n["pubkey"] and c["slot"] == n["slot"] for c in self.pending):
            return False
        self.pending.append({"account": n["pubkey"], "mint": n["mint"], "slot": n["slot"], "tries": 0})
        return True

    def _call(self, method, params):
        if not self.cap.take():
            raise CapHit()
        return self.key(method, params)

    def _drop(self, c, why, signature=None):
        self.db.execute("INSERT OR IGNORE INTO dropped VALUES (?,?,?,?,?,?)",
                        (c["account"], c["mint"], c["slot"], signature, why, iso(self.now_fn())))

    def resolve(self):
        """Resolve queued rises until the hourly cap. A rise whose signature is not visible yet stays
        queued for TRIES passes; a cap hit leaves the queue as it is and is logged once an hour.

        NEWEST FIRST (Codex W1, 2026-10-06): oldest-first spent the whole hourly budget on a backlog
        whose capture windows had already closed, so 193 of 208 units resolved hours late and none of
        them could be observed. A late rise is still resolved when budget allows, so the registered
        denominator stays whole; it just cannot starve one that can still be measured."""
        for c in sorted(self.pending, key=lambda c: -int(c["slot"] or 0)):
            try:
                done = self._resolve_one(c)
            except CapHit:
                hour = int(self.now_fn() // 3600)
                if self.db.execute("INSERT OR IGNORE INTO cap_hits VALUES (?,?)",
                                   (hour, iso(self.now_fn()))).rowcount:
                    logger.warning("research-key cap of %d calls/hour reached; %d rises wait",
                                   self.cap.per_hour, len(self.pending))
                break
            if not done:
                c["tries"] += 1
                if c["tries"] >= TRIES:
                    self._drop(c, "unresolved")
                    done = True
            if done:
                self.pending.remove(c)
        self.db.commit()

    def _resolve_one(self, c):
        # Newest first, a page at a time, until a page reaches back past the notified slot: dust sends
        # or a backlog behind the cap can bury the buy under one page of newer transactions (Gamma LOW 2).
        sigs, before = [], None
        for _ in range(SIG_PAGES):
            opts = {"limit": SIG_PAGE, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            page = self._call("getSignaturesForAddress", [c["account"], opts]) or []
            sigs += [s for s in page if s.get("slot") == c["slot"] and not s.get("err")]
            if len(page) < SIG_PAGE or page[-1].get("slot", 0) < c["slot"]:
                break
            before = page[-1]["signature"]
        if not sigs:
            return False
        for s in sigs:
            tx = self._call("getTransaction", [s["signature"], {"encoding": "json", "commitment": "confirmed",
                                                                "maxSupportedTransactionVersion": 1}])
            if not tx:
                return False
            self._record(c, tx)
        return True

    def _record(self, c, tx):
        from sniper.services.memecoin_source_valuation import value_source_buy
        sig = tx["transaction"]["signatures"][0]
        meta = tx.get("meta") or {}
        delta = sum((1 if side == "postTokenBalances" else -1) * int(b["uiTokenAmount"]["amount"])
                    for side in ("preTokenBalances", "postTokenBalances") for b in meta.get(side) or []
                    if b.get("owner") == self.owner and b.get("mint") == c["mint"])
        if meta.get("err") is not None or delta <= 0:
            self._drop(c, "not_his_buy", sig)
            return
        # His balance rising is not his buy: other wallets send coins into active wallets' accounts
        # (all three "buys" of the first smoke, 2026-10-02). A buy is a transaction he signed. A
        # cross-chain fill delivered by a solver would land here too, visibly, as dropped.
        import dump_forensics as dfx
        if self.owner not in dfx._signers(tx):
            self._drop(c, "not_his_signature", sig)
            return
        now = self.now_fn()
        sol = self.sol_usd_fn()
        size, basis = None, "unknown"
        try:
            facts = value_source_buy(tx, self.owner, sol_usd=sol, now=now)
            # The valuation names the mint its event bought. A gift of another coin in the same signed
            # transaction must not borrow that size (Codex W4).
            if facts.get("mint") == c["mint"]:
                size, basis = facts.get("source_size_usd"), facts.get("basis") or "unknown"
            elif facts.get("mint"):
                basis = "valued_other_mint"
        except Exception:                       # a receipt the valuation cannot read stays unknown
            logger.exception("valuation failed sig=%s", sig)
        if size is None and sol is not None and basis == "unknown" and len(_acquired_mints(meta, self.owner)) > 1:
            basis = "ambiguous_multi_mint"   # trade_of would price every acquired coin with the one SOL leg
        if size is None and sol is not None and basis == "unknown":
            import dump_forensics as dfx
            t = dfx.trade_of(tx, c["mint"], float(sol[0]))
            if t and t["trader"] == self.owner and t["side"] == "buy":
                size, basis = t["sol"] * float(sol[0]), "trade_of_%s" % t["quote"]
        # The chain clock is T0 of the unit and of its capture. Unknown is never replaced by our own
        # resolution time (Codex W5): such a buy is kept, but cannot be a unit.
        bt = tx.get("blockTime")
        if isinstance(bt, bool) or not isinstance(bt, (int, float)) or bt <= 0:
            bt = None
            self._drop(c, "no_source_clock", sig)
        unit = int(size is not None and bt is not None and size >= self.min_usd and bt >= self.t0
                   and not self.db.execute("SELECT 1 FROM buys WHERE mint=? AND unit=1", (c["mint"],)).fetchone())
        cur = self.db.execute("INSERT OR IGNORE INTO buys VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (sig, c["mint"], c["account"], tx.get("slot"), tx.get("blockTime"), c["slot"],
                               iso(now), str(delta), size, basis, unit))
        if unit and cur.rowcount:
            self.db.commit()             # registered first: a crash before the start is recovered at restart
            self.start_fn(mint=c["mint"], origin_event_id=origin_id(self.owner, sig), refused_at=iso(bt))

    def recover(self, has_capture_fn):
        """Restart recovery (Codex W1): every registered unit with no capture row at all, neither an
        observation nor a terminal refusal, is offered to the capture again. Its window is judged on the
        chain clock, so a unit registered before a crash and never started is refused `not_captured_stale`
        durably instead of vanishing from the denominator. Returns the number offered."""
        n = 0
        for sig, mint, bt in self.db.execute(
                "SELECT signature, mint, block_time FROM buys WHERE unit=1 ORDER BY block_time").fetchall():
            origin = origin_id(self.owner, sig)
            if not has_capture_fn(mint, origin):
                self.start_fn(mint=mint, origin_event_id=origin, refused_at=iso(bt))
                n += 1
        return n


def _acquired_mints(meta, owner):
    """Coins (not quote tokens) whose balance for `owner` rose in this receipt."""
    delta = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") == owner and b.get("mint") not in (WSOL, USDC):
                delta[b["mint"]] = delta.get(b["mint"], 0) + sign * int(b["uiTokenAmount"]["amount"])
    return {m for m, d in delta.items() if d > 0}


def origin_id(owner, signature):
    return "forward:%s:%s" % (owner, signature)


def record_downtime(db, heartbeat, *, now):
    """At start: the time since the last heartbeat is a gap, so a buy while the process was down (a
    crash, a restart, the overnight hibernate) is censored, not silently absent (Gamma LOW 1)."""
    try:
        last = json.loads(Path(heartbeat).read_text())["ts"]
        down = datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, KeyError, TypeError):
        return
    if now > down:
        db.execute("INSERT INTO gaps VALUES (?,?,?)", (last, iso(now), "process_down"))
        db.commit()


def capture_starter(captures, *, gate, horizon_s, cadence_s, max_concurrent, refuse_fn,
                    mono=time.monotonic, wall=time.time):
    """start_fn for a Recorder: one exact-route capture per unit, from his buy time (so `age_s` shows
    the detection lag), on the refusal capture's own code. A refused start (window already closed, or
    capacity) is written by `refuse_fn` as a terminal capture row, so every unit has a typed status
    (Codex W1)."""
    from sniper.services import memecoin_refusal_capture as cap

    def on_refuse(cid, outcome, mint, gate_, refused_at):
        refuse_fn(cid, outcome, mint=mint, gate=gate_, refused_at=refused_at, horizon_s=horizon_s,
                  expected_obs=int(float(horizon_s) // float(cadence_s)))

    def start(*, mint, origin_event_id, refused_at):
        return cap.start(captures, mint=mint, origin_event_id=origin_event_id, gate=gate, refused_at=refused_at,
                         network_id="solana", horizon_s=horizon_s, cadence_s=cadence_s,
                         max_concurrent=max_concurrent, now=mono(), now_epoch=wall(), on_refuse=on_refuse)
    return start


def has_capture(conn, mint, origin_event_id):
    """True when the capture store holds any row, observation or terminal, for this unit."""
    from sniper.services import memecoin_refusal_capture as cap
    return conn.execute("SELECT 1 FROM memecoin_refusal_captures WHERE capture_id=? LIMIT 1",
                        (cap.capture_id("solana", mint, origin_event_id),)).fetchone() is not None


def reap_captures(conn, *, now_iso, horizon_s, cadence_s):
    """Stamp a terminal coverage outcome (complete / gapped / truncated_worker) on every capture whose
    window has passed (Codex W2: the recorder never called the reaper, so all 206 rows stayed NULL).
    Runs on the wall clock from the stored rows, so a capture that died with the process is stamped at
    the next start."""
    from sniper.persistence import db_memecoin_captures as store
    from sniper.services import memecoin_refusal_capture as cap
    return cap.reap(store.observation_counts(conn, before_iso=now_iso), now_iso=now_iso, horizon_s=horizon_s,
                    cadence_s=cadence_s,
                    stamp_fn=lambda cid, outcome, **kw: store.terminal_row(conn, cid, outcome, **kw))


# --- the process ---------------------------------------------------------------------------------------

def _socket(ws_url, owner, frames, status, stop, *, connect=None, clock=time.time, wait=None):
    """One websocket, two programSubscribe on his token accounts. Idle, it pings every PING_S: Helius
    closed an idle socket after 5 minutes on 2026-10-02 (ConnectionClosedOK), and this sync client sends
    no keepalive of its own. Every outage, from the drop to the next subscription, is a gap: a buy
    inside it is never seen, so it is censored."""
    if connect is None:
        from websockets.sync.client import connect
    wait = wait or stop.wait
    backoff, down = 1.0, None
    while not stop.is_set():
        try:
            with connect(ws_url, open_timeout=15, max_size=None) as ws:
                for i, program in enumerate((TOKEN_PROGRAM, TOKEN_2022_PROGRAM), 1):
                    ws.send(json.dumps(_routed_subscribe(i, program, owner)))
                up = clock()
                status.update(connected=True, since=up)
                if down is not None:
                    frames.put({"_gap": (down[0], up, down[1])})
                    down = None
                backoff = 1.0
                while not stop.is_set():
                    try:
                        frames.put(json.loads(ws.recv(timeout=PING_S)))
                    except TimeoutError:
                        ws.ping()
        except Exception as exc:                # noqa: BLE001 - every failure is a gap, then a retry
            if down is None:
                down = (clock(), type(exc).__name__)
            status.update(connected=False)
            wait(backoff)
            backoff = min(60.0, backoff * 2)
            continue
        # A clean exit (stop set) is not an outage.


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--owner", required=True)
    p.add_argument("--label", required=True, help="names the gate (forward_<label>) and the default DB")
    p.add_argument("--min-usd", type=float, default=300.0)
    p.add_argument("--calls-per-hour", type=int, default=CALLS_PER_HOUR)
    p.add_argument("--db")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_path, state_root
    load_env_files(str(state_root()))
    from sniper.execution.solana_real_seams import declare_measurement_process
    from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp
    from sniper.execution.solana_wallet import _http as raw_http
    from sniper.persistence import db_memecoin_captures as store
    from sniper.services import memecoin_refusal_capture as cap
    from sniper.settings import memecoin as cfg
    import funded_exit_truth as fet             # the research key: refuses a funded key, shared pacer
    import slot_lane_send_monitor as mon
    import os
    declare_measurement_process()               # Jupiter: the MEASUREMENT key, never the funded one
    urls = cap.assert_pool_disjoint(os.environ)  # decimals reads on the capture pool, disjoint from funded
    ws_url = ws_url_from_http(mon.rpc_url())
    if not ws_url:
        raise SystemExit("no research websocket url")
    db_path = a.db or str(state_path("data", "wallet_forward_%s.db" % a.label))
    conn = store.connect(db_path)               # the capture tables, in this recorder's own DB
    captures = {}
    horizon, cadence = 330.0, float(cfg.MEMECOIN_CAPTURE_CADENCE_S)  # prereg: TP/SL or the +300 s mark
    sol_cache = {}

    def sol_usd():
        """(SOL/USD, observed epoch) from one capture-role Jupiter quote, refreshed at most once a
        minute, exactly as the on-chain watcher prices its source buys; None while unavailable."""
        if sol_cache.get("at", 0) < time.time() - 60:
            from sniper.execution.solana_real_seams import ROLE_CAPTURE, _quote_with_fallback, jupiter_role
            observed = time.time()
            with jupiter_role(ROLE_CAPTURE):
                q = _quote_with_fallback(WSOL, USDC, 1_000_000_000, 50, None) or {}
            if q.get("inputMint") == WSOL and q.get("outputMint") == USDC and str(q.get("inAmount")) == "1000000000":
                sol_cache.update(price=float(q["outAmount"]) / 1e6, at=observed)
        return (sol_cache["price"], sol_cache["at"]) if "price" in sol_cache else None

    def research(method, params):
        try:
            return fet.rpc(method, params)
        except fet.Stop as exc:
            logger.warning("research key stop: %s", str(exc)[:160])
            return None
    rec = Recorder(conn, a.owner, rpc=research, sol_usd_fn=sol_usd, min_usd=a.min_usd,
                   cap=CallCap(a.calls_per_hour),
                   start_fn=capture_starter(captures, gate="forward_%s" % a.label, horizon_s=horizon,
                                            cadence_s=cadence, max_concurrent=4,
                                            refuse_fn=lambda cid, outcome, **kw: store.terminal_row(
                                                conn, cid, outcome, **kw)))
    offered = rec.recover(lambda mint, origin: has_capture(conn, mint, origin))
    stamped = reap_captures(conn, now_iso=datetime.now(timezone.utc).isoformat(), horizon_s=horizon,
                            cadence_s=cadence)
    logger.info("restart recovery: %d unit(s) re-offered to capture, %d capture(s) stamped", offered, stamped)
    logger.info("forward recorder: owner=%s gate=forward_%s min=$%.0f T0=%s cap=%d calls/h db=%s",
                a.owner, a.label, a.min_usd, iso(rec.t0), a.calls_per_hour, db_path)
    frames, status, stop = queue.Queue(), {}, threading.Event()
    threading.Thread(target=_socket, args=(ws_url, a.owner, frames, status, stop), name="fwd-socket",
                     daemon=True).start()
    http = RotatingSolanaRpcHttp(raw_http, urls)
    yielder = cap.JupiterYield(hold_s=float(cfg.MEMECOIN_CAPTURE_JUPITER_YIELD_S))
    heartbeat = Path(db_path + ".heartbeat")
    record_downtime(conn, heartbeat, now=time.time())
    seen = {"acks": 0, "notifications": 0, "last_at": None}
    last_reap = time.monotonic()
    while True:
        while True:
            try:
                f = frames.get_nowait()
            except queue.Empty:
                break
            if "_gap" in f:
                a0, a1, why = f["_gap"]
                conn.execute("INSERT INTO gaps VALUES (?,?,?)", (iso(a0 or a1), iso(a1), why))
                conn.commit()
                continue
            # A socket can accept a subscription and then deliver nothing (memory: publicnode's
            # logsSubscribe); acks and notifications are counted so the heartbeat shows it.
            if "result" in f and "id" in f:
                seen["acks"] += 1
            elif f.get("method") == "programNotification":
                seen["notifications"] += 1
                seen["last_at"] = datetime.now(timezone.utc).isoformat()
            rec.on_frame(f)
        rec.resolve()
        now_iso = datetime.now(timezone.utc).isoformat()
        cap.pump(captures, now_mono=time.monotonic(), now_iso=now_iso, http=http, rpc_url=urls[0],
                 clip_usd=cfg.MEMECOIN_CAPTURE_CLIP_USD, record_fn=lambda row: store.record_observation(conn, row),
                 jup_yield=yielder)
        for k in [k for k, c in captures.items() if c.done or c.expired(time.monotonic())]:
            captures.pop(k, None)
        if time.monotonic() - last_reap >= 60.0:
            last_reap = time.monotonic()
            reap_captures(conn, now_iso=now_iso, horizon_s=horizon, cadence_s=cadence)
        units = conn.execute("SELECT COUNT(*) FROM buys WHERE unit=1").fetchone()[0]
        heartbeat.write_text(json.dumps({"ts": now_iso, "connected": bool(status.get("connected")),
                                         "units": units, "pending": len(rec.pending), "active": len(captures),
                                         "calls_this_hour": rec.cap.used, "subscription_acks": seen["acks"],
                                         "notifications": seen["notifications"], "last_notification_at": seen["last_at"],
                                         "dropped": dict(conn.execute("SELECT why, COUNT(*) FROM dropped GROUP BY why"))}))
        time.sleep(float(cfg.MEMECOIN_CAPTURE_POLL_S))


if __name__ == "__main__":
    sys.exit(main())
