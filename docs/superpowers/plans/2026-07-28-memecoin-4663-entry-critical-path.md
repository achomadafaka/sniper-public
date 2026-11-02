# 4663 entry critical-path remediation plan

1. Add failing settings tests for a validated chain-scoped approval-pipeline allowlist.
2. Add failing executor tests proving 4663 pipelines while 8453 remains receipt-first.
3. Add failing integration coverage proving approval provider attempts are recorded against the
   entry reservation.
4. Implement the smallest settings and executor changes.
5. Expose the effective per-chain policy in the EVM runtime context and status payload.
6. Run focused EVM execution, submission, deploy, and schema tests; then the memecoin regression
   suite required by the touched surfaces.
7. Run the Knowledge Sync Gate, push the branch, open a PR, verify CI, merge to main, deploy the
   exact merged SHA, and verify the running release without clearing the funded kill switch.
