"""Wallet analysis protocol v1 (pre-registered).

Each hypothesis has its own helper module. analyze_wallet() composes them into
a fixed-section ProtocolReport.

Spec: docs/superpowers/specs/wallet-analysis-protocol-v1.md
Methodology brain entry: methodology/wallet-analysis-protocol-v1
"""

from sniper.analysis.wallet_protocol.h5_tag_edge import (
    H5TagResult,
    PEER_COUNT_INFERENCE_FLOOR,
    PEER_COUNT_OK_THRESHOLD,
    classify_peer_confidence,
)

__all__ = [
    "H5TagResult",
    "PEER_COUNT_INFERENCE_FLOOR",
    "PEER_COUNT_OK_THRESHOLD",
    "classify_peer_confidence",
]
