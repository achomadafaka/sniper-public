#!/usr/bin/env python3
"""Cron helper (called by memecoin_desk_narrative_guesser_watchdog.sh in model mode): page when the narrative-guesser heartbeat is stale >120 s, and on recovery.
    python3 scripts/memecoin_desk_narrative_guesser_page.py --state-dir data/desk [--started-this-run]"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_narrative_guesser_page as P  # noqa: E402


def send(text):
    P.load_page_env(ROOT)                                    # state root first: a pinned release directory has no .env
    from sniper.services.memecoin_telegram import send_telegram_audited
    ok, _ms = send_telegram_audited(text, kind="narrative_guesser_heartbeat")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--started-this-run", action="store_true")
    a = ap.parse_args()
    print(P.check(Path(a.state_dir) / "narrative_guess", time.time(), send, started_this_run=a.started_this_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
