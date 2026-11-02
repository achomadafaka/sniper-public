# Followup R1 — Fix FH6 NO-side flip in `flipper.py`

**Status:** fixed (2026-05-06)
**Severity:** critical
**Source:** wallet-protocol semantic-correctness audit (2026-05-06), §R1
**Owner:** unassigned
**Hard prerequisite for:** Timing Surface v1.5, any code that injects
`price_history_fn` into the flipper branch.

---

## 1. The bug

`sniper/analysis/wallet_protocol/flipper.py:417-419`:

```python
outcome_label = (f.outcome or "").lower()
if outcome_label in ("no", "short"):
    market_dir = -market_dir
```

`market_dir` is computed at lines 412 from `price_history_fn(asset_id, sell_ts)
− price_history_fn(asset_id, buy_ts)`. `f.asset_id` is the held token's
CLOB id (per `flip_reconstruction.py:104`); `price_history_fn` returns
prices for that specific asset id (held-token coordinate, not YES
probability). The asymmetric `-1` flip on NO/Short is therefore
inverting an already-aligned coordinate.

## 2. Why it's currently dormant

FH6 is `underpowered` in every default run because no production caller
injects a `price_history_fn`. The defect is dormant until one of:

- a strict-repricing adapter is wired into `analyze_wallet`,
- a v1.5 / H6 surface starts calling `price_history_fn`,
- a test or batch harness injects a real adapter.

The moment any of those happen, FH6 will silently misclassify NO flips:
a winning NO flip whose market also moved in the wallet's favor (NO
appreciated) would be routed to `against_market` instead of
`with_market`. The aggregated `wr_with_market` vs `wr_against_market`
statistics would be inverted for the NO-flip slice.

## 3. Required fix

Remove the side-adjustment and add a regression test. The fix is a
single-line removal:

```python
# DELETE these three lines (flipper.py:417-419):
outcome_label = (f.outcome or "").lower()
if outcome_label in ("no", "short"):
    market_dir = -market_dir
```

Justification (mirroring §6.1 of Timing Surface v1):

- `flip_roi = (sell_price − buy_price) / buy_price` already works
  correctly for any outcome without side-adjustment because both prices
  are in held-token coordinate.
- `market_dir = price_history_fn(asset_id, sell_ts) − price_history_fn(
  asset_id, buy_ts)` is the **same coordinate** as flip_roi (same
  asset_id, same source). The wallet wins iff held-token appreciated;
  market_dir > 0 iff held-token appreciated. They align natively.
- The FH6 categorization "with_market" iff `flip_won` and `market_dir > 0`
  is correct under raw owned-token coordinate. No flip needed.

## 4. Required regression test

Drop in `tests/test_wallet_protocol_flipper.py` (or wherever FH6 tests
live):

```python
def test_fh6_does_not_invert_no_side_market_direction():
    """Lock the audit-R1 fix.

    A winning NO flip whose held-token (NO) market appreciated must be
    counted as `with_market`, not `against_market`. Pre-fix code applied
    `market_dir = -market_dir` on outcome=No, which inverted the routing.
    """
    # Wallet bought NO at 0.40, sold at 0.65 (NO token appreciated by 0.25).
    flip = Flip(
        condition_id="cid_a",
        outcome="No",
        buy_ts=1000,
        sell_ts=2000,
        buy_price=0.40,
        sell_price=0.65,
        size=100.0,
        buy_notional=40.0,
        flip_roi=0.625,                # (0.65-0.40)/0.40
        hold_duration_seconds=1000,
        asset_id="tok_no",
    )
    # price_history_fn returns NO-token-coordinate prices because we
    # passed asset_id="tok_no" (NO token id). Wallet's market direction
    # was therefore +0.25 in held-token space — favorable.
    def price_history_fn(asset_id: str, ts: int) -> float | None:
        if asset_id != "tok_no":
            return None
        # Mid-flip prices: 0.42 at buy_ts, 0.62 at sell_ts.
        return 0.42 if ts == 1000 else (0.62 if ts == 2000 else None)

    result = run_fh6_directional_skill(
        [flip],
        price_history_fn=price_history_fn,
        n_iter=200,
        seed=42,
    )
    # Pre-fix bug: would have routed the flip to `against_market`.
    # Post-fix: must route to `with_market`.
    assert result.details["wr_with_market"] == 1.0
    # Both buckets non-empty assertion may fail with only one flip; in
    # that case use a 4-flip fixture (2 with_market, 2 against_market)
    # with the same shape on each side to keep stratification populated.
```

Optional secondary test: assert the same fixture under the pre-fix code
path would have produced `wr_against_market = 1.0` and `wr_with_market =
0.0` (i.e. the exact inversion). This documents the bug for future
readers.

## 5. Acceptance criteria

- The three lines at `flipper.py:417-419` are removed.
- The regression test above passes.
- Full repo suite green.
- Calibration suite green (FH6 will continue reporting `underpowered`
  when no `price_history_fn` is injected, so no calibration drift).
- Audit cross-reference: this ticket is closed in the
  `2026-05-06-wallet-protocol-semantic-audit.md` followup tracker.

## 6. Out of scope

- Wiring an actual `price_history_fn` into production. That belongs to
  Timing Surface v1.5 / H6.
- Any other FH6 logic changes (e.g. coverage thresholds, stratification
  rules). This ticket is strictly the side-flip removal.
- Touching H6 (held-analyst version). H6 already does NOT apply this
  flip and is correct under raw-token coordinate.

## 7. Resolution

Fixed in commit `20a1888` on `2026-05-06`.

Changes:
- `sniper/analysis/wallet_protocol/flipper.py`: removed three-line
  NO/short side-adjustment (lines 417-419 pre-fix); replaced misleading
  comment with ADR-003 coordinate note.
- `tests/test_wallet_protocol_flipper.py`: added
  `test_fh6_does_not_invert_no_side_market_direction` — 20-flip fixture
  (10 rising NO wins + 10 falling NO losses), verified pre-fix produced
  inverted buckets (`wr_with_market=0.0, wr_against_market=1.0`), post-fix
  produces correct routing (`wr_with_market=1.0, wr_against_market=0.0`).
- All 25 flipper tests pass. No regressions in existing tests.
