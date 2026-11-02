"""Polymarket crypto-5m capture spine (Phase 0).

Studies the Polymarket BTC Up/Down 5-minute *binary* (Chainlink settlement), NOT BTC spot
direction. Binance is a feature/timing proxy only — never settlement truth, never the label.

Phase 0 is capture-only: market resolver, rule contract, event-driven book capture, settlement
capture, and Binance-vs-Chainlink basis-audit inputs. No paper fills, no PnL, no live orders.

See docs/superpowers/specs/2026-06-20-polymarket-crypto5m-paper-simulator-design.md.
"""
