#!/usr/bin/env python3
"""Bounded, restartable graph embedding backfill using the offline local model.

Requires migration 038. Existing vectors are preserved. The row limit covers all
three tables; rerun to continue. No fact status or verification is changed.
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.brain_graph import READ_STATUSES
from bot.brain_graph.store import BrainUnavailable

FIELDS = {'brain_facts': ('statement', 'detail'),
          'brain_entities': ('name', 'summary'),
          'brain_episodes': ('body',)}
# brain_match_facts returns only live facts, and a closed fact never reopens: embedding one spends the
# limit for nothing (2026-10-02: 8,008 of 8,572 facts without a vector were closed).
LIVE = {'brain_facts': (('valid_to', 'is', 'null'),
                        ('status', 'in', '(' + ','.join(READ_STATUSES) + ')'))}


def configure_process():
    # CLI process only; never called by a funded runner or on a shared thread.
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    os.environ['BRAIN_EMBED_THREADS'] = '4'
    os.environ['HF_HUB_OFFLINE'] = '1'


def run(store, *, embed_fn=None, limit=500, batch_size=32, dry_run=False,
        sleep_fn=time.sleep, progress=None):
    if not 1 <= limit <= 100000 or not 1 <= batch_size <= 64:
        raise ValueError('limit must be 1..100000 and batch_size 1..64')
    report = progress if progress is not None else {}
    report.update(selected=0, written=0, batches=0, dry_run=dry_run)

    def request(fn):
        for attempt in range(3):
            sleep_fn(.2)  # All read/write attempts are paced to at most five/sec.
            try:
                return fn()
            except BrainUnavailable:
                if attempt == 2:
                    raise
                sleep_fn(.5 * (2 ** attempt))

    for table, fields in FIELDS.items():
        after = None
        while report['selected'] < limit:
            count = min(batch_size, limit - report['selected'])
            page = request(lambda: store.rows_page(table, select=','.join(('id',) + fields),
                filters=(('embedding', 'is', 'null'),) + LIVE.get(table, ()), after_id=after, limit=count))
            if not page:
                break
            if len(page) > count or any(not r.get('id') or (after and r['id'] <= after) for r in page):
                raise ValueError('embedding page did not advance or exceeded limit')
            report['selected'] += len(page)
            if not dry_run:
                if embed_fn is None:
                    from bot.embeddings import embed_passages
                    embed_fn = embed_passages
                texts = ['\n'.join(str(r[f]) for f in fields if r.get(f))[:6000] for r in page]
                vectors = list(embed_fn(texts))
                if len(vectors) != len(page) or any(len(v) != 384 or any(
                        not math.isfinite(float(x)) for x in v) for v in vectors):
                    raise ValueError('embedding model returned invalid vectors')
                from bot.embeddings import to_pgvector
                rows = [dict(table=table, id=r['id'], embedding=to_pgvector(v))
                        for r, v in zip(page, vectors)]
                written = request(lambda: store._call('POST', 'rpc/brain_set_embeddings', {'p_rows': rows}))
                if not isinstance(written, int) or not 0 <= written <= len(rows):
                    raise ValueError('embedding RPC returned invalid count')
                report['written'] += written
                report['batches'] += 1
            after = page[-1]['id']
            # Continue until an empty page, even if the server capped this page.
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--dry-run', action='store_true', help='count missing rows without loading the model or writing')
    args = parser.parse_args(argv)
    progress = {}
    try:
        configure_process()
        from bot.brain_graph.live import state_root
        from sniper.util.envfiles import load_env_files
        load_env_files(state_root(ROOT))
        from bot.brain_graph.store import RestStore
        result = run(RestStore(timeout=10), limit=args.limit, batch_size=args.batch_size,
                     dry_run=args.dry_run, progress=progress)
        print(json.dumps(result))
        return 0
    except Exception as exc:
        # Provider exception messages can contain URLs/credentials; expose class only.
        print(json.dumps(dict(progress, error=type(exc).__name__)), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
