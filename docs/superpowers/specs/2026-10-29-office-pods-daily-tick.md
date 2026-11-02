# Office pods: daily reallocation tick (Task 15 hook)

Owner Lambda. Reviewers Delta, Zeta. Builds on `sniper/office/pod_floor.py` (`reallocate`) and `pods.py` (frozen rule).

## What it is

`sniper/office/pod_tick.py: tick(conn, rule, *, epoch_ids, clock, fx)`. The Office floor's housekeeping thread calls it every cycle. It returns
`{epoch_id: ['booked'|'replayed'|'none'|'error:<reason>']}`, or `{'skipped': 'clock_unknown'}`. The floor integration (Task 15, Codex) supplies:
- `epoch_ids`: the unbroken EVALUATED ledger epochs, from the roster. This module never infers it.
- `clock`: the corrected clock (`clock.corrected_now`). `fx`: returns `(sol_usd, sol_at)` from the shared SOL/USD observation.

## Edge cases

1. **Money path.** Paper ledger only (`office/ledger.db`). It moves paper capital between pod books. Not funded, no signing.
2. **Fill path speed.** Not on it. It runs outside `run_turn` and `submit`. It takes the ledger write lock only inside `L.transfer`. Measured (2,100 closed orders, 3 pods, one worker): the day's booking 3.1 ms once; a replay tick p50 0.029 ms, p90 0.031 ms (200 ticks).
3. **Concurrency and global state.** No module state. Two concurrent ticks: both pass the "already booked" lookup, both plan, `L.transfer` is idempotent on `transfer_id`; the second returns `replayed`, or raises `transfer_id_conflict` if the balances moved, which is logged and returned as an error, never a second effect.
4. **Absent, zero, unknown.** Clock `None`, NaN or a bool: skip. FX unknown or older than 120 s: nothing booked and no guess. A pod with fewer than `min_closed` closes is unknown, not zero: the rule plans nothing.
5. **Dependency failure.** A ledger error for one epoch is logged (`logger.exception`) and returned as `error:<reason>`. It never raises into the floor and never stops the next epoch.
6. **Units and identity.** Epoch ids are ledger epoch ids (books are `epoch:pod`). The period is the UTC day of the passed clock.
7. **The sample.** Scores count realized closes only. Open positions (bags) score as nothing (Gamma finding 2).
8. **Rollback.** Remove the call from the floor: nothing else depends on it. Booked transfers stay as the audited record.

## Not handled

- Which epochs are evaluated and unbroken (roster, Task 15).
- Mapping roster epoch ids to ledger epoch ids.
- Alerting on repeated `error:` results: the floor reads the returned dict.
