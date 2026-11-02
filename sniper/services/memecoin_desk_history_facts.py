"""Cohort D sheet blocks from the Helius history store (G1 creator history + G2 coin age) and the dev/curve-creator pair. PURE READ: one read-only SQLite connection per call, no provider call,
no write, no wait (0.5 s busy timeout). Shapes and the closed reason set are Gamma's (cohort D field shapes 2026-10-28); Beta wires these into ps_facts v3 / the sheet.

Clocks (Amendment 2): these are EXTERNAL-block facts, so `sheet_as_of` = ps_facts.as_of (the claim-time refresh), not the tape's market cut; age_s = sheet_as_of - creation block_time, computed once at sheet build.
All times are CORRECTED UTC epoch seconds: a stored fetched_at (host time.time()) is converted with the clock_offset_s sampled beside it (NULL offset = clock_unknown, never assumed 0).
LEAK RULE (Gamma, confirmed): a fetch counts only if fetched_at_corrected <= the PASS stage persist time AND <= the decision a2; a creator scan also needs fetched_at_corrected >= the coin's
block_time + 60 s; the count includes only launches the CHOSEN scan had seen (seen_at <= its fetched_at); the EARLIEST valid scan is chosen (a later rescan never leaks).
UNKNOWN is never 0 and never absent: lb None + a reason from REASONS. lb == 0 means 'none seen' ONLY with status 'complete'; a capped scan with lb 0 is 'at least 0' = NO information (the block may be known/capped, read and counted as no information about prior launches). lb is always a LOWER bound (a launch funded by another payer
is not in the wallet's history). dev (the create-instruction argument), the curve creator and create_event_user (launches.dev) are three different things and are never inferred from each other."""
import hashlib
import json
import sqlite3
from pathlib import Path

FINALITY_MARGIN_S = 60
BUSY_TIMEOUT_S = 0.5
BASIS = "onchain_creation_block_time"
WITNESS = "create_instruction_in_first_tx"
HISTORY_SOURCE = "helius_history"
CREATOR_SOURCE = "create_ix_arg"
# the closed reason set (committed in the definition): prior_launches_why + coin_creation reasons + g12_inline_log reasons + this module's own typed ones
REASONS = frozenset({
    "no_scan", "unknown_scan", "scan_before_coin", "rescan_after_decision",                                   # prior_launches_why
    "no_history", "first_tx_not_creation", "block_time_unusable", "slot_unusable", "unreadable_tx", "transport_error", "http_error", "rpc_error",   # coin_creation / scan
    "rate_limited", "credits_exhausted", "max_usage", "no_cycle", "deadline", "pagination_incomplete",
    "knob_off", "costs_not_calibrated", "no_key", "key_pin_mismatch", "daily_cap", "inline_error",            # g12_inline_log
    "history_unreadable", "not_attempted", "creator_unknown", "creation_future", "fetched_after_decision", "clock_unknown", "creator_not_in_history_source", "future_source",   # this module
})
DEFINITION = {"version": "history-facts-v1", "basis": BASIS, "witness": WITNESS, "history_source": HISTORY_SOURCE, "creator_source": CREATOR_SOURCE,
              "finality_margin_s": FINALITY_MARGIN_S, "reasons": sorted(REASONS), "statuses": ["complete", "capped", "unknown"], "fetched_via": ["nightly", "inline"],
              "leak_rule": "fetched_at_corrected <= min(pass_persist_at, decision_at); scan fetched_at_corrected >= coin block_time + 60; lb counts launches seen_at <= chosen scan; earliest valid scan",
              "clocks": "external blocks use sheet_as_of (ps_facts.as_of); age_s = sheet_as_of - block_time computed once at sheet build; tape blocks use the market cut",
              "block_keys": {"creator_history": ["state", "reason", "definition_sha256", "as_of", "creator_prior_launches_lb", "dev_wallet", "curve_creator", "create_event_user", "fetched_at", "clock_offset_s",
                                                 "fetched_via", "history_reason", "dev_equals_creator", "field_unknown"],
                             "coin_age": ["state", "reason", "basis", "age_s", "occurred_at", "known_at"], "coin_age_witness": ["signature", "slot", "witness", "source"]},
              "create_event_user_source": "launches.dev, labelled with the stored launches.dev_source value ('create_event_user'); null (source null) on rows from before the column and for PS-only coins", "unknown_block": "state unknown + reason; a null field is named in field_unknown with a closed reason",
              "prior_definition": "launches by this creator wallet with slot STRICTLY BEFORE this coin's creation slot (the coin's own launch and any launch at or after its slot are excluded); NOT 'before the decision': a later launch is not history of this coin",
              "lb_zero": "lb 0 is 'none seen' only with status complete; capped with lb 0 is 'at least 0', no information (P8 controls use complete and lb 0 only)",
              "source_knowledge": "launches rows count only if received_at - host_offset_s <= sheet_as_of; swaps_ps coin_creator only if its earliest row received_at <= the market cut (host); g12_inline_log rows only if ts - clock_offset_s <= decision; a later source row => the field is null with future_source; a missing clock/offset => clock_unknown, never an implicit 0",
              "creation_slot_unusable": "creation slot unknown or not a positive int => lb null, status unknown, reason slot_unusable, for complete and capped",
              "lb_is_lower_bound": True, "dev_source_curve": "launches.creator|swaps_ps.coin_creator", "dev_never_inferred_from_curve_creator": True}
DEFINITION_SHA256 = hashlib.sha256(json.dumps(DEFINITION, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


VERSION = DEFINITION["version"]


def definition():
    """A deep copy of the frozen definition (policy binds its sha256; mutating the copy changes nothing)."""
    return json.loads(json.dumps(DEFINITION))


def policy_entry():
    """The cohort policy key `history` = {version, definition_sha256} (Gamma Amendment 2 (1)); Codex's provisioner binds exactly this."""
    return {"version": VERSION, "definition_sha256": DEFINITION_SHA256}


def _slug(reason):
    return reason if reason in REASONS else "inline_error"


def _ro(path):
    p = Path(path)
    if not p.exists():
        return None
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=BUSY_TIMEOUT_S)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _corrected(fetched_at, offset):
    if not _num(fetched_at) or not _num(offset):
        return None
    return fetched_at - offset


def _inline_reason(con, mint, decision_at):
    """The ticket's own g12_inline_log reason (knob_off, daily_cap, ...) when the history rows are absent or unusable; None if no row. A log row WRITTEN AFTER the decision (ts - clock_offset_s > decision_at) is
    ignored; a usable row with no clock offset is clock_unknown, never an implicit 0."""
    try:
        rows = con.execute("select reason, ts, clock_offset_s from g12_inline_log where mint=? order by ts desc", (mint,)).fetchall()
    except sqlite3.Error:
        return None
    for reason, ts, offset in rows:
        corr = _corrected(ts, offset)
        if corr is None:
            return "clock_unknown"
        if corr <= decision_at:
            return _slug(reason) if reason else None
    return None


def _via(con, kind, key, fetched_at):
    """'nightly' | 'inline' from the sibling g12_via (absent table or row = a nightly row written before it existed)."""
    try:
        row = con.execute("select via from g12_via where kind=? and key=? and fetched_at=?", (kind, key, fetched_at)).fetchone()
    except sqlite3.OperationalError:
        return "nightly"
    return row[0] if row else "nightly"


def _coin_row(con, mint):
    row = con.execute("select state, reason, signature, slot, block_time, creator, payer, fetched_at, clock_offset_s from coin_creation where mint=?", (mint,)).fetchone()
    return None if row is None else row + (_via(con, "coin", mint, row[7]),)


def _unknown_age(reason):
    return {"state": "unknown", "reason": _slug(reason), "basis": BASIS, "age_s": None, "occurred_at": None, "known_at": None}


def _coin_usable(row, decision_at, pass_persist_at, sheet_as_of=None):
    """-> (ok, reason). The coin row must be a known creation witness fetched (corrected) at or before BOTH the PASS persist and the decision."""
    if row is None:
        return False, None
    state, reason, sig, slot, bt, creator, payer, fetched_at, offset, via = row
    if state != "known":
        return False, _slug(reason or "no_history")
    corr = _corrected(fetched_at, offset)
    if corr is None:
        return False, "clock_unknown"
    if corr > min(decision_at, pass_persist_at, *(() if sheet_as_of is None else (sheet_as_of,))):
        return False, "fetched_after_decision"
    return True, None


def coin_age_fact(desk_dir, mint, *, sheet_as_of, decision_at, pass_persist_at):
    """-> {coin: ages.coin dict, witness: {signature, slot, witness, source} | None}. age_s = sheet_as_of - the on-chain creation block_time (the creating tx, bound to this mint)."""
    unknown = lambda r: {"coin": _unknown_age(r), "witness": None}      # noqa: E731
    try:
        con = _ro(Path(desk_dir) / "backfill" / "history.db")
        if con is None:
            return unknown("not_attempted")
        try:
            row = _coin_row(con, mint)
            ok, why = _coin_usable(row, decision_at, pass_persist_at, sheet_as_of)
            if not ok:
                return unknown(why or _inline_reason(con, mint, decision_at) or "not_attempted")
            _, _, sig, slot, bt, creator, payer, fetched_at, offset, via = row
            if not _num(bt) or bt <= 0:
                return unknown("block_time_unusable")
            if bt > sheet_as_of:
                return unknown("creation_future")
            return {"coin": {"state": "known", "reason": None, "basis": BASIS, "age_s": sheet_as_of - bt, "occurred_at": bt, "known_at": _corrected(fetched_at, offset)},
                    "witness": {"signature": sig, "slot": slot, "witness": WITNESS, "source": HISTORY_SOURCE}}
        finally:
            con.close()
    except sqlite3.Error:
        return unknown("history_unreadable")


def _valid_scan(con, creator, coin_bt, decision_at, pass_persist_at):
    """-> (scan row | None, reason). Earliest complete/capped scan that ran >= 60 s after the coin and (corrected) at or before both limits."""
    rows = [r + (_via(con, "scan", creator, r[0]),) for r in
            con.execute("select fetched_at, state, pages, covered_slot, sort_order, clock_offset_s from creator_scan_log where creator=? order by fetched_at", (creator,)).fetchall()]
    if not rows:
        return None, "no_scan"
    usable = [r for r in rows if r[1] in ("complete", "capped")]
    if not usable:
        return None, "unknown_scan"
    clocked = [r for r in usable if _corrected(r[0], r[5]) is not None]
    if not clocked:
        return None, "clock_unknown"
    timely = [r for r in clocked if _corrected(r[0], r[5]) >= coin_bt + FINALITY_MARGIN_S]
    if not timely:
        return None, "scan_before_coin"
    ok = [r for r in timely if _corrected(r[0], r[5]) <= min(decision_at, pass_persist_at)]
    if not ok:
        return None, "rescan_after_decision"
    return ok[0], None


def _launches_creator(desk_dir, mint, sheet_as_of, host_offset_s):
    """-> (creator, dev, dev_source, why). The curve tape's launches row, newest days first, ONLY if its own receipt time is known by the sheet: received_at - host_offset_s <= sheet_as_of. A later row => why
    'future_source'; no sampled offset => 'clock_unknown'; no row => (None, None, None, None). The curve tape is retired: PS-only coins have no row."""
    import datetime
    for back in range(4):
        day = datetime.datetime.fromtimestamp(sheet_as_of - back * 86400, datetime.timezone.utc).date().isoformat()
        con = _ro(Path(desk_dir) / "launches" / (day + ".db"))
        if con is None:
            continue
        try:
            cols = {r[1] for r in con.execute("pragma table_info(launches)")}
            sel = "creator, dev, dev_source, received_at" if {"dev", "dev_source"} <= cols else "creator, NULL, NULL, received_at"
            row = con.execute("select %s from launches where mint=? limit 1" % sel, (mint,)).fetchone()
        except sqlite3.Error:
            continue
        finally:
            con.close()
        if row is not None:
            if not _num(host_offset_s) or not _num(row[3]):
                return None, None, None, "clock_unknown"
            if row[3] - host_offset_s > sheet_as_of:
                return None, None, None, "future_source"
            return row[0], row[1], row[2], None
    return None, None, None, None


def _swaps_ps_creator(desk_dir, mint, pool, market_cut):
    """-> (coin_creator, why). The PS tape's coin_creator of the pool's EARLIEST row that carries it, ONLY if that row was received at or before the market cut (host basis, the tape's own clock).
    A later row => 'future_source'; no market cut given => 'clock_unknown'."""
    if not pool:
        return None, None
    if not _num(market_cut):
        return None, "clock_unknown"
    con = _ro(Path(desk_dir) / "pumpswap" / "tape.db")
    if con is None:
        return None, None
    try:
        row = con.execute("select coin_creator, received_at from swaps_ps where pool=? and mint=? and coin_creator is not null and coin_creator != '' order by received_at limit 1", (pool, mint)).fetchone()
    except sqlite3.Error:
        return None, None
    finally:
        con.close()
    if not row:
        return None, None
    return (None, "future_source") if row[1] > market_cut else (row[0], None)


def _curve_creator(desk_dir, mint, pool, sheet_as_of, host_offset_s, market_cut):
    """-> (curve_creator, source, event_user, event_user_source, curve_why, user_why). launches row first (sheet-knowledge bound), else the PS tape's coin_creator (market-cut bound)."""
    creator, dev, dev_src, why = _launches_creator(desk_dir, mint, sheet_as_of, host_offset_s)
    if creator:
        return creator, "launches.creator", dev or None, ((dev_src or "create_event_user") if dev else None), None, (None if dev else "not_attempted")
    ps, ps_why = _swaps_ps_creator(desk_dir, mint, pool, market_cut)
    if ps:
        return ps, "swaps_ps.coin_creator", None, None, None, why or "not_attempted"
    return None, None, None, None, ps_why or why or "creator_unknown", why or "not_attempted"


def creator_history_fact(desk_dir, mint, *, sheet_as_of, decision_at, pass_persist_at, pool=None, market_cut=None, host_offset_s=None):
    """-> the creator_history block fields {state, reason, definition_sha256, as_of, creator_prior_launches_lb, dev_wallet, curve_creator, create_event_user, fetched_at, clock_offset_s, fetched_via,
    history_reason, dev_equals_creator, field_unknown}. `market_cut` = the tape's host receipt cut (swaps_ps rows later than it are future_source); `host_offset_s` = the sampled host-minus-corrected offset (launches rows
    have no stored offset: None => clock_unknown). Creation slot unknown or not a positive int => lb null, status unknown, slot_unusable (complete or capped), never 0, never an exception."""
    curve_val, curve_src, ev_user, ev_src, curve_why, user_why = _curve_creator(desk_dir, mint, pool, sheet_as_of, host_offset_s, market_cut)
    out = {"state": "unknown", "reason": None, "definition_sha256": DEFINITION_SHA256, "as_of": sheet_as_of,
           "creator_prior_launches_lb": {"lb": None, "status": "unknown", "pages": None, "source": CREATOR_SOURCE, "reason": None},
           "dev_wallet": {"value": None, "source": CREATOR_SOURCE}, "curve_creator": {"value": curve_val, "source": curve_src},
           "create_event_user": {"value": ev_user, "source": ev_src},
           "fetched_at": None, "clock_offset_s": None, "fetched_via": None, "history_reason": None, "dev_equals_creator": None, "field_unknown": {}}

    def finish():
        """state known iff a lower bound exists; every null field is named in field_unknown with a closed reason (Amendment 2)."""
        lbk = out["creator_prior_launches_lb"]["status"] in ("complete", "capped")
        out["state"], out["reason"] = ("known", None) if lbk else ("unknown", out["history_reason"])
        fu = {}
        if not lbk:
            fu["creator_prior_launches_lb"] = out["history_reason"]
        if out["dev_wallet"]["value"] is None:
            fu["dev_wallet"] = out["history_reason"] or "creator_unknown"
        if out["curve_creator"]["value"] is None:
            fu["curve_creator"] = _slug(curve_why or "creator_unknown")
        if out["create_event_user"]["value"] is None:
            fu["create_event_user"] = _slug(user_why or "not_attempted")      # null on rows from before the column, for every PS-only coin (curve tape retired), or a source row known too late
        out["field_unknown"] = fu
        return out

    def fail(reason):
        out["creator_prior_launches_lb"]["reason"] = out["history_reason"] = _slug(reason)
        return finish()
    try:
        con = _ro(Path(desk_dir) / "backfill" / "history.db")
        if con is None:
            return fail("not_attempted")
        try:
            row = _coin_row(con, mint)
            ok, why = _coin_usable(row, decision_at, pass_persist_at, sheet_as_of)
            if not ok:
                return fail(why or _inline_reason(con, mint, decision_at) or "not_attempted")
            _, _, sig, slot, bt, creator, payer, fetched_at, offset, via = row
            if not creator:
                return fail("creator_unknown")
            out["dev_wallet"]["value"] = creator
            if curve_val:
                out["dev_equals_creator"] = creator == curve_val
            if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0 or not _num(bt):
                return fail("slot_unusable")                                     # the 'prior' boundary is unknown: no count at all, complete or capped
            scan, why = _valid_scan(con, creator, bt, decision_at, pass_persist_at)
            if scan is None:
                return fail(_inline_reason(con, mint, decision_at) if why == "no_scan" and _inline_reason(con, mint, decision_at) else why)
            s_fetched, state, pages, covered, order, s_offset, s_via = scan
            lb = con.execute("select count(*) from creator_launches where creator=? and slot < ? and seen_at <= ?", (creator, slot, s_fetched)).fetchone()[0]
            complete = state == "complete" or (order == "asc" and isinstance(covered, int) and not isinstance(covered, bool) and slot <= covered)
            out["creator_prior_launches_lb"] = {"lb": lb, "status": "complete" if complete else "capped", "pages": pages, "source": CREATOR_SOURCE, "reason": None}
            out.update(fetched_at=s_fetched, clock_offset_s=s_offset, fetched_via=s_via, history_reason=None)
            return finish()
        finally:
            con.close()
    except sqlite3.Error:
        return fail("history_unreadable")


def dev_block(desk_dir, mint, *, sheet_as_of, decision_at, pass_persist_at, pool=None, market_cut=None, host_offset_s=None):
    """-> {curve_creator, dev, dev_equals_creator, dev_source, curve_creator_source}: the curve creator (launches.creator or swaps_ps.coin_creator, each bound by its own knowledge time) and the dev: launches.dev
    ('create_event_user') when the curve tape has the coin, else the history store's create-instruction argument under the leak rule. Two fields, two meanings, never inferred from each other. Absent side = None."""
    curve_val, curve_src, dev, dev_src, _w, _u = _curve_creator(desk_dir, mint, pool, sheet_as_of, host_offset_s, market_cut)
    if not dev:
        dev, dev_src = None, None
        try:
            con = _ro(Path(desk_dir) / "backfill" / "history.db")
            if con is not None:
                try:
                    row = _coin_row(con, mint)
                    ok, _ = _coin_usable(row, decision_at, pass_persist_at, sheet_as_of)
                    if ok and row[5]:
                        dev, dev_src = row[5], CREATOR_SOURCE
                finally:
                    con.close()
        except sqlite3.Error:
            pass
    return {"curve_creator": curve_val, "dev": dev, "dev_equals_creator": (dev == curve_val) if dev and curve_val else None, "dev_source": dev_src, "curve_creator_source": curve_src}
