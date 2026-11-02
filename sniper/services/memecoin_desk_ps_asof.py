"""As-of-decision refresh of a PumpSwap U candidate's pool facts, from the LOCAL PS tape (zero calls).

Sibling of memecoin_desk_asof.py (curve). refresh() returns a new, valid `desk-candidate/pumpswap-v1` envelope (same ticket
identity; only `emitted_at` and `snapshot` change, hash via U.build_snapshot) plus an audit meta dict. The original candidate is
never edited. A fact that cannot be proven at the as-of time raises memecoin_desk_asof.StaleSnapshot(reason): the caller records a
typed no-call refusal that STAYS in the day's denominator. Never a default, never zero for unknown, never a prior-swap fallback.

Rules (Gamma's pinned recipe, 2026-10-22):
  receipt_cut = min(as_of, ps_committed_through); the finite, fresh commit watermark must not follow the heartbeat.
  cut_slot   = (max slot over blocks rows with received_at < receipt_cut) + 1; an input is usable iff slot < cut_slot AND
               received_at < receipt_cut (both clocks, strict). All tape queries share one read-only transaction. No block
               row, or a gap row between the cut block's receipt (or the tip swap's receipt) and as_of => unknown.
  reserves   from the LATEST usable swap of the pool (ordered slot, tx_index, ordinal); it must be a proven buy with post
               reserves. A later sell or v2 event (also of the same transaction), an unproven post state or a same-slot tx_index
               ambiguity refuses. reserve_known_at =
               that swap's received_at (never the as-of time, block time or map time). Age bound RESERVE_MAX_AGE_S.
  virtual    only the SAME event's proof: ix_name legacy buy / buy_exact_quote_in, payload virtual_quote_source == VQ_SOURCE,
               virtual_quote_atomic == str(virtual_quote_reserves), raw signed int in 0..u64. Clock = that event's received_at/slot. No pool-account fallback; v2 or anything else refuses.
  sol_usd    from the stamped heartbeat, age = stored age + heartbeat age, <= SOL_MAX_AGE_S.
  metadata   local metadata.db read, receipt strictly before as_of, metadata.slot < cut_slot;
               supply is supply_at_read (display only).
"""
from __future__ import annotations

import copy
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_ps_metadata as MD
from sniper.services import memecoin_desk_pumpswap as PS
from sniper.services import memecoin_desk_ps_losses as PSL
from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_universe as U
from sniper.services import memecoin_desk_safety as S
from sniper.services.memecoin_desk_asof import StaleSnapshot

RESERVE_MAX_AGE_S = 60.0
SOL_MAX_AGE_S = 120.0
HEARTBEAT_MAX_AGE_S = 5.0
U64_MAX = 2**64 - 1
SRC = "pumpswap_grpc"
VQ_SOURCE = "pumpswap_grpc:buy_event_virtual_quote"
LEGACY_BUYS = ("buy", "buy_exact_quote_in")
_COLS = ("slot, signature, ordinal, tx_index, is_buy, post_base_reserve_atomic, post_quote_reserve_atomic, lp_fee_bps, "
         "protocol_fee_bps, creator_fee_bps, received_at, map_known_at, map_status, mint, quote_mint, payload_json, "
         "cashback_fee_bps")


def _days(ts, back=1):
    day = datetime.fromtimestamp(ts, timezone.utc).date()
    return [day.fromordinal(day.toordinal() - i).isoformat() for i in range(back + 1)]


def _ro(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
    except sqlite3.Error:
        raise StaleSnapshot("tape_unreadable") from None


def _query(conn, sql, args):
    """Rows, or a typed refusal: a read error is unknown, never an empty result."""
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        raise StaleSnapshot("tape_unreadable") from None


def _attach_days(conn, desk_dir, now):
    schemas = {"blocks": [], "gaps": []}
    for folder in schemas:
        for i, day in enumerate(_days(now)):
            path = Path(desk_dir) / folder / ("%s.db" % day)
            if not path.exists():
                continue
            schema = "%s_%s" % (folder, i)
            _query(conn, "ATTACH DATABASE ? AS " + schema, (path.resolve().as_uri() + "?mode=ro",))
            schemas[folder].append(schema)
    return schemas


def _day_rows(conn, schemas, folder, sql, args):
    return [row for schema in schemas[folder]
            for row in _query(conn, sql.format(table=schema + "." + folder), args)]


def _state_indexes(conn):
    try:
        PSL._index(conn,'swaps_ps','swaps_ps_pool_slot',('pool','slot'))
        PSL._index(conn,'pools','pools_pool',('pool',))
    except PSL.LossUnreadable:
        raise StaleSnapshot('ps_tape_index_unknown') from None


def _heartbeat(desk_dir, now, sol_max_age_s, *, integrity=False):
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts = beat["ts"]
    except Exception:
        raise StaleSnapshot("tape_heartbeat_unreadable") from None
    if not K._number(ts, minimum=0) or ts > now or now - ts > HEARTBEAT_MAX_AGE_S or beat.get("connected") is not True:
        raise StaleSnapshot("tape_stale")
    if beat.get("pumpswap_capture") is not True:
        raise StaleSnapshot("ps_capture_off")
    pending = {}
    if integrity:
        pending = beat.get('ps_pending_gaps', {})
        if (not isinstance(pending, dict) or type(beat.get('ps_gap_pending')) is not bool
                or bool(pending) != beat['ps_gap_pending']):
            raise StaleSnapshot('ps_gap_pending')
    elif beat.get("ps_gap_pending") is not False:
        raise StaleSnapshot("ps_gap_pending")
    committed = beat.get("ps_committed_through")
    if (not K._number(committed, minimum=0) or committed > ts
            or now - committed > HEARTBEAT_MAX_AGE_S):
        raise StaleSnapshot("ps_commit_unproven")
    sol, age = beat.get("sol_usd"), beat.get("sol_age_s")
    sol_ok = K._number(sol, minimum=0) and sol > 0
    age_ok = K._number(age, minimum=0) and age + (now - ts) <= sol_max_age_s
    if not sol_ok or not age_ok:
        raise StaleSnapshot("sol_usd_stale")
    return float(sol), float(age) + (now - ts), now - ts, float(committed), pending


def _int_text(value):
    """A decimal TEXT atomic -> int, or None when absent / not canonical."""
    if (isinstance(value, str) and 1 <= len(value) <= 20 and value.isascii() and value.isdigit()
            and (value == "0" or value[0] != "0") and int(value) <= U64_MAX):
        return int(value)
    return None


def refresh(candidate, *, desk_dir, now, reserve_max_age_s=RESERVE_MAX_AGE_S, sol_max_age_s=SOL_MAX_AGE_S, intake_mint=None, integrity=False,
            mint_max_age_s=None, accept_curve_derived=False, rpc_state=None, resolve_zero_fees=False):
    """(refreshed_candidate, meta). `now` = the as-of epoch seconds. Raises StaleSnapshot(reason). The two age bounds default to
    the module constants; the caller passes the SAME values the cap/book use (one number for sheet and cap).
    intake_mint is the caller-bound hard-filter raw-mint receipt, never provider metadata.
    A later immutable receipt keeps its actual timestamp/slot; band refresh ignores it."""
    try:
        return _refresh(candidate, desk_dir, now, reserve_max_age_s, sol_max_age_s, intake_mint, integrity, mint_max_age_s,
                        accept_curve_derived, rpc_state, resolve_zero_fees)
    except (OverflowError, ValueError, OSError, sqlite3.Error, TypeError, KeyError) as exc:
        raise StaleSnapshot("unreadable:%s" % type(exc).__name__.lower()) from None   # never an uncaught crash in the call path


RPC_SOURCE = "pumpswap_rpc:pool_state"


def _rpc_values(state, cut_slot, as_of, max_age_s):
    """(base, quote, virtual_quote, read_at, slot) from one exact confirmed pool read, or StaleSnapshot('rpc_state_unusable').
    The read must be at or after the tape cut block, known by the as-of, and no older than the reserve bound."""
    try:
        base, quote, vq = state["base_reserve"], state["quote_reserve"], state["virtual_quote"]
        slot, at = state["slot"], state["read_at"]
    except (TypeError, KeyError):
        raise StaleSnapshot("rpc_state_unusable") from None
    ints = all(type(v) is int for v in (base, quote, vq, slot))
    if (not ints or base <= 0 or quote <= 0 or vq < 0 or max(base, quote, vq) > U64_MAX or slot < cut_slot
            or not K._number(at, minimum=0) or at > as_of or as_of - at > max_age_s):
        raise StaleSnapshot("rpc_state_unusable")
    return base, quote, vq, at, slot


def resolve_fees(conn, pool, slot, received, lp, proto, creator, max_age_s=RESERVE_MAX_AGE_S):
    """Paper and Office sheet fee basis -> (lp, protocol, creator, label). Fees are per transaction: a real event can carry 0/0/0 (0.23% of tape rows, 2026-10-31), and our order
    would not get that rate. An all-zero tip takes the pool's newest non-zero, zero-cashback fees within the reserve age bound; none found keeps the zero, labelled. Non-zero or unreadable fees pass through for the existing checks."""
    values = [None if v is None else _int_text(str(v)) for v in (lp, proto, creator)]
    if None in values or sum(values):
        return lp, proto, creator, "tip"
    row = conn.execute("SELECT lp_fee_bps,protocol_fee_bps,creator_fee_bps FROM swaps_ps WHERE pool=? AND slot<=? AND received_at>=? AND received_at<=? AND cashback_fee_bps='0' "
                       "AND CAST(lp_fee_bps AS INTEGER)+CAST(protocol_fee_bps AS INTEGER)+CAST(creator_fee_bps AS INTEGER)>0 ORDER BY slot DESC,tx_index DESC,ordinal DESC LIMIT 1",
                       (pool, slot, received - max_age_s, received)).fetchone()
    return (lp, proto, creator, "tip_zero_unresolved") if row is None else (*row, "newest_nonzero")


def _intake_facts(receipt, source, as_of, print_cut, max_age_s):
    if receipt is None:
        raise StaleSnapshot("intake_mint_missing")
    if not isinstance(receipt, dict) or receipt.get("authority_mint") != "closed":
        raise StaleSnapshot("intake_mint_mutable")
    if set(receipt) != {"supply_atomic", "decimals", "known_at", "context_slot", "authority_mint"}:
        raise StaleSnapshot("intake_mint_unusable")
    supply, decimals = _int_text(receipt["supply_atomic"]), receipt["decimals"]
    known, context = receipt["known_at"], receipt["context_slot"]
    if K._number(known, minimum=0) and known >= as_of:
        raise StaleSnapshot("intake_mint_future")
    if (supply is None or supply <= 0 or not K._number(decimals, integer=True, minimum=0, maximum=18)
            or not K._number(context, integer=True, minimum=1) or context < source["slot"]
            or not K._number(known, minimum=0) or not print_cut <= known < as_of
            or as_of-known > max_age_s):
        raise StaleSnapshot("intake_mint_unusable")
    if any(source.get(key) is not None and source[key] != value
           for key,value in (("supply_atomic",receipt["supply_atomic"]),("base_decimals",decimals))):
        raise StaleSnapshot("intake_mint_conflict")
    # shortcut: a later burn can understate historical mcap near the safety ceiling; no burn-path proof yet.
    return {"supply_atomic":receipt["supply_atomic"],"decimals":decimals,
            "metadata_known_at":known,"metadata_slot":context,"metadata_source":"intake_mint_read"}


def _refresh(candidate, desk_dir, now, reserve_max_age_s, sol_max_age_s, intake_mint, integrity, mint_max_age_s=None,
             accept_curve_derived=False, rpc_state=None, resolve_zero_fees=False):
    if not K._number(now, minimum=0):
        raise StaleSnapshot("clock_invalid")
    if any(not K._number(v, minimum=0) or v <= 0 for v in (reserve_max_age_s, sol_max_age_s)):
        raise StaleSnapshot("invalid_age_bound")
    if not isinstance(candidate, dict) or candidate.get("schema") != K.PUMPSWAP_SCHEMA:
        raise StaleSnapshot("not_a_ps_candidate")
    try:
        cand = K.validate_candidate(candidate)
    except ValueError:
        raise StaleSnapshot("candidate_invalid") from None
    source_state = cand["snapshot"]["fields"].get("pool_state")
    if source_state is None or source_state["value"] is None:
        raise StaleSnapshot("unmapped_pool")
    intake = "intake" in cand
    if source_state["completeness"] != "complete" and not (intake and source_state["completeness"] == "partial"):
        raise StaleSnapshot("pool_state_incomplete")
    if intake and any(source_state["value"].get(key) is None for key in K._POOL_STATE - {
            "supply_atomic","base_decimals","metadata_known_at","metadata_slot","metadata_source"}):
        raise StaleSnapshot("pool_state_incomplete")
    as_of_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    as_of = K._utc(as_of_iso)                                            # the exact value the envelope will carry
    emitted_at = cand["snapshot"]["emission_at"]
    intake_facts = _intake_facts(intake_mint, source_state["value"], as_of, emitted_at,
                                 sol_max_age_s if mint_max_age_s is None else mint_max_age_s) if intake else None
    if as_of < emitted_at:
        raise StaleSnapshot("clock_before_emission")
    sol_usd, sol_age, hb_age, committed_through, pending = _heartbeat(desk_dir, as_of, sol_max_age_s, integrity=integrity)
    receipt_cut = min(as_of, committed_through)
    if receipt_cut < emitted_at:
        raise StaleSnapshot("candidate_after_tape_cut")
    base = Path(desk_dir) / "pumpswap"
    tape = _ro(base / "tape.db")
    if tape is None:
        raise StaleSnapshot("ps_tape_missing")
    try:
        schemas = _attach_days(tape, desk_dir, as_of)
        _query(tape, "BEGIN", ())
        if integrity:
            _state_indexes(tape)
        blocks = _day_rows(tape, schemas, "blocks", "SELECT slot, received_at FROM {table} WHERE received_at < ? "
                           "ORDER BY slot DESC LIMIT 1", (receipt_cut,))
        if not blocks:
            raise StaleSnapshot("no_block_before_as_of")
        if any(not K._number(s, integer=True, minimum=1) or not K._number(at, minimum=0) for s, at in blocks):
            raise StaleSnapshot("tape_unreadable")
        cut_block = max(blocks)                                           # (slot, received_at) of the newest-slot block
        cut_slot = cut_block[0] + 1
        pool, mint = cand["pool"], cand["mint"]
        try:
            rows = PT.window_rows(tape, _COLS, pool, PT.retained_from(tape, pool, receipt_cut), receipt_cut,
                                  cut_slot=cut_slot, limit=1, order="slot DESC,tx_index DESC,ordinal DESC")
        except PT.TapeUnreadable:
            raise StaleSnapshot("tape_unreadable") from None
        if not rows:
            raise StaleSnapshot("no_swaps_before_cut")
        tip = rows[0]
        (slot, sig, ordinal, tx_index, is_buy, post_base, post_quote, lp, prot, crt, recv, map_known, map_status,
         row_mint, row_quote, payload_json, cashback) = tip
        ambiguous = _query(tape, "SELECT tx_index FROM swaps_ps WHERE pool=? AND slot=? AND received_at<? "
                           "GROUP BY tx_index HAVING tx_index IS NULL OR typeof(tx_index)!='integer' "
                           "OR tx_index<=0 OR COUNT(DISTINCT signature)>1 LIMIT 1", (pool, slot, receipt_cut))
        ambiguous += _query(tape, "SELECT signature FROM swaps_ps WHERE pool=? AND slot=? AND received_at<? "
                            "GROUP BY signature HAVING COUNT(DISTINCT tx_index)>1 LIMIT 1", (pool, slot, receipt_cut))
        if ambiguous:
            raise StaleSnapshot("same_slot_ambiguous")
        derived = intake and source_state["value"].get("map_source") == "intake_canonical_pda"
        if derived:
            if (pool != S.pumpswap_pool_address(mint)
                    or map_status != "unmapped" or row_mint is not None or row_quote is not None
                    or _query(tape,"SELECT 1 FROM pools WHERE pool=? AND known_at<? LIMIT 1",(pool,receipt_cut))):
                raise StaleSnapshot("unmapped_pool")
            map_known = recv
        curve = False
        if (not derived and accept_curve_derived and map_status == "mapped_curve_derived" and row_mint == mint
                and row_quote == K.WSOL_MINT and K._number(map_known, minimum=0) and map_known <= recv < receipt_cut):
            # Office sheet only: the Am19/Am23 graduation checks bind the pool; pricing still needs the buy-tip proof below.
            from sniper.services.memecoin_desk_ps_evidence import _curve_binding
            try:
                _curve_binding(tape, pool, mint, slot, recv)
            except ValueError as exc:
                raise StaleSnapshot(str(exc)) from None
            curve = True
        if not derived and not curve and (map_status != "mapped" or row_mint != mint or row_quote != K.WSOL_MINT or map_known is None
                or not K._number(map_known, minimum=0) or not map_known <= recv < receipt_cut):
            raise StaleSnapshot("unmapped_pool")
        maps = _query(tape, "SELECT mint,quote_mint,pool_creator,canonical,slot,signature,ordinal,tx_index,received_at,known_at "
                      "FROM pools WHERE pool=? AND slot<? AND received_at<? AND known_at<?" + (" LIMIT 20001" if integrity else ""),
                      (pool, cut_slot, receipt_cut, receipt_cut))
        if integrity and len(maps) > 20000:
            raise StaleSnapshot("pool_map_row_limit")
        if len({m[:4] for m in maps}) > 1:
            raise StaleSnapshot("pool_map_conflict")
        mapped = derived or curve
        for m in maps:
            if (m[0] != mint or m[1] != K.WSOL_MINT or m[3] != 1
                    or not all(K._number(v, minimum=0) for v in (m[8], m[9]))
                    or not m[9] <= map_known or not m[8] <= recv or not m[4] <= slot):
                continue
            ordered = m[4] < slot or (m[5] == sig and m[6] < ordinal) or (
                m[5] != sig and type(m[7]) is int and 0 < m[7] < tx_index)
            try:
                canonical = PS._canonical_pool({"base_mint": mint, "quote_mint": m[1], "creator": m[2],
                                                "pool": pool, "index": 0})
            except (TypeError, ValueError):
                canonical = False
            mapped |= ordered and canonical
        if not mapped:
            raise StaleSnapshot("unmapped_pool")
        if recv is None or recv >= receipt_cut:
            raise StaleSnapshot("reserve_unknown")
        # Office sheet fallback: an exact on-chain pool read replaces the tape tip's reserves + virtual quote when the tape cannot
        # prove price (latest swap a sell, vq unproven, reserve stale). Binding, map and gap checks above/below stay tape-based.
        rpc_used = False
        if not is_buy:
            if rpc_state is None:
                raise StaleSnapshot("latest_swap_not_buy")
            rpc_used = True
        base_res, quote_res = _int_text(post_base), _int_text(post_quote)
        if base_res is None or quote_res is None or base_res <= 0 or quote_res <= 0:
            raise StaleSnapshot("reserve_unknown")
        fee_basis = "tip"
        if resolve_zero_fees:
            lp, prot, crt, fee_basis = resolve_fees(tape, pool, slot, recv, lp, prot, crt, reserve_max_age_s)
        fees = [_int_text(str(v)) if v is not None else None for v in (lp, prot, crt)]
        if any(v is None for v in fees) or sum(fees) >= 10000 or cashback != "0":
            raise StaleSnapshot("fee_unknown")
        try:
            payload = json.loads(payload_json)
            raw = payload["virtual_quote_reserves"]
            proven = (payload.get("ix_name") in LEGACY_BUYS and payload.get("virtual_quote_source") == VQ_SOURCE
                      and type(raw) is int and 0 <= raw <= U64_MAX and payload.get("virtual_quote_atomic") == str(raw))
            vq = raw if proven else None
        except (TypeError, ValueError, KeyError, AttributeError):
            vq = None
        if vq is None:
            if rpc_state is None:
                raise StaleSnapshot("virtual_quote_unproven")
            rpc_used = True
        if as_of - recv > reserve_max_age_s:
            if rpc_state is None:
                raise StaleSnapshot("reserve_stale")
            rpc_used = True
        vq_source = VQ_SOURCE
        if rpc_used:
            base_res, quote_res, vq, recv, slot = _rpc_values(rpc_state, cut_slot, as_of, reserve_max_age_s)
            post_base, post_quote, vq_source = str(base_res), str(quote_res), RPC_SOURCE
            cut_slot = slot + 1        # the state is causal to the read's own slot (contract: state slot < cut_slot)
        def gaps_after(start, slot_cut):
            if integrity:
                try:
                    gaps = PSL.losses(desk_dir, pool, start, math.nextafter(as_of, math.inf),
                                      cut_slot=slot_cut, pending=pending, connection=tape)
                except PSL.LossUnreadable:
                    raise StaleSnapshot('ps_loss_unreadable') from None
            else:
                gaps = _query(tape, "SELECT 1 FROM gaps_ps WHERE at >= ? AND at <= ? LIMIT 1", (start, as_of))
            return gaps + _day_rows(tape, schemas, "gaps", "SELECT 1 FROM {table} WHERE at >= ? AND at <= ? LIMIT 1",
                                    (start, as_of))
        gaps = gaps_after(recv if rpc_used else min(cut_block[1], recv), cut_slot)
        if gaps and rpc_state is not None and not rpc_used:
            # Gap-only retries also need the supplied exact state, even with a healthy buy tip.
            base_res, quote_res, vq, recv, slot = _rpc_values(rpc_state, cut_slot, as_of, reserve_max_age_s)
            post_base, post_quote, vq_source = str(base_res), str(quote_res), RPC_SOURCE
            cut_slot = slot + 1
            gaps = gaps_after(recv, cut_slot)
        if gaps:
            failure = StaleSnapshot("tape_gap_since_cut")
            failure.pending_gaps = dict(pending)        # the Office sheet retries only while attribution is pending; a settled gap has none
            raise failure
    finally:
        tape.close()
    if intake:
        facts = intake_facts
    else:
        meta_db = base / "metadata.db"
        if not meta_db.exists():
            raise StaleSnapshot("metadata_not_yet_known")
        try:
            mconn = _ro(meta_db)
        except StaleSnapshot:
            raise StaleSnapshot("metadata_unreadable") from None
        if mconn is None:
            raise StaleSnapshot("metadata_not_yet_known")
        try:
            facts = MD.read_metadata(mconn, mint, as_of=as_of)
        except sqlite3.Error:
            raise StaleSnapshot("metadata_unreadable") from None
        finally:
            mconn.close()
        if facts is None:
            raise StaleSnapshot("metadata_not_yet_known")
        if (not K._number(facts["metadata_slot"], integer=True, minimum=1) or facts["metadata_slot"] >= cut_slot
                or not K._number(facts["metadata_known_at"], minimum=0) or facts["metadata_known_at"] >= as_of):
            raise StaleSnapshot("metadata_not_yet_known")
    decimals, supply = facts["decimals"], _int_text(facts["supply_atomic"])
    if not K._number(decimals, integer=True, minimum=0, maximum=18) or supply is None or supply <= 0:
        raise StaleSnapshot("metadata_unusable")
    try:
        price_sol = (quote_res + vq) / 1e9 / (base_res / 10**decimals)
        mcap = price_sol * (supply / 10**decimals) * sol_usd
        liq = quote_res / 1e9 * sol_usd
    except (OverflowError, ZeroDivisionError):
        raise StaleSnapshot("price_unreadable") from None
    if not all(math.isfinite(v) and v > 0 for v in (mcap, liq)):
        raise StaleSnapshot("price_unreadable")

    fields0 = cand["snapshot"]["fields"]
    state = dict(fields0["pool_state"]["value"] or {})
    state.update({"base_reserve_atomic": post_base, "quote_reserve_atomic": post_quote, "quote_mint": K.WSOL_MINT,
                  "base_decimals": decimals, "supply_atomic": facts["supply_atomic"], "virtual_quote_atomic": str(vq),
                  "lp_fee_bps": fees[0], "protocol_fee_bps": fees[1], "creator_fee_bps": fees[2], "slot": slot,
                  "map_known_at": map_known, "metadata_known_at": facts["metadata_known_at"],
                  "metadata_slot": facts["metadata_slot"], "metadata_source": facts["metadata_source"],
                  "reserve_known_at": recv, "sol_usd": sol_usd, "sol_age_s": sol_age,
                  "virtual_quote_known_at": recv, "virtual_quote_slot": slot, "virtual_quote_source": vq_source,
                  "cut_slot": cut_slot})
    if fee_basis != "tip":
        state["fee_basis"] = fee_basis
    fields = {}
    for name, row in fields0.items():
        fields[name] = U.Field(row["value"], row["source"], emitted_at if row["known_at"] is None else row["known_at"],
                          row["slot"], row["completeness"])
    for name, value in (("pool_state", state), ("mcap_usd", round(mcap, 2)), ("liq_usd", round(liq, 2))):
        fields[name] = U.Field(value, SRC + ":pool_reserves", as_of, slot, "complete")
    snap = U.build_snapshot(emission_at=as_of, fields=fields)
    out = {k: copy.deepcopy(v) for k, v in cand.items() if k not in ("snapshot", "emitted_at")}
    out["emitted_at"], out["snapshot"] = as_of_iso, snap
    try:
        K.validate_candidate(out)
    except ValueError as exc:                                            # a bug in this module or an old contract
        raise StaleSnapshot("refresh_invalid:%s" % exc) from None
    meta = {"as_of": as_of, "emission_at": emitted_at, "lag_s": round(as_of - emitted_at, 3),
            "emission_snapshot_hash": cand["snapshot"]["hash"], "snapshot_hash": snap["hash"],
            "receipt_cut": receipt_cut, "committed_through": committed_through,
            "receipt_lag_s": round(as_of - receipt_cut, 3),
            "cut_slot": cut_slot, "tip": {"signature": sig, "slot": slot, "ordinal": ordinal, "tx_index": tx_index,
                                           "received_at": recv},
            "reserve_age_s": round(as_of - recv, 3), "sol_usd": sol_usd, "sol_age_s": round(sol_age, 1),
            "tape_heartbeat_age_s": round(hb_age, 3), "supply_label": "supply_at_read",
            "metadata_known_at": facts["metadata_known_at"], "version": "psasof-v2" if integrity else "psasof-v1"}
    return out, meta
