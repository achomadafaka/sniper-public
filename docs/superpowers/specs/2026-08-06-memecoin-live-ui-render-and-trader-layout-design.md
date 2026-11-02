# Memecoin Live UI Render and Trader Layout Design

**Date:** 2026-08-06
**Status:** Approved by the operator
**Scope:** The Memecoin Armed, Paper results, and Traders surfaces

## Goal

The Armed and Paper results surfaces must render without a JavaScript error.
The Traders surface must keep its chain-arm action visible without horizontal scroll.

## Current defects

The Armed renderer maps each API row as `r` but reads `w.funded_arm`.
This identifier error stops the complete Armed table.

The Paper results renderer uses `censored` without a local definition.
The value is the count of closed rows without `full_book_pnl`.
Open arms remain separate and do not count as censored.

The Traders table puts one column for each arm before the action column.
The table width is 1,564 pixels in a 1,247-pixel wrapper.
The action button starts outside the visible area.

## Selected layout

The operator needs current evidence and the chain action in one scan.
The primary row has these columns:

```text
Trader and Details | Signals | Best arm | Total | Open | Action
```

`Best arm` is a survivor diagnostic.
It does not state or select the funded exit policy.
The current explanatory text keeps this boundary visible.

The Details control expands a second row for the selected trader.
The second row shows every configured arm in a wrap grid.
Each arm item shows the arm name, PnL, close count, and sample state.
An unavailable momentum arm shows `n/a` with the current reason.

```text
+----------------+---------+-----------+--------+------+----------+
| Trader  Detail | Signals | Best arm  | Total  | Open | Action   |
+----------------+---------+-----------+--------+------+----------+
| arm rail: ctrl  +$X/4 | shadow -$Y/4 | scalp2 +$Z/4 | ...       |
+------------------------------------------------------------------+
```

The expanded state uses semantic `details` and `summary` elements.
This choice keeps keyboard control and native state without new JavaScript state.
The chain action stays outside the summary control.
Thus, the action does not change the detail state.
A background poll pauses while a detail element is open.

## Responsive behavior

The main table must fit its wrapper at a 1,365-pixel viewport.
The action button must be inside the wrapper bounds.

At narrow widths, the row keeps Trader, Total, Open, and Action visible.
Signals and Best arm move into the Trader cell as secondary text.
The arm grid wraps to one or two columns as space permits.

The table can still scroll for abnormal text or browser zoom.
The normal desktop and mobile layouts must not require horizontal scroll.

## Visual system

The change keeps the current Memecoin visual system.
The palette uses `#0b1220`, `#151d30`, `#25324e`, `#5b9dff`, `#37d382`, and `#ff5d5d`.
Inter remains the interface typeface.
JetBrains Mono remains the numeric typeface.

The arm rail is the signature element.
It presents the arm comparison as a compact evidence instrument.
It does not add decoration, motion, or a new color language.

## Data and authority invariants

- The API payloads do not change.
- The database does not change.
- The funded selector does not change.
- The chain-arm action does not change.
- Research chains do not gain a funded action.
- Missing open exposure remains unknown.
- Fomo-priced fills remain excluded.
- The survivor diagnostic remains separate from funded authority.

## Error behavior

The Armed renderer reads the active row only.
The Paper results renderer computes `censored` before it builds the row markup.
The count equals `grp.length - known.length`.
The count excludes `stillOpen`.

## Tests

The tests execute the actual Memecoin methods in a browser context.
Controlled API payloads cover the Armed, Paper results, and Traders surfaces.

The tests prove these results:

1. The Armed renderer completes without a `ReferenceError`.
2. The Armed table includes the funded-arm explanation.
3. The Paper results renderer completes without a `ReferenceError`.
4. The Paper results row reports the correct censored count.
5. The Traders action button stays inside the table wrapper.
6. The Details control shows all configured arm values.
7. The narrow layout keeps the action visible.

## Deployment

The change will merge into `main` without a pull request.
The web runtime will deploy from the exact merged SHA.
The final checks will verify the release link, process path, authenticated page, console, and three affected surfaces.

## Knowledge Sync

Knowledge delta: none.
Reason: this change repairs UI errors and changes only the display layout.
It does not change a research claim, authority rule, API, schema, or runtime policy.
