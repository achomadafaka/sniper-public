# App review correction plan

The user authorized correction and deployment of the verified review findings.
The implementation uses existing functions and tests. It adds no dependency or trade threshold.

1. Write failing checks for USDC size persistence, unmarked-position alerts, empty follows, and skipped feed polls.
2. Retain USDC purchase size and source price through the existing detection table and funded event adapter.
3. Correct the alert age callback and preserve valid empty follow lists.
4. Skip feature provider work when the tape has no recent swaps. Keep source isolation intact.
5. Correct the health labels and stale operational documentation.
6. Run focused tests, funded-path tests, schema checks, and browser checks.
7. Commit the corrections and merge them into main without unrelated local files.
8. Use the existing funded and web deploy procedures, with their backup and reconciliation checks.
9. Restart the changed watcher through its supervisor. Verify release identity, heartbeats, endpoints, and new detection data.

The inactive 4663 observation tape stays inactive until a separate source exists.
The feature job resumes automatically when recent supported swaps appear.
Existing position policy, wallet authority, and trade thresholds remain unchanged.
