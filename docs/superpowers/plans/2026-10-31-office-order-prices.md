# Office order display increments

Authenticated Alpha operator order: censored orders never filled; show not filled and a plain reason, without Bought/Sold/Net. Filled ledger rows and charts show modelled USD unit prices and market caps.

First increment1000e188a: shared both-book unfilled label; Zeta source/UI PASS20 cases. GPT API omitted the stored reason; explicit reason not recorded until enrichment.

Formatter243a1efb8: shared display-only formatting in ledger/chart, sub-cent/zero/invalid quantity handling; Zeta formatter/UI PASS22. Missing API fields remained a separate gap.

API increment: new isolated read-only enrichment called from Epsilon-owned api_office.py, preserving Beta operator_view.py/position_charts.py. Exact book/order/epoch/mint/state and snapshot cutoff. Entry price is stored entry_unit_price_usd. Exit price uses Decimal exit_usd / (tokens_atomic / 10**decimals). No current FX. Both books keep separate identities.

Market cap uses recorded decision-sheet supply, bound by the ledger decision-bundle hash, matching decimals and metadata_known_at <= filled_at. Both entry and exit caps are modelled with that supply held constant. Exit is explicitly labelled entry supply estimate; this is not measured exit-time supply. Unknown supply produces an explanation, never one-billion-token synthesis.

Bounds: fixed two-book paths, mode=ro connections, at most200 rows per book/10 chart rows,200ms display budget plus existing SQLite progress deadlines. No provider calls, production writes, service control or money authority. Busy/missing/malformed sources keep existing report content and explicit field unavailability. Rollback via prior Alpha-owned web release.

Proof: ~/.cache/epsilon-office-api-prices-proof/ contains fresh authenticated served snapshots, separately enriched candidate snapshots, desktop/phone both-book row/chart screenshots and timings. Candidate read-only enrichment covered all35 filled rows of40 and both returned charts; p50 7.42ms/p90 13.36ms over10 reads. These are candidate/source proofs, not publication or organic outcome proof.
