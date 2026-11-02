# Dossier test fixtures

Committed fixtures used by `tests/test_wallet_dossier_v0_micro.py` for
real-shape regression on the wallet dossier builder. Unlike the larger
captures under `tests/calibration/fixtures-v4-staging/` (untracked, may
be absent), files here ship in the repo so CI exercises real Polymarket
field shapes on every run.

## Canonical fixture

`0x8493374739b45eb032382857feeb7ed025215366_2026-05-25.json`

| Property | Value |
|---|---|
| Closed positions | 275 |
| Distinct conditions | 274 |
| Redemptions | 249 (100% paired, indexSets=`["1","2"]`) |
| Activity rows | 1065 (100% TRADE) |
| File size | ~590 KB |

The wallet is one of the smallest in the v4 staging set while still
covering paired-redemption shape — the same data shape SwissMiss carries
at 4× the size.

## Field allowlist

The trimmed JSON keeps only fields the dossier reads. Adding a new
field consumer? Add the field name to `scripts/trim_dossier_fixture.py`
and re-run the trimmer.

| Top-level | Per-row |
|---|---|
| `closed_positions` | `conditionId`, `asset`, `oppositeAsset`, `outcome`, `title`, `avgPrice`, `totalBought`, `realizedPnl`, `cashPnl`, `outcomeIndex`, `curPrice`, `endDate`, `timestamp` |
| `open_positions` | (closed-position fields) + `size` |
| `activity_rows` | `type`, `side`, `asset`, `conditionId`, `price`, `size`, `timestamp`, `outcomeIndex` |
| `redemptions` | `condition`, `indexSets`, `payout`, `timestamp` |

## Regenerating

```bash
python3 scripts/trim_dossier_fixture.py \
    --in  tests/calibration/fixtures-v4-staging/<wallet>_<date>.json \
    --out tests/calibration/dossier-fixtures/<wallet>_<date>.json
```

Tests are pinned to file names. If you bump the date or wallet, update
the constants at the top of `tests/test_wallet_dossier_v0_micro.py`.
