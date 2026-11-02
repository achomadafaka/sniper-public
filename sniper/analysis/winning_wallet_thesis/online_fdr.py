"""e-value online error control over the wallet stream.

What this PROVES (v1, conservative): a wallet is flag-eligible at its monotone test index t iff
    E_t >= 1/(alpha * gamma_t),   gamma_t = 6/(pi^2 * t^2)   (sum_t gamma_t = 1).
Because E[E_t | H0] <= 1, Markov gives P(E_t >= 1/(alpha*gamma_t) | H0) <= alpha*gamma_t, and
summing over the stream bounds the EXPECTED NUMBER of false discoveries by alpha*sum(gamma_t)
<= alpha. That is a PFER (per-family error rate / expected false-discovery COUNT) bound, which
is STRICTLY STRONGER than FDR (the expected false-discovery FRACTION) in the sparse-discovery
regime we expect. We deliberately call it PFER, not FDR — true FDR (e-LOND, Wang-Ramdas 2022)
needs adaptive spending over prior discoveries, which this static schedule does not do.

Practical consequence: the threshold grows fast (t=1 -> ~16; t=10 -> ~1645; t=100 -> ~1.6e5),
so only very strong e-values clear it late in a long stream. That is intentional precision-over-
recall for an early-warning flag; relax to adaptive e-LOND when false-positive feedback exists.

The test index t is the wallet's position in the global analysis stream; it is supplied by the
orchestrator from persistent state, so this module stays pure.
"""
from __future__ import annotations

import math


def gamma(t: int) -> float:
    """Predictable spending sequence with sum_t gamma_t = 1 (Basel problem)."""
    return 6.0 / (math.pi ** 2 * (max(1, int(t)) ** 2))


def fdr_threshold(t: int, alpha: float = 0.10) -> float:
    """The e-value a wallet must clear at test index t for the target PFER alpha."""
    return 1.0 / (alpha * gamma(t))


def is_flag_eligible(e_value: float, t: int, alpha: float = 0.10) -> bool:
    return float(e_value) >= fdr_threshold(t, alpha)
