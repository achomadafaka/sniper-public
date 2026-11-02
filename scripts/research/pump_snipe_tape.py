#!/usr/bin/env python3
"""Capture the pump.fun firehose and rank snipers by 15-30s response at tiny mcaps.

The fomo feed does not carry the 40k->140k-in-seconds game: last 24h it held 38 solana buys
under $250k mcap and our marks tape covered none of them. This collector takes the venue's
own trade stream instead: PumpPortal (wss://pumpportal.fun/api/data, free, keyless) pushes
every pump.fun trade with the buyer address and the bonding-curve marketCapSol AFTER the
trade — an exact per-trade price path, admission-independent, no quote polling.

    run     capture: subscribe new tokens, follow each token's trades for its first 30 min
    report  rank buyers: median peak response +15s/+30s after their buys (exclude creators)

Research-only. Own DB (data/pump_snipe_tape.sqlite). No custody, no funded imports, no keys.
"""
import argparse, asyncio, json, sqlite3, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DB = REPO / "data" / "pump_snipe_tape.sqlite"
HEARTBEAT = REPO / "data" / "pump_snipe.heartbeat"


def _api_key():
    # subscribeTokenTrade is paywalled (needs a key funded with >=0.02 SOL). Free tier
    # delivers creates only. Key lives in .env as PUMPPORTAL_API_KEY.
    try:
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("PUMPPORTAL_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


_KEY = _api_key()
WS_URL = "wss://pumpportal.fun/api/data" + (f"?api-key={_KEY}" if _KEY else "")
FOLLOW_S = 1800          # follow a token's trades for its first 30 min
MAX_SUBS = 300           # ponytail: FIFO cap; raise if unsub churn proves too lossy

SCHEMA = """
CREATE TABLE IF NOT EXISTS ps_tokens (
    mint TEXT PRIMARY KEY,
    created_ts REAL NOT NULL,
    creator TEXT,
    symbol TEXT,
    initial_mcap_sol REAL
);
CREATE TABLE IF NOT EXISTS ps_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts REAL NOT NULL,              -- our receive time (epoch)
    trader TEXT NOT NULL,
    tx_type TEXT NOT NULL,         -- buy | sell
    sol_amount REAL,
    mcap_sol REAL                  -- bonding-curve mcap AFTER this trade
);
CREATE INDEX IF NOT EXISTS idx_ps_trades_mint_ts ON ps_trades(mint, ts);
CREATE INDEX IF NOT EXISTS idx_ps_trades_trader ON ps_trades(trader);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def run():
    import websockets
    db = sqlite3.connect(DB, timeout=30)
    db.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)
    subs = {}                     # mint -> subscribe epoch
    backoff = 2.0
    n_tokens = n_trades = 0
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, max_size=2_000_000) as ws:
                backoff = 2.0
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                # re-subscribe survivors after a reconnect
                live = [m for m, t in subs.items() if time.time() - t < FOLLOW_S]
                for i in range(0, len(live), 50):
                    await ws.send(json.dumps({"method": "subscribeTokenTrade",
                                              "keys": live[i:i + 50]}))
                last_prune = 0.0
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                    ts = time.time()
                    tx = msg.get("txType")
                    mint = msg.get("mint")
                    if tx == "create" and mint:
                        db.execute("INSERT OR IGNORE INTO ps_tokens VALUES (?,?,?,?,?)",
                                   (mint, ts, msg.get("traderPublicKey"),
                                    msg.get("symbol"), msg.get("marketCapSol")))
                        subs[mint] = ts
                        n_tokens += 1
                        await ws.send(json.dumps({"method": "subscribeTokenTrade",
                                                  "keys": [mint]}))
                    elif tx in ("buy", "sell") and mint:
                        db.execute("INSERT INTO ps_trades(mint,ts,trader,tx_type,sol_amount,mcap_sol) "
                                   "VALUES (?,?,?,?,?,?)",
                                   (mint, ts, msg.get("traderPublicKey") or "?", tx,
                                    msg.get("solAmount"), msg.get("marketCapSol")))
                        n_trades += 1
                    if ts - last_prune > 60:
                        last_prune = ts
                        db.commit()
                        dead = [m for m, t in subs.items() if ts - t >= FOLLOW_S]
                        if len(subs) - len(dead) > MAX_SUBS:
                            extra = sorted((t, m) for m, t in subs.items()
                                           if m not in dead)[:len(subs) - len(dead) - MAX_SUBS]
                            dead += [m for _, m in extra]
                        for i in range(0, len(dead), 50):
                            await ws.send(json.dumps({"method": "unsubscribeTokenTrade",
                                                      "keys": dead[i:i + 50]}))
                        for m in dead:
                            subs.pop(m, None)
                        HEARTBEAT.write_text(json.dumps(
                            {"ts": now_iso(), "subs": len(subs),
                             "tokens": n_tokens, "trades": n_trades}))
        except Exception as exc:
            db.commit()
            print(f"{now_iso()} ws reconnect after error: {exc}; sleep {backoff:.0f}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)


def sol_usd():
    try:
        req = urllib.request.Request(
            "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112")
        pairs = json.load(urllib.request.urlopen(req, timeout=10)).get("pairs") or []
        px = [float(p["priceUsd"]) for p in pairs
              if p.get("priceUsd") and p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT")]
        if px:
            px.sort()
            return px[len(px) // 2]
    except Exception:
        pass
    return 77.0   # last verified 2026-08-19


def report(hours, mcap_max_usd, min_buys):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    sol = sol_usd()
    mcap_max_sol = mcap_max_usd / sol
    since = time.time() - hours * 3600
    creators = {m: c for m, c in conn.execute("SELECT mint, creator FROM ps_tokens")}
    tapes = defaultdict(list)     # mint -> [(ts, mcap_sol)]
    buys = []
    for mint, ts, trader, tx, mcap in conn.execute(
            "SELECT mint, ts, trader, tx_type, mcap_sol FROM ps_trades "
            "WHERE ts >= ? AND mcap_sol > 0 ORDER BY ts", (since,)):
        tapes[mint].append((ts, mcap))
        if tx == "buy" and mcap <= mcap_max_sol and trader != creators.get(mint):
            buys.append((mint, ts, trader, mcap))
    print(f"SOL=${sol:.0f}  window={hours}h  mcap<=${mcap_max_usd/1000:.0f}k "
          f"({len(buys)} buys, {len(tapes)} mints)")
    byw = defaultdict(list)
    for mint, ts, trader, base in buys:
        tape = tapes[mint]
        path15 = [m for t, m in tape if ts < t <= ts + 15]
        path30 = [m for t, m in tape if ts < t <= ts + 30]
        if not path30:
            continue               # no trade after = no measurable move, no exit either
        byw[trader].append({
            "mint": mint, "mcap_usd": base * sol,
            "peak15": (max(path15) / base - 1) * 100 if path15 else 0.0,
            "peak30": (max(path30) / base - 1) * 100,
            "end30": (path30[-1] / base - 1) * 100})
    rows = []
    for w, xs in byw.items():
        if len(xs) < min_buys or len({x["mint"] for x in xs}) < 2:
            continue
        p15 = sorted(x["peak15"] for x in xs)
        p30 = sorted(x["peak30"] for x in xs)
        e30 = sorted(x["end30"] for x in xs)
        rows.append((p30[len(p30) // 2], w, len(xs), len({x["mint"] for x in xs}),
                     p15[len(p15) // 2], e30[len(e30) // 2],
                     sum(1 for v in p30 if v >= 25) / len(p30),
                     sorted(x["mcap_usd"] for x in xs)[len(xs) // 2] / 1000))
    rows.sort(reverse=True)
    print(f"{'trader':<46}{'n':>4}{'mints':>6}{'medPk15':>9}{'medPk30':>9}"
          f"{'medEnd30':>9}{'P(pk30>=25)':>12}{'medMcap':>9}")
    for med30, w, n, m, p15, e30, hit, mc in rows[:20]:
        print(f"{w:<46}{n:>4}{m:>6}{p15:>+8.1f}%{med30:>+8.1f}%{e30:>+8.1f}%"
              f"{hit:>11.0%}{mc:>8.0f}k")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="run", choices=("run", "report"))
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--mcap-max-usd", type=float, default=150_000)
    ap.add_argument("--min-buys", type=int, default=3)
    a = ap.parse_args()
    if a.mode == "run":
        asyncio.run(run())
    else:
        report(a.hours, a.mcap_max_usd, a.min_buys)
