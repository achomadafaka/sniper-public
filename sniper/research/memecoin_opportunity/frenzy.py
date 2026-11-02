"""Provide the venue and age acceleration ceiling for the v3 entry gate."""
from __future__ import annotations

from collections import defaultdict, deque

from sniper.analysis.memecoin_v6_statistics import linear_percentile

FRENZY_AGE_BUCKET_MS = 5_000
FRENZY_PERCENTILE = 75
FRENZY_MIN_SAMPLES = 40
FRENZY_MAX_SAMPLES = 200


def _bucket(launch_age_ms: int) -> int:
    return int(launch_age_ms) // FRENZY_AGE_BUCKET_MS


class FrenzyCeiling:
    """Keep a bounded acceleration history in memory."""

    def __init__(self) -> None:
        self._samples = defaultdict(
            lambda: deque(maxlen=FRENZY_MAX_SAMPLES),
        )

    def observe(
        self,
        venue_id: str,
        launch_age_ms: int,
        acceleration: int | None,
    ) -> None:
        if acceleration is None:
            return
        key = (str(venue_id), _bucket(launch_age_ms))
        self._samples[key].append(int(acceleration))

    def ceiling(self, venue_id: str, launch_age_ms: int) -> int | None:
        samples = self._samples.get((str(venue_id), _bucket(launch_age_ms)))
        if samples is None or len(samples) < FRENZY_MIN_SAMPLES:
            return None
        return int(linear_percentile(tuple(samples), FRENZY_PERCENTILE))
