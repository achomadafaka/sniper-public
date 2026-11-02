"""Ink admission parity: every row of the normative matrix, three outcomes each (plan Task 7).

Source of rows: docs/superpowers/research/2026-09-29-ink-filter-parity.md, section
"## Admission matrix". Each outcome is either a pytest node id ("file.py::test_name", relative to
tests/ink) that exercises that outcome ON INK, or "gap: <why>". A node id is claimed only when the
test really drives that gate for chain 57073; a nearby test is not coverage.

test_parity_coverage.py enforces: every matrix row is listed here, every node id resolves to a test
function, and while ANY gap remains the pinned manifest must keep Ink blocked.
"""

PASS, REFUSE, UNKNOWN = "pass", "refuse", "unknown"

COVERAGE = {
    "Concrete chain/mint/venue identity": {
        PASS: "test_identity.py::test_ink_canonical_identity",
        REFUSE: "test_identity.py::test_ink_context_refuses_wrong_chain_rpc",
        UNKNOWN: "gap: an unknown/ambiguous chain on an event is not tested to refuse",
    },
    "Supported executable context": {
        PASS: "test_real_executor.py::test_a_canonical_entry_fills_once_through_the_real_seams",
        REFUSE: "test_identity.py::test_enabled_ink_without_context_stays_unroutable",
        UNKNOWN: "gap: router/quoter code unverifiable at startup is not tested",
    },
    "Source identity and per-chain authority": {
        PASS: "gap: an armed Ink source admitted through the roster gate is not tested",
        REFUSE: "test_identity.py::test_ink_wallet_authority_is_chain_local_and_pause_clears_it",
        UNKNOWN: "gap: an unknown source on Ink is not tested",
    },
    "Pre-arm backlog and source freshness": {
        PASS: "gap: a fresh canonical Ink buy admitted on its own source clock is not tested",
        REFUSE: "test_source_attribution.py::test_old_canonical_buy_keeps_source_clock_and_exact_quantity",
        UNKNOWN: "test_source_attribution.py::test_missing_block_time_cannot_be_restamped_as_fresh",
    },
    "Entry enable / kill / quiet hours": {
        PASS: "test_identity.py::test_ink_shared_entry_reaches_injected_transport",
        REFUSE: "gap: kill switch / quiet hours on an Ink entry are not tested",
        UNKNOWN: "gap: a gate changing between receipt and dispatch is not tested",
    },
    "Market-data capability": {
        PASS: "gap: no Ink market-data producer yet (Task 5b)",
        REFUSE: "test_state_evidence.py::test_manifest_capability_requires_route_and_market_safety_proofs",
        UNKNOWN: "gap: no Ink market-data producer yet (Task 5b)",
    },
    "Quote/native balance and bankroll sizing": {
        PASS: "gap: Ink bankroll sizing from a fresh ETH mark is not tested",
        REFUSE: "gap: zero Ink funds are not tested",
        UNKNOWN: "test_identity.py::test_ink_quote_price_unknown_remains_unknown",
    },
    "Native entry + approval + full-book exit gas": {
        PASS: "test_identity.py::test_ink_shared_entry_reaches_injected_transport",
        REFUSE: "test_identity.py::test_ink_gas_shortage_stops_before_signing",
        UNKNOWN: "gap: an unreadable Ink native balance is not tested",
    },
    "Global slots and exposure; local funds": {
        PASS: "gap: an Ink entry under the global cap is not tested",
        REFUSE: "gap: concurrent Ink + other-chain reservations at the cap are not tested",
        UNKNOWN: "gap: unresolved Ink reservations in the global book are not tested",
    },
    "Atomic reservation / duplicate / unresolved signed": {
        PASS: "test_real_executor.py::test_a_canonical_entry_fills_once_through_the_real_seams",
        REFUSE: "test_crash_restart.py::test_an_unsent_signed_swap_is_held_until_the_never_admitted_proof",
        UNKNOWN: "test_execution_recovery.py::test_confirmed_swap_without_booking_keeps_exposure_even_if_balance_zero",
    },
    "Daily loss and drawdown pause": {
        PASS: "gap: Ink under the daily loss limit is not tested",
        REFUSE: "gap: an Ink loss tripping the global daily loss latch is not tested",
        UNKNOWN: "gap: missing Ink proceeds must not book as a zero loss (not tested at the latch)",
    },
    "Source-buy size floor/ceiling/cumulative conviction": {
        PASS: "gap: needs Ink source valuation (Task 4)",
        REFUSE: "gap: needs Ink source valuation (Task 4)",
        UNKNOWN: "gap: needs Ink source valuation (Task 4)",
    },
    "Market cap / bands / allowed bands": {
        PASS: "gap: needs Ink supply/price producer (Task 5b)",
        REFUSE: "gap: needs Ink supply/price producer (Task 5b)",
        UNKNOWN: "gap: needs Ink supply/price producer (Task 5b)",
    },
    "Regime and liquidity classification": {
        PASS: "gap: needs Ink market data (Task 5b)",
        REFUSE: "gap: needs Ink market data (Task 5b)",
        UNKNOWN: "gap: needs Ink market data (Task 5b)",
    },
    "Coin/pair minimum age and maximum age": {
        PASS: "gap: an old-enough Ink coin admitted by the presign recheck is not tested",
        REFUSE: "test_admission_parity.py::test_ink_unknown_age_recheck_refuses_before_approval",
        UNKNOWN: "gap: an unknown Ink age recorded unresolved (never passed) is not tested",
    },
    "Display/source, first-seen and exact-route chase": {
        PASS: "gap: needs Ink source price and pool identity (Task 4)",
        REFUSE: "gap: needs Ink source price and pool identity (Task 4)",
        UNKNOWN: "gap: needs Ink source price and pool identity (Task 4)",
    },
    "Execution premium and post-peak confirmation": {
        PASS: "gap: exact-quote premium on Ink is not tested",
        REFUSE: "gap: exact-quote premium on Ink is not tested",
        UNKNOWN: "gap: exact-quote premium on Ink is not tested",
    },
    "Volume activity / turnover / trades / sell share": {
        PASS: "gap: needs the Ink tape (Tasks 3/4, 5b)",
        REFUSE: "gap: needs the Ink tape (Tasks 3/4, 5b)",
        UNKNOWN: "gap: needs the Ink tape (Tasks 3/4, 5b)",
    },
    "Reentry cooldown / loser / winner ladder": {
        PASS: "gap: an Ink re-entry after a winner inside the ladder is not tested",
        REFUSE: "test_admission_parity.py::test_ink_losing_reentry_refuses_before_reservation",
        UNKNOWN: "gap: an unreadable prior close on Ink is not tested",
    },
    "Consensus / cluster": {
        PASS: "gap: cluster gate disabled -> Ink admitted is not tested",
        REFUSE: "test_admission_parity.py::test_enabled_cluster_gate_cannot_be_skipped_by_ink_route",
        UNKNOWN: "gap: an unreadable consensus count on Ink is not tested",
    },
    "Token safety and true reverse sellability": {
        PASS: "gap: a safe Ink token passing the strict route verdict is not tested",
        REFUSE: "test_state_evidence.py::test_ink_route_revert_is_not_cleared_by_token_pull_proof",
        UNKNOWN: "gap: an Ink safety read failing (429/timeout) is not tested",
    },
    "Route capacity / impact / round-trip / fresh simulation": {
        PASS: "gap: Task 6 route certification",
        REFUSE: "gap: Task 6 route certification",
        UNKNOWN: "gap: Task 6 route certification",
    },
    "Selection exemptions / manual override": {
        PASS: "gap: a manual Ink entry is not tested",
        REFUSE: "gap: an override that tries to skip Ink safety is not tested",
        UNKNOWN: "gap: an unreadable exemption lookup on Ink is not tested",
    },
}
