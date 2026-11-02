from sniper.research.memecoin_opportunity.types import VenueId
from sniper.research.memecoin_opportunity.venue_policy import (
    VENUE_AUTHORITY,
    VenueAuthority,
    active_capture_venues,
    scoreable_venues,
    venue_authority,
)


def test_authority_covers_each_known_venue_once():
    assert set(VENUE_AUTHORITY) == set(VenueId)


def test_active_and_scoreable_sets_match_the_approved_contract():
    assert active_capture_venues() == frozenset({
        VenueId.PUMPFUN_SOLANA,
        VenueId.PONS_V1_ROBINHOOD_4663,
    })
    assert scoreable_venues() == frozenset({
        VenueId.PUMPFUN_SOLANA,
        VenueId.PONS_V1_ROBINHOOD_4663,
    })
    assert venue_authority(VenueId.PONS_V1_ROBINHOOD_4663) is VenueAuthority.SCOREABLE
    assert venue_authority(VenueId.BAGS_SOLANA) is VenueAuthority.HISTORICAL_ONLY


def test_both_bags_identities_are_retired_but_still_resolvable():
    """Retired, never deleted.

    `bags_robinhood_4663` produced 0 alerts and 0 paper outcomes across the whole
    recorded ledger while its chain-4663 poller failed every call against a backend the
    host cannot route to. It is retired 2026-08-19 by operator instruction. The identity
    stays readable so historical rows that reference it keep resolving.
    """
    for venue in (VenueId.BAGS_SOLANA, VenueId.BAGS_ROBINHOOD_4663):
        assert venue_authority(venue) is VenueAuthority.HISTORICAL_ONLY
        assert venue not in active_capture_venues()
        assert venue not in scoreable_venues()
        assert venue in VENUE_AUTHORITY  # still a known identity, not deleted
