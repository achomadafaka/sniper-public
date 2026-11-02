#!/usr/bin/env python3
"""Fixed launcher entrypoint. No provider credentials or environment are inherited."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.office.model_child import main

if __name__ == '__main__':
    raise SystemExit(main())
