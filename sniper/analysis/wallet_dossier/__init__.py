"""Wallet Dossier v0 — deterministic table-first per-wallet artifact.

Consumes WalletData (fetch fixture) + ProtocolReport (analyzer JSON) and
produces a SQLite database + optional JSON exports of the structured
tables defined in the spec.

Spec: docs/superpowers/specs/2026-05-25-wallet-dossier-semantics-v0.md

Current slice (`v0_complete`) ships twelve tables:
  positions, position_exit_episodes, result_semantics,
  mechanism_evidence, market_semantics, diagnostic_accounting,
  hypothesis_results, surfaces, crosstabs,
  data_lineage, data_gaps, dossier_meta

It covers §6.1-§6.11 schema, §7.1-§7.8 derivations, §8 honesty gates,
and §9 hard rules. The §10 markdown report ships alongside this slice
via scripts/build_wallet_dossier.py --out-md.

No live API calls. No mutation of ProtocolReport. No LLM in the data path.
"""

from sniper.analysis.wallet_dossier.builder import build_dossier
from sniper.analysis.wallet_dossier.schema import DOSSIER_SCHEMA_VERSION

__all__ = ["build_dossier", "DOSSIER_SCHEMA_VERSION"]
