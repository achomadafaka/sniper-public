# Office communication implementation plan

> Execute inline under the authenticated build instruction; no runtime starts or deployment.

Goal: real-event communication furniture and seven seats, with separate Judge GPT ledger.
Architecture: existing bounded read-only feeds plus a focused communications frontend module.
Stack: stdlib SQLite/Python, DOM/canvas, existing Office Pixel font, existing Chromium fixtures.

- [x] Red backend tests: identity/time-bound model labels, missing quota, fixed GPT state path,
      separate GPT book and exact ticker maker attribution. Add bounded read-only projections;
      preserve unknown/future/partial data and current receipt/export guards. Green focused tests.
- [x] Red browser tests: seven seat geometry, real new-mail sprite/dedup/reduced motion, expandable
      escaped thought/mail history with stable selection, local unread/filter, coin-maker/model labels,
      separate GPT ledger. Add communications module and small existing floor/UI integration changes;
      wire both page templates and versioned assets. Green browser regression checks nice19 -n2.
- [x] Copy current payloads with read-only projections, render Chromium desktop/mobile, inspect images,
      exercise message/ledger controls; commit owned paths and send SHA+screenshots to Alpha first.
