"""WO-4 — typed quote-failure classification + sweep decision.

Only token-specific failures advance the unsellable-miss counter; infra failures advance an ops
streak. Defense in depth: all exact-relevant positions failing in one sweep is an outage -> advance
nothing (the Jupiter-outage false-CUT class that actually bit on 4663).
"""
from sniper.analysis.memecoin_quote_failures import (
    ADVANCE, INFRA_CLASSES, OPS, RESET, TOKEN_CLASSES, QuoteFailure, is_infra,
    sweep_miss_verdicts)

QF = QuoteFailure


def test_class_partition_is_complete_and_disjoint():
    assert TOKEN_CLASSES | INFRA_CLASSES == set(QuoteFailure)
    assert not (TOKEN_CLASSES & INFRA_CLASSES)
    assert all(not is_infra(c) for c in TOKEN_CLASSES)
    assert all(is_infra(c) for c in INFRA_CLASSES)


def test_success_resets():
    assert sweep_miss_verdicts([(1, None)]) == {1: RESET}


def test_single_token_failure_advances():
    # a lone real no-route must still count toward the CUT (a late CUT is a rug loss)
    assert sweep_miss_verdicts([(1, QF.NO_ROUTE)]) == {1: ADVANCE}


def test_single_untyped_failure_advances_failsafe():
    assert sweep_miss_verdicts([(1, "unknown")]) == {1: ADVANCE}


def test_single_infra_failure_is_ops_only():
    assert sweep_miss_verdicts([(1, QF.RATE_LIMIT)]) == {1: OPS}


def test_sweep_wide_outage_advances_nothing():
    # >=2 positions, ALL fail this sweep (mids alive) -> provider outage -> zero miss increments
    v = sweep_miss_verdicts([(1, QF.NO_ROUTE), (2, "unknown"), (3, QF.SIMULATION_REVERT)])
    assert v == {1: OPS, 2: OPS, 3: OPS}


def test_partial_failure_is_not_an_outage():
    # one success proves the provider is up -> the failing token genuinely can't sell -> advance
    v = sweep_miss_verdicts([(1, None), (2, QF.NO_ROUTE)])
    assert v == {1: RESET, 2: ADVANCE}


def test_outage_defense_is_scoped_to_provider_group():
    """A healthy direct-EVM quote says nothing about Jupiter health."""
    v = sweep_miss_verdicts([
        (1, QF.RPC_TRANSPORT, "solana:jupiter"),
        (2, None, "4663:direct"),
        (3, QF.NO_ROUTE, "4663:direct"),
    ])
    assert v == {1: OPS, 2: RESET, 3: ADVANCE}


def test_all_fail_outage_overrides_per_class():
    # defense in depth: EVERY exact-relevant position failing in one sweep is an outage even when a
    # failure classifies as token-specific — during a dark sweep we don't trust any "no_route", so
    # nothing advances. Priority: never a false CUT during an outage.
    v = sweep_miss_verdicts([(1, QF.RPC_TRANSPORT), (2, QF.NO_ROUTE)])
    assert v == {1: OPS, 2: OPS}


def test_empty_sweep():
    assert sweep_miss_verdicts([]) == {}
