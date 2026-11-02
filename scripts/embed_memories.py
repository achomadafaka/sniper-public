#!/usr/bin/env python3
"""
embed_memories.py — Backfill / refresh pgvector embeddings for the `memories` brain.

Embeds each memory (name + description + content) with the local bge-small-en model
and stores the vector via the `set_memory_embedding` RPC (migration 029). Idempotent:
rows whose embedded text is unchanged (matching `embedding_hash`) are skipped, so this
is cheap to re-run after `sync_session_findings.py` adds new memories.

Usage:
    python3 scripts/embed_memories.py            # embed all new/changed memories
    python3 scripts/embed_memories.py --dry-run  # show what would be embedded
    python3 scripts/embed_memories.py --all      # re-embed everything (ignore hash)
    python3 scripts/embed_memories.py --active-only
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

import bot.supabase_helper as sb  # noqa: E402
from bot.embeddings import (  # noqa: E402
    embed_passages, to_pgvector, memory_passage_text, content_hash, is_available, MODEL_NAME,
)

BATCH = 128


def _fetch_rows(active_only: bool) -> list[dict]:
    # Paginate explicitly — PostgREST can silently cap responses (db-max-rows),
    # which would drop the tail of the corpus without error (a silent cap this
    # repo's doctrine bans). Order by a stable key (id) so paging is consistent.
    base = "memories?select=id,name,description,content,embedding_hash&order=id.asc"
    if active_only:
        base += "&status=eq.active"
    PAGE = 1000
    rows: list[dict] = []
    offset = 0
    while True:
        batch = sb._request("GET", f"{base}&limit={PAGE}&offset={offset}") or []
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill pgvector embeddings for memories")
    ap.add_argument("--dry-run", action="store_true", help="Show counts, embed/write nothing")
    ap.add_argument("--all", action="store_true", help="Re-embed every row (ignore embedding_hash)")
    ap.add_argument("--active-only", action="store_true", help="Only embed status=active rows")
    args = ap.parse_args()

    if not is_available():
        print(f"ERROR: embedding model {MODEL_NAME} unavailable. Seed the fastembed cache "
              "(see bot/embeddings.py docstring) then retry.")
        sys.exit(1)

    rows = _fetch_rows(args.active_only)
    todo = []
    for r in rows:
        text = memory_passage_text(r.get("name", ""), r.get("description", ""), r.get("content", ""))
        h = content_hash(text)
        if args.all or r.get("embedding_hash") != h:
            todo.append((r["id"], text, h))

    print(f"{len(rows)} memories scanned, {len(todo)} need (re)embedding"
          f"{' (--all)' if args.all else ''}.")
    if args.dry_run or not todo:
        if args.dry_run:
            print("(dry-run: nothing embedded or written)")
        return

    written = failed = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        vecs = embed_passages([t for _, t, _ in chunk])
        for (rid, _text, h), vec in zip(chunk, vecs):
            try:
                sb._request("POST", "rpc/set_memory_embedding",
                            body={"p_id": rid, "p_embedding": to_pgvector(vec), "p_hash": h})
                written += 1
            except Exception as e:
                print(f"  ❌ {rid}: {e}")
                failed += 1
        print(f"  …{min(i + BATCH, len(todo))}/{len(todo)}")

    print(f"Done: {written} embedded, {failed} failed")


if __name__ == "__main__":
    main()
