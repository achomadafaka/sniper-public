#!/usr/bin/env python3
"""What happens to a buy at curve fill 90-97.6%? Per coin, first sample second inside the band; exact clip buy at +1.5 s; exit at the earlier of +300 s and the last
pre-completion swap ('rode to migration'). usage: DAY SAMPLES.npz  (samples from tape_reader_samples.py; X col curve_fill, mcap)"""
import bisect, sqlite3, sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
from tape_reader_samples import FEATS
from tape_reader_confirm import load_coin

ROOT = Path(__file__).resolve().parents[2]
LAT, COST = 1.5, 0.025
BANDS = ((0.90, 0.94), (0.94, 0.976), (0.976, 1.0))


def main(day, samples):
    d = np.load(samples, allow_pickle=False)
    cf = d["X"][:, FEATS.index("curve_fill")]
    db = sqlite3.connect("file:%s?mode=ro&immutable=1" % (ROOT / "data" / "desk" / "swaps" / ("%s.db" % day)), uri=True, isolation_level=None)
    for lo, hi in BANDS:
        idx = np.where((cf >= lo) & (cf < hi))[0]
        seen, rows = set(), []
        for r in idx:
            m = str(d["mint"][r])
            if m in seen:
                continue
            seen.add(m)
            c = load_coin(db, m); t = float(d["t"][r])
            j = bisect.bisect_right(c.t, t + LAT) - 1
            if j < 0 or c.fee[j] is None or (c.rt[j] is not None and c.rt[j] <= 0):
                continue
            qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j]); tok = c.b[j] * qn / (c.q[j] + qn)
            end = bisect.bisect_right(c.t, t + LAT + 300)
            done = next((k for k in range(j + 1, end) if c.rt[k] is not None and c.rt[k] <= 0), None)
            e = (done - 1) if done is not None else end - 1
            if e <= j:
                rows.append((None, done is not None, None)); continue
            rows.append((E.value(c, e, tok, qn) - 1 - COST, done is not None, c.t[done] - t if done is not None else None))
        R = np.array([x[0] for x in rows if x[0] is not None]); comp = [x[1] for x in rows]
        ttc = [x[2] for x in rows if x[2] is not None]
        print("%s band %.3f-%.3f coins %d  valued %d  completes<=300s %.0f%%  meanR %+.3f  medianR %+.3f  R>0 %.0f%%  median_s_to_complete %s" % (
            day, lo, hi, len(rows), len(R), 100 * np.mean(comp) if comp else 0, R.mean() if len(R) else float("nan"), np.median(R) if len(R) else float("nan"),
            100 * (R > 0).mean() if len(R) else 0, round(float(np.median(ttc))) if ttc else None))


if __name__ == "__main__":
    main(*sys.argv[1:])
