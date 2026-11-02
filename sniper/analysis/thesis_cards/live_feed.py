"""Live candidate feed (batch-runner Option B) — retires the frozen Becker enumerator for
the LIVE loop by collecting CURRENTLY-active wallets from active-market trades.

Becker is point-in-time clean (right for backtests) but frozen (~Feb 2026), so it misses
this month's wallets. This feed enumerates live: sample active markets (above a volume
floor), pull their recent BUY trades, and collect the buyers (= position OPENERS, the same
outcome-neutral signal as the Becker opened-count — buying requires no future outcome). No
PnL, no leaderboard, no rank.

`opened` here is a live-activity PROXY (distinct sampled markets the wallet bought in), used
for the activity-tier readout; the definitive card-power gate is still the producer's
`min_n` on the wallet's full live history (fetched downstream by the batch). Markets are
hash-ordered (deterministic, unbiased by volume/recency) before sampling; wallets are
hash-ordered before the limit.

Providers are injectable so the enumeration logic is unit-tested offline; `build_live_providers`
wires the real Gamma/Data-API calls.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping, Sequence

from sniper.analysis.thesis_cards.becker_feed import Candidate

MarketsProvider = Callable[[], "Sequence[str]"]
TradesProvider = Callable[[str], "Sequence[Mapping[str, Any]] | None"]


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)


def live_candidate_feed(markets_provider: MarketsProvider, trades_provider: TradesProvider, *,
                        min_appearances: int = 2, limit: int = 100) -> list[Candidate]:
    """Collect buyers across the provided markets' BUY trades; floor on appearances; hash-order."""
    appearances: dict[str, set[str]] = {}
    for cid in markets_provider():
        for t in (trades_provider(cid) or []):
            if str(t.get("side", "")).upper() != "BUY":          # openers only
                continue
            wallet = str(t.get("proxyWallet") or "").strip().lower()
            if wallet:
                appearances.setdefault(wallet, set()).add(str(cid))
    cands = [Candidate(wallet=w, opened=len(m)) for w, m in appearances.items()
             if len(m) >= min_appearances]
    cands.sort(key=lambda c: _h(c.wallet))
    return cands[:limit]


def build_live_providers(session: Any, *, min_volume_24h: float = 20000.0, max_pages: int = 2,
                         n_markets: int = 200, trades_per_market: int = 200
                         ) -> "tuple[MarketsProvider, TradesProvider]":
    """Wire the real Gamma (active markets) + Data-API (per-market BUY trades) providers."""
    from sniper.markets import polymarket_api as P
    _ids, _a2c, meta = P.fetch_market_index(session, min_volume_24h=min_volume_24h, max_pages=max_pages)
    markets = sorted(meta.keys(), key=_h)[:n_markets]            # broad hash-ordered sample
    def markets_provider() -> list[str]:
        return markets
    def trades_provider(cid: str) -> list[dict]:
        return P.fetch_market_trades(session, cid, side="BUY", limit=trades_per_market)
    return markets_provider, trades_provider
