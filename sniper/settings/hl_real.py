"""Config knobs for the HL real copy leg (loaded from env). Spec:
docs/superpowers/specs/2026-07-17-hl-real-copy-leg-design.md

Fail-closed by design: disarmed + dry-run unless explicitly flipped in .env.
"""
import os


def _b(name, default):
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes"}


HL_REAL_ENABLED = _b("HL_REAL_ENABLED", False)      # entries-only kill switch (exits always run)
HL_REAL_DRY_RUN = _b("HL_REAL_DRY_RUN", True)       # record intents, never submit
HL_REAL_PER_POS_USD = float(os.getenv("HL_REAL_PER_POS_USD", "15"))
HL_REAL_MAX_CONCURRENT = int(os.getenv("HL_REAL_MAX_CONCURRENT", "5"))
HL_REAL_MAX_TOTAL_NOTIONAL = float(os.getenv("HL_REAL_MAX_TOTAL_NOTIONAL", "75"))
HL_REAL_MAX_CHASE_BPS = float(os.getenv("HL_REAL_MAX_CHASE_BPS", "100"))
HL_REAL_DAY_LOSS_BREAKER_USD = float(os.getenv("HL_REAL_DAY_LOSS_BREAKER_USD", "20"))
# SDK default market slippage is 5% — never rely on it; these are passed explicitly.
HL_REAL_ENTRY_SLIPPAGE = float(os.getenv("HL_REAL_ENTRY_SLIPPAGE", "0.005"))
HL_REAL_EXIT_SLIPPAGE = float(os.getenv("HL_REAL_EXIT_SLIPPAGE", "0.01"))
HL_REAL_TARGETS = [w.strip().lower() for w in os.getenv(
    "HL_REAL_TARGETS", "0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8").split(",") if w.strip()]
# Restrict real entries to the cell the copy edge actually lives in: non-major shorts.
# Default ON — funding this program means funding the alt-short edge, not the blended book
# (longs and BTC/ETH/SOL showed no copy edge). Exits/reconcile ignore this (only gates ENTRIES).
HL_REAL_ALT_SHORT_ONLY = _b("HL_REAL_ALT_SHORT_ONLY", True)
HL_REAL_API_WALLET_FILE = os.getenv("HL_REAL_API_WALLET_FILE", "data/hl_api_wallet.json")
HL_REAL_ACCOUNT_ADDRESS = os.getenv("HL_REAL_ACCOUNT_ADDRESS", "")
HL_REAL_BASE_URL = os.getenv("HL_REAL_BASE_URL", "https://api.hyperliquid.xyz")
HL_REAL_TAIL_SECONDS = float(os.getenv("HL_REAL_TAIL_SECONDS", "5"))
