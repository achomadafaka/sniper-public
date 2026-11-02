# Office communication rendering

Approved brief: Alpha's authenticated operator instruction, 2026-10-31. Implement on af6e19030;
Alpha views copied-live-data browser evidence and SHA before Zeta review. No deployment.

Keep the established Office Pixel typeface, ink #223c43, sage #a8c58d, paper #f1e8bd,
wood #a87e59, and floor #c6bf96. Add a post desk as furniture in the room, not a side panel.
Seats remain spatially separate; seven current roster roles are rendered, never fabricated when absent.

1. Real new peer mail IDs launch envelope sprites from sender to recipient, including sleeping or idle
   senders (sending does not prove current activity). Initial history never auto-replays as fresh.
   Repeated polls do not replay an ID. Reduced motion shows the recorded communication without travel.
2. Clickable speech/message boxes open full escaped text. Previous/next browse that seat's retained
   thought and sent-mail history, with event time, source and bounded-history note. Preserve selected
   event during polling; Escape closes, focus returns. No invented dialogue or current-work claims.
3. Operator post desk combines actual to-operator mail, requests and seat thoughts ("seat ticker" =
   a seat's recorded text ticker, separate from recorded coin ticker cards). Show author/role and available model
   with its provenance; unknown stays unknown. Filter by seat. Unread is local browser read state,
   never a server acknowledgement. Opening a row marks only that row read. Text uses textContent.
4. Render seven roster roles including Judge GPT, Narrative, Forensics using existing room layout.
   Judge GPT ledger uses its exact book ID and that book's orders/equity only; no totals across books.
   Missing book/binding/valuation stays unknown. Keep existing main-ledger modelled/estimate warnings.
5. Added ideas: event-time labels and explicit reduced-motion control. Keep mail/history bounded.

Scope: frontend room/floor and browser checks; read-only feed metadata only where source has it.
No roster mutations, DB read receipts, service starts, provider calls, admission, execution, .env,
funded actions, deployment or write API changes. Local UI read storage only. Missing or future events,
identity turnover, duplicated IDs, stale data, missing models and absent book data remain explicit.

Proof: red browser acceptance checks; focused niced -n2 regression set; actual Chromium screenshots
from a copy of current feed/report/chart payloads. Copied data is not served/runtime acceptance.

Builder-check resolution: expose requested model only from a current-identity quota wake covering
that event's host timestamp; never infer historical model from current config. Coin ticker cards are
separate from thought tickers and require explicit recorded ticker plus exact decision actor attribution.
Read Judge GPT's fixed explore-gpt-ledger/ledger.db scoped to office-v2-explore-gpt-1; never rebind
explore_report.EPOCH or use the snapshot-writing PaperService.book_view. Fix the six-seat truncation,
GPT host-state filename mapping, and three-row small-screen layout. All choices are implementation
calls under the operator's build authorization, not additional operator promises.

Added explicit Replay recorded mail control: plays only an already retained event route, says replay, never sends.
Coin cards use recorded symbols where present, otherwise an explicit unknown symbol plus recorded mint; no guessed token names.
