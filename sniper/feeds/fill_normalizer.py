"""Normalize Goldsky/subgraph OrderFilled rows into FillEvents (wallet-identified fills —
the feed that unblocks first-attention / follower-flow methods)."""
from __future__ import annotations

from sniper.feeds.thesis_feed_types import FillEvent


def _float_amount(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _derive_price(maker_asset: str, taker_asset: str,
                  maker_amount: float, taker_amount: float) -> float | None:
    """Outcome-token price in [0,1] from the USDC side (asset id '0'). USDC and share amounts
    share the same 6-decimal scale, so the ratio IS the price. maker pays USDC -> buys the token
    (price = USDC/shares); taker pays USDC -> maker sells (price = USDC/shares). None when neither
    side is USDC (rare token-for-token fill)."""
    if maker_asset == "0" and taker_amount:
        return maker_amount / taker_amount
    if taker_asset == "0" and maker_amount:
        return taker_amount / maker_amount
    return None


def fill_event_from_subgraph_row(row: dict) -> FillEvent:
    maker_asset = str(row.get("makerAssetId") or "")
    taker_asset = str(row.get("takerAssetId") or "")
    maker_amount = _float_amount(row.get("makerAmountFilled"))
    taker_amount = _float_amount(row.get("takerAmountFilled"))
    return FillEvent(
        tx_hash=str(row.get("transactionHash") or ""),
        log_id=str(row.get("id") or ""),
        ts=int(row.get("timestamp") or 0),
        maker=str(row.get("maker") or "").lower(),
        taker=str(row.get("taker") or "").lower(),
        maker_asset_id=maker_asset,
        taker_asset_id=taker_asset,
        maker_amount=maker_amount,
        taker_amount=taker_amount,
        price=_derive_price(maker_asset, taker_asset, maker_amount, taker_amount),
    )
