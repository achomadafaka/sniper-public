"""Latency of the tx-shape sampler inside Worker.process(): realistic-size synthetic transaction frames (30 account keys, 8 top-level and
6 inner instructions, one trade event), pct 0 vs 5 vs 100, per-frame microseconds p50/p90/p99/max. Offline, temp dirs, no network.
Run at low priority: nice -n 19 python3 scripts/research/txshape_latency_bench.py --n 20000"""
import argparse
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from sniper.feeds.yellowstone import geyser_pb2 as pb                       # noqa: E402
from sniper.services import memecoin_desk_universe as U                     # noqa: E402
from sniper.services import memecoin_desk_txshape as T                      # noqa: E402
from test_memecoin_desk_universe_worker import W, trade_event, vq_for       # noqa: E402


def frame(i):
    u = pb.SubscribeUpdate()
    t = u.transaction
    t.slot = 1000 + i // 40
    t.transaction.index = 1 + i % 40
    t.transaction.signature = i.to_bytes(8, "big") * 8
    keys = [bytes([9]) * 32, U.b58decode(U.PUMP_PROGRAM), U.b58decode(T.SYSTEM), U.b58decode(T.COMPUTE_BUDGET)] + [bytes([20 + k]) * 32 for k in range(26)]
    t.transaction.transaction.message.account_keys.extend(keys)
    msg = t.transaction.transaction.message
    for pi, data in ((3, bytes([2]) + struct.pack("<I", 200000)), (3, bytes([3]) + struct.pack("<Q", 5000)), (2, struct.pack("<IQ", 2, 1_000_000)),
                     (2, struct.pack("<IQ", 2, 2_000_000)), (7, b"route" * 8), (1, b"x" * 24), (8, b"y" * 16), (9, b"z" * 16)):
        ix = msg.instructions.add()
        ix.program_id_index = pi
        ix.accounts = bytes(range(0, 12))
        ix.data = data
    g = t.transaction.meta.inner_instructions.add()
    g.index = 4
    for pi in (1, 2, 10, 11, 12):
        ix = g.instructions.add()
        ix.program_id_index = pi
        ix.accounts = bytes([0, 1, 2])
        ix.data = struct.pack("<IQ", 2, 3000) if pi == 2 else b"q" * 20
    ev = g.instructions.add()
    ev.program_id_index = 1
    ev.data = U.CPI_EVENT_PREFIX + trade_event(*vq_for(20_000 + (i % 50) * 100), is_buy=bool(i % 2))
    return u.SerializeToString()


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)


def run(n, share):
    cfg = W.knobs({})
    cfg.update({"cooldown_s": "0", "txshape_pct": str(share)})
    base = Path(tempfile.mkdtemp())
    w = W.Worker(cfg, base=base, sol_price_fn=lambda: 150.0, alert_fn=lambda *a: None)
    w.sol_usd, w.sol_at = 150.0, time.time()
    frames = [frame(i) for i in range(n)]
    out = []
    for f in frames:
        a = time.perf_counter()
        w.process(f)
        out.append((time.perf_counter() - a) * 1e6)
    w.files.commit()
    return {"pct": share, "frame_us_p50_p90_p99_max": [pct(out, .5), pct(out, .9), pct(out, .99), round(max(out), 1)],
            "sampled_cost_us_p50_p90": [pct(w.txshape_us, .5), pct(w.txshape_us, .9)] if w.txshape_us else None,
            "written": w.txshape.stats["written"] if w.txshape else 0, "errors": w.txshape.stats["errors"] if w.txshape else 0}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    a = ap.parse_args()
    for share in (0, 5, 100, 0, 5):
        print(run(a.n, share))
