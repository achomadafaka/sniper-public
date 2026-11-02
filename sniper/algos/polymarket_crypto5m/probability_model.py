"""Probability-model contract + diagnostic baselines.

A model estimates ``q_up = P(Polymarket Up wins under the Chainlink rule | features)``.
``q_down = 1 - q_up``, ``0 <= q_up <= 1``. **A ``calibrated=False`` model can never activate a paper
entry** — the shadow decision engine enforces that. All baselines here are diagnostic only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class ProbabilityPrediction:
    q_up: Optional[float]
    q_down: Optional[float]
    model_id: str
    model_version: str
    calibrated: bool
    prediction_status: str            # "ok" | "diagnostic_only" | "unavailable"
    reason: str = ""
    training_cutoff_ts: Optional[int] = None
    feature_version: Optional[str] = None


class ProbabilityModel(Protocol):
    model_id: str
    model_version: str

    def predict_up_probability(self, features: dict) -> ProbabilityPrediction: ...


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _ok(model_id, model_version, q_up, *, calibrated, status, feature_version=None, reason=""):
    q_up = min(1.0, max(0.0, float(q_up)))
    return ProbabilityPrediction(q_up=q_up, q_down=1.0 - q_up, model_id=model_id,
                                 model_version=model_version, calibrated=calibrated,
                                 prediction_status=status, reason=reason,
                                 feature_version=feature_version)


def _unavailable(model_id, model_version, reason):
    return ProbabilityPrediction(q_up=None, q_down=None, model_id=model_id,
                                 model_version=model_version, calibrated=False,
                                 prediction_status="unavailable", reason=reason)


class MarketMidpointBaseline:
    """Benchmark: q_up = the Up midpoint probability. NOT an independent edge model (calibrated=False)."""
    model_id = "market_midpoint_baseline"
    model_version = "v0"

    def predict_up_probability(self, features: dict) -> ProbabilityPrediction:
        q = features.get("up_midpoint_probability")
        if q is None:
            return _unavailable(self.model_id, self.model_version, "up_midpoint_unavailable")
        return _ok(self.model_id, self.model_version, q, calibrated=False, status="diagnostic_only",
                   feature_version=features.get("feature_version"))


class ThresholdDiffusionV0:
    """Driftless log-price terminal-above-threshold probability (diagnostic, uncalibrated):
    ``q_up = Phi( ln(chainlink_now / price_to_beat) / (vol_per_s * sqrt(seconds_remaining)) )``.
    Flat/at-threshold -> 0.5. Assumes zero drift and constant per-second log-vol — a transparent
    baseline, NOT a calibrated edge model."""
    model_id = "threshold_diffusion_v0"
    model_version = "v0"

    def predict_up_probability(self, features: dict) -> ProbabilityPrediction:
        now = features.get("chainlink_price_now")
        p2b = features.get("price_to_beat")
        vol = features.get("vol_per_s")
        secs = features.get("seconds_remaining")
        if None in (now, p2b, vol, secs) or now <= 0 or p2b <= 0 or vol <= 0 or secs <= 0:
            return _unavailable(self.model_id, self.model_version, "diffusion_inputs_insufficient")
        z = math.log(float(now) / float(p2b)) / (float(vol) * math.sqrt(float(secs)))
        return _ok(self.model_id, self.model_version, _phi(z), calibrated=False,
                   status="diagnostic_only", feature_version=features.get("feature_version"),
                   reason="driftless_lognormal")


class LegacySpotSignalDiagnostic:
    """Carries the btc5m LONG/SHORT/FLAT signal as a DIAGNOSTIC only — it is never translated into a
    usable probability and can never activate a paper entry. Emits a neutral, uncalibrated prediction
    with the legacy signal in ``reason``."""
    model_id = "legacy_spot_signal_diagnostic"
    model_version = "v0"

    def predict_up_probability(self, features: dict) -> ProbabilityPrediction:
        sig = features.get("legacy_spot_signal")   # -1 / 0 / 1 / None
        return ProbabilityPrediction(q_up=None, q_down=None, model_id=self.model_id,
                                     model_version=self.model_version, calibrated=False,
                                     prediction_status="diagnostic_only",
                                     reason=f"legacy_spot_signal={sig}",
                                     feature_version=features.get("feature_version"))


BASELINES = {m.model_id: m for m in (MarketMidpointBaseline(), ThresholdDiffusionV0(),
                                     LegacySpotSignalDiagnostic())}
