# Crypto5m Adaptive Paper Cockpit Design

**Date:** 2026-07-05
**Status:** Approved design
**Scope:** read-only UI/API visibility for the active adaptive paper challenger and the `maker-regime-v1` control.

**2026-07-05 update:** `maker-adaptive-paper-v2` is the current default challenger. `maker-adaptive-paper-v1` remains historical and can still be inspected by overriding `CRYPTO5M_ADAPTIVE_MAKER_DB`.

## Decision

Build a compact adaptive cockpit on top of the existing crypto5m Maker desk.

The cockpit reads two separate paper DBs:

```text
control:   data/crypto5m/maker_v0.sqlite
adaptive:  data/crypto5m/maker_adaptive_v2.sqlite
```

Do not merge the DBs. The existing `maker_windows` table is keyed by `condition_id`, so two same-window live campaigns in one DB can collide. Separate DBs are the correct isolation boundary.

## Goal

Make the adaptive paper engine inspectable while it runs:

```text
What is the adaptive engine doing now?
Why did it choose that profile?
Is it outperforming or underperforming the control on the same windows?
Which contexts/profiles are being explored, rewarded, or disabled?
```

This is visibility and comparison. It is not a browser control panel for changing policy.

## Non-Goals

- No real orders.
- No private keys.
- No production DB.
- No DB merge.
- No browser-side policy mutation.
- No live service restart from the API/UI.
- No retroactive changes to adaptive choices.
- No claim that adaptive is profitable before paired-window evidence exists.

## API

Add:

```text
GET /api/crypto5m/maker-adaptive
```

Read-only inputs:

```text
CRYPTO5M_MAKER_DB or data/crypto5m/maker_v0.sqlite
CRYPTO5M_ADAPTIVE_MAKER_DB or data/crypto5m/maker_adaptive_v2.sqlite
```

The endpoint returns:

```text
available
control
adaptive
paired_windows
current
profile_stats
recent_fills
disabled_profiles
warnings
```

### Control Block

Fields:

```text
db_path
available
campaign
engine_age_s
n_settled
realized_pnl
n_open
n_pending
latest_window_start_ts
latest_condition_id
```

Use the same ledger-derived realized PnL logic as `/api/crypto5m/maker` where practical.

### Adaptive Block

Fields:

```text
db_path
available
campaign
engine_age_s
selector_version
reward_version
n_settled
realized_pnl
n_open
n_pending
n_profile_stats
n_disabled_profiles
latest_window_start_ts
latest_condition_id
```

### Current Block

Fields:

```text
condition_id
window_start_ts
seconds_left
fair_value_up
regime
dwell_seconds
moment_key
profiles_by_token
```

Each token profile includes:

```text
profile_id
selection_source
exploration
context_key
parent_context_used
n_fills
mean_reward
```

### Paired Windows

Pair by `window_start_ts`, not by DB row id.

Fields:

```text
window_start_ts
control_realized_pnl
adaptive_realized_pnl
delta_pnl
control_winning_outcome
adaptive_winning_outcome
status
```

Only settled windows with realized PnL count in `paired_summary`.

Summary:

```text
n_paired
adaptive_minus_control
adaptive_wins
control_wins
mean_delta
median_delta
```

Do not compare open marks as if they are settled results. Open mark is shown separately.

### Profile Stats

Read `crypto5m_maker_profile_stats`.

Expose:

```text
context_key
profile_id
n_decisions
n_quotes
n_fills
fill_rate
mean_reward
pnl_sum
reward_sum
hard_cuts
disabled
last_updated_ts
```

Sort by:

```text
disabled desc,
n_fills desc,
n_decisions desc
```

### Recent Fills

Read adaptive fills only.

Fields:

```text
id
ts
condition_id
window_start_ts
token
side
price
size
fair
fill_model
profile_id
selection_mode
adaptive_context_key
reason
```

If a reward row exists for the fill, join:

```text
reward
fee_adjusted_pnl
hard_cut_penalty
reward_version
```

Unsettled fills show reward as `null`.

## UI

Keep the existing Maker desk as the first screen.

Add one compact section under the accounting ladder:

```text
Adaptive Challenger
```

The section shows:

- control vs adaptive settled PnL;
- paired-window delta;
- current adaptive profile per token;
- selection source: `safety_override`, `fallback`, `exploration`, or `learned`;
- current context key;
- top profile stats;
- disabled profiles;
- recent adaptive fills.

Use honest labels:

```text
paper challenger
settled paired delta
exploring
learned from settled fills
insufficient sample
```

Avoid promotional labels:

```text
AI edge
profitable regime
production best
```

## Watchdog

Add an optional watchdog stanza for the adaptive paper process:

```text
data/crypto5m/maker_adaptive_v1.sqlite
data/crypto5m/maker_adaptive_v1.pid
campaign maker-adaptive-paper-v1
```

The adaptive watchdog is separate from the existing campaign guard.

Reason:

- the control campaign guard evaluates `maker-regime-v1`;
- the adaptive challenger has different learning behavior and must not be paused by the control verdict;
- adaptive kill/disable policy belongs to Learner V2.

The watchdog may restart the adaptive paper process if dead. It must not touch `maker_v0.pid`.

## Error Handling

If one DB is missing:

- return the other side normally;
- set missing side `available=false`;
- include a warning.

If both DBs exist but campaigns differ from expected:

- show actual campaign names;
- do not pool rows.

If `maker_state` is stale:

- keep campaign aggregate stats visible;
- mark engine status as stale;
- do not show stale quote state as live.

## Tests

Add API tests for:

- missing adaptive DB does not break control response;
- missing control DB does not break adaptive response;
- separate DBs are read and not merged;
- paired windows are matched by `window_start_ts`;
- open windows are excluded from paired settled summary;
- adaptive current profile fields surface from `maker_state`;
- profile stats include fill rate and mean reward;
- rewarded and unrewarded fills are both returned;
- disabled profiles are surfaced;
- endpoint does not create missing DB files.

Add UI tests for:

- app fetches `/api/crypto5m/maker-adaptive`;
- adaptive challenger section renders;
- selection source is visible;
- paired delta is visible;
- missing adaptive DB shows a compact warning instead of blanking the desk.

## Launch Check

After implementation:

```text
control process alive
adaptive process alive
control DB has no adaptive campaign rows
adaptive DB has adaptive campaign rows
/api/crypto5m/maker still works
/api/crypto5m/maker-adaptive returns both sides
```

## Open Questions

None blocking.

Optional later choice:

- whether the existing Maker desk should default to control-only, adaptive-only, or side-by-side after enough paired windows exist.
