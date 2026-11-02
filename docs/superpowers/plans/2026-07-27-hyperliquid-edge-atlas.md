# Hyperliquid Edge Atlas Implementation Plan

## Scope

Extend the existing read-only Edge Desk with a fixed edge decomposition and make it the
Hyperliquid venue landing page. Preserve funded and paper execution paths unchanged.

## Tasks

1. Add RED tests for the pure Atlas read model: matrix, temporal pulse, exit attribution, copy gap,
   forward counts, and missing-table behavior.
2. Implement `sniper/hyperlive/edge_atlas.py` as deterministic transforms over row dictionaries.
3. Query the required ledgers inside the existing read-only Edge Desk connection and add
   `edge_atlas` to its payload.
4. Add RED UI contract tests for the Edge Desk landing route and Atlas mounts.
5. Render a compact concentration matrix, six-week pulse, exit-gap ledger, and one explicit next
   falsification in `templates/index.html`, `static/app.js`, and `static/styles.css`.
6. Run focused Python and UI-contract tests plus `git diff --check`.
7. Merge the scoped branch, restart only the existing systemd-owned web process on port 5000, then
   inspect authenticated desktop and mobile views. Do not start a second server.

## Verification

```bash
python3 -m pytest tests/test_hl_edge_atlas.py tests/test_api_hyperlive_edge_desk.py tests/test_ui_algo_tabs.py -q
git diff --check
```

Runtime verification is read-only: authenticated `GET /api/hyperlive/edge-desk`, browser console,
desktop/mobile screenshots, one Sniper web listener on the original port, and unchanged funded
dry-run state.
