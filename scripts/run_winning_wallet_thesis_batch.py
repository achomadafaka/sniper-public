#!/usr/bin/env python3
"""Operator CLI: batch winning-wallet thesis extractor over curated active winners."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.analysis.winning_wallet_thesis.batch_cli import main  # noqa: E402

if __name__ == "__main__":
    main()
