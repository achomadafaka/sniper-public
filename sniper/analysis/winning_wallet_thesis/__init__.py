"""Winning-wallet thesis extractor (spec 2026-05-31 rev 2).

Phase 1: foundation + cluster engine. ingestion (ledger) -> enrichment ->
position-book -> slicer + shuffle-baseline -> cluster-builder -> profile assembler.
NOT a validated edge engine until Phase 3 wires the validation spine.
"""
from sniper.analysis.winning_wallet_thesis.cli import run_extractor  # noqa: F401,E402
