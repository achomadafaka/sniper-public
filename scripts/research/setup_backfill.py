#!/usr/bin/env python3
"""Step 2 chain backfill for the setup study: each funded EVM close's pool swaps over [t - 1 h, t),
strictly before the source's own swap (Gamma's amendment A1, e333ec66e; Alpha's bounded go).

ONE-OFF RESEARCH JOB, not a lane. Hosts (rule 0724bc08 + Alpha 2026-09-30, 56 moved 2026-10-02):
  56   MEMECOIN_RESEARCH_DRPC_RPC_URL_56 only, through the dRPC account's shared pacer (drpc_paced,
       1/1.5 s across every reader of the account), stop on the first 429. Never the BSC tape's
       hop-seed host 1rpc.io, which this job used until 2026-10-02. Untried on dRPC: its getLogs
       range cap and archive depth (the 56 block-rate read goes 200,000 blocks back) are unknown;
       a refusal stops the run with the provider's error;
  4663 MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663 only, 0.45 s spacing, stop on the first 429.
Reads the ledger read-only; writes only data/research_setup_backfill.db. Resumable: a decision
already stored is skipped. No outcome column is read.

Per decision:
  pool   our entry fill's receipt -> the LAST swap (memecoin_executed_pool.executed_pool_from_receipt);
         the coin side = the leg that left the pool in OUR swap (we bought the coin);
  t      the source transaction's block time when the origin carries its hash (swap:raybot:<tx>,
         onchain:<chain>:<tx>), and the window ends strictly before the source's first log;
         else the signal event's created_at (basis 'created_at');
  logs   ONE eth_getLogs over [block(t - 1 h), end] on that pool: Swap, Sync, Mint/Initialize;
  time   each log's own blockTimestamp when the provider returns it, else linear interpolation
         between the two anchor blocks whose timestamps were read (error measured and stored).

    python3 scripts/research/setup_backfill.py --chain 56 [--limit N]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sniper.services.memecoin_executed_pool import executed_pool_from_receipt  # noqa: E402
from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP, V4_INITIALIZE  # noqa: E402
from sniper.services.memecoin_tick_stream import (V2_SWAP, V3_SWAP, V2_SYNC, V2_MINT,  # noqa: E402
                                                  decode_evm_log)

V3_INITIALIZE = "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95"
HOSTS = {"56": ("MEMECOIN_RESEARCH_DRPC_RPC_URL_56", 1 / 1.5),
         "4663": ("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", 0.45)}
BLOCK_RATE = {"56": None, "4663": 10.0}          # blocks per second; 56 measured at start
WINDOW_S = 3600
CHUNK = [10_000]                                    # getLogs sub-range; halved on a 413, then kept
TX = re.compile(r"0x[0-9a-fA-F]{64}")
DDL = """
CREATE TABLE IF NOT EXISTS decisions (
  position_id INTEGER PRIMARY KEY, chain TEXT, mint TEXT, pool TEXT, pool_source TEXT, swaps_in_fill INTEGER,
  coin_leg INTEGER, t REAL, t_basis TEXT, source_tx TEXT, source_block INTEGER, source_log_index INTEGER,
  from_block INTEGER, to_block INTEGER, ts_from REAL, ts_to REAL, time_basis TEXT, interp_err_s REAL,
  n_logs INTEGER, status TEXT, detail TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS logs (
  position_id INTEGER, block_no INTEGER, log_index INTEGER, tx TEXT, kind TEXT, amount0 TEXT, amount1 TEXT,
  sqrt_price_x96 TEXT, ts REAL, PRIMARY KEY (position_id, block_no, log_index));
CREATE TABLE IF NOT EXISTS calls (chain TEXT, method TEXT, n INTEGER, PRIMARY KEY (chain, method));
"""


class RateLimited(Exception):
    pass


class BudgetExhausted(Exception):
    pass


class TooLarge(Exception):
    pass


class Rpc:
    def __init__(self, chain, url, spacing, db, budget=0):
        from sniper.services.memecoin_research_pacer import drpc_paced
        self.chain, self.url, self.spacing, self.db, self.last = chain, url, spacing, db, 0.0
        self.budget = budget
        self._send = drpc_paced(self._post)

    @staticmethod
    def _post(url, method, params):
        req = urllib.request.Request(url, json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            {"Content-Type": "application/json", "User-Agent": "sniper-research/1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.load(resp)
        return out, out.get("error")

    def __call__(self, method, params):
        if self.budget:
            used = self.db.execute("SELECT COALESCE(SUM(n), 0) FROM calls WHERE chain=?", (self.chain,)).fetchone()[0]
            if used >= self.budget:
                raise BudgetExhausted(f"{used} calls used of a {self.budget}-call budget")
        wait = self.spacing - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.db.execute("INSERT INTO calls VALUES (?, ?, 1) ON CONFLICT(chain, method) DO UPDATE SET n = n + 1",
                        (self.chain, method))
        self.db.commit()
        try:
            out, err = self._send(self.url, method, params)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RateLimited(f"HTTP 429 on {method}")
            if exc.code == 413:
                raise TooLarge(f"HTTP 413 on {method}")
            raise
        if err:
            text = json.dumps(err)
            if "429" in text or "rate limit" in text.lower() or "too many" in text.lower():
                raise RateLimited(text[:200])
            raise RuntimeError(text[:300])
        return out.get("result")


def env_value(name, env_file=None):
    for line in Path(env_file or ROOT / ".env").read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return None


def decisions(conn, chain):
    return conn.execute(
        "SELECT p.id, p.mint, p.origin_event_id, e.created_at, "
        "(SELECT tx_sig FROM memecoin_fills f WHERE f.position_id = p.id AND f.kind='entry' ORDER BY f.id LIMIT 1), "
        "lower(p.executed_pool), p.executed_pool_source, NULL, p.evm_route_json "
        "FROM memecoin_positions p LEFT JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL "
        "AND COALESCE(p.signal_source,'') != 'manual' AND p.network_id = ? ORDER BY p.id", (chain,)).fetchall()


def source_tx(origin):
    if origin and (origin.startswith("swap:raybot:") or origin.startswith("onchain:")):
        m = TX.search(origin)
        return m.group(0).lower() if m else None
    return None


def block_ts(rpc, n):
    """A block's timestamp, or None. A node-side "Remote Error" (1rpc, near the tip) is retried once."""
    for attempt in (0, 1):
        try:
            b = rpc("eth_getBlockByNumber", [hex(n), False])
            return int(b["timestamp"], 16) if b else None
        except RuntimeError as exc:
            if attempt or "Remote Error" not in str(exc):
                return None


class Clock:
    """Block <-> time for one chain: an anchor near the head, then a bounded Newton search."""
    def __init__(self, rpc, rate):
        self.rpc, self.rate = rpc, rate
        self.head = int(rpc("eth_blockNumber", []), 16) - 30      # the tip block can 404 on 1rpc
        self.head_ts = block_ts(rpc, self.head)

    def block_at(self, t, *, not_after=False):
        b = self.head - int((self.head_ts - t) * self.rate)
        ts = None
        for _ in range(4):
            ts = block_ts(self.rpc, b)
            if ts is None:
                return None, None
            if abs(ts - t) <= 1:
                break
            b += int(round((t - ts) * self.rate))
        while not_after and ts is not None and ts > t:             # the window must start at or before t
            b -= int(self.rate * 2) + 1
            ts = block_ts(self.rpc, b)
        return b, ts


def v4_leg(key_json, mint):
    try:
        k = json.loads(key_json)
    except (TypeError, ValueError):
        return None
    c0, c1 = str(k.get("currency0", "")).lower(), str(k.get("currency1", "")).lower()
    return 0 if c0 == mint else 1 if c1 == mint else None


def v4_key(conn, chain, pool):
    # pool ids are stored lowercase: an equality keeps the primary-key index (a lower() scan read
    # all 462k keys per decision on the live ledger)
    r = conn.execute("SELECT key_json FROM memecoin_evm_v4_pool_keys WHERE network_id=? AND pool_id=?",
                     (chain, pool)).fetchone()
    return r[0] if r else None


_V3_FACTORY = {}


def route_pool(rpc, chain, route_json):
    """Our route's own pool when the receipt is not served: a V2 pair by CREATE2 (no call) or a V3
    pool by the configured quoter's factory().getPool (at most two calls)."""
    try:
        route = json.loads(route_json or "{}")
    except ValueError:
        return None
    tokens = [str(t).lower() for t in route.get("tokens") or ()]
    proto = route.get("protocol")
    if len(tokens) < 2:
        return None
    if proto == "uniswap_v2" and chain == "56":
        from sniper.services.memecoin_evm_post_trade import V2_FACTORIES, _v2_pair
        factory, init_hash = V2_FACTORIES[("56", "0x10ed43c718714eb63d5aa57b78b54704e256024e")]
        return _v2_pair(factory, init_hash, tokens[-2], tokens[-1])
    if proto in ("pancake_v3", "uniswap_v3") and route.get("fees"):
        from sniper.settings.memecoin import MEMECOIN_EVM_V3_QUOTER
        if chain not in _V3_FACTORY:
            f = rpc("eth_call", [{"to": MEMECOIN_EVM_V3_QUOTER[chain], "data": "0xc45a0155"}, "latest"])
            _V3_FACTORY[chain] = "0x" + f[-40:] if f and len(f) >= 66 else None
        if not _V3_FACTORY[chain]:
            return None
        a, b = tokens[-2], tokens[-1]
        data = "0x1698ee82" + a[2:].rjust(64, "0") + b[2:].rjust(64, "0") + hex(int(route["fees"][-1]))[2:].rjust(64, "0")
        out = rpc("eth_call", [{"to": _V3_FACTORY[chain], "data": data}, "latest"])
        pool = "0x" + out[-40:] if out and len(out) >= 66 else None
        return pool if pool and int(pool, 16) else None
    return None


def pair_leg(rpc, pool, mint):
    t0 = rpc("eth_call", [{"to": pool, "data": "0x0dfe1681"}, "latest"])           # token0()
    if not t0 or len(t0) < 66:
        return None
    return 0 if ("0x" + t0[-40:]).lower() == mint else 1


def run_one(rpc, db, ledger, clock, chain, row, validate):
    pid, mint, origin, created, fill_tx, ex_pool, ex_src, key_json, route_json = row
    mint = str(mint).lower()
    rec = {"position_id": pid, "chain": chain, "mint": mint, "status": "ok", "detail": None}
    pool, source, swaps = ex_pool, ex_src, None
    if pool:
        source = "ledger_" + str(ex_src)
    elif fill_tx:
        receipt = rpc("eth_getTransactionReceipt", [fill_tx])
        if receipt:
            pool, source, swaps = executed_pool_from_receipt(chain, receipt)
        else:
            rec["detail"] = "fill receipt not served (pruned tx index)"
    if not pool:
        pool = route_pool(rpc, chain, route_json)
        source = "route_derived" if pool else source
    rec.update(pool=pool, pool_source=source, swaps_in_fill=swaps)
    if not pool:
        rec.update(status="pool_unresolved", detail=rec["detail"] or "no V2/V3/V4 swap in our fill receipt (curve venue?)")
        return rec, []
    if len(pool) == 66:
        rec["coin_leg"] = v4_leg(key_json or v4_key(ledger, chain, pool), mint)
    else:
        rec["coin_leg"] = pair_leg(rpc, pool, mint)
    if rec["coin_leg"] is None:
        rec.update(status="coin_leg_unknown")
        return rec, []
    end_block = end_index = None
    stx = source_tx(origin)
    if stx:
        srec = rpc("eth_getTransactionReceipt", [stx])
        if srec and srec.get("blockNumber"):
            end_block = int(srec["blockNumber"], 16)
            idx = [int(l["logIndex"], 16) for l in srec.get("logs") or [] if l.get("logIndex") is not None]
            end_index = min(idx) if idx else 0
            rec.update(source_tx=stx, source_block=end_block, source_log_index=end_index, t_basis="source_tx")
    if end_block is not None:
        t = ts_end = block_ts(rpc, end_block)
        if t is None:
            rec.update(status="block_time_unreadable",
                       detail="source block header not served (history limit of the research host)")
            return rec, []
    else:
        if not created:
            rec.update(status="no_signal_time")
            return rec, []
        t = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        end_block, ts_end = clock.block_at(t)
        rec.update(t_basis="created_at")
    start_block, ts_start = clock.block_at(t - WINDOW_S, not_after=True)
    if start_block is None or ts_end is None:
        rec.update(status="block_time_unreadable", detail="block header not served (1rpc keeps about 3-4 days of BSC blocks; 600k back served, 800k not, 2026-09-30)" if chain == "56" else "block header not served")
        return rec, []
    rec.update(t=t, from_block=start_block, to_block=end_block, ts_from=ts_start, ts_to=ts_end)
    if validate and end_block > start_block + 2:
        mid = (start_block + end_block) // 2
        ts_mid = block_ts(rpc, mid)
        est = ts_start + (ts_end - ts_start) * (mid - start_block) / (end_block - start_block)
        rec["interp_err_s"] = abs(est - ts_mid) if ts_mid is not None else None
    if len(pool) == 66:
        flt = {"address": V4_POOL_MANAGERS[chain], "topics": [[V4_SWAP, V4_INITIALIZE], pool]}
    else:
        # Swap topics only (Alpha: keep each response small); V3 Initialize is at most one log
        flt = {"address": pool, "topics": [[V2_SWAP, V3_SWAP, V3_INITIALIZE]]}
    logs, lo = [], start_block
    try:
        while lo <= end_block:                          # one window, consecutive sub-ranges
            hi = min(end_block, lo + CHUNK[0] - 1)
            try:
                part = rpc("eth_getLogs", [{**flt, "fromBlock": hex(lo), "toBlock": hex(hi)}])
            except TooLarge:
                if CHUNK[0] <= 250:
                    raise
                CHUNK[0] //= 2                          # halve on a 413 and keep the size that works
                continue
            logs.extend(part or [])
            lo = hi + 1
    except (RateLimited, BudgetExhausted):
        raise
    except Exception as exc:
        rec.update(status="limit_cut", detail=str(exc)[:300])
        return rec, []
    rec["detail"] = f"sub-range {CHUNK[0]} blocks"
    out, own_ts = [], 0
    for log in logs or []:
        b, i = int(log["blockNumber"], 16), int(log["logIndex"], 16)
        if b > end_block or (b == end_block and end_index is not None and i >= end_index):
            continue                                   # the source's swap and after: excluded
        ts = int(log["blockTimestamp"], 16) if log.get("blockTimestamp") else None
        own_ts += ts is not None
        if ts is None and end_block > start_block:
            ts = ts_start + (ts_end - ts_start) * (b - start_block) / (end_block - start_block)
        if ts is None or ts >= t or ts < t - WINDOW_S:
            continue                                   # [t - 1 h, t) only
        topic = (log.get("topics") or [""])[0]
        if topic in (V4_INITIALIZE, V3_INITIALIZE):
            out.append((pid, b, i, log.get("transactionHash"), "initialize", None, None, None, ts))
            continue
        drow = decode_evm_log(chain, log, None)
        if drow:
            out.append((pid, b, i, drow.get("tx_sig"), drow.get("kind"), drow.get("amount0"), drow.get("amount1"),
                        drow.get("sqrt_price_x96"), ts))
    rec.update(n_logs=len(out), time_basis=("log_blockTimestamp" if logs and own_ts == len(logs) else "interpolated"))
    return rec, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", required=True, choices=sorted(HOSTS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default=str(ROOT / "data" / "research_setup_backfill.db"))
    ap.add_argument("--ledger", default=str(ROOT / "sniper_tracker.db"))
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--budget", type=int, default=0, help="stop at this many total calls on the chain")
    a = ap.parse_args()
    var, spacing = HOSTS[a.chain]
    url = env_value(var, a.env_file)
    if not url:
        print("no research host for chain", a.chain); return 2
    db = sqlite3.connect(a.db, timeout=30)
    db.executescript(DDL)
    ledger = sqlite3.connect(f"file:{a.ledger}?mode=ro", uri=True, timeout=30)
    rpc = Rpc(a.chain, url, spacing, db, budget=a.budget)
    rate = BLOCK_RATE[a.chain]
    done = {r[0] for r in db.execute("SELECT position_id FROM decisions")}
    todo = [r for r in decisions(ledger, a.chain) if r[0] not in done]
    if a.limit:
        todo = todo[:a.limit]
    try:
        if rate is None:                                # 56: measure the block rate once
            head = int(rpc("eth_blockNumber", []), 16) - 30   # the tip block can 404 on 1rpc
            rate = 200_000 / (block_ts(rpc, head) - block_ts(rpc, head - 200_000))
        clock = Clock(rpc, rate)
        print(f"chain {a.chain}: {len(todo)} decisions, block rate {rate:.4f}/s", flush=True)
        for k, row in enumerate(todo):
            rec, logs = run_one(rpc, db, ledger, clock, a.chain, row, validate=k < 3)
            rec["fetched_at"] = datetime.now(timezone.utc).isoformat()
            cols = ",".join(rec)
            db.execute(f"INSERT OR REPLACE INTO decisions ({cols}) VALUES ({','.join('?' * len(rec))})",
                       list(rec.values()))
            db.executemany("INSERT OR REPLACE INTO logs VALUES (?,?,?,?,?,?,?,?,?)", logs)
            db.commit()
            print(rec["position_id"], rec["status"], rec.get("t_basis"), rec.get("n_logs"), flush=True)
    except RateLimited as exc:
        print("STOPPED on the first 429:", exc, flush=True)
        return 3
    except BudgetExhausted as exc:
        # every decision not yet stored is C0 "budget": counted, never guessed
        stored = {r[0] for r in db.execute("SELECT position_id FROM decisions")}
        now = datetime.now(timezone.utc).isoformat()
        for row in todo:
            if row[0] not in stored:
                db.execute("INSERT INTO decisions (position_id, chain, mint, status, detail, fetched_at) "
                           "VALUES (?, ?, ?, 'budget', ?, ?)", (row[0], a.chain, row[1], str(exc), now))
        db.commit()
        print("STOPPED on the call budget:", exc, flush=True)
        return 4
    finally:
        print("calls:", db.execute("SELECT method, n FROM calls WHERE chain=?", (a.chain,)).fetchall(), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
