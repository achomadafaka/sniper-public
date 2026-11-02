# Operator-only Office position charts

Owner Epsilon; Alpha merges/deploys, Gamma checks scale/visibility, Zeta auth/XSS. Read-only, no funded handles, providers, keys, .env or seat exports.

- Fixed Office explore ledger and `data/desk/pumpswap/tape.db`; exact order pool, mint/WSOL identity. Recorded pre-trade reserves only. Event block time (swap field or slot join), never receipt/batch time. Missing clocks are counted, not imputed.
- Bound the recent pool-slot tail and a separate fill-proof-slot anchor lookup. Anchor is at/before fill, within 10 seconds; otherwise no indexed tape line. Print anchor time/slot, modelled fill SOL/token and model-fill/tape factor as text.
- Tape percent is indexed to that anchor and explicitly approximate. Marks percent is mark lamports/spend lamports minus one. Host peak and levels use the order's peak and hash-validated frozen policy, never tape extrema. Null marks and gaps over 30 seconds split segments; no last-value extension after a pool ends.
- Operator auth before source reads, no-store. Shared five-second server memo/singleflight; bounded read-only SQLite handles, progress deadlines and row/output caps. Browser polls only while visible. No chart data through tools, brain or mail.
- Vendored Lightweight Charts; fixed DOM/text nodes for ticker/reason/notes. Entry reference is percent zero, not a model fill price on the tape axis. Exit marker includes its modelled price/reason; recent closed windows bounded. Retain a just-closed chart briefly for an existing viewer; no open positions gives a plain empty state inside the room.
- RED/GREEN: missing clocks/anchor/decimals/policy, wrong pool/mint, zero versus null marks, separate scale/peak, gap segmentation, auth-before-read, singleflight and read-only sources. Real browser desktop/phone, no live DM/orders.

Not handled: other epochs/chains/quote currencies, historical tape reconstruction, executable landing proof, new collectors, changing rails or fixing marks. Failure remains typed unknown. Rollback removes only the UI/read route; stored orders and tape stay unchanged.

Verification: 31 focused chart/floor/UI/route tests PASS (`-n 2`), including real vendored Chromium and actual producer DDL extracted without importing the environment-loading worker. Desktop/390px phone previews have zero overflow or browser errors. Fixture screenshots: `output/playwright/charts-fixture-desktop.png`, `charts-fixture-phone.png`; empty state `charts-empty-phone.png`. One private read-only source check returned known, zero open positions, no chart rows, 0.88 ms; this is one timing observation, not a latency distribution or live chart acceptance. No provider, live DM, runtime or money action.


## Room presentation revision — Alpha order 2026-10-30T17:47:59Z

Supersedes the earlier compact-room/card-strip layout. The original pixel room and framed monitor wall share one office frame; each existing operator position chart can enlarge in a native dialog, keeping the same chart instance/data, captions and stale warning. On phone the monitor wall and workspace stack. No external seat-card strip: a sprite opens its details dialog with validated state/reason, action, last words, known counters/unknown note and Reply. Parked seats have a red named reason badge; canvas failure retains accessible seat controls. Unknown roster state cannot promote a working panel. Roster removal closes the panel without retargeting its draft; polling preserves focus, and chart disappearance closes enlargement safely. Closed-chart captions remain visible when open_count becomes zero.

Data/price/auth routes, poll cadence, Gamma scale rules and host rails unchanged. Browser witnesses cover hostile strings, modal focus, missing canvas, parked/unknown states, removed recipients, enlargement/exit/stale states and phone width. Early fixture-labelled screenshots: `epsilon-office-wall-monitors/output/playwright/wall-monitors-preview-desktop.png` and `wall-monitors-preview-phone.png`; no live open-position claim.
