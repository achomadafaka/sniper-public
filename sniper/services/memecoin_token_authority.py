"""WO-14 — token-authority observation collection (OBSERVATION-ONLY, zero decision authority).

Entry-time token safety is checked once; freeze-authority exercise, mint-authority changes, and
Token-2022 transfer-fee / permanent-delegate changes alter EXITABILITY mid-hold. No CUT rule can be
registered without an event corpus, so this collects one FIRST: a deduplicated current snapshot per
held mint plus an append-only change-event history linked to affected positions. It reuses the
pre-entry Token-2022 parser (`solana_token_safety.parse_token_authority`) — no second parser.

HARD RULES:
- The first valid observation is BASELINE, never a change.
- An unreadable response is EVIDENCE_GAP, never a change (and never overwrites the last-known snapshot).
- Reads are bounded to one poll per held mint (currently at most seven).
- NOTHING in the V5 brain / executor / adapter / CUT path reads this evidence (grep-provable).
- Promotion to any rule requires a separate WO-13 preregistration — never here.
"""
from __future__ import annotations

import hashlib
import json as _json

MAX_HELD_MINT_POLLS = 7

# Fields whose CHANGE over a hold is a security-relevant exitability event.
_DIFF_FIELDS = ("mint_authority", "freeze_authority", "supply", "token_program",
                "transfer_fee_bps", "permanent_delegate")


def ensure_token_authority_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_token_authority_snapshot(
            mint TEXT PRIMARY KEY,
            mint_authority TEXT, freeze_authority TEXT, supply TEXT,
            token_program TEXT, is_token_2022 INTEGER, transfer_fee_bps INTEGER,
            permanent_delegate TEXT, known INTEGER,
            slot INTEGER, commitment TEXT, provider_label TEXT, observed_at TEXT,
            raw_sha256 TEXT, parser_version TEXT, updated_at TEXT)""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_token_authority_events(
            id INTEGER PRIMARY KEY,
            mint TEXT NOT NULL, position_id INTEGER,
            event_type TEXT NOT NULL,          -- BASELINE | CHANGE | EVIDENCE_GAP
            field TEXT, before TEXT, after TEXT,
            slot INTEGER, commitment TEXT, provider_label TEXT, observed_at TEXT,
            raw_sha256 TEXT, parser_version TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_auth_events_mint "
                 "ON memecoin_token_authority_events(mint, position_id)")
    conn.commit()


def raw_response_sha256(raw):
    """SHA-256 over the canonical JSON of the raw getAccountInfo response — provenance for a snapshot."""
    return hashlib.sha256(_json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bounded_held_mints(mints):
    """At most MAX_HELD_MINT_POLLS distinct held mints, deterministically (one getAccountInfo each)."""
    return sorted({str(m) for m in (mints or ()) if m})[:MAX_HELD_MINT_POLLS]


def _norm(x):
    return None if x is None else str(x)


def _get_snapshot(conn, mint):
    return conn.execute("SELECT * FROM memecoin_token_authority_snapshot WHERE mint=?",
                        (str(mint),)).fetchone()


def _append_event(conn, mint, position_id, event_type, field, before, after, meta):
    conn.execute(
        "INSERT INTO memecoin_token_authority_events(mint,position_id,event_type,field,before,after,"
        "slot,commitment,provider_label,observed_at,raw_sha256,parser_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(mint), position_id, event_type, field, _norm(before), _norm(after),
         meta["slot"], meta["commitment"], meta["provider_label"], meta["observed_at"],
         meta["raw_sha256"], meta["parser_version"]))


def _upsert_snapshot(conn, mint, snap, meta):
    conn.execute(
        "INSERT OR REPLACE INTO memecoin_token_authority_snapshot(mint,mint_authority,freeze_authority,"
        "supply,token_program,is_token_2022,transfer_fee_bps,permanent_delegate,known,slot,commitment,"
        "provider_label,observed_at,raw_sha256,parser_version,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(mint), snap.get("mint_authority"), snap.get("freeze_authority"), _norm(snap.get("supply")),
         snap.get("token_program"), 1 if snap.get("is_token_2022") else 0, snap.get("transfer_fee_bps"),
         snap.get("permanent_delegate"), 1 if snap.get("known") else 0, meta["slot"], meta["commitment"],
         meta["provider_label"], meta["observed_at"], meta["raw_sha256"], meta["parser_version"],
         meta["observed_at"]))


def _positions_with_history(conn, mint, position_ids):
    known = set()
    for pid in position_ids:
        row = conn.execute(
            "SELECT 1 FROM memecoin_token_authority_events WHERE mint=? AND position_id IS ? LIMIT 1",
            (str(mint), pid)).fetchone()
        if row is not None:
            known.add(pid)
    return known


def observe_token_authority(conn, mint, snapshot, *, position_ids, slot, commitment, provider_label,
                            observed_at, raw_sha256, parser_version):
    """Record one observation of `mint`'s token authority. `snapshot` is the parsed field dict
    (`solana_token_safety.parse_token_authority`), or None when the response was unreadable.

    Returns "BASELINE" (first valid) / "CHANGE" / "UNCHANGED" / "EVIDENCE_GAP". Change events are
    appended per affected position id; the snapshot row is deduplicated (one per mint). An
    EVIDENCE_GAP never overwrites the last-known-good snapshot. Observation-only: no rule reads this."""
    ensure_token_authority_tables(conn)
    pids = list(position_ids) if position_ids else [None]
    meta = {"slot": slot, "commitment": commitment, "provider_label": provider_label,
            "observed_at": observed_at, "raw_sha256": raw_sha256, "parser_version": parser_version}

    if snapshot is None:
        for pid in pids:
            _append_event(conn, mint, pid, "EVIDENCE_GAP", None, None, None, meta)
        conn.commit()
        return "EVIDENCE_GAP"

    prior = _get_snapshot(conn, mint)
    if prior is None:
        for pid in pids:
            _append_event(conn, mint, pid, "BASELINE", None, None, None, meta)
        _upsert_snapshot(conn, mint, snapshot, meta)
        conn.commit()
        return "BASELINE"

    known_pids = _positions_with_history(conn, mint, pids)
    new_pids = [pid for pid in pids if pid not in known_pids]
    for pid in new_pids:
        # A position that became held after the mint-level baseline starts from the current known
        # state. Calling a pre-hold difference its "change" would fabricate exposure history.
        _append_event(conn, mint, pid, "BASELINE", None, None, None, meta)

    diffs = [(f, prior[f], snapshot.get(f)) for f in _DIFF_FIELDS
             if _norm(prior[f]) != _norm(snapshot.get(f))]
    if diffs:
        for field, before, after in diffs:
            for pid in pids:
                _append_event(conn, mint, pid, "CHANGE", field, before, after, meta)
    _upsert_snapshot(conn, mint, snapshot, meta)     # dedup: one current row per mint
    conn.commit()
    return "CHANGE" if diffs else "UNCHANGED"


def poll_held_mint_authority(conn, held_position_map, get_account_info_fn, *, now_iso,
                             provider_label, commitment="confirmed"):
    """One bounded observation pass over held mints. `held_position_map` = {mint: [position_ids]};
    `get_account_info_fn(mint, commitment) -> (raw_response | None, slot | None)` is INJECTED (no live
    call here — the caller owns the RPC). Bounded to MAX_HELD_MINT_POLLS mints, one read each. Returns
    {mint: result}. Never raises for one bad mint. Observation-only; no rule consumes the result."""
    from sniper.execution.solana_token_safety import PARSER_VERSION, parse_token_authority
    results = {}
    for mint in bounded_held_mints(held_position_map.keys()):
        pids = held_position_map.get(mint) or []
        try:
            raw, slot = get_account_info_fn(mint, commitment)
            snap = parse_token_authority((raw or {}).get("value") if isinstance(raw, dict) else None)
            if raw is None or not snap.get("known"):
                snap = None                       # unreadable -> EVIDENCE_GAP
            results[mint] = observe_token_authority(
                conn, mint, snap, position_ids=pids, slot=slot, commitment=commitment,
                provider_label=provider_label, observed_at=now_iso,
                raw_sha256=(raw_response_sha256(raw) if raw is not None else None),
                parser_version=PARSER_VERSION)
        except Exception:
            results[mint] = observe_token_authority(
                conn, mint, None, position_ids=pids, slot=None, commitment=commitment,
                provider_label=provider_label, observed_at=now_iso, raw_sha256=None,
                parser_version=PARSER_VERSION)
    return results


def collect_held_token_authority(conn, get_account_info_fn, *, now_iso, provider_label,
                                 commitment="confirmed"):
    """Collect one observation pass for the currently open funded Solana positions.

    This is the complete read boundary for a worker/cron: EVM, paper, and closed rows cannot enter
    the RPC set. Multiple positions in one mint share one account read and retain separate evidence
    links. Still observation-only; callers supply the RPC function and schedule.
    """
    held = {}
    for row in conn.execute(
            "SELECT id,mint FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
            "AND COALESCE(network_id,'solana')='solana' AND COALESCE(mint,'')<>'' ORDER BY id"):
        mint = str(row["mint"] if hasattr(row, "keys") else row[1])
        pid = int(row["id"] if hasattr(row, "keys") else row[0])
        held.setdefault(mint, []).append(pid)
    return poll_held_mint_authority(
        conn, held, get_account_info_fn, now_iso=now_iso, provider_label=provider_label,
        commitment=commitment)
