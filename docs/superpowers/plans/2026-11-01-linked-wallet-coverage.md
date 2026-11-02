# Linked-wallet coverage implementation plan

> **For agentic workers:** use test-first implementation in owned worktrees; independent review before integration.

**Goal:** Remove artificial research ceilings and close observed wallet-movement and group-catalogue gaps.

**Architecture:** Existing capture feeds the existing forensic graph. Observation aliases and dual payload readers improve coverage; paged retrospective leads stay separate from live quote evidence.

**Tech Stack:** Python, SQLite, existing Yellowstone stream, standard-library compression.

**Spec:** `docs/superpowers/specs/2026-11-01-linked-wallet-coverage-design.md`

## Global constraints

No money/env/funded/provider-wall changes or Office restart. No enabling billing overages. Preserve proof, removals, timestamps and unknowns. Nice19, pytest -n2, path-owned crew commits.

## Review focus

Unknown account ownership cannot become a selector or signer claim; stale aliases cannot resurrect removals. New compressed writes require all readers first. Budget fields must serialize as null when uncapped. Retrospective facts cannot create historical early warnings. Pagination must advance without dropping candidates or upgrading imported evidence.

## Tasks

1. Codex: RED tests for uncapped graph/watch defaults and alias identity/removal. Implement in wallet_network.py, wallet_watch.py and focused tests; keep explicitly configured test limits valid.
2. Delta: RED/GREEN immediate SOL follow, token-metadata owner scoping, uncapped expansion/selector handling, compressed dual reader/writer and retention. Own collector/capture/codec and their tests. Preserve cooperative processing budgets and observe latency.
3. Epsilon: RED/GREEN retrospective finalized creation binding and new paged group catalogue/CLI/tests. Own wallet_campaign.py and new catalogue files; no live quote eligibility for unknown historical receipt time.
4. Zeta: adversarial exact-source review; Codex combines focused tests and publishes immutable research releases. Upgrade consumers before compressed producer; verify watch/alias exports, actual selectors, cursor/usage/gaps and unchanged funded runtime. Record residual limits and exact runtime evidence.
