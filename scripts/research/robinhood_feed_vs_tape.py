#!/usr/bin/env python3
"""When does our 4663 swap tape see a transaction, against Robinhood Chain's own sequencer feed?

Asked 2026-09-28 (operator: "robinhood must see and fill under a second"). The public sequencer
feed (wss://feed.mainnet.chain.robinhood.com, docs.robinhood.com/chain/connecting) broadcasts every
sequenced transaction; nothing sees a 4663 transaction earlier without a node beside the sequencer.
This pairs each feed transaction with the first `pool_ticks` row our volume writer stored for the
same hash and reports tape minus feed.

ONE CLOCK. The volume writer stamps `observed_at` with `real_utc_now()` (memecoin_volume_writer,
`iso(now)`), so feed arrivals are stamped with the same function. Its offset is rewritten per J7
message, so each difference carries tens of ms of offset movement.

Read-only: a public websocket that no funded path uses, and data/memecoin_onchain_volume.db opened
mode=ro.

    python3 scripts/research/robinhood_feed_vs_tape.py --seconds 120
    python3 scripts/research/robinhood_feed_vs_tape.py --selftest
"""
import argparse
import asyncio
import base64
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

FEED = "wss://feed.mainnet.chain.robinhood.com"
TAPE = REPO / "data" / "memecoin_onchain_volume.db"


def txs(buf):
    """Raw signed-tx bytes in an Arbitrum L2 message: kind 4 = one signed tx, kind 3 = a batch of
    8-byte big-endian length-prefixed sub-messages. Other kinds carry no user transaction."""
    if not buf:
        return
    kind, body = buf[0], buf[1:]
    if kind == 4:
        yield body
    elif kind == 3:
        i = 0
        while i + 8 <= len(body):
            n = int.from_bytes(body[i:i + 8], "big")
            i += 8
            yield from txs(body[i:i + n])
            i += n


async def _listen(seconds, seen, now_fn):
    import websockets
    from eth_hash.auto import keccak
    async with websockets.connect(FEED, max_size=2 ** 24) as ws:
        end = time.time() + seconds
        while time.time() < end:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            at = float(now_fn())
            for m in json.loads(raw).get("messages") or []:
                l2 = base64.b64decode(m["message"]["message"].get("l2Msg") or "")
                for tx in txs(l2):
                    seen.setdefault("0x" + keccak(tx).hex(), at)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        one, two = b"\x02abc", b"\x02defg"
        seg = lambda k, p: (len(p) + 1).to_bytes(8, "big") + bytes([k]) + p
        batch = bytes([3]) + seg(4, one) + seg(4, two) + seg(9, b"xx")
        assert list(txs(batch)) == [one, two], list(txs(batch))
        assert list(txs(bytes([4]) + one)) == [one] and list(txs(b"")) == []
        print("selftest ok")
        return 0
    from sniper.util.clock import real_utc_now
    seen = {}
    asyncio.run(_listen(args.seconds, seen, real_utc_now))
    time.sleep(5)                                  # let the tape land the window's last rows
    lo = datetime.fromtimestamp(min(seen.values()) - 5, timezone.utc).isoformat()
    conn = sqlite3.connect("file:%s?mode=ro" % TAPE, uri=True, timeout=5)
    rows = conn.execute("SELECT tx_sig, MIN(observed_at) FROM pool_ticks WHERE chain_id='4663' "
                        "AND observed_at>=? GROUP BY tx_sig", (lo,)).fetchall()
    conn.close()
    lags = sorted(datetime.fromisoformat(obs).timestamp() - seen[str(sig).lower()]
                  for sig, obs in rows if str(sig).lower() in seen)
    print(f"feed txs {len(seen)}; tape txs {len(rows)}; matched {len(lags)}")
    if lags:
        q = lambda p: lags[min(len(lags) - 1, int(p * len(lags)))]
        print(f"tape minus feed (s): p10 {q(.1):+.3f} p50 {q(.5):+.3f} p90 {q(.9):+.3f} "
              f"min {lags[0]:+.3f} max {lags[-1]:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
