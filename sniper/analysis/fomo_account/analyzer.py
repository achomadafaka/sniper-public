"""Orchestrate one trader's analysis: fetch profile + recent swaps -> reconstruct -> metrics.

No pagination (fomo caps at newest <=100 swaps); coverage is derived from the
profile's lifetime swapCount.
"""
from __future__ import annotations

from sniper.feeds.fomo_account import get_profile, fetch_recent_swaps, fetch_balances
from sniper.settings import memecoin as _cfg
from .reconstruct import reconstruct
from .metrics import compute_metrics


def analyze_account(auth, uid, *, now_iso, large_buy_usd=None, fetch=None):
    large_buy_usd = _cfg.MEMECOIN_COPY_SIZE_USD if large_buy_usd is None else large_buy_usd
    profile = get_profile(auth, uid, fetch=fetch)
    profile.setdefault("id", uid)
    swaps = fetch_recent_swaps(auth, uid, fetch=fetch)
    balances = fetch_balances(auth, uid, fetch=fetch)
    recon = reconstruct(swaps)
    return compute_metrics(profile, swaps, recon, now_iso=now_iso,
                           large_buy_usd=large_buy_usd,
                           swap_count=profile.get("swapCount"), balances=balances)
