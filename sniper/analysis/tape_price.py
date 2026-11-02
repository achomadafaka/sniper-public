"""Atomic tape ratio -> USD. The ONE place these units convert.

`exec_price_path_per_block` yields a size-weighted per-block price as an ATOMIC quote/base ratio —
not a human price, and emphatically not a USD price. Everything downstream that wants dollars comes
through here so the conversion exists exactly once.

Fail-closed: any missing input returns None. A missing decimal or quote-USD must never become a
silently-wrong number — mixing atomic and human units already produced one P0 defect in this project
(2026-07-23, drawdown-from-executable-peak).
"""
from __future__ import annotations

import math

from sniper.analysis.exit_features import exec_price_path_per_block


def tape_price_usd(swaps, *, quote_side, base_decimals, quote_decimals, quote_usd):
    """USD per base token from the most recent block in `swaps`, or None.

    human = (|quote|/10**quote_decimals) / (|base|/10**base_decimals)   # quote-token per base-token
    usd   = human * quote_usd                                           # USD per base-token
    """
    if base_decimals is None or quote_decimals is None:
        return None
    if not quote_usd or quote_usd <= 0:
        return None
    path = exec_price_path_per_block(swaps, quote_side=quote_side)
    if not path:
        return None
    atomic = path[-1][1]                      # newest block's size-weighted ratio
    human = atomic * (10 ** (int(base_decimals) - int(quote_decimals)))
    usd = human * float(quote_usd)
    return usd if math.isfinite(usd) and usd > 0 else None
