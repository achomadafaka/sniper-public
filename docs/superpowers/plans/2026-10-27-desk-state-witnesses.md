# Desk State Witness Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans task by task.

**Goal:** Bounded full-frame diagnostics for six first-blocking PS event kinds on refused frames; no effect proof, swap or pricing change.

**Architecture:** A small filesystem writer owns a new diagnostic namespace. A guarded sibling hook runs after original loss/pending/witness handling, using its exact first-blocking discriminator. It never touches trade or proof records. This private branch is for the release AFTER the current combined scope/stamp release.

**Tech Stack:** Standard library filesystem/base64/hash/JSON, existing protobuf fixtures, pytest -n 2.

**Ownership:** Delta: new sniper/services/memecoin_desk_state_witness.py, worker sibling hook/status, focused tests and this plan. Beta reviews the hook, Zeta independent review, Codex stages only after Alpha release order.

**Bounds:** Six fixed event discriminators plus3fixed event/instruction subtype keys;20slots/key/day and hard200/day total across restarts;256KiB encoded file;3hostUTCdays retained. Current9key bound135MiB; hard-total ceiling150MiB. Failed/oversized attempts consume in-process slots. Kill knob read at startup, default1 under capture (0 rollback). No RPC, model, credentials, event effects, old-loss rewriting or pricing exemption.

**Edge cases:** Missing/zero/unknown clocks and IDs; unsupported/oversized frames; restart cap; day rollover and rollback; interrupted temporary files; filename collisions; permissions/full disk; symlinks/foreign files; unexpected helper bugs. Publish complete bytes atomically from same-directory temp with no-overwrite link+unlink; partial temps never evidence. Scan/prune only on startup/day change, never normal per-frame. Every expected storage failure is counted; any unexpected exception is contained in the worker, with bounded logs. Original loss/pending/curve processing must survive.

- [x] RED resource/control tests:20cap/restart, all five kinds, max encoded bytes, raw bytes/hash/clock preserved, knob off, rollover retention/unknown files, atomic publish failure, no per-frame directory scan.
- [x] Minimal writer; no config abstraction, provider or parser.
- [x] RED worker hook failure test; new capture only after original loss path and guarded independently.
- [x] Focused GREEN including original loss/routed/witness/worker + treewalk; measure capture file-write overhead with explicit fixture limits.
- [ ] Private crew_commit exact paths; Beta/Zeta exact-SHA PASS; hand next-release source to Codex without activation.

**Evidence limit:** Existing2081 historical losses lack raw event vectors. Capturing diagnostics does not clear their loss or establish post reserves. Per-event complete-effect proofs await new source frames and Alpha's next ruling.

**Verification:** Initial missing-writer and missing-hook tests failed before implementation. Additional genuine RED checks caught foreign-name pruning, optional initialization/status escape, and short-write publication. Broad affected witness/routed/store/worker/treewalk set:72 passed. After the final short-write repair, witness+treewalk:27 passed in2.22s; diff check clean. No full suite.

**Measurement limit:** Synthetic first-blocking markers in real-size public routed frames only;40 frames per worker setting, original40 losses and zero swap rows. Sequential runs are cache/scheduling sensitive and establish no speedup: off p50/p90 .8717/.9071ms, on .5243/.5707ms. Ten195000-byte synthetic captures: .5494/1.005ms. No genuine state-event frame or production latency proof. Optional initialization, capture and heartbeat status failures are guarded; logs are bounded by exception type.


**Alpha subtype amendment:** Six event kinds (adding BuyEvent67f4521f2cf57777), plus three exact observed event/instruction pairs. A scope pair is selected only by exact final colon-delimited suffix and matching original event: Sweep82a4/a039592ab58b2b42 (unknown_instruction,a8,d8); Buy67f4/5e06ca73ff60e8b7 (unknown_instruction,a6,d8); Buy67f4/c62e1552b4d9e870 (layout_data,a26,d24). Otherwise the event uses its 'none' bucket. No prefix/raw-byte search, generic reason parser, double capture or new effect proof. Original event discriminator and reason remain unchanged; scope selection adds scope_instruction/capture_key and first_blocking_scope_instruction label. Existing event-only filenames remain readable and counted.

**Bounds superseding event-only limits:**20 per (event,instruction-or-none)/hostUTCday, across restarts; hard total200/day across all keys. Nine currently allowed keys imply180/day and135MiB over3days; the hard200/day ceiling implies150MiB if more fixed keys are later reviewed.256KiB encoded limit unchanged. Failed attempts consume process-local slots; only published/temp files persist across restarts. Foreign files excluded from this bound and preserved. Six event-only buckets alone remain90MiB.

**Evidence:** Codex desk-tape-witness-loss-identities-20261027.json binds five call1998/1999 loss rows:3Sweep82a4,2Buy67f4. Synthetic raw-byte selection checks preserve their exact original reasons/discriminators; no historical full frames reconstructed. Genuine RED:2missingBuy cases, then4subtype/total-cap cases before repairs. Common event cap cannot consume the separate subtype slots. Exact subtype selection still never promises future coverage once that subtype's20slots are exhausted.

**Successor verification:**41 witness/treewalk checks passed in2.41s -n2; diff check clean. Worker hook remains byte-identical to7be077ab0. Successor Writer selection/budget needs Beta and Zeta exact-SHA review.

**Same-pool virtual-quote row:** First-N Sweep sampling only, no capture guarantee. Its exact event/reason equals common Sweep losses; a reason-only extra bucket cannot distinguish it. Pool-membership capture keys require separate Alpha scope.
