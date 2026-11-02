# Memecoin V6 Paper Decision Cockpit Design

**Status:** approved for implementation on 2026-07-26

## Goal

Make the V6 live-paper section answer, within one screen and roughly five seconds:

1. Is the V6 experiment receiving current evidence and producing decisions?
2. Which positions are gaining or losing against their modeled entry?
3. What action did V6 choose, and why?
4. What exact post-fill fact is V6 watching next?

The current surface exposes correct raw telemetry but does not expose those answers. This change is
an operator-view redesign, not a V6 policy change.

## Scope

Replace the current V6 card dump in the Real Positions tab with a compact decision cockpit. Extend
the read-only V6 report contract only where the UI needs derived, reconstructable facts.

The change must not:

- alter `step(state, observation) -> V6Decision`, rule precedence, thresholds, capture, quote
  cadence, entry selection, or modeled fills;
- merge V6 rows into `/api/memecoin/real/positions`;
- add funded controls or funded mutation URLs;
- add V5, replay, or strategy-arm comparisons;
- call external services from a report or browser render;
- claim net PnL while fee components remain unknown.

The only V6 operator mutation remains censoring an open paper position with an audit reason.

## Chosen Layout

Use a decision-cockpit hybrid: one experiment health strip, compact position rows ordered by
urgency, and expandable evidence details. A dense table alone would compare positions well but
would still hide the decision rationale. A timeline-first view would audit one position well but
would make simultaneous positions hard to scan.

### Experiment health strip

Show these facts above the book:

- open and closed lifecycle counts;
- current feeds and gaps;
- quote availability and data-insufficient decision count;
- receive-to-observation p95 and observation-to-decision p99;
- a single state label: `HEALTHY`, `DEGRADED`, `STALE`, or `NO DATA`.

`HEALTHY` means every reported feed has persisted `current=true`, no feed has a gap, and the open
positions have healthy executable quotes. `STALE` means any feed has persisted `current=false` or
an open position's latest `quote_status` is `stale`. `DEGRADED` means current evidence exists but a
gap, unavailable quote, or insufficient rule input remains. `NO DATA` means the report has no feed
or observation evidence. The report uses these persisted states rather than inventing a second
wall-clock timeout. It must expose the component facts; the label must never replace them.

Do not color latency as a failure merely because it exceeds one second. The user requirement is
fast evidence intake, not a forced sub-second decision. Display the measured stages separately.

### Position rows

Open positions appear first. Within open positions, order `EXIT_FULL`, then `DERISK`, degraded or
data-insufficient `HOLD`, then healthy `HOLD`; break ties by largest absolute modeled price return
and then newest observation.

The collapsed row shows:

- ticker first, chain second, and a shortened mint as secondary identity;
- strong `HOLD`, `DERISK`, or `EXIT` action chip;
- modeled entry price, current executable price, modeled price return, high-water return, current
  drawdown, position age, and quote age;
- one plain-English `Why` sentence derived only from the persisted decision, rule results, and the
  latest observation;
- one `Watching next` line showing the exact status of every relevant V6 rule, not an invented
  prediction;
- source wallet or source id and `MODELED LIVE QUOTE / NO FUNDS` honesty label.

Raw atomic quantities, full mint, full-precision prices, policy identifiers, evidence identities,
and raw JSON do not appear in the collapsed row.

For an open position, `modeled_price_return_frac` is:

```text
(latest executable unit price / modeled entry unit price) - 1
```

This is a gross price return in the position's quote asset. It is not USD PnL and excludes fees.
If either price is absent or invalid, it is null. Closed rows continue to use reconstructed modeled
fills for realized gross results. Net modeled PnL remains null with
`unknown_cost_components` until all costs are captured.

### Decision explanation

The UI must not merely repeat `HOLD: no_trigger`. The report emits a deterministic presentation
object assembled from persisted evidence:

```json
{
  "action": "HOLD",
  "severity": "normal",
  "why": "Holding: no V6 exit rule is triggered; executable quote is healthy.",
  "watching": [
    {"rule_id": "staged_break", "state": "not_triggered", "label": "Break manager: no confirmed break"},
    {"rule_id": "catastrophe", "state": "not_triggered", "label": "Catastrophe: current return remains above -80%"},
    {"rule_id": "route_loss", "state": "not_triggered", "label": "Route: healthy"},
    {"rule_id": "time_expiry", "state": "not_triggered", "label": "Time cap: 11h 26m remaining"},
    {"rule_id": "lp_loss", "state": "insufficient", "label": "LP pull: depth evidence unavailable"}
  ],
  "missing_facts": ["depth_pull_flag", "depth_provenance"]
}
```

Labels are a fixed rendering of V6 rule ids, rule status, reason codes, and observation values.
They are not a second decision layer. Triggered rules name their persisted reason and action.
Insufficient rules name the missing fields. A no-trigger hold explicitly says that no rule fired.
An active or unfilled action intent is prominent and never described as an ordinary hold.

The `Watching next` content covers all five current V6 rule families:

- `staged_break`: current break episode status and confirmation state;
- `catastrophe`: current modeled price return against the exact -80% engine threshold;
- `route_loss`: quote status, consecutive route-absence checks against 4, and elapsed time against
  60 seconds when route absence is active;
- `time_expiry`: held time or remaining time against 43,200 seconds;
- `lp_loss`: depth-pull state, tape completeness, provenance, and missing inputs.

If V6 adds or removes a rule, an unmapped rule must still render by stable rule id and status. The
surface may not silently omit it.

### Expanded evidence

Expanding a row fetches `/api/memecoin/v6-paper/positions/<position_id>` and shows:

- latest market facts: break state, prior low, price, flow, buy share, arrival rate, realized
  volatility, chain p90, route status, LP/depth status, and capture provenance;
- current inventory in human units when decimals are known, with atomic values in a technical
  subsection;
- ordered decision timeline with action, fraction, thought, reason codes, rule results, applied
  outcome, and timestamp;
- ordered modeled fills and observation timestamps;
- full identity and frozen policy/evidence metadata under `Technical details`.

The timeline is collapsed by default and loaded on demand so the normal four-position view stays
fast and readable.

## Report Contract

`sniper/services/memecoin_v6_paper_report.py` remains read-only. Each list card gains:

- `latest_observed_at`;
- `modeled_price_return_frac` and `modeled_high_water_return_frac`;
- `inventory.remaining_fraction`;
- a bounded `market_facts` object containing only the latest persisted observation fields needed
  by the cockpit;
- `decision_context` with `action`, `severity`, `why`, `watching`, and `missing_facts`.

The server computes price ratios with `Decimal` and rejects non-finite or non-positive values.
Missing or malformed facts become null or explicit insufficient labels; they never become zero.
All presentation lists are bounded by the five persisted rule results.

The existing detail response remains the source for the full timeline. Existing response keys and
honesty labels remain compatible.

## Browser Behavior

- Poll the compact list and summary at the existing V6 cadence.
- Preserve an expanded row across compact rerenders.
- Fetch detail only when a row is expanded; cache it until a newer `latest_observed_at` appears.
- Escape every server-derived string before inserting it into HTML.
- Use semantic CSS classes in the existing stylesheet instead of large inline-style blocks.
- Use text and shape as well as color for action and health states.
- On narrow screens, stack metrics below identity and decision; never require horizontal scrolling
  to read `Why` or `Watching next`.
- Keep the censor action visually secondary and available only for open V6 paper positions.

## Failure and Empty States

- Positions endpoint unavailable: show `V6 paper unavailable` with the last successful render left
  visible but marked stale when available.
- Summary unavailable: render positions and label aggregate metrics unavailable.
- Detail fetch failure: keep the row and show an inline detail error; do not collapse the book.
- No positions: keep the experiment health strip and show `Waiting for an accepted signal and
  modeled live fill.`
- Missing quote: show `Executable quote unavailable`; do not display a zero return.
- Missing engine fact: show the exact missing fact from `missing_fields`.

## Testing

Use RED-first focused tests only.

- Report unit tests cover price-return math, invalid/missing values, human inventory fractions,
  deterministic decision explanations, all five rule families, unknown rule fallback, active
  intents, and data-insufficient labels.
- API tests prove the new fields are exposed only through V6 endpoints and retain all funded
  boundary and honesty guarantees.
- UI source-contract tests prove ticker-first identity, action/health labels, `Why`, `Watching
  next`, lazy detail loading, escaped server data, responsive semantic classes, and censor-only
  mutation.
- Browser verification uses the real local app and current V6 paper book to confirm the four
  simultaneous positions are readable at desktop and narrow widths. Browser inspection is not a
  substitute for the focused automated tests.

No full suite, live API call, production DB write, funded runner restart, deploy, push, or V6 policy
change is part of this work.

## Acceptance Criteria

- The default V6 viewport explains every open position without expanding raw data.
- A healthy hold never renders only `HOLD: no_trigger`.
- Entry versus executable performance is visible as a rounded percentage, with missing values
  honest and non-zero-by-default.
- Every current V6 rule is visible as triggered, not triggered, or insufficient.
- Four simultaneous positions remain scannable on one desktop screen and readable on a narrow
  viewport.
- Funded controls remain structurally absent; V6 remains paper-only and separately fetched.
