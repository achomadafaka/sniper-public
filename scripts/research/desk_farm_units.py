#!/usr/bin/env python3
"""One JSON row per (coin, level) unit: farm features at t0, CRASH labels, R300 (optimistic fill). usage: DAY OUT.jsonl"""
import bisect, json, sqlite3, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import desk_farm_features as F

E = F.E
LEVELS = (12500, 15000, 17500, 20000, 25000, 30000, 35000)


def unit_rows(c, silent, span, stats):
    if not (c.first_ok and c.quote_ok):
        stats["quote_unverified"] += 1
        return
    crossed = set()
    for i in range(len(c.t)):
        m = E.mcap_usd(c, i)
        for lv in LEVELS:
            if lv in crossed or m < lv:
                continue
            crossed.add(lv)
            t0, tf = c.t[i], c.t[i] + E.ENTRY_DELAY_S
            if tf + 300 + 600 > span[1] or t0 - 900 < span[0]:
                stats["day_edge"] += 1
                continue
            if E.overlaps(silent, t0 - 300, t0 + 905):
                stats["tape_silence"] += 1
                continue
            f = F.features(c, i)
            if f is None:
                stats["thin"] += 1
                continue
            j = E.fill_index(c, i, False)
            end = bisect.bisect_right(c.t, tf + 300) - 1
            if c.fee[j] is None or c.fee[end] is None or min(c.rt[j:end + 1]) <= 0:
                stats["fee_or_completed"] += 1
                continue
            qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
            tokens = c.b[j] * qn / (c.q[j] + qn)
            r300 = E.value(c, end, tokens, qn) - 1.0 - 0.025
            row = {"mint": c.mint, "level": lv, "t0": t0, "r300": r300, **f}
            for d in (0.30, 0.40, 0.50):
                row["crash%d" % int(d * 100)] = F.crash_label(c, i, 600.0, d)
            if any(row["crash%d" % x] is None for x in (30, 40, 50)):
                stats["slot_end_unknown"] += 1                   # a slot with no unique last swap: unknown, never a label
                continue
            yield row


def run(day, out):
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < __import__("time").strftime("%Y-%m-%d", __import__("time").gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, span = E.silence_intervals(db)
    stats, n, mint, coin = defaultdict(int), 0, None, None
    with open(out, "w") as fh:
        def flush(cn):
            nonlocal n
            if cn is None or len(cn.t) < 2:
                return
            stats["coins"] += 1
            for row in unit_rows(cn, silent, span, stats):
                fh.write(json.dumps(row) + "\n"); n += 1
        cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, "
                         "virtual_token, real_token, fee_bps, creator_fee_bps, %s FROM swaps ORDER BY mint, slot, %s, received_at, ordinal" % (qm, txi))
        for r in cur:
            if r[0] != mint:
                flush(coin); mint, coin = r[0], E.Coin(r[0])
            coin.add(r)
        flush(coin)
    print(json.dumps({"day": day, "units": n, **stats}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
