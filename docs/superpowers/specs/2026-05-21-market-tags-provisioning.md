# Market Tags Provisioning

**Date:** 2026-05-21
**Status:** implemented on `codex/market-tags-provisioning`

## Mission

Provision the existing tag-grain data path so wallet-protocol runs can use
`leafs_by_cid` and `nav_lookup` instead of degrading to
`tag_lookup_state="unavailable"`. This is data infrastructure only. It does
not change verdicts, gates, `primary_edge_type`, or copyability framing.

## Consumer Contract

The analyzer contract is defined by
`sniper/analysis/wallet_protocol/tag_inference.py::fetch_tag_lookups_from_db`.
It returns:

- `leafs_by_cid`: `dict[str, list[str]]`, where keys are lowercased
  `condition_id` values and values are raw Polymarket leaf tag slugs from
  `market_tags.tag`.
- `nav_lookup`: `dict[str, str]`, where keys are leaf tag slugs and values are
  nav categories from `tag_nav_category.nav_category`.

Required schema:

```sql
CREATE TABLE IF NOT EXISTS market_tags (
    condition_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    tag_label TEXT,
    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (condition_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_market_tags_tag ON market_tags(tag);
CREATE INDEX IF NOT EXISTS idx_market_tags_condition_id ON market_tags(condition_id);

CREATE TABLE IF NOT EXISTS tag_nav_category (
    tag TEXT PRIMARY KEY,
    nav_category TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'manual',
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tag_nav_category_nav ON tag_nav_category(nav_category);
```

`market_tags` is the only production table this provisioning task writes.
`tag_nav_category` is an existing support table created by `init_db()` and
seeded separately from `data/tag_nav_category_seed.yaml`; missing nav mapping
degrades to `nav_lookup={}` and `hierarchy_source="none"`.

## Data Source

Verified with `curl -4` on 2026-05-21:

- `https://gamma-api.polymarket.com/markets?...` returned market objects with
  `conditionId` and nested `events`, but no usable market-level `tags` field in
  the probed response.
- `https://gamma-api.polymarket.com/events?...` returned event objects with
  top-level `tags` plus nested `markets[].conditionId`. Example tags in the
  live response included `economy`, `business`, `2025-predictions`, `crypto`,
  `microstrategy`, and `stocks`.

Chosen source: Gamma `/events`, bulk-paginated. One event page carries many
markets, so the backfill avoids per-market fetches.

## Backfill Design

`scripts/backfill_market_tags.py` now provides a page-wise provisioning path:

- creates `market_tags` if absent, without touching unrelated production data;
- extracts tags from each Gamma `/events` page;
- maps every nested market `conditionId` to the event's tag slugs;
- lowercases `condition_id` and `tag`;
- inserts with `INSERT OR IGNORE`;
- commits page by page so a mid-run API failure leaves a partial population
  that can be completed by rerunning the idempotent backfill from offset 0;
- reruns are idempotent and preserve first-seen labels;
- malformed and empty pages log warnings and are skipped.

The streamer-side helper `db.bulk_upsert_market_tags` now returns actual
inserted rows, not attempted rows.

## Runbook

Scratch smoke, allowed while Phase B captures are running:

```bash
python3 scripts/backfill_market_tags.py --db-path /tmp/market_tags_smoke.db --max-pages 1
```

Production population is gated. Do not run this while the Phase B fixture
recapture sweep is active:

```bash
python3 scripts/backfill_market_tags.py --db-path sniper_tracker.db --max-pages 200
```

Real-wallet verification after population:

```bash
python3 scripts/run_wallet_protocol.py 0x8493374739b45eb032382857feeb7ed025215366 \
  --db sniper_tracker.db --json --no-persist
```

Expected proof points in the JSON:

- `data_completeness.tag_lookup_state == "ok"`
- `outcome_side_breakdown.category_clusters` is present and non-empty

## Acceptance Matrix

Implemented regression coverage:

- market with no tags from Gamma: no row, no crash;
- API failure after a partial page: committed rows remain, rerun resumes;
- duplicate condition IDs: one row per `(condition_id, tag)`;
- leaf tag with no nav category: `nav_lookup` degrades, no crash;
- rerun on populated table: stable row count;
- `fetch_tag_lookups_from_db`: empty, full, partial, and missing nav table;
- malformed/empty API page: logged warning, no crash;
- deterministic source pages: deterministic ordered table state;
- schema/index shape for `market_tags` and `tag_nav_category`.

## Scope Guards

This slice does not alter wallet-protocol verdict, gate, primary edge, or
copyability logic. It does not write production tables other than
`market_tags`, and production population must wait until the Phase B fixture
recapture sweep is complete.

## Remaining Limits

Tag coverage is only as complete as Gamma `/events` coverage. Markets missing
event-level tags remain unclassified. Nav-category coverage is bounded by the
manual seed file and should be reviewed when new high-frequency tags appear.
