#!/usr/bin/env python3
"""What the m5 volume ACTUALLY was, on chain, at the second a 4663 entry was refused for it.

WHY. The m5 turnover floor refuses entries on a market-data provider's five-minute volume. Measured
on the ten `m5_turnover_under_floor` refusals in the 72 h to 2026-09-22 22:41Z:

    PATIENCE  provider $79.88    organic $88.14   floor $1,473  -> correctly refused
    TOMMY     provider $2,035.63 organic $196.64  floor $1,318  -> provider 8x HIGH
    GIGABYTE  provider $308.90   UNDECIDABLE      floor $2,846  -> 60 swaps, none priceable
    PAWSINU   provider $494.10   UNDECIDABLE      floor $2,436  -> 4 swaps, none priceable

THE COPIED TRADE IS NOT ORGANIC VOLUME, and this tool got that wrong once. The first run of it
reported PATIENCE at $1,584.03 and called the refusal a false one. $1,495.69 of that was the source
trader's OWN buy -- 0.543010 native in pool 0xf7872b55, transaction 0xda65692d, the exact swap the
entry was copying. Organic volume was $88.14 against a $1,473 floor, so the gate was right and the
tool was wrong. The floor asks whether OTHER people are trading, so the tool now excludes the
source transaction by hash, the same rule `memecoin_onchain_volume.py` states. With that exclusion,
NONE of the ten refusals would have passed on chain volume.

THE PROVIDER IS NOT ONE-DIRECTIONAL. It was 8x HIGH on TOMMY and close to the truth on PATIENCE, so
any fix premised on "the provider undercounts" is wrong about half the time.

UNDECIDABLE IS NOT ZERO, and this tool will not print a zero for it. A window whose swaps exist but
cannot be priced -- an equity-quoted pool, a quote token with no feed -- is reported as UNDECIDABLE
with the swap count. Printing $0.00 there is the error the volume module exists to prevent.

EVERY FIGURE IS A LOWER BOUND: V4 ONLY. The V3 gap this warned about is now mostly closed upstream
-- the pool store knows 1,398 v3 pools over 440 mints on 4663 since 2026-09-22 -- but this tool
still reads the V4 PoolManager alone. It costs nothing for the ten refusals above: every one of
those mints has V4 pools only, so V4 IS the complete set for them. For a mint that also trades on
V3 the row is still a lower bound. Measured on our own tape over the hour to 2026-09-22 23:31Z,
V3 is 18,795 of 164,475 swaps on this chain (11.4% of the count).

TRANSPORT IS SPLIT ON PURPOSE. publicnode answers `eth_getLogs` with HTTP 403 on this chain, so the
only endpoint that serves it is the FUNDED custody lane. Block timestamps therefore go to
publicnode, and custody is used for getLogs ALONE under a hard budget that ABORTS rather than
overruns -- a research loop on the funded key is what burned ~7k of quota on 2026-08-21.

Block numbers are ANCHORED by binary search on real block timestamps. Never extrapolate from
seconds-per-block: that has missed by thousands of seconds on this class of chain.

    python3 scripts/research/memecoin_m5_truth_4663.py --since '-72 hours'
    python3 scripts/research/memecoin_m5_truth_4663.py --since '-6 hours' --budget 40
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

READ_RPC = "https://robinhood-rpc.publicnode.com"     # timestamps: free, isolated
CHAIN = "4663"
UNDECIDABLE = "UNDECIDABLE"


class Budget(RuntimeError):
    """Raised instead of quietly overrunning the funded lane."""


class Rpc:
    def __init__(self, log_url, budget):
        self.log_url, self.budget = log_url, int(budget)
        self.calls = self.log_calls = 0

    def __call__(self, method, params, retries=3):
        url = READ_RPC
        if method == "eth_getLogs":
            self.log_calls += 1
            if self.log_calls > self.budget:
                raise Budget("custody getLogs budget of %d exhausted" % self.budget)
            url = self.log_url
        for attempt in range(retries):
            self.calls += 1
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params}).encode()
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out = json.load(resp)
                if "error" in out and attempt + 1 < retries:
                    time.sleep(1.0)
                    continue
                return out
            except Budget:
                raise
            except Exception:
                if attempt + 1 >= retries:
                    raise
                time.sleep(1.0)
        return {}


def block_at(rpc, target_ts, lo, hi):
    """The last block whose timestamp is <= `target_ts`. Anchored, never extrapolated."""
    def ts(n):
        blk = rpc("eth_getBlockByNumber", [hex(n), False]).get("result")
        return int(blk["timestamp"], 16) if blk else None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        at = ts(mid)
        if at is None:
            hi = mid - 1
        elif at <= target_ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _signed(word):
    value = int(word, 16)
    return value - (1 << 256) if value >> 255 else value


def source_tx_of(origin_event_id):
    """The copied trader's own transaction hash, carried inside the origin event id.

    The id looks like `swap:raybot:0x<tx>:0x<from>:0x<token>`, so the transaction is the one
    66-character field. Returns None when the id has no such field, and the caller then sums the
    whole window -- which OVERSTATES organic volume, so the absence is reported, never hidden.
    """
    for part in str(origin_event_id or "").split(":"):
        part = part.lower()
        if len(part) == 66 and part.startswith("0x"):
            return part
    return None


def window_volume(rpc, manager, swap_topic, pool_ids, lo, hi, meta, price_fn, exclude_tx=None):
    """(usd, priced_swaps, unpriced_swaps, pools_traded) over [lo, hi] for these pools.

    `exclude_tx` drops the copied trade. The floor asks whether OTHER people are trading, and one
    source buy of $1,495.69 was 94% of a window this tool once reported as organic (PATIENCE).
    """
    from sniper.services.memecoin_onchain_volume import _default_price  # noqa: F401
    exclude_tx = (exclude_tx or "").lower() or None
    total, priced, unpriced, traded = 0.0, 0, 0, set()
    step, start = 800, lo
    while start <= hi:
        end = min(start + step - 1, hi)
        out = rpc("eth_getLogs", [{"address": manager, "fromBlock": hex(start),
                                   "toBlock": hex(end),
                                   "topics": [swap_topic, list(pool_ids)]}])
        if "error" in out:
            if step <= 100:
                raise RuntimeError("getLogs refused even at 100 blocks: %s" % out["error"])
            step = max(100, step // 2)
            continue
        for log in out.get("result") or []:
            if exclude_tx and str(log.get("transactionHash") or "").lower() == exclude_tx:
                continue
            pool = str(log["topics"][1]).lower()
            data = str(log.get("data") or "0x")[2:]
            words = [data[i:i + 64] for i in range(0, len(data), 64)]
            entry = meta.get(pool)
            if len(words) < 2 or not entry or entry["quote_side"] not in (0, 1):
                unpriced += 1
                continue
            side = int(entry["quote_side"])
            token = entry["token0"] if side == 0 else entry["token1"]
            decimals = entry["token0_decimals"] if side == 0 else entry["token1_decimals"]
            price = price_fn(CHAIN, token)
            if decimals is None or price is None:
                unpriced += 1
                continue
            total += abs(_signed(words[0] if side == 0 else words[1])) / (10 ** int(decimals)) \
                * float(price)
            priced += 1
            traded.add(pool)
        start = end + 1
    return total, priced, unpriced, len(traded)


def verdict_for(usd, priced, unpriced, floor):
    """The honest label. UNDECIDABLE beats a number whenever nothing could be priced."""
    if priced == 0 and unpriced > 0:
        return UNDECIDABLE
    if floor is None:
        return "no floor parsed"
    if usd >= floor:
        return "WOULD HAVE PASSED"
    return "under (lower bound)" if unpriced else "under (clean)"


def since_cutoff(conn, since):
    """`since` as a timestamp shaped like the column it is compared against.

    THE SEPARATOR DECIDES THE WINDOW (2026-09-23, Gamma's catch). `datetime('now', ?)` renders
    `2026-09-22 23:25:20` with a SPACE, while `decision_started_at` is ISO with a 'T'. On the same
    calendar date the comparison reaches position 10 and finds 'T' (0x54) against ' ' (0x20), so
    every same-day row sorts ABOVE the threshold: `--since '-2 hours'` returned refusals twelve
    hours old. Rendering the cutoff with `strftime` keeps the flexible modifier syntax and makes
    both sides the same shape. Third time this trap has cost this project time; see
    `finding-sqlite-datetime-now-space-separator-2026-08-23`.
    """
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', datetime('now', ?))", (since,)).fetchone()[0]


def m5_refusals(conn, chain, since, limit):
    """The m5-floor refusals inside the window. Extracted so a test drives the REAL comparison.

    A test that only checked the cutoff string would keep passing if someone put
    `datetime('now', ?)` back into the WHERE clause, which is the regression worth catching.
    """
    return conn.execute(
        "SELECT decision_started_at, ticker, mint, terminal_reason, classification_json, "
        "origin_event_id "
        "FROM memecoin_entry_attempts WHERE network_id=? AND decision_started_at>=? "
        "AND terminal_reason LIKE '%m5_turnover_under_floor%' "
        "ORDER BY decision_started_at LIMIT ?",
        (str(chain), since_cutoff(conn, since), int(limit))).fetchall()


def _floor_from(reason):
    if "under floor $" not in str(reason or ""):
        return None
    try:
        return float(str(reason).split("under floor $")[1].split()[0].replace(",", ""))
    except (IndexError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="-72 hours", help="SQLite datetime modifier")
    ap.add_argument("--budget", type=int, default=120, help="hard cap on custody getLogs calls")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args(argv)

    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
    from sniper.services.memecoin_onchain_volume import _default_price

    log_url = os.getenv("MEMECOIN_EVM_RPC_URL_4663")
    if not log_url:
        print("MEMECOIN_EVM_RPC_URL_4663 unset; getLogs has no endpoint on this chain")
        return 2
    rpc = Rpc(log_url, args.budget)
    manager = V4_POOL_MANAGERS[CHAIN]

    led = sqlite3.connect("file:%s/sniper_tracker.db?mode=ro" % ROOT, uri=True)
    led.row_factory = sqlite3.Row
    vol = sqlite3.connect("file:%s/data/memecoin_onchain_volume.db?mode=ro" % ROOT, uri=True)
    vol.row_factory = sqlite3.Row

    rows = m5_refusals(led, CHAIN, args.since, args.limit)
    print("m5-floor refusals since %s: %d\n" % (args.since, len(rows)))
    if not rows:
        return 0

    head = int(rpc("eth_blockNumber", [])["result"], 16)
    head_ts = int(rpc("eth_getBlockByNumber", [hex(head), False])["result"]["timestamp"], 16)

    print("%-10s %-9s %12s %14s %10s  %s"
          % ("coin", "time", "provider", "chain V4", "floor", "verdict"))
    flips, undecidable, no_source = [], [], []
    for row in rows:
        when = dt.datetime.fromisoformat(str(row["decision_started_at"]).replace("Z", "+00:00"))
        target = int(when.timestamp())
        # A generous lower bound; the SEARCH is what fixes the block, not this guess.
        guess = max(1, head - int((head_ts - target + 600) * 14))
        hi_b = block_at(rpc, target, guess, head)
        lo_b = block_at(rpc, target - 300, guess, hi_b)
        pools = [r["pool"] for r in vol.execute(
            "SELECT pool FROM onchain_pool_protocols WHERE chain_id=? AND mint=?",
            (CHAIN, str(row["mint"]).lower()))]
        meta = {}
        for pool in pools:
            got = vol.execute(
                "SELECT token0, token1, quote_side, token0_decimals, token1_decimals "
                "FROM pool_meta WHERE chain_id=? AND pool=?", (CHAIN, pool)).fetchone()
            if got:
                meta[pool] = dict(got)
        provider = None
        try:
            provider = ((json.loads(row["classification_json"]) or {}).get("facts")
                        or {}).get("volume_m5")
        except (TypeError, ValueError):
            pass
        floor = _floor_from(row["terminal_reason"])
        exclude_tx = source_tx_of(row["origin_event_id"])
        if exclude_tx is None:
            no_source.append(row["ticker"])
        usd, priced, unpriced, traded = (0.0, 0, 0, 0)
        if pools:
            try:
                usd, priced, unpriced, traded = window_volume(
                    rpc, manager, V4_SWAP, pools, lo_b, hi_b, meta, _default_price,
                    exclude_tx=exclude_tx)
            except Budget as exc:
                print("\nABORTED: %s" % exc)
                break
        v = verdict_for(usd, priced, unpriced, floor)
        shown = UNDECIDABLE if v == UNDECIDABLE else "$%.2f" % usd
        print("%-10s %-9s %12s %14s %10s  %s"
              % (row["ticker"], str(row["decision_started_at"])[11:19],
                 ("$%.2f" % provider) if provider is not None else "none", shown,
                 ("$%.0f" % floor) if floor else "?", v))
        print("%-10s %-9s   swaps priced %d, unpriced %d, pools known %d traded %d, blocks %d..%d"
              % ("", "", priced, unpriced, len(pools), traded, lo_b, hi_b))
        if v == "WOULD HAVE PASSED":
            flips.append(row["ticker"])
        if v == UNDECIDABLE:
            undecidable.append(row["ticker"])

    print("\n=== SUMMARY ===")
    print("  would have PASSED on chain V4 volume alone : %d  %s"
          % (len(flips), ", ".join(flips) or "-"))
    print("  UNDECIDABLE (swaps existed, none priceable): %d  %s"
          % (len(undecidable), ", ".join(undecidable) or "-"))
    print("  the copied trade is excluded by hash; these are ORGANIC figures.")
    if no_source:
        print("  NO SOURCE HASH, so these rows include the copied trade and OVERSTATE: %s"
              % ", ".join(no_source))
    print("  V3 is NOT included, so a mint that also trades on V3 is a lower bound.")
    print("  rpc: %d calls, of which %d getLogs on the FUNDED custody lane (budget %d)"
          % (rpc.calls, rpc.log_calls, args.budget))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
