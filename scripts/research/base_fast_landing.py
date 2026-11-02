#!/usr/bin/env python3
"""Grade the Base 8453 step-0 would-send decisions on what their pool did next. Research only.

Spec docs/superpowers/specs/2026-10-02-memecoin-base-8453-fast-lane-design.md rev 3 §2 (Gamma MED-2,
LOW 1, LOW 4). The watcher records each decision (base_fast_decisions, its own file); this job reads
that file READ-ONLY, so the watcher service never touches the research host, and writes its grades
to its own file. Per would-send decision, on the 8453 research host (rule 0724bc08):
- B's slot0 and liquidity at the end of blocks S, S+1 and S+2 (one PoolManager extsload batch per
  block tag): would our min_out still clear on that state? End of S catches copiers later in the
  source's own block. END OF S+1 IS A PROXY, NOT OUR FILL AND NOT A BOUND (review EVM05,
  2026-10-08): it includes swaps placed after our would-be position, and a later sell can make it
  clear where our insertion would fail (a later buy, the reverse), so it is end-of-block modeled
  min-out survival, wrong in either direction;
- the PoolManager Swap logs of B over S..S+2 (one getLogs): other swaps, after the source's own;
- for a bridge, hop A's real price at S (the deeper of two hookless native/USDC pools), against the
  ETH/USD mark the decide used.
A 429, a transport error or a refused historical read censors the row (censored=<why>): never a
clear, never in the 60% denominator. Our clip's swap is modelled inside one liquidity range at the
pool's LP fee; a hook's own fee is not modelled.

    python3 scripts/research/base_fast_landing.py [--decisions PATH] [--db PATH] [--limit 50]
    python3 scripts/research/base_fast_landing.py --loop 300    # one pass every 5 min, one process

Run it while step 0 records (Gamma LOW 3): the reads are of B's state at S..S+2, and a pass days
later depends on the host still serving those blocks (it served 3-day-old Base blocks on 2026-10-02).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

URL = "https://base.gateway.tenderly.co"
GAP_S = 0.5
SETTLE_S = 30.0                       # S+2 is mined and indexed well inside this
MAX_429 = 3
EXTSLOAD_BATCH = "dbd035ff"          # extsload(bytes32[])
POOLS_SLOT, LIQUIDITY_OFFSET = 6, 3
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
A_CANDIDATES = ((500, 10), (3000, 60))   # hookless native/USDC fee tiers read at S; the deeper wins

DDL = """
CREATE TABLE IF NOT EXISTS base_fast_landing(tx_hash TEXT NOT NULL, mint TEXT NOT NULL, pool TEXT,
  block INTEGER, route_kind TEXT, wallet TEXT, source_user_id TEXT, handle TEXT, decided_at TEXT,
  clears_s INTEGER, clears_s1 INTEGER, clears_s2 INTEGER, other_swaps_s INTEGER, other_swaps_s1 INTEGER,
  other_swaps_s2 INTEGER, a_mark_err_pct REAL, censored TEXT, graded_at TEXT NOT NULL, detail_json TEXT,
  path TEXT, since_arrival_ms REAL, PRIMARY KEY(tx_hash, mint));
"""


class Censored(Exception):
    """A read that did not answer: the row is censored with this reason."""


class Gateway:
    """The research host, paced; a 429 pauses and, after MAX_429 in a row, stops the run."""

    def __init__(self, url=URL, *, gap_s=GAP_S, sleep=time.sleep, clock=time.monotonic, opener=None):
        self.url, self.gap_s, self.sleep, self.clock = url, gap_s, sleep, clock
        self.opener = opener or urllib.request.urlopen
        self.last, self.limited, self.calls = 0.0, 0, 0

    def __call__(self, method, params):
        wait = self.last + self.gap_s - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.last = self.clock()
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json",
                                                      "User-Agent": "Mozilla/5.0 Sniper/1.0"})
        try:
            with self.opener(req, timeout=15) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self.limited += 1
                self.sleep(min(60.0, 5.0 * self.limited))
                raise Censored("http_429") from None
            raise Censored("http_%s" % exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Censored("transport:%s" % type(exc).__name__) from None
        self.limited = 0
        # A JSON-RPC answer is an object with an error or a result (review EVM03): anything else is
        # a read that did not answer, never an empty result. A present null result is passed on for
        # the caller to judge by method.
        if not isinstance(out, dict):
            raise Censored("rpc_shape")
        if out.get("error"):
            raise Censored("rpc_error:%s" % (out["error"].get("code")
                                             if isinstance(out["error"], dict) else "?"))
        if "result" not in out:
            raise Censored("rpc_shape")
        return out["result"]


def _state_slot(pool_id):
    from eth_utils import keccak
    return int.from_bytes(keccak(bytes.fromhex(str(pool_id)[2:]) + POOLS_SLOT.to_bytes(32, "big")), "big")


def read_states(call, pool_ids, block):
    """{pool_id: (sqrt, tick, lp_fee, liquidity)} at the END of `block`, in one extsload batch."""
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS
    slots = []
    for pid in pool_ids:
        base = _state_slot(pid)
        slots += [base, base + LIQUIDITY_OFFSET]
    data = ("0x" + EXTSLOAD_BATCH + "%064x" % 32 + "%064x" % len(slots) + "".join("%064x" % s for s in slots))
    raw = call("eth_call", [{"to": V4_POOL_MANAGERS["8453"], "data": data}, hex(int(block))])
    if not isinstance(raw, str) or not raw.startswith("0x") or (len(raw) - 2) % 64:
        raise Censored("extsload_shape")
    try:
        words = [int(raw[2 + i:66 + i], 16) for i in range(0, len(raw) - 2, 64)]
    except ValueError:
        raise Censored("extsload_shape") from None
    if len(words) != 2 + len(slots) or words[1] != len(slots):
        raise Censored("extsload_shape")
    out = {}
    for k, pid in enumerate(pool_ids):
        slot0, liquidity = words[2 + 2 * k], words[3 + 2 * k]
        tick = (slot0 >> 160) & 0xFFFFFF
        out[pid] = (slot0 & ((1 << 160) - 1), tick - (1 << 24) if tick >> 23 else tick,
                    (slot0 >> 208) & 0xFFFFFF, liquidity & ((1 << 128) - 1))
    return out


def clears(state, *, x_is_c0, clip_x, min_out, lp_fee_pips):
    """Would `clip_x` of X buy at least `min_out` coin against this state (one liquidity range)?"""
    from sniper.services.memecoin_rh_fast_decide import swap_native_in, swap_one_for_zero
    sqrt, _tick, _fee, liquidity = state
    net = int(clip_x) * (10 ** 6 - int(lp_fee_pips)) // 10 ** 6
    got = (swap_native_in if x_is_c0 else swap_one_for_zero)(sqrt, liquidity, net)
    return None if got is None else int(got[1] >= int(min_out))


def checked_logs(logs, pool, s):
    """The eth_getLogs answer for B's Swap topic over S..S+2, or Censored('logs_shape') (review
    EVM03). Only a list is an answer, and every log in it must be a live (not removed) Swap of THIS
    pool on the PoolManager inside the window: one bad log censors the row rather than being
    counted or quietly dropped. `[]` is a valid zero."""
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
    if not isinstance(logs, list):
        raise Censored("logs_shape")
    manager, swap, pool = V4_POOL_MANAGERS["8453"].lower(), V4_SWAP.lower(), str(pool).lower()
    for log in logs:
        try:
            block = int(log["blockNumber"], 16)
            int(log["logIndex"], 16)
            topics = [str(t).lower() for t in log["topics"]]
            address = str(log["address"]).lower()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise Censored("logs_shape") from None
        if (log.get("removed") or address != manager or topics[:2] != [swap, pool]
                or not s <= block <= s + 2):
            raise Censored("logs_shape")
    return logs


def other_swaps(logs, block, after_index=None):
    return sum(1 for log in logs if int(log["blockNumber"], 16) == block
               and (after_index is None or int(log["logIndex"], 16) > after_index))


def a_pool_ids():
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    return [v4_pool_id({"currency0": "0x" + "00" * 20, "currency1": USDC, "fee": fee, "tick_spacing": sp,
                        "hooks": "0x" + "00" * 20}) for fee, sp in A_CANDIDATES]


def grade(rec, call):
    """One landing row for a would-send record (its record_json), or a censored one."""
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
    s, pool, key = int(rec["block"]), rec["pool"], rec["key"]
    x_is_c0 = str(key["currency0"]).lower() == str(rec["x"]).lower()
    fee = int(key["fee"])
    row = {"tx_hash": rec["tx_hash"], "mint": rec["mint"], "pool": pool, "block": s,
           "route_kind": rec.get("route_kind"), "wallet": rec.get("wallet"),
           "source_user_id": rec.get("source_user_id"), "handle": rec.get("handle"),
           "decided_at": rec.get("decided_at"),
           # direct (socket thread) or relay_verified (after the origin verifier): Gamma splits on it
           "path": rec.get("path") or "direct", "since_arrival_ms": rec.get("since_arrival_ms")}
    detail = {"x_is_c0": x_is_c0, "lp_fee_static": fee,
              "s1_estimand": "end_of_block_modeled_min_out_survival"}     # not a bound (EVM05)
    try:
        for name, block in (("s", s), ("s1", s + 1), ("s2", s + 2)):
            st = read_states(call, [pool], block)[pool]
            lp = st[2] if fee & 0x800000 else fee          # dynamic-fee pools keep it in slot0
            row["clears_" + name] = clears(st, x_is_c0=x_is_c0, clip_x=rec["clip_x"],
                                           min_out=rec["min_out"], lp_fee_pips=lp)
            detail[name] = {"sqrt": str(st[0]), "tick": st[1], "lp_fee": lp, "liquidity": str(st[3])}
        logs = checked_logs(call("eth_getLogs", [{"address": V4_POOL_MANAGERS["8453"], "fromBlock": hex(s),
                                                  "toBlock": hex(s + 2), "topics": [V4_SWAP, pool]}]),
                            pool, s)
        detail["logs_checked"] = True       # provenance: rows graded before EVM03 lack this key
        row["other_swaps_s"] = other_swaps(logs, s, rec.get("swap_log_index"))
        row["other_swaps_s1"] = other_swaps(logs, s + 1)
        row["other_swaps_s2"] = other_swaps(logs, s + 2)
        if rec.get("route_kind") == "v4_bridge" and rec.get("eth_usd"):
            ids = a_pool_ids()
            states = read_states(call, ids, s)
            best = max(ids, key=lambda pid: states[pid][3])
            if states[best][3] > 0:
                usdc_per_eth = (states[best][0] / 2 ** 96) ** 2 * 10 ** 12
                row["a_mark_err_pct"] = (float(rec["eth_usd"]) / usdc_per_eth - 1.0) * 100.0
                detail["a_pool"] = best
    except Censored as exc:
        row = {k: v for k, v in row.items() if not k.startswith(("clears_", "other_", "a_mark"))}
        row["censored"] = str(exc)
    row["detail_json"] = json.dumps(detail, sort_keys=True)
    row["graded_at"] = datetime.now(timezone.utc).isoformat()
    return row


COLS = ("tx_hash", "mint", "pool", "block", "route_kind", "wallet", "source_user_id", "handle", "decided_at",
        "clears_s", "clears_s1", "clears_s2", "other_swaps_s", "other_swaps_s1", "other_swaps_s2",
        "a_mark_err_pct", "censored", "graded_at", "detail_json", "path", "since_arrival_ms")


# Columns added after the first landing DBs were made (3fa583899): nullable, so a row graded before
# them keeps NULL, which reads as unknown, never as a default (review EVM04).
ADDED_COLUMNS = (("path", "TEXT"), ("since_arrival_ms", "REAL"))


def migrate(db):
    """Add each missing ADDED_COLUMNS column to an existing base_fast_landing, once. Idempotent; each
    ALTER is atomic in SQLite, so a failure leaves the table as it was and the next pass resumes."""
    have = {r[1] for r in db.execute("PRAGMA table_info(base_fast_landing)")}
    for name, kind in ADDED_COLUMNS:
        if name not in have:
            db.execute("ALTER TABLE base_fast_landing ADD COLUMN %s %s" % (name, kind))
            print("base_fast_landing: added column %s (older rows keep NULL = unknown)" % name,
                  flush=True)
    db.commit()


def run(decisions_path, db_path, *, call, limit=50, now=None):
    now = now or datetime.now(timezone.utc)
    src = sqlite3.connect("file:%s?mode=ro" % decisions_path, uri=True)
    db = sqlite3.connect(str(db_path), timeout=30)
    try:
        db.executescript(DDL)
        migrate(db)
        done = {(r[0], r[1]) for r in db.execute(
            "SELECT tx_hash, mint FROM base_fast_landing WHERE censored IS NULL")}
        graded = censored = 0
        for tx_hash, mint, decided_at, raw in src.execute(
                "SELECT tx_hash, mint, decided_at, record_json FROM base_fast_decisions "
                "WHERE reason='would_send' ORDER BY decided_at"):
            if (tx_hash, mint) in done:
                continue
            if (now - datetime.fromisoformat(decided_at)).total_seconds() < SETTLE_S:
                continue
            row = grade(json.loads(raw), call)
            db.execute("INSERT OR REPLACE INTO base_fast_landing (%s) VALUES (%s)"
                       % (",".join(COLS), ",".join("?" * len(COLS))), [row.get(c) for c in COLS])
            db.commit()
            graded += 1
            censored += bool(row.get("censored"))
            if getattr(call, "limited", 0) >= MAX_429 or graded >= limit:
                break
    finally:
        src.close()
        db.close()
    return {"graded": graded, "censored": censored, "calls": getattr(call, "calls", None)}


def main(argv=None):
    from sniper.util.state_paths import state_path
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--decisions", default=str(state_path("data", "base_fast_decisions.db")))
    p.add_argument("--db", default=str(state_path("data", "research_base_fast_landing.db")))
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--loop", type=float, default=0.0, help="seconds between passes; 0 = one pass")
    a = p.parse_args(argv)
    import fcntl
    with open(a.db + ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another landing run holds %s.lock" % a.db)
        while True:
            if Path(a.decisions).exists():
                print(json.dumps(run(a.decisions, a.db, call=Gateway(), limit=a.limit)), flush=True)
            if not a.loop:
                return 0
            time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
