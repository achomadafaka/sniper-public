#!/usr/bin/env python3
"""BUILT, NOT RUN (Alpha's cross-session instruction, 2026-09-08) -- do not execute until Alpha
authorizes it. The fomo ban guard is active from the 2026-09-06 app-layer ban and will refuse every
call locally right now (FomoBanGuardActive); running this before the new account is live and Alpha
says so risks that account too.

Harvests each armed Solana wallet's /v2/users/{uid} profile for a Solana wallet address, and stores
any found into memecoin_wallet_addresses under a NEW method value distinct from the existing
'fomoscan' (2 confirmed rows) and 'holder_intersection' (3 REFUTED rows -- read verify_status on
that table before trusting any row in it).

WHY THIS MIGHT WORK WHERE THE EVM VERSION DID NOT: a companion check (Piece 1, same session,
2026-09-08 -- see HANDOFF.md) validated the underlying METHOD, not this specific endpoint field:
"does an address exposed by some fomo-adjacent source actually appear as the on-chain buyer".
Tested on the 2 known-good Solana addresses (frankdegods, Aurelius0121): 72 on-chain detections
cross-matched to an independent memecoin_events row on the same mint within a 180s window, with a
consistent ~60s chain-lead. Real, non-circular evidence the method CAN work on Solana. It is NOT
evidence /v2/users/{uid} carries a correct or even populated Solana field -- those 2 known-good
addresses came from fomoscan, a different, already-external source, not from this endpoint.

FIELD NAME -- VERIFIED live 2026-09-08 on one real profile before this ran at any scale, per
Alpha's explicit instruction to check one wallet first. It is the bare `"address"` key, sitting
right next to `"evmAddress"` (whose value at that same fetch matched the profile-scrape convention
`profile_evm_address` already uses in sniper/services/memecoin_evm_onchain_watch.py). All four
originally-guessed names (solanaAddress/solAddress/solanaWallet/walletAddress) were wrong and are
kept only as harmless fallbacks. Every candidate value is still shape-validated with
sniper.analysis.memecoin_x.normalize.is_solana_address before being trusted, so a profile shaped
differently than the one checked would still fail safe (read as "no address exposed", never as a
wrong address).

SAFETY MODEL:
  - Never overwrites an existing memecoin_wallet_addresses row (5 exist today: 3 REFUTED,
    2 fomoscan_confirmed) -- skips any source_user_id already present. INSERT OR IGNORE, not
    INSERT OR REPLACE, so the refuted-method history and the known-good rows are never at risk.
  - Single bounded pass: one profile fetch per not-yet-covered armed Solana wallet. No retry loop,
    no polling, exits after one full pass over the roster.
  - Goes through the SAME shared fomo rate guard every other caller in this repo uses
    (sniper.feeds.fomo_feed.fomo_get, via sniper.feeds.fomo_account.get_profile) -- not a
    bespoke client. While the ban is active this raises FomoBanGuardActive on the first call; the
    script logs that plainly and stops the whole pass rather than looping through 50+ refusals.
  - Writes ONLY memecoin_wallet_addresses, on Alpha's explicit instruction -- every other research
    script this session writes to a phase-owned table instead; this is a deliberate, scoped
    exception, not the new default pattern.
  - --dry-run (default): fetch, print, stage nothing written. --confirm: fetch and write.
    Mirrors this repo's fund-moving script convention on purpose, even though nothing here moves
    money -- same "nothing happens without an explicit flag" discipline.

    python3 scripts/research/harvest_solana_profile_addresses.py --dry-run
    python3 scripts/research/harvest_solana_profile_addresses.py --confirm
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DB = REPO / "sniper_tracker.db"
METHOD = "fomo_profile_solana"
CALL_PAUSE_S = 1.2   # our own pacing -- see main()'s note on why the shared guard alone isn't enough right now
# VERIFIED live 2026-09-08 against one real profile (xfilesboy): the field is the bare "address"
# key, sitting right next to "evmAddress". None of the originally-guessed names
# (solanaAddress/solAddress/solanaWallet/walletAddress) were it -- kept as harmless fallbacks in
# case some profile shape differs, but "address" goes first since it's the confirmed real key.
SOLANA_ADDRESS_KEYS = ("address", "solanaAddress", "solAddress", "solanaWallet", "walletAddress")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def extract_solana_address(profile):
    """First shape-valid Solana address found under any candidate key, else (None, None)."""
    from sniper.analysis.memecoin_x.normalize import is_solana_address
    if not isinstance(profile, dict):
        return None, None
    for key in SOLANA_ADDRESS_KEYS:
        value = profile.get(key)
        if isinstance(value, str) and is_solana_address(value):
            return value, key
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="fetch + print only (default)")
    g.add_argument("--confirm", action="store_true", help="fetch + write to memecoin_wallet_addresses")
    ap.add_argument("--state-root", default=str(REPO))
    ap.add_argument("--gap", type=float, default=CALL_PAUSE_S,
                     help=f"seconds between calls (default {CALL_PAUSE_S})")
    ap.add_argument("--limit", type=int, default=None,
                     help="attempt at most N wallets this invocation (a 'batch'); reruns pick up "
                          "where the last one left off since already-written wallets are skipped")
    args = ap.parse_args()
    gap_s = args.gap

    from sniper.util.envfiles import load_env_files
    load_env_files(args.state_root)
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.persistence.db_memecoin import real_approved_user_ids
    from sniper.feeds.fomo_feed import FomoAuth, fomo_get, FomoBanGuardActive, FomoThrottleBacklog
    from sniper.feeds.fomo_account import get_profile

    ro = get_readonly_connection(str(DB))
    ro.row_factory = sqlite3.Row
    armed = real_approved_user_ids(ro, network_id="solana")
    if not armed:
        sys.exit("REFUSE: real_approved_user_ids(network_id='solana') returned nothing -- "
                 "check MEMECOIN_EVM_ALLOWLIST/db state before trusting this script at all")
    handles = {r["source_user_id"]: r["handle"] for r in ro.execute(
        f"SELECT source_user_id, handle FROM memecoin_wallets "
        f"WHERE source_user_id IN ({','.join('?' for _ in armed)})", tuple(armed))}
    existing = {r[0] for r in ro.execute(
        "SELECT source_user_id FROM memecoin_wallet_addresses").fetchall()}
    todo = sorted(armed - existing)
    remaining_before_limit = len(todo)
    if args.limit:
        todo = todo[:args.limit]
    print(f"armed on solana: {len(armed)}  already have a row (skipped): {len(armed & existing)}  "
          f"remaining: {remaining_before_limit}  this batch: {len(todo)}  gap: {gap_s}s", flush=True)
    if not todo:
        print("nothing to do.")
        return

    auth = FomoAuth()
    found, no_field, refused = [], [], []
    for i, uid in enumerate(todo):
        if i:
            time.sleep(gap_s)   # our own pacing -- the shared guard's base gap is 0 right now
            # (restored 2026-09-08 to the value that ran 34 clean days), so nothing else paces us.
        handle = handles.get(uid, "?")
        try:
            profile = get_profile(auth, uid, fetch=fomo_get)
        except (FomoBanGuardActive, FomoThrottleBacklog) as exc:
            print(f"REFUSED at {handle} ({uid}): {exc} -- stopping the pass, not looping through "
                  f"the rest of the roster against a guard that is refusing everything.", flush=True)
            refused.append(uid)
            break
        except Exception as exc:
            import urllib.error
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                print(f"429 at {handle} ({uid}): {exc} -- stopping the pass on first sign of "
                      f"rate-limit escalation, not pushing through (account is hours old).", flush=True)
                refused.append(uid)
                break
            print(f"{handle:<20} {uid}  fetch failed: {exc}", flush=True)
            no_field.append((uid, handle, None))
            continue
        addr, key = extract_solana_address(profile)
        if addr:
            print(f"{handle:<20} {uid}  {key}={addr}", flush=True)
            found.append((uid, handle, addr, key))
        else:
            print(f"{handle:<20} {uid}  no Solana address under any of {SOLANA_ADDRESS_KEYS}",
                  flush=True)
            no_field.append((uid, handle, None))

    print(f"\nfound: {len(found)}  no_field: {len(no_field)}  refused_by_guard: {len(refused)}")

    if not args.confirm:
        print("\nDRY-RUN -- nothing written. Re-run with --confirm to store found addresses.")
        return
    if not found:
        print("\n--confirm: nothing found, nothing to write.")
        return

    w = sqlite3.connect(DB, timeout=30)
    w.executemany(
        "INSERT OR IGNORE INTO memecoin_wallet_addresses "
        "(source_user_id, handle, network_id, address, resolved_at, method, verify_status, "
        "verify_detail) VALUES (?,?,?,?,?,?,?,?)",
        [(uid, handle, "solana", addr, now_iso(), METHOD, "unverified",
          f"scraped from /v2/users/{{uid}} field={key}; not yet checked against on-chain activity")
         for uid, handle, addr, key in found])
    w.commit()
    print(f"\n--confirm: wrote {len(found)} row(s) to memecoin_wallet_addresses "
          f"(method={METHOD}, verify_status=unverified).")


if __name__ == "__main__":
    main()
