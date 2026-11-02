#!/usr/bin/env python3
"""Writes data/office/candidate_lists.json for the candidate_lists seat tool. Read-only on the tape, no provider calls. Run every 5 min, niced, under a flock.

    python3 scripts/office_candidate_lists.py [--state-root DIR]"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sniper.office.seat_candidates import FILE_NAME, write

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('--state-root', default=str(Path(__file__).resolve().parents[1]))
root = Path(ap.parse_args().state_root)
out = write(root / 'data/desk', root / 'data/office' / FILE_NAME, time.time())
print(out['state'], out.get('reason'), out.get('graduated_liquid_n'), out.get('buyer_growth_n'))
sys.exit(0 if out['state'] == 'known' else 1)
