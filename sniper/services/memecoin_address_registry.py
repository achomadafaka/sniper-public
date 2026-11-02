"""Reconcile the existing wallet-address evidence into memecoin_address_registry, and report which
armed wallets each on-chain lane can see.

Spec: docs/superpowers/specs/2026-09-26-solana-address-registry-design.md. Step 1 of 3: this module
writes only the registry tables and reads everything else. No lane reads the registry yet, so
running it changes no subscription and no entry.

Sources, and the status each earns (stronger evidence wins; refuted beats verified):
  pump_profile_id    a `pump:<address>` source_user_id: the id IS the address       -> verified
  j7_trader_receipt  a data/solana_roster_owners.jsonl row whose signature's J7 event names the
                     same trader; the profile id comes from that event, never the handle
                                                                                    -> verified
                     (the event's handle disagreeing with the row's                 -> candidate)
  jsonl_handle_only  a JSONL row with no event for its signature, joined by handle  -> candidate
  memecoin_wallet_addresses rows: live_confirmed / fomoscan_confirmed -> verified, REFUTED ->
                     refuted, anything else -> candidate
No RPC anywhere: every source is already on disk.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter

from sniper.persistence import db_memecoin as m

logger = logging.getLogger(__name__)

_CONFIRMED = {"live_confirmed", "fomoscan_confirmed"}


def _is_solana_address(text):
    try:
        from solders.pubkey import Pubkey
        return str(Pubkey.from_string(str(text))) == str(text)
    except Exception:
        return False


def _is_evm_address(text):
    t = str(text or "")
    try:
        return len(t) == 42 and t.startswith("0x") and int(t[2:], 16) >= 0
    except ValueError:
        return False


def _valid(network_id, address):
    return (_is_solana_address(address) if str(network_id) == "solana"
            else _is_evm_address(address))


def _j7_event_for(conn, signature):
    """(source_user_id, handle, trader_lower) of the J7 raybot event carrying `signature`, or None.
    A range on the unique fomo_event_id index: 'swap:raybot:<sig>:<trader>:<mint>'."""
    lo = "swap:raybot:%s:" % signature
    row = conn.execute(
        "SELECT source_user_id, source_handle, fomo_event_id FROM memecoin_events "
        "WHERE fomo_event_id >= ? AND fomo_event_id < ? LIMIT 1", (lo, lo[:-1] + ";")).fetchone()
    if row is None:
        return None
    parts = str(row[2]).split(":")
    return row[0], row[1], (parts[3] if len(parts) > 3 else None)


# FOMO BUY RECEIPT MATCH (2026-09-28, operator "try finding them"; Alpha GO (a)). A profile whose
# buys reach us only as Fomo activity ids carries no signature and no owner, so no J7 receipt can
# name its wallet. A candidate address (the Axiom directory's x_handle match) is verified when, for
# each of the profile's recent DISTINCT-mint Fomo buys, the address has a successful transaction
# within the window in which its OWN balance of that mint rose, and a CONTROL profile's buys match
# none. Ew4rdo and smol_intern: 8/8 each; swapped between the two profiles: 0/8 each.
FOMO_BUY_RECEIPT_KIND = "fomo_buy_receipt_match"
FOMO_BUY_MIN_MATCHES = 5
FOMO_BUY_WINDOW_S = 180


def owner_token_rise(tx, owner, mint):
    """The owner's atomic balance change of `mint` in one transaction, summed over its accounts.
    A failed transaction moves nothing: 0."""
    meta = (tx or {}).get("meta") or {}
    if (tx or {}).get("err") is not None or meta.get("err") is not None:
        return 0

    def total(side):
        return sum(int(b["uiTokenAmount"]["amount"]) for b in meta.get(side) or ()
                   if b.get("owner") == owner and b.get("mint") == mint)
    return total("postTokenBalances") - total("preTokenBalances")


def fomo_buy_receipt_status(checks, control_checks, *, min_matches=FOMO_BUY_MIN_MATCHES):
    """'verified' or None. `checks` / `control_checks`: [(mint, signature or None)], one per
    distinct-mint Fomo buy of the profile / of a control profile. Every one of at least
    `min_matches` must match, and the control (at least as many) must match none."""
    mints = [mint for mint, _sig in checks]
    if (len(checks) >= min_matches and len(set(mints)) == len(mints)
            and all(sig for _mint, sig in checks)
            and len(control_checks) >= min_matches
            and not any(sig for _mint, sig in control_checks)):
        return "verified"
    return None


def record_fomo_buy_receipt_match(conn, *, source_user_id, handle, address, checks,
                                  control_checks, control_user_id, now_iso, actor):
    """Write the verified row with its proof, or nothing. Returns what record_address_evidence
    returns (None when unchanged or not verified). Does not commit."""
    if fomo_buy_receipt_status(checks, control_checks) is None or not _is_solana_address(address):
        return None
    evidence = {"window_s": FOMO_BUY_WINDOW_S,
                "checks": [{"mint": m_, "signature": s_} for m_, s_ in checks],
                "control_user_id": control_user_id,
                "control_checks": [{"mint": m_, "signature": s_} for m_, s_ in control_checks]}
    return m.record_address_evidence(
        conn, source_user_id=source_user_id, network_id="solana", address=address,
        status="verified", evidence_kind=FOMO_BUY_RECEIPT_KIND,
        evidence_json=json.dumps(evidence, sort_keys=True), handle=handle, now_iso=now_iso,
        actor=actor)


def reconcile(conn, *, roster_jsonl_path, now_iso, actor="reconcile"):
    """Mirror every existing source into the registry. Idempotent: a second run changes nothing.
    Returns a Counter of what happened. Does not commit: the caller owns the transaction."""
    seen = Counter()
    handles = {}
    for uid, handle in conn.execute(
            "SELECT source_user_id, handle FROM memecoin_wallets WHERE source_user_id IS NOT NULL"):
        handles[str(uid)] = handle
    by_handle = {str(h).lower(): uid for uid, h in handles.items() if h}

    def put(uid, network_id, address, status, kind, evidence, handle):
        if not _valid(network_id, address):
            seen["invalid_address"] += 1
            return
        changed = m.record_address_evidence(
            conn, source_user_id=uid, network_id=network_id, address=address, status=status,
            evidence_kind=kind, evidence_json=json.dumps(evidence, sort_keys=True),
            handle=handle, now_iso=now_iso, actor=actor)
        seen["%s:%s" % (kind, changed or "unchanged")] += 1

    # 1. pump.fun profile ids carry their own address.
    for uid, handle in handles.items():
        if uid.startswith("pump:") and _is_solana_address(uid[5:]):
            put(uid, "solana", uid[5:], "verified", "pump_profile_id", {"source_user_id": uid},
                handle)

    # 2. The J7-receipt JSONL (handle -> owner, one resolved signature per trader).
    try:
        with open(roster_jsonl_path) as fh:
            lines = fh.readlines()
    except OSError:
        lines = []
        seen["jsonl_unreadable"] += 1
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            seen["jsonl_bad_line"] += 1
            continue
        owner, sig = row.get("owner"), row.get("signature")
        if not owner or row.get("error"):
            seen["jsonl_no_owner"] += 1
            continue
        evidence = {"signature": sig, "trader_lower": row.get("trader_lower"),
                    "read_at": row.get("read_at"), "jsonl_handle": row.get("handle")}
        event = _j7_event_for(conn, sig) if sig else None
        if event is not None:
            uid, ev_handle, trader = event
            same_trader = trader == str(owner).lower() == str(row.get("trader_lower") or "")
            same_handle = str(ev_handle or "").lower() == str(row.get("handle") or "").lower()
            status = "verified" if same_trader and same_handle else "candidate"
            put(str(uid), "solana", owner, status, "j7_trader_receipt",
                {**evidence, "event_handle": ev_handle, "same_trader": same_trader,
                 "same_handle": same_handle}, ev_handle)
            continue
        uid = by_handle.get(str(row.get("handle") or "").lower())
        if uid is None:
            seen["jsonl_unjoined"] += 1
            continue
        put(uid, "solana", owner, "candidate", "jsonl_handle_only", evidence, row.get("handle"))

    # 3. The old one-address-per-profile table (created by scripts, not init_db: may be absent).
    try:
        old_rows = conn.execute(
            "SELECT source_user_id, handle, network_id, address, method, verify_status, "
            "verify_detail FROM memecoin_wallet_addresses").fetchall()
    except Exception:  # noqa: BLE001 - no table is no evidence, never a failure
        old_rows = []
        seen["wallet_addresses_missing"] += 1
    for uid, handle, network_id, address, method, vstatus, detail in old_rows:
        vs = str(vstatus or "")
        status = ("verified" if vs in _CONFIRMED
                  else "refuted" if vs.lower().startswith("refuted") else "candidate")
        address = str(address).lower() if str(network_id) != "solana" else str(address)
        put(str(uid), str(network_id), address, status, "wallet_addresses:%s" % (method or "?"),
            {"verify_status": vs, "method": method, "detail": detail}, handle)

    return seen


def armed_verified_owners(conn, armed=None, network_id="solana"):
    """{address: (source_user_id, handle)}: the VERIFIED addresses of profiles armed on
    `network_id`, plus the address a pump.fun profile id names (the id IS the wallet), unless the
    registry refutes it. None when the registry table does not exist, so a caller can keep its
    legacy source.

    The ONE definition of "watched": the slot lane (owners_from_roster) and the confirmed watcher
    (registry_watchlist) both read it, so the two lanes cannot disagree about who is watched.
    Keyed by source_user_id; the handle is the wallet's current one, for logs only."""
    try:
        rows = m.address_registry_rows(conn, network_id)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if armed is None:
        armed = m.real_approved_user_ids(conn, network_id=network_id)
    armed = {str(u) for u in armed}
    handles = {str(r[0]): r[1] for r in conn.execute(
        "SELECT source_user_id, handle FROM memecoin_wallets")}
    out, refuted = {}, set()
    for r in rows:
        uid = str(r["source_user_id"])
        if r["status"] == "refuted":
            refuted.add((uid, r["address"]))
        elif r["status"] == "verified" and uid in armed:
            out[r["address"]] = (uid, handles.get(uid) or r["handle_seen"])
    if str(network_id) == "solana":
        for uid in sorted(armed):
            addr = uid[5:]
            if (uid.startswith("pump:") and _is_solana_address(addr)
                    and (uid, addr) not in refuted):
                out.setdefault(addr, (uid, handles.get(uid)))
    return out


def solana_coverage(conn):
    """One row per wallet armed on solana, plus counts. Read-only.

    label: `on-chain + J7` | `on-chain only` | `conflict` | `candidate mapping` | `refuted mapping`
    | `J7 only` | `no source`. `J7 only` means no VERIFIED address, not that J7 delivers every
    trade. Subscription acknowledgements are step 3; until then `watcher_legacy` says whether the
    confirmed watcher's current loader would watch the wallet."""
    armed = sorted(m.real_approved_user_ids(conn, network_id="solana"))
    handles = {str(uid): h for uid, h in conn.execute(
        "SELECT source_user_id, handle FROM memecoin_wallets")}
    try:
        j7 = {str(r[0]) for r in conn.execute("SELECT DISTINCT source_user_id FROM j7_tracked_wallets")}
    except Exception:
        j7 = set()
    per = {}
    for row in m.address_registry_rows(conn, "solana"):
        per.setdefault(row["source_user_id"], []).append(row)
    try:
        watcher = {str(r[0]) for r in conn.execute(
            "SELECT source_user_id FROM memecoin_wallet_addresses WHERE network_id='solana' "
            "AND verify_status IN ('fomoscan_confirmed','live_confirmed')")}
    except Exception:  # noqa: BLE001 - absent table: the watcher watches nobody from it
        watcher = set()
    wallets, counts = [], Counter()
    for uid in armed:
        rows = per.get(str(uid), [])
        by = Counter(r["status"] for r in rows)
        in_j7 = str(uid) in j7
        if by["verified"]:
            label = "on-chain + J7" if in_j7 else "on-chain only"
        elif any(r["conflict_at"] for r in rows):
            label = "conflict"
        elif by["candidate"]:
            label = "candidate mapping"
        elif by["refuted"]:
            label = "refuted mapping"
        else:
            label = "J7 only" if in_j7 else "no source"
        counts[label] += 1
        wallets.append({"source_user_id": uid, "handle": handles.get(str(uid)), "label": label,
                        "verified": by["verified"], "candidate": by["candidate"],
                        "refuted": by["refuted"], "j7_tracked": in_j7,
                        "watcher_legacy": str(uid) in watcher})
    return {"armed": len(armed), "labels": dict(counts),
            "j7_only": sorted(w["handle"] or w["source_user_id"] for w in wallets
                              if w["label"] in ("J7 only", "no source", "candidate mapping",
                                                "refuted mapping", "conflict")),
            "wallets": wallets}


def main(argv=None):
    """Dry run by default; --apply commits. Writes only the registry tables."""
    import argparse
    from contextlib import closing
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_path, state_root
    load_env_files(str(state_root()))
    from sniper.persistence.db_core import get_connection
    from sniper.util.clock import real_utc_now_iso
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    with closing(get_connection(str(state_path("sniper_tracker.db")))) as conn:
        # One short write transaction. A dry run creates the two tables inside it and rolls the
        # whole thing back, so nothing touches the ledger; --apply commits.
        conn.execute("BEGIN IMMEDIATE")
        try:
            m.ensure_address_registry_tables(conn)
            seen = reconcile(
                conn, roster_jsonl_path=str(state_path("data", "solana_roster_owners.jsonl")),
                now_iso=real_utc_now_iso())
            cov = solana_coverage(conn)
        except Exception:
            conn.rollback()
            raise
        if a.apply:
            conn.commit()
        else:
            conn.rollback()
        print(("APPLIED" if a.apply else "DRY RUN (rolled back)"),
              json.dumps(dict(seen), indent=1, sort_keys=True))
        print(json.dumps({k: cov[k] for k in ("armed", "labels", "j7_only")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
