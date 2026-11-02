#!/usr/bin/env python3
"""Read frozen evidence and write the four-rule quote comparison. No live calls."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sniper.analysis.memecoin_adaptive_exit_replay import SPLIT, VARIANTS, replay, summarize, timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    raw = args.evidence.read_bytes()
    bundles = json.loads(raw)
    rows, sensitivity = [], []
    for b in bundles:
        p = b['position']
        split = 'earlier' if timestamp(p['opened_at']) < timestamp(SPLIT) else 'later'
        if split == 'earlier' and timestamp(p['closed_at']) >= timestamp(SPLIT):
            split = 'crosses_split'
        base = {k: p[k] for k in ('id', 'network_id', 'mint', 'ticker', 'source_handle', 'opened_at', 'closed_at', 'exit_reason')}
        base.update(split=split, actual_pnl=float(p['realized_pnl']),
                    outcomes={v: replay(b, v) for v in VARIANTS})
        rows.append(base)
        sensitivity.append(dict(base, outcomes={v: replay(b, v, max_gap_s=5) for v in VARIANTS}))
    earlier = [r for r in rows if r['split'] == 'earlier']
    later = [r for r in rows if r['split'] == 'later']
    train = summarize(earlier)
    test = summarize(later)
    nominee = None
    if train['coins'] >= 30:
        candidate = max(VARIANTS[1:], key=lambda v: train['variants'][v]['delta'])
        if train['variants'][candidate]['delta'] > 0:
            nominee = candidate
    seen = {(r['network_id'], r['mint']) for r in earlier}
    report = dict(
        evidence_sha256=hashlib.sha256(raw).hexdigest(), split_utc=SPLIT,
        verdict='no_funded_recommendation_missing_safety_and_fill_evidence',
        earlier_nominee=nominee, earlier=train, later=test, all=summarize(rows),
        later_new_coins=summarize([r for r in later if (r['network_id'], r['mint']) not in seen]),
        later_nominee_result=(test['variants'].get(nominee) if nominee else None),
        by_chain={chain: {split: summarize([r for r in rows if r['network_id'] == chain and r['split'] == split])
                          for split in ('earlier', 'later')}
                  for chain in sorted({r['network_id'] for r in rows})},
        provenance_complete={split: summarize([r for r in rows if r['split'] == split and
            all(o['status'] == 'quoted' and o['provenance_complete'] for o in r['outcomes'].values())])
            for split in ('earlier', 'later')},
        five_second_gap={split: summarize([r for r in sensitivity if r['split'] == split])
                         for split in ('earlier', 'later')},
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'results.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    (args.output/'paths.json').write_text(json.dumps(rows, indent=2, allow_nan=False)+'\n')
    flat = [dict({k: value for k, value in r.items() if k != 'outcomes'}, variant=v, **o)
            for r in rows for v, o in r['outcomes'].items()]
    fields = list(dict.fromkeys(key for row in flat for key in row))
    with (args.output/'outcomes.csv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flat)
    print(json.dumps({k: report[k] for k in ('verdict', 'earlier_nominee', 'earlier', 'later')}, indent=2))


if __name__ == '__main__':
    main()
