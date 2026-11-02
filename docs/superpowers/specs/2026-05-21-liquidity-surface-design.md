# Wallet Protocol Liquidity Surface Design

**Date:** 2026-05-21
**Status:** implemented in this slice
**Surface:** `liquidity_surface_v1`
**Field:** `ProtocolReport.liquidity_surface`

## Purpose

Add a wallet-protocol surface that answers: is the wallet's observed edge
concentrated in thin markets or in markets where observed size/friction looks
less fragile?

The surface is display-only and self-contained. It MUST NOT:

- mutate `overall_verdict`, `gate_result`, `primary_edge_type`, or
  `copyability_note`;
- append to top-level `ProtocolReport.data_quality_flags`;
- feed branch hypotheses, gates, honesty-floor logic, Strategy Pattern Audit
  labels, or copyability logic;
- call live APIs, place/cancel orders, read/write DBs, or mutate fixtures.

## Data Availability

Historical order-book depth is not available from the public Polymarket APIs
currently used by Sniper. `/book`, `/books`, `/midpoint`, `/spread`, and
`/price` expose current token state. `/prices-history` and
`/batch-prices-history` expose historical midpoint-like prices only.

Therefore v1 uses fidelity tier `proxy_only_no_historical_depth`.

## Inputs

All inputs already exist on `WalletData`:

- `closed_positions`
- `activity_rows`
- `price_history_lookups`

Optional per-row volume fields are consumed when present:

- `market_volume_24h`
- `marketVolume24h`
- `volume_24h`
- `volume24h`
- `volume24hr`
- `volume24hrClob`

No fixture-schema change is required. Fixtures predating this surface analyze
cleanly and return `data_unavailable` when no usable trade rows or lookups are
present.

## JSON Shape

```python
{
    "surface_version": "liquidity_surface_v1",
    "status": "proxy_only" | "underpowered" | "data_unavailable",
    "fidelity_tier": "proxy_only_no_historical_depth",
    "historical_depth_available": False,
    "summary": {
        "n_markets": int,
        "n_trade_rows": int,
        "n_modeled_trade_rows": int,
        "total_observed_notional_usdc": float,
        "weighted_execution_cost_bps": float | None,
        "liquidity_read": str,
    },
    "degradation": {
        "states": ["proxy_only" | "underpowered" | "data_unavailable", ...],
        "reasons": [str, ...],
    },
    "market_observations": [
        {
            "condition_id": str,
            "title": str,
            "realized_pnl": float,
            "tokens": [
                {
                    "token_id": str,
                    "outcome": str,
                    "n_trades": int,
                    "notional_usdc": float,
                    "max_trade_notional_usdc": float,
                    "price_history_state": "available" | "missing",
                    "volume_state": "available" | "missing",
                    "market_volume_24h_usdc": float | None,
                    "size_pressure": {
                        "max_trade_to_volume_24h": float | None,
                        "capped": bool,
                    },
                    "near_resolution": bool,
                    "negative_risk_guard": bool,
                    "modeled_slippage": {
                        "status": "proxy_only" | "underpowered" | "data_unavailable",
                        "method": "trade_vs_captured_mid_proxy" | None,
                        "weighted_cost_bps": float | None,
                        "max_cost_bps": float | None,
                        "capped": bool,
                        "caveats": [str, ...],
                    },
                    "liquidity_bucket": "deep_proxy" | "moderate_proxy" | "thin_proxy" | "unknown",
                    "caveats": [str, ...],
                }
            ],
            "token_asymmetry": {
                "state": "observed" | "underpowered" | "data_unavailable",
                "max_notional_ratio": float | None,
                "max_pressure_ratio": float | None,
                "tokens": [dict, ...],
            },
        }
    ],
}
```

## Slippage Proxy

For each fill with an exact captured midpoint at `asset|timestamp`:

- BUY cost bps = `max(fill_price - mid, 0) / mid * 10000`
- SELL cost bps = `max(mid - fill_price, 0) / mid * 10000`
- weighted cost uses the fill's actual observed notional.

This is not order-book slippage. It is a proxy for observed execution friction
versus captured midpoint. Values are capped at 10,000 bps and flagged when
capped. Missing midpoint means no cost number.

## Degradation Rules

- `data_unavailable`: no closed positions, no usable trade rows, no exact
  price-history lookup, or no volume proxy where volume-specific claims would
  otherwise be needed.
- `underpowered`: single-market wallet, negative-risk guard, near-resolution
  guard, zero-price guard, or otherwise too little usable evidence.
- `proxy_only`: historical depth unavailable; all retained numeric estimates
  are proxies.

All degradation states live inside `liquidity_surface.degradation` and token
`modeled_slippage.caveats`. Nothing is emitted to top-level
`data_quality_flags`.

## Acceptance Tests

Implemented in `tests/test_wallet_protocol_liquidity_surface.py`:

1. zero closed positions;
2. single-market wallet;
3. no price-history data;
4. no volume data;
5. negative-risk / out-of-range price guard;
6. near-resolution price guard;
7. trade size larger than volume proxy;
8. zero-price / division-by-zero guard;
9. old fixtures / no activity backcompat;
10. replay determinism;
11. degradation states render in Markdown and JSON;
12. large-wallet linear lookup behavior;
13. YES/NO token liquidity asymmetry;
14. verdict/copyability/data-quality-flag invariance.

## Out of Scope / Future

Liquidity -> copyability wiring is explicitly deferred. That future slice must
be operator-gated and include:

- calibrated historical depth or quote dataset;
- delay/slippage/liquidity replay model;
- validation against outcomes;
- Phase B calibration re-baseline;
- explicit review of Strategy Pattern Audit quality-label impact.
