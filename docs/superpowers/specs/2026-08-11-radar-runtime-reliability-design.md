# Radar Runtime Reliability Design

**Date:** 2026-08-11

**Status:** Approved by the operator

**Scope:** Radar paper research and its operator status only

## Goal

Radar must process each active source without a shared-lane stall.

The API must report the current capture state without a false `live` label.

The Coin Radar worker must start from a content-addressed release.

This change does not change a rule, threshold, paper result, V6 policy, or funded authority.

## Current faults

The opportunity worker used one CPU core while the host had free CPU and memory.

The shared 4663 scan fell behind the chain head. Pons evidence therefore became stale.

The Bags decoder kept an old `UNKNOWN_PROGRAM_OR_CONTRACT_VERSION` gap open.

The Pump source received new events, but transport failures kept its source state degraded.

The alert watchdog counted alerts. It did not test direct quote-path progress.

The API returned `status=live` when every active capture source failed its health limit.

The Coin Radar watchdog started code from the mutable main checkout.

## Source isolation

Pump, Bags, and Pons keep separate runtime states and separate failure facts.

A Bags decode fault must not stop the Pons scan frontier.

A Pons backlog must not stop Pump event work.

The worker must yield after each bounded persistence batch.

The 4663 scanner must keep a bounded block range, request deadline, queue, and pool set.

The scanner must retry the same block range after a transport failure.

The scanner must not open a data gap for a request that wrote no partial range.

An unknown Bags version must create a typed Bags gap and preserve the raw event identity.

Radar must ignore a Bags event topic from a foreign contract before decode.

Radar must keep an unknown event from the known Bags factory or a tracked curve as a gap.

The Pons frontier must continue when the same log does not affect the Pons decoder.

## Pump recovery

The Pump source must use the configured source endpoint and the common certificate policy.

A source reconnect must use a bounded delay and must keep the last failure detail.

A new valid event must clear a recoverable transport failure.

An open data gap must clear only after the source proves the missing cursor range.

## Direct quote-path health

The worker must publish a direct quote-path status row.

The status must include the last request time, last success time, last failure, and queue depth.

The status must identify each venue. One venue success must not hide another venue failure.

The alert watchdog must read this status. It must not infer quote health from alert counts.

The watchdog must page after a configured stale interval and one confirmed failed path.

The watchdog must clear its latch after a new successful quote-path result.

## Runtime metrics

The worker must publish bounded loop and lane duration metrics.

Each metric must include the last duration, recent maximum, run count, and over-limit count.

The metrics must cover the main loop, Pump lane, 4663 lane, quote lane, and paper lifecycle.

The metrics must not add an unbounded table or a per-event status write.

The worker must use a bounded idle delay when no queue has work.

## Exact atomic quantities

An exact token quantity can exceed the signed 64-bit SQLite `INTEGER` range.

Radar must store each paper quantity as decimal `TEXT`.

The request, fill, exit observation, and outcome tables use this rule.

The schema migration must preserve each immutable row and its fingerprint.

The writer must bind decimal text before SQLite receives the quantity.

The worker must not stop because a token quantity exceeds 64 bits.

## API status

The API uses three active states: `healthy`, `degraded`, and `stale`.

`healthy` means all required capture and quote paths pass their configured limits.

`degraded` means at least one path fails, but at least one required capture path stays current.

`stale` means all required capture paths fail or the worker status exceeds its time limit.

`disabled` and `unavailable` keep their current meanings.

The API must list each failed source and its reason.

The API must not use `live` as a health verdict.

## Content-addressed Coin Radar release

The Coin Radar watchdog must derive code from its own release path.

It must use `SNIPER_STATE_ROOT` for the shared database, logs, and `.env` file.

Cron must call the watchdog through the active content-addressed release.

The watchdog must match the exact worker path from that release.

The deployment must start one Coin Radar process from the merged release.

## Cutover proof

The deployment must use one merged commit for the opportunity and web releases.

The cutover must show one opportunity worker and one Coin Radar worker.

The API and health endpoint must return HTTP 200.

Pump, Bags, and Pons must have no open data gap after recovery.

The 4663 scan must stay within its configured head-lag limit.

The quote-path status must show direct progress for each active scoreable venue.

The queues must stay below their configured limits and report zero drops.

The opportunity worker must stay below one full CPU core during the 30-minute proof period.

The logs must show no repeated traceback, restart loop, or hidden source failure.

## Safety

Radar remains paper research.

The change does not import a signer or funded execution module.

The change does not write the funded database.

The funded runner does not restart during this cutover.

The change does not alter the V6 process or candidate source.

## Knowledge sync

Knowledge delta: Radar receives reliable runtime paths and unbounded exact paper quantities.
