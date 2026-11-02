# Proven-Bad Memecoin Wallet Cleanup Design

**Date:** 2026-07-25
**Status:** approved

## Goal

Provide one dry-run-first operator utility that excludes watch-only wallets whose canonical honest
`alpha_b` paper evidence has at least the configured promotion sample and cumulative PnL at or below
the symmetric negative promotion floor.

## Safety contract

- Use `db_memecoin.alpha_b_copy_stats_all()` as the metric source; do not duplicate its SQL.
- Never target `real_approved=1`, already excluded, or already paused wallets.
- Open only an existing database. A bad path must never create an empty SQLite file.
- Dry-run is read-only.
- Apply requires `--apply --expect-count N --expect-digest SHA256`; count or exact target-identity
  mismatch aborts before backup or mutation.
- Create a verified SQLite backup and a target manifest before mutation.
- Recompute the exact target identity set under `BEGIN IMMEDIATE`; any drift aborts.
- Update all selected wallets in one transaction. Any failure rolls back every wallet.
- Exclusion sets both `excluded=1` and `paused=1`, matching `set_wallet_excluded()` semantics.
- Do not call live APIs, restart services, change flags, or deploy.

## Interface

```text
python3 scripts/cleanup_proven_bad_memecoin_wallets.py [--db PATH]
python3 scripts/cleanup_proven_bad_memecoin_wallets.py --db PATH --apply --expect-count N --expect-digest SHA256
```

The default database is `state_path("sniper_tracker.db")`, respecting `SNIPER_STATE_ROOT`.

## Audit artifacts

Apply writes beside the database:

```text
<db>.bak-provenbad-<timestamp>.db
<db>.bak-provenbad-<timestamp>.json
```

The JSON records the criterion, exact wallet identities, stats, backup path, and final status. A
planned manifest is written before mutation and atomically replaced with `status="applied"` after
commit.
