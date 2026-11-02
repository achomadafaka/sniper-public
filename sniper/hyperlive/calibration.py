"""Online calibration — per-(setup, regime) win-rate and move estimates from rolling
outcomes. Beta-shrunk to a weak prior so small samples can't dominate. This is what makes
flexible-A 'not stupid': the numbers track realized behavior instead of hardcoded constants.
"""
from __future__ import annotations

from collections import defaultdict

PRIOR_WINS = 1.0      # Beta(1,1) weak prior -> small n shrinks toward 0.5
PRIOR_LOSSES = 1.0
CONF_FULL_N = 30      # n at which confidence ~1


class Calibration:
    def __init__(self):
        self._wins = defaultdict(float)
        self._losses = defaultdict(float)
        self._move_sum = defaultdict(float)   # realized favorable move bps (winners)
        self._n = defaultdict(int)

    def p_win(self, setup: str, regime: str) -> float:
        k = (setup, regime)
        w, l = self._wins[k] + PRIOR_WINS, self._losses[k] + PRIOR_LOSSES
        return w / (w + l)

    def expected_move_bps(self, setup: str, regime: str, fallback_bps: float) -> float:
        k = (setup, regime)
        n = self._n[k]
        if n < 3:
            return fallback_bps
        # blend realized average favorable move with the vol-based fallback
        realized = self._move_sum[k] / max(1, self._wins[k]) if self._wins[k] else fallback_bps
        return 0.5 * realized + 0.5 * fallback_bps

    def confidence(self, setup: str, regime: str) -> float:
        return min(1.0, self._n[(setup, regime)] / CONF_FULL_N)

    def count(self, setup: str, regime: str) -> int:
        return self._n[(setup, regime)]

    def update(self, setup: str, regime: str, *, won: bool, realized_move_bps: float) -> None:
        k = (setup, regime)
        self._n[k] += 1
        if won:
            self._wins[k] += 1
            self._move_sum[k] += max(0.0, realized_move_bps)
        else:
            self._losses[k] += 1

    def snapshot(self) -> dict:
        return {f"{s}|{r}": {"n": self._n[(s, r)], "p_win": round(self.p_win(s, r), 3)}
                for (s, r) in set(list(self._n) + list(self._wins) + list(self._losses))}
