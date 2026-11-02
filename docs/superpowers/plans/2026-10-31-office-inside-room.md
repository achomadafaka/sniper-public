# Office inside-room UI

Operator order via authenticated Alpha, 2026-10-31. Epsilon owns UI; Zeta reviews; Alpha publishes.

All boards and dialogs belong to `.pixel-scene`: small simultaneous position charts, both paper ledgers, operator inbox/timeline/DM and read-only wallet network. Each board expands on click. Phone width keeps two small charts and a single room column.

Seat facts come from current-identity stored events: latest event ID/time/kind, age, last tool call/result and recorded sleeping-until. Live age advances locally; no synthetic events, inferred work or recurring idle animations. Only newly observed, valid recent event IDs pulse or move seats. Reduced motion stops motion. Busy timeline failure must not erase an independent bounded read-only seat projection.

Edges: zero values remain values; missing/future timestamps stay unavailable; unknown lifecycle does not revive an old working state; filesystem timestamps use host clock, events use corrected clock; both books may share order IDs; recently closed charts remain while returned by the API; source errors stay visible, not empty success. No trade/provider calls or live writes. Does not repair stopped seats or extend Beta's recent-position API window. Rollback is the prior web release, owned by Alpha.

Validation: focused pytest with `-n 2`; real Chromium containment, chart pixels, both-book identities, live age, event-only motion, reduced motion, phone overflow, AA ledger contrast and expanded content. Authenticated real snapshots captured under `~/.cache/epsilon-office-inside-proof/`: current empty chart read is separate from retained real two-chart layout evidence. Candidate screenshots do not establish publication.
