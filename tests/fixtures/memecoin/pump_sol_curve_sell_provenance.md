# Frozen curve sell evidence

`pump_sol_curve_sell_pairs.json` is Gamma's corrected v2, SHA256
`2f18bb19eb533277299815ecfdce9046dcbb35c09c5757983168a3c70429964a`.
2,000 continuous public-market trade pairs, 2026-10-23, seed 23 with small-reserve margins
oversampled. Quote units are classified as SOL by positive legacy fields, not retained mint
identity. Gamma reports a separate 100-mint raw-account spot check, 100 SOL, which Codex did
not repeat. Use for arithmetic; never fee variety, chain confirmation or account identity.
One three-unit gross sale leaves net one and no positive floor at 3%; the builder refuses it.
The withdrawn v1 contained 188 zero artifacts and is not committed.

`pump_sol_curve_sells.json` is Lambda's corrected fixture from `1bc55b823`, integrated on main
at `b50a4404a`, SHA256
`79e8f9021ffdee70f70b267519eb6818971e7d25b0aaf9ca0771601bf7256d17`.
All 17 `rows` are SOL; the 22 `quote_token_rows` are explicitly separate. Tests check raw curve
quote-mint bytes 83..114 equal the default public key, decoded event mint/quote identity and
nonzero quote-mint bytes on every excluded row. This replaces the old mixed `9fffca2a6` fixture
(eight SOL rows of 17). Creator300/100/225/60/5 rows are token-quoted, not SOL fee coverage.
Zero legacy `sol_amount` belongs to non-SOL rows; the collector's initial event-version claim
was withdrawn. SOL rows have positive `sol_amount`, creator30 or cashback creator0.

The collector labels the capture confirmed. It retains raw pre-curve bytes, instruction account
keys/data, balances and logs, but not confirmation requests/status, `meta.err`, transaction
signature/header privileges, raw Global or signature-bracketing responses. Those labels are
not independently retained proof. Arithmetic/event decoding is independently reproducible.
Only rows2/3/4/15 meet the initial normal SOL mode scope; row3 alone is a16-account legacy sell.
Rows6/10/13 have holder rewards enabled despite their `kind` labels; raw flags select the scope.
Its signed minimum is one, so it cannot distinguish gross-versus-net minimum enforcement.
No provider or outcome calls are part of these tests.
