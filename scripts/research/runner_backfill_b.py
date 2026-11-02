"""Backfill B for the U runner study (docs/audits/2026-10-21-desk-u-runner-study-preregistration.md §1).

Frozen sample: the 2,000 ps_tokens launches of 2026-08-22..10-11 (UTC) with the lowest
sha256("gamma-runner-bf-v1|" + mint). For each one, the bonding curve's first 60 minutes of
successful transactions via Helius getTransactionsForAddress (full, ascending, 100 a page, cap 30
pages, a capped launch is marked truncated). Pump curve TradeEvents are decoded from the
transaction logs. Research key only; stop on the first error or 429 (the body is recorded), and stop
at the credit budget. Resumable: a launch is either done, truncated or failed, never half-written.

    python3 scripts/research/runner_backfill_b.py select
    python3 scripts/research/runner_backfill_b.py run --key-index 0 --rate 2 --budget 100000
    python3 scripts/research/runner_backfill_b.py health

Never pooled with the forward U study (B is reported separately).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data/research/2026-10-21-runner-backfill-b.sqlite"
TAPE = ROOT / "data/pump_snipe_tape.sqlite"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE = bytes([189, 219, 127, 211, 78, 230, 97, 238])
SEED = "gamma-runner-bf-v1|"
N, WINDOW_S, PAGE, MAX_PAGES, CREDITS_PER_CALL = 2000, 3600, 100, 30, 10
LO, HI = "2026-08-22T00:00:00+00:00", "2026-10-12T00:00:00+00:00"


def db():
    c = sqlite3.connect(OUT, timeout=30)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sample(mint TEXT PRIMARY KEY, rank INTEGER, created_ts REAL, creator TEXT,
        symbol TEXT, initial_mcap_sol REAL, curve TEXT, status TEXT NOT NULL DEFAULT 'pending',
        pages INTEGER, txs INTEGER, trades INTEGER, credits INTEGER, done_at TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS trades(mint TEXT, signature TEXT, slot INTEGER, block_time INTEGER,
        tx_index INTEGER, ordinal INTEGER, is_buy INTEGER, sol INTEGER, token INTEGER, user TEXT,
        vsol INTEGER, vtok INTEGER, PRIMARY KEY(signature, ordinal));
    CREATE TABLE IF NOT EXISTS run_log(at TEXT, key_id TEXT, event TEXT, detail TEXT);
    """)
    return c


def curve_of(mint):
    from solders.pubkey import Pubkey
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                           Pubkey.from_string(PUMP))[0])


def select():
    t = sqlite3.connect(f"file:{TAPE}?mode=ro", uri=True)
    lo, hi = (datetime.fromisoformat(x).timestamp() for x in (LO, HI))
    rows = t.execute("SELECT mint, created_ts, creator, symbol, initial_mcap_sol FROM ps_tokens "
                     "WHERE created_ts >= ? AND created_ts < ?", (lo, hi)).fetchall()
    rows.sort(key=lambda r: hashlib.sha256((SEED + r[0]).encode()).hexdigest())
    c = db()
    if c.execute("SELECT COUNT(*) FROM sample").fetchone()[0]:
        print("sample already frozen:", c.execute("SELECT COUNT(*) FROM sample").fetchone()[0])
        return
    for i, r in enumerate(rows[:N]):
        c.execute("INSERT INTO sample(mint,rank,created_ts,creator,symbol,initial_mcap_sol,curve) "
                  "VALUES(?,?,?,?,?,?,?)", (r[0], i, r[1], r[2], r[3], r[4], curve_of(r[0])))
    c.commit()
    print("population", len(rows), "sample", min(N, len(rows)))


class Stop(Exception):
    pass


def make_rpc(url, rate):
    """One call at a time through the key's SHARED cross-process pacer (Alpha 2026-10-02), never
    faster than the pacer's gap (1.1 s: the research key 429d at 0.13 s). `rate` can only slow it."""
    from sniper.services.memecoin_research_pacer import SharedPacer
    pacer = SharedPacer.for_url(url, gap_s=max(1.1, 1.0 / rate))

    def rpc(method, params):
        pacer.acquire()
        req = urllib.request.Request(url, data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 Sniper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:          # 429 body says burst vs "max usage reached"
            if exc.code == 429:
                pacer.pause(30)                        # every reader of the key backs off
            raise Stop(f"http {exc.code}: {exc.read()[:300]!r}") from None
        except Exception as exc:
            raise Stop(f"transport: {exc!r}") from None
        if "error" in body:
            raise Stop(f"rpc error: {json.dumps(body['error'])[:300]}")
        return body.get("result")
    return rpc


def decode(tx):
    """Pump curve TradeEvents in one transaction, in log order."""
    out = []
    for line in (tx.get("meta") or {}).get("logMessages") or []:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[14:], validate=True)
        except Exception:
            continue
        if raw[:8] != TRADE or len(raw) < 8 + 32 + 8 + 8 + 1 + 32 + 8 + 32:
            continue
        p = 8
        mint = raw[p:p + 32]; p += 32
        sol, tok = struct.unpack_from("<QQ", raw, p); p += 16
        is_buy = raw[p]; p += 1
        user = raw[p:p + 32]; p += 32
        p += 8                                         # i64 timestamp
        vsol, vtok = struct.unpack_from("<QQ", raw, p)
        out.append((mint, sol, tok, is_buy, user, vsol, vtok))
    return out


def b58(b):
    from solders.pubkey import Pubkey
    return str(Pubkey.from_bytes(b))


def _urls():
    """MEMECOIN_RESEARCH_HELIUS_RPC_URLS entries that share no key with a funded pool or the watcher
    (the same refusal as slot_lane_send_monitor.pick_research_url). Key ids only are ever printed."""
    import os
    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    from sniper.services.memecoin_research_rpc import funded_overlap
    from sniper.execution.solana_rpc_pool import key_id
    urls = [u.strip() for u in (os.environ.get("MEMECOIN_RESEARCH_HELIUS_RPC_URLS") or "").split(",") if u.strip()]
    watcher = {"MEMECOIN_SOLANA_RPC_URL": os.environ.get("ONCHAIN_WATCH_HELIUS_RPC_URL") or ""}
    bad = set(funded_overlap(urls)) | set(funded_overlap(urls, watcher))
    return [(key_id(u), u, key_id(u) in bad) for u in urls]


def run(key_index, rate, budget):
    kid, url, bad = _urls()[key_index]
    if bad:
        raise SystemExit("research key %s overlaps a funded or watcher key; refusing" % kid)
    rpc = make_rpc(url, rate)
    c = db()
    spent = c.execute("SELECT COALESCE(SUM(credits),0) FROM sample").fetchone()[0]
    c.execute("INSERT INTO run_log VALUES(?,?,?,?)", (datetime.now(timezone.utc).isoformat(), kid, "start",
              json.dumps({"rate": rate, "budget": budget, "spent_before": spent})))
    c.commit()
    todo = c.execute("SELECT mint, curve, created_ts FROM sample WHERE status='pending' ORDER BY rank").fetchall()
    try:
        for mint, curve, created in todo:
            rows, token, pages, txs, credits, truncated = [], None, 0, 0, 0, False
            while True:
                if spent + credits + CREDITS_PER_CALL > budget:
                    raise Stop("budget")
                opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": PAGE, "encoding": "json",
                        "maxSupportedTransactionVersion": 1,
                        "filters": {"status": "succeeded",
                                    "blockTime": {"gte": int(created) - 5, "lte": int(created) + WINDOW_S}}}
                if token:
                    opts["paginationToken"] = token
                page = rpc("getTransactionsForAddress", [curve, opts])
                pages += 1
                credits += CREDITS_PER_CALL
                if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                    raise Stop(f"shapeless page for {mint}")
                for tx in page["data"]:
                    txs += 1
                    sig = tx["transaction"]["signatures"][0]
                    for k, (m, sol, tok, buy, user, vsol, vtok) in enumerate(decode(tx)):
                        if b58(m) != mint:
                            continue
                        rows.append((mint, sig, tx.get("slot"), tx.get("blockTime"), tx.get("transactionIndex"),
                                     k, buy, sol, tok, b58(user), vsol, vtok))
                token = page.get("paginationToken")
                # A short page is the last one: on 20 probed launches (ranks 2-21, 440 credits) every
                # page under 100 was followed by an empty page only. Saves the empty call per launch.
                if not token or len(page["data"]) < PAGE:
                    break
                if pages >= MAX_PAGES:
                    truncated = True
                    break
            with c:
                c.executemany("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                c.execute("UPDATE sample SET status=?, pages=?, txs=?, trades=?, credits=?, done_at=? WHERE mint=?",
                          ("truncated" if truncated else "done", pages, txs, len(rows), credits,
                           datetime.now(timezone.utc).isoformat(), mint))
            spent += credits
    except Stop as exc:
        with c:
            c.execute("INSERT INTO run_log VALUES(?,?,?,?)", (datetime.now(timezone.utc).isoformat(), kid, "stop",
                      json.dumps({"why": str(exc), "spent": spent})))
        print("STOP:", exc)
    s = dict(c.execute("SELECT status, COUNT(*) FROM sample GROUP BY 1").fetchall())
    print("key", kid, "credits spent", spent, "status", s)


def health():
    for i, (kid, url, bad) in enumerate(_urls()):
        if bad:
            print(i, kid, "REFUSED: shares a funded/watcher key")
            continue
        try:
            print(i, kid, make_rpc(url, 1)("getHealth", []))
        except Stop as exc:
            print(i, kid, "STOP", exc)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("select", "run", "health"))
    ap.add_argument("--key-index", type=int, default=0)
    ap.add_argument("--rate", type=float, default=2.0)
    ap.add_argument("--budget", type=int, default=100000)
    a = ap.parse_args()
    if not 1 <= a.rate <= 5:
        raise SystemExit("rate must be 1..5 calls/s")
    {"select": select, "run": lambda: run(a.key_index, a.rate, a.budget), "health": health}[a.cmd]()
