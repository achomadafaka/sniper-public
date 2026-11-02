#!/usr/bin/env python3
"""Forward collector for the source-entry study: one row per (network_id, mint) FIRST source buy, finalised once the desk tape has covered tc + 1900 s + 60 s.
Read-only on the ledger (get_readonly_connection) and on the desk tape (mode=ro); writes only data/research/source-entry/live/. No provider calls, no outcome labels.
    nice -n 19 ionice -c3 python3 scripts/research/source_entry_live.py [--since ISO] [--limit 200]
Row: facts + (for in-support rows) raw compact swaps; the 32 Tier W features are computed offline from those swaps (parity-gated path), not here."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "source-entry-live-v1"
FEATS32 = ["age_s", "log_mcap", "ret_10", "ret_30", "ret_60", "ret_300", "noise5", "wal_10", "wal_30", "wal_60", "wal_180", "buy_10", "buy_30", "buy_60",
           "buy_180", "bshare_10", "bshare_60", "bshare_180", "net_10", "net_60", "swaps_10", "swaps_60", "maxsell2s_60", "drawdown_900", "since_high",
           "since_low", "med_buy", "p90_buy", "growth", "w60max300", "impact", "curve_fill"]
FEATS_HASH = hashlib.sha256(",".join(FEATS32).encode()).hexdigest()[:16]
SWAP_COLS = ["bt", "slot", "ordinal", "is_buy", "qa", "ta", "actor", "vq", "vb", "rt", "fee_bps", "cfee_bps", "sig", "recv", "clk", "quote_ok", "cashback_fee_bps", "cashback", "quote_mint"]   # clk 1 = bt from received_at (block_time NULL); quote_ok 0 = non-SOL quote curve (other unit); older day dbs lack quote_mint/cashback: read as SOL / NULL
PRE_S, FWD_S, READY_PAD_S, SILENCE_S, LOOKBACK_DAYS, TIMEOUT_S = 900, 1900, 60, 10.0, 2, 6 * 3600
INIT_Q, INIT_B, INIT_TOL, FX0, MCAP_MAX, AGE_MAX = 30e9, 1.073e15, 0.005, 104.329875, 40000.0, 1800.0
SOL_QUOTES = (None, "", "So11111111111111111111111111111111111111112", "1" * 32)
LEDGER = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
DESK = "/home/bigpapi/Desktop/Sniper/data/desk"
OUT = "/home/bigpapi/Desktop/Sniper/data/research/source-entry/live"
SINCE_DEFAULT = "2026-10-26T12:14:00Z"                       # tape + memecoin_events resume after the 10-24..10-26 host outage


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)


def iso_to_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def days_for(lo, hi):
    d, end, out = datetime.fromtimestamp(lo, timezone.utc).date(), datetime.fromtimestamp(hi, timezone.utc).date(), []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def tape_max_received(path):
    """Newest received_at in a day db via the slot index (never a day scan)."""
    con = ro(path)
    try:
        r = con.execute("select received_at from swaps where slot=(select max(slot) from swaps) order by received_at desc limit 1").fetchone()
    finally:
        con.close()
    return r[0] if r else None


def day_silences(path, state, prev_high=None):
    """Whole-tape gaps > 10 s in a day db, incremental by slot (state: last_slot, high, silences). Running max absorbs arrival jitter."""
    con = ro(path)
    try:
        last_slot, high, sil = state.get("last_slot", -1), state.get("high", prev_high), state.setdefault("silences", [])   # prev_high: the previous day's newest swap, so a gap over midnight shows
        for slot, recv in con.execute("select slot, received_at from swaps where slot > ? order by slot, received_at", (last_slot,)):
            if high is not None and recv - high > SILENCE_S:
                sil.append((high, recv))
            high = recv if high is None else max(high, recv)
            last_slot = slot
        state.update(last_slot=last_slot, high=high)
    finally:
        con.close()
    return sil


def coin_rows(desk, mint, days):
    rows, no_quote_col = [], []
    for day in days:
        p = "%s/swaps/%s.db" % (desk, day)
        if not os.path.exists(p):
            continue
        con = ro(p)
        try:
            cols = {r[1] for r in con.execute("pragma table_info(swaps)")}
            if "quote_mint" not in cols:
                no_quote_col.append(day)                           # older day db: SOL by construction, said in the row
            sel = ", ".join(c if c in cols else "NULL" for c in ("quote_mint", "cashback_fee_bps", "cashback"))
            rows += con.execute("select slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, fee_bps, "
                                "creator_fee_bps, signature, block_time, received_at, %s from swaps where mint=?" % sel, (mint,)).fetchall()
        finally:
            con.close()
    out = []
    for slot, ordinal, buy, qa, ta, actor, vq, vb, rt, fee, cfee, sig, bt, recv, qm, cbf, cb in sorted(rows, key=lambda r: (r[13], r[0], r[1])):
        out.append([bt if bt is not None else int(recv), slot, ordinal, int(buy), qa, ta, actor, vq, vb, rt, fee, cfee, sig, recv, int(bt is None),
                    int(qm in SOL_QUOTES), cbf, cb, qm])
    return out, no_quote_col


def gap_rows(desk, days):
    out = []
    for day in days:
        p = "%s/gaps/%s.db" % (desk, day)
        if os.path.exists(p):
            con = ro(p)
            try:
                out += [(at, cause) for at, cause, _s in con.execute("select at, cause, last_slot from gaps")]
            finally:
                con.close()
    return out


REASONS = ["non_solana", "not_on_tape", "nonstandard_start", "not_curve_at_tc", "out_of_support_age", "out_of_support_mcap", "tape_missing", "tape_silence"]


def finalise(ev, desk, now, sil_state, git_sha):
    """One finished row for first-buy event ev = dict(id, network_id, mint, created_at, clock_domain, source_user_id), or None while the tape has not
    reached tc+1960 and tc+6h has not passed. reason = first true flag in REASONS order (Gamma's fixed precedence), else ok; every flag is stored."""
    tc = iso_to_ts(ev["created_at"])
    lo, hi = tc - PRE_S, tc + FWD_S + READY_PAD_S
    flags = dict.fromkeys(REASONS, False)
    row = {"schema_version": SCHEMA_VERSION, "feats_hash": FEATS_HASH, "extractor_sha": git_sha, "swap_cols": SWAP_COLS, "network_id": ev["network_id"],
           "mint": ev["mint"], "event_id": ev["id"], "source_user_id": ev.get("source_user_id"), "created_at": ev["created_at"], "tc": tc,
           "clock_domain": ev.get("clock_domain"), "finalised_at": now, "pair_first": ev.get("pair_first", True), "in_support": False, "fwd_source": "tape", "fwd_complete": False}

    def done(reason_override=None):
        row["flags"] = flags
        row["reason"] = reason_override or next((r for r in REASONS if flags[r]), "ok")
        return row

    if ev["network_id"] != "solana":
        flags["non_solana"] = True
        return done()
    days = days_for(lo, hi)
    missing = [d for d in days if not os.path.exists("%s/swaps/%s.db" % (desk, d))]
    tape_short = False
    if not missing:
        newest = tape_max_received("%s/swaps/%s.db" % (desk, days[-1]))
        tape_short = newest is None or newest < hi
    if (missing or tape_short) and now < tc + TIMEOUT_S:
        return None                                              # not ready: wait for the tape (or the 6 h timeout)
    flags["tape_missing"] = bool(missing)
    row["missing_days"] = missing
    rows, no_quote_col = coin_rows(desk, ev["mint"], days_for(tc - LOOKBACK_DAYS * 86400, hi))
    row["quote_col_absent_days"] = no_quote_col
    fill = int(tc)
    pre = [r for r in rows if r[0] < fill]
    row["clock_recv_any"] = any(r[14] for r in pre)
    if not pre:
        if missing:
            flags["tape_missing"] = True                          # absence on a missing day is unknown, not "not on tape"
            return done()
        flags["not_on_tape"] = True
        return done()
    first, last = pre[0], pre[-1]
    pre_q = first[7] - first[4] if first[3] else first[7] + first[4]
    pre_b = first[8] + first[5] if first[3] else first[8] - first[5]
    row["init"] = [pre_q, pre_b, (first[9] + first[5] if first[3] else first[9] - first[5]) if first[9] is not None else None, first[3]]
    used = pre + [r for r in rows if fill < r[0] <= fill + FWD_S]
    flags["nonstandard_start"] = (not (abs(pre_q / INIT_Q - 1) <= INIT_TOL and abs(pre_b / INIT_B - 1) <= INIT_TOL)) or any(not r[15] for r in used)
    row["cashback_nonzero"] = any((r[16] or 0) or (r[17] or 0) for r in used)
    row.update(t0=first[0], last_state={"bt": last[0], "vq": last[7], "vb": last[8], "rt": last[9]}, age_s_tc=tc - first[0],
               mcap_usd_tc=last[7] / last[8] * 1e6 * FX0)
    row["curve_complete_at_tc"] = flags["not_curve_at_tc"] = last[9] is not None and last[9] <= 0
    flags["out_of_support_age"] = row["age_s_tc"] > AGE_MAX
    flags["out_of_support_mcap"] = row["mcap_usd_tc"] > MCAP_MAX
    gaps = [(a, c) for a, c in gap_rows(desk, days) if lo <= a <= hi]
    sils = []
    prev_high = None
    for d in days:
        if d not in missing:
            st = sil_state.setdefault(d, {})
            sils += [x for x in day_silences("%s/swaps/%s.db" % (desk, d), st, prev_high) if x[1] > lo and x[0] < hi]
            prev_high = st.get("high")
    flags["tape_silence"] = bool(gaps or sils or tape_short)
    row["silence_overlap"] = flags["tape_silence"]
    reason = done()["reason"]
    if reason in ("ok", "tape_missing", "tape_silence"):         # the three reasons that still carry a (possibly censored) path
        row.update(in_support=True, swaps_pre=pre, swaps_fwd=[r for r in rows if fill < r[0] <= fill + FWD_S],
                   fwd_complete=reason == "ok", silence_gaps=gaps, silences=sils)
    return row


ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
COLS = ("id", "network_id", "mint", "created_at", "clock_domain", "source_user_id")


def first_buys(conn, since_ts, selected=None):
    """Pair-first buys (first per (network_id, mint) across ALL stored history) and, when a frozen list is loaded, each SELECTED wallet's own first
    buy of a pair that is not already the pair-first (pair_first False). created_at compares as ISO text, so the format is asserted."""
    since = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    base = ("select id, network_id, mint, created_at, clock_domain, source_user_id from memecoin_events e where side='buy' and created_at >= ? and not exists "
            "(select 1 from memecoin_events p where p.mint=e.mint and p.network_id is e.network_id and p.side='buy' %s and "
            "(p.created_at < e.created_at or (p.created_at = e.created_at and p.id < e.id))) order by created_at")
    out = []
    for r in conn.execute(base % "", (since,)):
        ev = dict(zip(COLS, r), pair_first=True)
        if not ISO_RE.match(ev["created_at"]):
            raise ValueError("created_at format %r breaks the string ordering" % ev["created_at"])
        out.append(ev)
    if selected:
        pair_ids = {e["id"] for e in out}
        for r in conn.execute(base % "and p.source_user_id is e.source_user_id", (since,)):
            ev = dict(zip(COLS, r), pair_first=False)
            if ev["source_user_id"] in selected and ev["id"] not in pair_ids:
                if not ISO_RE.match(ev["created_at"]):
                    raise ValueError("created_at format %r breaks the string ordering" % ev["created_at"])
                out.append(ev)
    return out


def ev_key(ev):
    return (ev["network_id"], ev["mint"], None if ev.get("pair_first", True) else ev["source_user_id"])


def later_buys(conn, ev):
    tc = iso_to_ts(ev["created_at"])
    out = []
    for i, ca, uid in conn.execute("select id, created_at, source_user_id from memecoin_events where mint=? and network_id is ? and side='buy' and id != ?",
                                   (ev["mint"], ev["network_id"], ev["id"])):
        if 0 < iso_to_ts(ca) - tc <= 120:
            out.append({"event_id": i, "created_at": ca, "source_user_id": uid})
    return out


def done_keys(out_dir):
    done, first_ev = set(), {}
    for p in sorted(Path(out_dir).glob("rows-2*.jsonl")):
        for line in open(p):
            if line.strip():
                r = json.loads(line)
                k = (r["network_id"], r["mint"], None if r.get("pair_first", True) else r["source_user_id"])
                done.add(k)
                if r.get("pair_first", True):
                    first_ev[k] = r["event_id"]
    return done, first_ev


def collect(conn, desk=DESK, out_dir=OUT, now=None, since=SINCE_DEFAULT, limit=200, git_sha="unknown", selected=None, selected_sha=None):
    now = time.time() if now is None else now
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    done, first_ev = done_keys(out_dir)
    state_p = Path(out_dir) / "silence-state.json"
    sil_state = json.load(open(state_p)) if state_p.exists() else {}
    corr_p = Path(out_dir) / "rows-corrections.jsonl"
    corrected = {(r["network_id"], r["mint"], r["event_id"]) for r in map(json.loads, open(corr_p))} if corr_p.exists() else set()
    written, pending, changed = 0, 0, 0
    for ev in first_buys(conn, iso_to_ts(since), selected):
        key = ev_key(ev)
        if key in done:
            if ev["pair_first"] and first_ev.get(key) != ev["id"] and (key[0], key[1], ev["id"]) not in corrected:
                changed += 1                                      # a later-ingested earlier buy: append a correction, never rewrite
                with open(corr_p, "a") as f:
                    f.write(json.dumps({"kind": "first_changed", "network_id": key[0], "mint": key[1], "old_event_id": first_ev.get(key), "event_id": ev["id"],
                                        "created_at": ev["created_at"], "at": now}) + "\n")
            continue
        if written >= limit:
            break
        row = finalise(ev, desk, now, sil_state, git_sha)
        if row is None:
            pending += 1
            continue
        row["selected_wallet"] = None if selected is None else ev.get("source_user_id") in selected   # Gamma's frozen list; None = no list loaded
        row["selected_list_sha"] = selected_sha
        row["later_buys_120s"] = later_buys(conn, ev)            # defined by later data: an outcome-time field, never a feature
        day = datetime.fromtimestamp(row["tc"], timezone.utc).strftime("%Y-%m-%d")
        with open(Path(out_dir) / ("rows-%s.jsonl" % day), "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        written += 1
    state_p.write_text(json.dumps(sil_state))
    return {"written": written, "pending": pending, "first_changed": changed}


def load_selected(path):
    """Frozen wallet list: Gamma's JSON document {"wallets": [{"uid": ...}]} (or one uid per line). Returns (set of uids, sha256 prefix of the file)."""
    raw = Path(path).read_bytes()
    try:
        doc = json.loads(raw)
        uids = {w["uid"] for w in doc["wallets"]}
    except (ValueError, KeyError, TypeError):
        uids = {x.strip() for x in raw.decode().splitlines() if x.strip()}
    return uids, hashlib.sha256(raw).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--selected-file", help="one source_user_id per line (frozen wallet list); omit = selected_wallet null")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from sniper.persistence.db_core import get_readonly_connection
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or "unknown"
    conn = get_readonly_connection(LEDGER)
    try:
        sel, sel_sha = load_selected(a.selected_file) if a.selected_file else (None, None)
        stats = collect(conn, out_dir=a.out, since=a.since, limit=a.limit, git_sha=sha, selected=sel, selected_sha=sel_sha)
    finally:
        conn.close()
    print(datetime.now(timezone.utc).isoformat(), json.dumps(stats), flush=True)


if __name__ == "__main__":
    main()
