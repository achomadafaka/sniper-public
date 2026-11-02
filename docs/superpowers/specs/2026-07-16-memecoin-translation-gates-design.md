# Memecoin paper→real translation gates

**Date:** 2026-07-16 · **Status:** BUILT (TDD, `tests/test_memecoin_translation_gates.py`) — operator-approved same day.

## Evidence (14d matched arms, read 2026-07-16)

Real lost on the SAME coins paper control won. Control Solana closed 14d split by whether real
also entered the mint: real-entered = **+$2,349/121**, real-skipped = **−$918/87** — the armed
allowlist SELECTS winners; it TIMES them badly. Mechanics, same-mint:

- Every fresh-coin real Solana entry was 25–262 min after the coin's first feed event at
  **+101%..+986% above first-seen price** (armed trigger structurally fires on the 3rd+ buyer).
  SOLdiers ×3 −$66, SCAM −$25, febu −$24. Feed lag itself is p50 0.6–0.9s — detection is not the problem.
- The +60% full TP capped the only fat tail: TrumpCoin **+$27 real vs +$543 control ride**.
- Sequential re-entries bled (07-09 study already: no re-entry edge).
- Delayed-catch (follow_activity) signals are 120–900s old by construction; their real leg is off
  (`MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED=0`) but Telegram still spammed them.

Explicitly NOT done: widening entry breadth (skipped coins are net losers) and sourcing signals
outside the fomo feed (unvalidated population; we already refuse ~967 in-band signals/week).

## The four gates

1. **First-seen chase guard** — `first_seen_chase_ok` + `m.first_seen_buy_price` (earliest
   positive buy `their_price` for exact `(network_id, mint)` in `memecoin_events`). Refuse real
   entry when executable (or event) price > `MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO` (default 2.0×)
   above first sighting. Status reuses `skipped_chase`. Fail-OPEN on missing data (honeypot/mcap
   lesson). The existing `chase_ok` (vs the copied event's own price) is unchanged and still runs first.
2. **Re-entry cooldown** — `m.recently_closed_real_coin_ids`; both risk gates
   (`memecoin_risk`/`memecoin_evm_risk`) reject when the coin's real position closed within
   `MEMECOIN_REAL_REENTRY_COOLDOWN_S` (default 86400; 0=off). One real campaign per coin per day.
3. **TP-vs-derisk-ride A/B** — `assign_tp_ab_arm` at fill: ODD position ids get override
   `source='ride', tp_pct=0, derisk_pct=MEMECOIN_REAL_TP_PCT` (new `derisk_pct` column on
   `memecoin_position_exit_overrides`, idempotent ALTER). At +60% the ride arm sells back the
   entry notional (existing `_run_derisk`, executable-confirmed) and rides the remainder under
   stop/mirror/timecap. EVEN ids keep today's full `real_tp` — the control arm.
   `MEMECOIN_REAL_TP_AB_ENABLED` default on. Operator overrides outrank (`operator` > `ride`).
   **Pre-registered read gate: ≥30 closes per arm; total PnL + drop-top-1; no peeking.**
4. **Alert hygiene** — front-run alert suppressed entirely when known lag >
   `MEMECOIN_TG_MAX_LAG_S` (default 60s; unknown/negative lag still sends, labeled);
   delayed-catch alert now opt-in via `MEMECOIN_TG_DELAYED_CATCH_ENABLED` (default off).

## Settings (all env-overridable)

| var | default | meaning |
|---|---|---|
| MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO | 2.0 | max price ratio vs first-seen; 0=off |
| MEMECOIN_REAL_REENTRY_COOLDOWN_S | 86400 | real re-entry cooldown per coin; 0=off |
| MEMECOIN_REAL_TP_AB_ENABLED | 1 | odd-id ride arm on/off |
| MEMECOIN_TG_MAX_LAG_S | 60 | suppress front-run alerts older than this |
| MEMECOIN_TG_DELAYED_CATCH_ENABLED | 0 | follow-activity delayed-catch alert |

Live band settings verified same day (runner reads `.env`): `MEMECOIN_REAL_MAX_MCAP_USD=1000000`
(tightened to the sub-$1M green band), mcap-tiered impact caps default, TP 60 / stop 60
(4663 stop 0) / hold 7d, `MEMECOIN_AUTO_TEMPLATES=off`.

<!-- schema:external=memecoin_position_exit_overrides -->
<!-- schema:external=memecoin_events -->
<!-- schema:external=memecoin_positions -->

## Verification

`tests/test_memecoin_translation_gates.py` (16, TDD red→green), full memecoin suite 1,656 green,
`validate_schema_refs.py` clean after `brain_index.py generate`. Post-deploy: expect
`skipped_chase … first-seen` and `re-entry cooldown` refusals in the decisions surface, `ride`
override rows on odd real fills, and zero DELAYED-CATCH sends.
