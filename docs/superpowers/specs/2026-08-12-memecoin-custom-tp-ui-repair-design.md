# Memecoin Custom TP UI Repair Design

Date: 2026-08-12

## Problem

The funded position card view has editable TP and SL controls. The dense table view does not have these controls.

The table rewrite at commit `6fd6dd37` removed the prior TP and SL columns. The shared click handler and modal remain active.

No custom TP request reached the server for the current open position. The API and funded exit sweep remain operational.

The editor has a second error for an mcap TP. The server stores `tp_pct=0` with `tp_target_mcap`.
The editor reads `tp_pct` first and shows `none`. A later SL edit can then remove the mcap TP.

## Scope

This change repairs the web operator surface only.

The change does not modify funded policy, exit nomination, quote confirmation, signing, broadcast, or database schema.

## Design

The dense table will show two columns: `TP` and `SL`.

Each cell will use the existing shared display and edit functions:

- TP uses `_tpDisplay(p)` and opens `_openExitEditor(pid, "tp")`.
- SL uses `_exitLegTxt(...)` and opens `_openExitEditor(pid, "sl")`.
- The SL cell shows the existing reset control when an effective override exists.

The table header and chart-row column span will include the two restored columns.

`_exitLegRaw` will check the mcap field before the percent field. This preserves an mcap TP when the server also stores `tp_pct=0`.

## Failure Behavior

The existing API remains fail closed for invalid values. The UI will use the existing toast for request errors.

The server remains the authority for the stored override. The next real-position refresh will show the stored value.

## Tests

Focused UI contract tests will verify these requirements:

1. The dense table contains editable TP and SL cells.
2. The table TP cell uses `_tpDisplay(p)`.
3. The table column span includes the two restored columns.
4. `_exitLegRaw` checks the mcap value before `tp_pct=0`.

The existing API and funded exit-override tests will run without a full suite.

## Deployment

The change will merge to `main` and deploy through the pinned web runtime process.

The funded memecoin runner will not restart. The deployment check will verify the web symlink, process path, served asset, and custom TP controls.
