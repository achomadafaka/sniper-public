"""Active Radar capture authority for each known venue identity."""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType

from .types import VenueId


class VenueAuthority(str, Enum):
    SCOREABLE = "scoreable"
    CAPTURE_ONLY = "capture_only"
    HISTORICAL_ONLY = "historical_only"


# `bags_robinhood_4663` RETIRED 2026-08-19 (operator instruction). It captured nothing:
# across the whole recorded ledger, 2026-07-27 to 2026-08-19, it produced 0 alerts and 0
# paper outcomes while pump.fun produced 13,733 and 289.
#
# CORRECTION: an earlier version of this comment blamed an unreachable chain. That was
# wrong. Chain 4663 is reachable and its shared scan runs at zero lag; the
# `dial tcp 10.31.25.38:8547` text came from the REMOTE endpoint's own error body, not
# from our routing. Bags produced nothing for reasons this retirement does not diagnose.
# Pons runs on the same chain and is still SCOREABLE.
#
# Retired, never deleted: the identity stays readable so historical rows resolve.
VENUE_AUTHORITY = MappingProxyType({
    VenueId.PUMPFUN_SOLANA: VenueAuthority.SCOREABLE,
    VenueId.BAGS_SOLANA: VenueAuthority.HISTORICAL_ONLY,
    VenueId.BAGS_ROBINHOOD_4663: VenueAuthority.HISTORICAL_ONLY,
    VenueId.PONS_V1_ROBINHOOD_4663: VenueAuthority.SCOREABLE,
})


if set(VENUE_AUTHORITY) != set(VenueId):
    raise RuntimeError("each Radar venue must have one authority")


def venue_authority(venue: VenueId | str) -> VenueAuthority:
    return VENUE_AUTHORITY[VenueId(venue)]


def active_capture_venues() -> frozenset[VenueId]:
    return frozenset(
        venue
        for venue, authority in VENUE_AUTHORITY.items()
        if authority is not VenueAuthority.HISTORICAL_ONLY
    )


def scoreable_venues() -> frozenset[VenueId]:
    return frozenset(
        venue
        for venue, authority in VENUE_AUTHORITY.items()
        if authority is VenueAuthority.SCOREABLE
    )


def is_active_capture_venue(venue: VenueId | str) -> bool:
    return venue_authority(venue) is not VenueAuthority.HISTORICAL_ONLY
