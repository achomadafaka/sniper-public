"""Wallet Map: herders, their herds and who copies them (spec
docs/superpowers/specs/2026-09-30-memecoin-wallet-map-overhaul-design.md, Gamma CLEAN d16c454df;
pre-registration docs/audits/2026-09-30-wallet-map-reads-preregistration.md).

Read-only: the graph DB's slot reads and a few ledger rows become the herders table. No chain,
price or funded-seam call. The herd rules (unit_reads, herds, regulars, probes) live here so the
page and Gamma's frozen lists call the same functions.
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

REG_K, REG_SHARE, REG_N = 3, 0.10, 10    # a regular: k >= 3 of the herder's n read coins, k/n >= 10%, n >= 10
PROBE_HERDS = 2
FEW_READS = 20                           # under this many read coins the row says "few reads"
UNDERCOUNTED = 0.80                      # under this share of first buys read the herd is "undercounted"
STALE_AFTER_S = 3 * 3600                 # the graph job runs hourly
LINK_TYPES = ("follows", "dumps_on", "shared_funder", "co_buy_slot", "slot_follower")
LEFT_OUT = ("truncated", "not_found", "no_slot")    # final unit reads out of k and n; not_first is still pending



def _parse(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _addr(node_id):
    """Chain address of a node id: strips the pump: profile prefix; EVM lowercased."""
    s = str(node_id or "").strip()
    s = s[5:] if s.startswith("pump:") else s
    return s.lower() if s.lower().startswith("0x") else s


def own_address_set(raw):
    return {_addr(a) for a in str(raw or "").split(",") if a.strip()}


def open_graph_readonly(path):
    """None when the file is absent (graph_unavailable); a read-only connection otherwise."""
    p = Path(path)
    if not p.is_file():
        return None
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=5)


# ---------------------------------------------------------------- the herd rules
def herds(reads):
    """{herder: (n, {member: k})} from `ok` slot reads that are not truncated. n = distinct read
    coins; k = distinct coins on which the member bought later in the herder's slot. Pass the reads
    `unit_reads` chose (one per pair)."""
    coins = collections.defaultdict(set)
    behind = collections.defaultdict(lambda: collections.defaultdict(set))
    for r in reads:
        if r.get("status") != "ok" or r.get("truncated") or r.get("network_id", "solana") != "solana":
            continue
        uid, mint, idx = r["source_user_id"], r["mint"], r.get("index")
        coins[uid].add(mint)
        for c in r.get("companions") or []:
            if idx is not None and c.get("index") is not None and c["index"] > idx and c["trader"] != uid:
                behind[uid][c["trader"]].add(mint)
    return {h: (len(cs), {m: len(v) for m, v in behind[h].items()}) for h, cs in coins.items()}


def _t(r):
    return _parse(r["detected_at"]).timestamp()


def unit_reads(reads, first_sigs):
    """(reads, details): ONE read per (herder, coin), the one that stands for the pair's first buy
    (pre-registration A). `first_sigs` = {(herder, coin): (first-buy signature or None, its time)}
    from the graph job's coverage_units over the job's window; `reads` = ALL of the graph's reads.

    The unit read is the pair's earliest final read at or before the window's first buy, else the
    read of its first-buy transaction. That covers a coverage ('j7') read, whose time IS the unit's
    buy time, and a read first buy that left the window before the pair rebought inside it: the same
    test coverage_pending uses to hold the pair (Gamma CLEAN 4fb73b112). A read of the rebuy never
    stands in when an earlier buy was read. A pair whose final reads are all of later buys is
    `not_first`; a unit read that is truncated, not_found, no_slot or no_signature is out of k and n.
    Each is counted per herder in `details`, never 0."""
    by_pair = collections.defaultdict(list)
    for r in reads:
        by_pair[(r["source_user_id"], r["mint"])].append(r)
    out, details = [], collections.defaultdict(collections.Counter)
    for (h, mint), (sig, t) in first_sigs.items():
        rs = [r for r in by_pair.get((h, mint), []) if r["status"] != "unread"]
        early = sorted((r for r in rs if r.get("detected_at") and _t(r) <= t), key=_t)
        unit = (early[0] if early else None) or next((r for r in rs if sig and r["signature"] == sig), None)
        if unit is None:
            details[h]["no_signature" if sig is None else ("not_first" if rs else "unread")] += 1
        elif unit["status"] != "ok":
            details[h][unit["status"]] += 1
        elif unit.get("truncated"):
            details[h]["truncated"] += 1
        else:
            out.append(unit)
            details[h]["read"] += 1
    return out, {h: dict(c) for h, c in details.items()}


def regulars(reads):
    """{herder: {member: k}}: k >= REG_K and k/n >= REG_SHARE; no regular under REG_N read coins."""
    out = {}
    for h, (n, members) in herds(reads).items():
        if n >= REG_N:
            out[h] = {m: k for m, k in members.items() if k >= REG_K and k / n >= REG_SHARE}
    return out


def probes(reads, frozen, *, excluded=()):
    """{bot: {herder: k}}: bots that are regulars of PROBE_HERDS or more `frozen` herders (all of
    them on the page; Gamma's frozen list for the trail). `excluded` holds our roster's ids and our
    own addresses: they are never probes."""
    by_bot = collections.defaultdict(dict)
    for h, members in regulars(reads).items():
        if h in frozen:
            for m, k in members.items():
                if m not in excluded:
                    by_bot[m][h] = k
    return {b: hs for b, hs in by_bot.items() if len(hs) >= PROBE_HERDS}


# ---------------------------------------------------------------- stored rows, read only
def read_slot_reads(graph, alias):
    """Every stored slot read, of any window, from a READ-ONLY graph connection. A companion whose
    address the registry proves is named by its source_user_id. A table made before the coverage
    intake has no `intake`: None."""
    cols = {r[1] for r in graph.execute("PRAGMA table_info(memecoin_wallet_slot_reads)")}
    rows = graph.execute("SELECT signature, source_user_id, network_id, mint, detected_at, status, idx, "
                         "companions, truncated, undecoded, %s, slot FROM memecoin_wallet_slot_reads"
                         % ("intake" if "intake" in cols else "NULL")).fetchall()
    out = []
    for sig, uid, net, mint, at, status, idx, comp, trunc, undecoded, intake, slot in rows:
        companions = [dict(c, trader=alias.get(_addr(c.get("trader")), c.get("trader")), address=c.get("trader"))
                      for c in json.loads(comp or "[]")]
        out.append({"signature": sig, "source_user_id": uid, "network_id": net, "mint": mint, "detected_at": at,
                    "status": status, "index": idx, "companions": companions, "truncated": bool(trunc),
                    "undecoded": undecoded, "intake": intake, "slot": slot})
    return out


def job_window(graph):
    """(window_from, finished_at) of the graph job's last finished run, or (None, None)."""
    run = graph.execute("SELECT window_from, finished_at FROM memecoin_wallet_graph_runs "
                        "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1").fetchone()
    return run if run else (None, None)


def first_sigs_for(ledger, since_iso):
    """{(wallet, coin): (first-buy signature or None, time)} over the job's own window, by the job's
    own coverage_units (Gamma: the page and the freeze build first_sigs the same way)."""
    from sniper.services.memecoin_wallet_coverage import coverage_records, coverage_units
    units, no_signature = coverage_units(coverage_records(ledger, since_iso))
    out = {(u["source_user_id"], u["mint"]): (u["signature"], u["t"]) for u in units}
    out.update({(u["source_user_id"], u["mint"]): (None, u["t"]) for u in no_signature})
    return out


def follow_states(ledger, wallet_ids):
    """{wallet: {state, last}}: "Real money on Solana" / "Paper only" / "Paused" / "Excluded" /
    "Not watched", and the newest Solana arm-or-revoke row of the approvals ledger. A copy PnL is
    kept only with closes behind it: 0 over 0 closes is unknown, not a result."""
    rows = {r[0]: r for r in ledger.execute(
        "SELECT source_user_id, real_approved, real_chains, paused, excluded FROM memecoin_wallets")}
    out = {}
    for w in wallet_ids:
        r = rows.get(w)
        if r is None:
            state = "Not watched"
        elif r[4]:
            state = "Excluded"
        elif r[3]:
            state = "Paused"
        elif r[1] and "solana" in {c.strip() for c in str(r[2] or "").split(",")}:
            state = "Real money on Solana"
        else:
            state = "Paper only"
        last = ledger.execute(
            "SELECT created_at, action, actor, reason, copy_pnl, copy_closes FROM memecoin_wallet_approvals "
            "WHERE source_user_id=? AND chain='solana' AND action IN "
            "('chain_arm','chain_revoke','approve','revoke','retire','reinstate') ORDER BY id DESC LIMIT 1",
            (w,)).fetchone() if r is not None else None
        out[w] = {"state": state, "last": None if last is None else {
            "at": last[0], "action": last[1], "actor": last[2], "reason": (last[3] or "")[:140],
            "copy_pnl": last[4] if last[5] else None, "copy_closes": last[5] or None}}
    return out


def impact_rows(graph):
    """{wallet: slice-3 row} of the latest impact run, or None when the job has not run slice 3 (no
    table). An unreadable table raises."""
    try:
        cols = ("source_user_id", "n", "median_delta_sol", "verdict", "k", "k_needed", "computed_at")
        rows = graph.execute(
            "SELECT %s FROM memecoin_wallet_impact WHERE network_id='solana' AND computed_at = "
            "(SELECT MAX(computed_at) FROM memecoin_wallet_impact WHERE network_id='solana')"
            % ", ".join(cols)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    return {r[0]: dict(zip(cols[1:], r[1:])) for r in rows}


# ---------------------------------------------------------------- the payload
def _state(graph, ledger, *, now_iso, own_addresses, first_sigs):
    """Everything both views read: the stamp, the unit reads and the herds, from the graph job's own
    window. On a named error only `stamp` is filled."""
    stamp = {"chain": "solana", "error": None, "graph_state": "unavailable", "window_from": None,
             "window_to": None, "job_window_from": None,
             "computed_at": None, "stale": None, "units": 0, "read": 0, "uuid_only": 0, "left_out": 0,
             "not_read_yet": 0, "wallets_unread": 0, "flow": None}
    st = {"stamp": stamp}
    if graph is None:
        stamp["error"] = "graph_unavailable"
        return st
    alias = {_addr(a): uid for a, uid in ledger.execute(
        "SELECT address, source_user_id FROM memecoin_address_registry WHERE network_id='solana' "
        "AND status='verified'")}
    handles = {r[0]: r[1] for r in ledger.execute("SELECT source_user_id, handle FROM memecoin_wallets")}
    try:
        window_from, finished = job_window(graph)
        reads = read_slot_reads(graph, alias)
        edges = graph.execute("SELECT edge_type, src_user_id, dst_user_id, n_coins, lag_p50_s FROM "
                              "memecoin_wallet_edges WHERE network_id='solana' AND src_user_id IS NOT NULL "
                              "AND dst_user_id IS NOT NULL").fetchall()
    except sqlite3.Error:
        stamp["error"] = "graph_unreadable"
        return st
    stamp["job_window_from"], stamp["computed_at"] = window_from, finished
    stamp["stale"] = finished is None or (_parse(now_iso) - _parse(finished)).total_seconds() > STALE_AFTER_S
    if window_from is None:
        stamp["error"] = "no_finished_run"
        return st
    if first_sigs is None:
        first_sigs = first_sigs_for(ledger, window_from)
    for r in reads:                                  # our own wallets are never a member (spec rule 3)
        r["companions"] = [c for c in r["companions"] if _addr(c.get("trader")) not in own_addresses]
    chosen, details = unit_reads(reads, first_sigs)
    herd = herds(chosen)
    reg = regulars(chosen)
    units = collections.Counter(h for h, _m in first_sigs)
    times = sorted(r["detected_at"] for r in chosen)
    stamp.update(window_from=times[0] if times else None, window_to=times[-1] if times else None,
                 units=sum(units.values()), read=len(chosen),
                 uuid_only=sum(d.get("no_signature", 0) for d in details.values()))
    stamp["left_out"] = sum(d.get(k, 0) for d in details.values() for k in LEFT_OUT)
    stamp["not_read_yet"] = stamp["units"] - stamp["read"] - stamp["uuid_only"] - stamp["left_out"]
    stamp["wallets_unread"] = sum(1 for h in units if h not in herd)
    try:
        flow = impact_rows(graph)
    except sqlite3.Error:
        flow, stamp["flow"] = None, "unreadable"
    stamp["flow"] = stamp["flow"] or ("not_run" if flow is None else "ok")
    stamp["graph_state"] = "stale" if stamp["stale"] else "ok"
    st.update(chosen=chosen, details=details, herd=herd, reg=reg, units=units, handles=handles, edges=edges,
              flow=flow, window_from=window_from)
    return st


def build_herders(graph, ledger, *, now_iso, own_addresses, first_sigs=None):
    """{stamp, herders, shared, probes} for Solana. `graph` None = the file is absent."""
    return _herders_payload(_state(graph, ledger, now_iso=now_iso, own_addresses=own_addresses,
                                   first_sigs=first_sigs), ledger)


def _herders_payload(st, ledger):
    out = {"stamp": st["stamp"], "herders": [], "shared": [], "probes": []}
    if st["stamp"]["error"]:
        return out
    chosen, details, herd, reg = st["chosen"], st["details"], st["herd"], st["reg"]
    units, handles, edges, flow = st["units"], st["handles"], st["edges"], st["flow"]
    herders = sorted(herd)
    follow = follow_states(ledger, herders)
    probe = probes(chosen, set(herders), excluded=set(handles))
    shared = collections.Counter()
    by_member = collections.defaultdict(set)
    for h, members in reg.items():
        for m in members:
            by_member[m].add(h)
    for m, hs in by_member.items():
        hs = sorted(hs)
        for i in range(len(hs)):
            for j in range(i + 1, len(hs)):
                shared[(hs[i], hs[j])] += 1
    drew = collections.Counter()
    entry_order = collections.defaultdict(list)
    for r in chosen:
        # One first-buy coin per unit read. Count each decoded peer's first buy once;
        # absent source order or no other indexed buyer is unknown, never early-zero.
        idx = r.get("index")
        peers = {}
        for c in r["companions"]:
            if c["trader"] != r["source_user_id"] and c.get("index") is not None:
                peers[c["trader"]] = min(peers.get(c["trader"], c["index"]), c["index"])
        if idx is not None and peers:
            entry_order[r["source_user_id"]].append(sum(i < idx for i in peers.values()) / len(peers))
        if any(c.get("index") is not None and r.get("index") is not None and c["index"] > r["index"]
               and c["trader"] != r["source_user_id"] for c in r["companions"]):
            drew[r["source_user_id"]] += 1
    hset = set(herders)
    links = collections.defaultdict(list)
    for etype, su, du, n, lag in edges:
        if etype in LINK_TYPES and su in hset and du in hset and su != du:
            for a, b, role in ((su, du, "src"), (du, su, "dst")):
                links[a].append({"type": etype, "other": b, "other_handle": handles.get(b), "role": role,
                                 "n_coins": n, "lag_p50_s": lag})
    for h in herders:
        n, members = herd[h]
        d = details.get(h, {})
        read = d.get("read", 0)
        partners = sorted(((c, b if a == h else a) for (a, b), c in shared.items() if h in (a, b)),
                          key=lambda x: (-x[0], x[1]))
        out["herders"].append({
            "id": h, "handle": handles.get(h), "follow": follow[h],
            "units": units[h], "read": read, "uuid_only": d.get("no_signature", 0),
            "left_out": sum(d.get(k, 0) for k in LEFT_OUT),
            "coverage": read / units[h] if units[h] else None,
            "undercounted": bool(units[h]) and read / units[h] < UNDERCOUNTED,
            "n": n, "few_reads": n < FEW_READS,
            "regulars": len(reg[h]) if h in reg else None,
            "regular_list": sorted(({"id": m, "handle": handles.get(m), "k": k} for m, k in reg.get(h, {}).items()),
                                   key=lambda x: (-x["k"], x["id"])),
            "drew": drew[h], "members": len(members),
            "entry_order": {"n": len(entry_order[h]),
                            "share_before": statistics.median(entry_order[h]) if entry_order[h] else None,
                            "scope": "decoded buyers in the same sampled first-buy slot"},
            "out_of_n": {k: v for k, v in d.items() if k not in ("read", "unread", "no_signature")},
            "shares": [{"id": o, "handle": handles.get(o), "count": c} for c, o in partners[:3]],
            "links": links.get(h, []),
            "flow": None if flow is None else flow.get(h),
        })
    out["herders"].sort(key=lambda r: (r["regulars"] is None, -(r["regulars"] or 0),
                                       -(r["drew"] / r["n"] if r["n"] else 0), r["id"]))
    out["shared"] = [{"a": a, "a_handle": handles.get(a), "b": b, "b_handle": handles.get(b), "count": c}
                     for (a, b), c in sorted(shared.items(), key=lambda x: (-x[1], x[0])) if c >= 2]
    out["probes"] = sorted(({"id": b, "handle": handles.get(b),
                             "herds": sorted(({"id": h, "handle": handles.get(h), "k": k, "n": herd[h][0]}
                                              for h, k in hs.items()), key=lambda x: (-x["k"], x["id"]))}
                            for b, hs in probe.items()), key=lambda x: (-len(x["herds"]), x["id"]))
    return out


# ---------------------------------------------------------------- one herder
HOUR_BLOCKS = ("00-06", "06-12", "12-18", "18-24")
COINS_MAX = 200                          # the wallet view's newest coins; the rest are counted


def _spread(values):
    """{n, median, p25, p75} of the known values; an unknown is left out, never a 0."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "p25": None, "p75": None}
    q = statistics.quantiles(vals, n=4, method="inclusive") if len(vals) > 1 else [vals[0]] * 3
    return {"n": len(vals), "median": statistics.median(vals), "p25": q[0], "p75": q[2]}


def patterns(ledger, graph, wallet, since_iso):
    """Spec section 3, stored rows only: over the wallet's first relay-reported buy of each coin in the
    window, the market cap and size at entry (0 or less is unknown: 52 of DegenCapitalLLC's 128 relay buys
    in 14 days carried a market cap of 0), the UTC hour block and the delivery lane; and the coin's
    age at the wallet's buy from slice 3's age probe only (our paper entry's age is a later clock and
    is left out). Descriptive; nothing is tested. `age_s` is None until slice 3 has a reads table."""
    first = {}
    for mint, at, mcap, usd, lane in ledger.execute(
            "SELECT mint, created_at, their_market_cap, their_size_usd, delivery_lane FROM memecoin_events "
            "WHERE source_user_id=? AND created_at >= ? AND network_id='solana' AND side='buy' AND mint IS NOT NULL",
            (wallet, str(since_iso)[:19])):
        t = _parse(at)
        if mint not in first or t < first[mint][0]:
            first[mint] = (t, mcap, usd, lane)
    hours = collections.Counter(HOUR_BLOCKS[t.hour // 6] for t, _m, _u, _l in first.values())
    try:
        ages = _spread(r[0] for r in graph.execute(
            "SELECT age_s FROM memecoin_wallet_impact_reads WHERE source_user_id=? AND network_id='solana'",
            (wallet,)))
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        ages = None
    known = lambda x: x if x is not None and x > 0 else None      # noqa: E731  the relay sends 0 for "no market cap"
    return {"first_buys": len(first), "mcap_usd": _spread(known(v[1]) for v in first.values()),
            "size_usd": _spread(known(v[2]) for v in first.values()),
            "hours_utc": {b: hours[b] for b in HOUR_BLOCKS},
            "lanes": dict(collections.Counter(v[3] or "unknown" for v in first.values())), "age_s": ages}


FUNDED_SCOPE = ("funded robot entries on this chain (execution_mode='real', grade exclusions applied); "
                "only wallets we armed have rows")


def _funded_position(p):
    """A funded row for the page. Return % is realized / entry notional on a CLOSED row with a positive
    notional; an open row, a missing PnL or a 0/NULL notional is unknown, never a division."""
    closed = not p["is_open"]
    notional, pnl = p["entry_notional"], p["realized_pnl"]
    known = closed and pnl is not None and notional is not None and notional > 0
    return {"position_id": p["id"], "opened_at": p["opened_at"], "closed_at": p["closed_at"],
            "entry_notional": notional, "entry_lag_s": p["entry_lag_s"], "fill_gap_pct": p["fill_gap_pct"],
            "total_copy_drift_pct": p["total_copy_drift_pct"], "realized_pnl": pnl,
            "return_pct": pnl / notional * 100.0 if known else None, "exit_reason": p["exit_reason"],
            "is_open": not closed}


_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")     # a base58 Solana transaction signature


def _seconds_between(earlier, later):
    """later - earlier in seconds, or None when either time is missing or unparseable (Gamma LOW on
    D3: one malformed stored timestamp must not fail the whole view)."""
    try:
        return (_parse(later) - _parse(earlier)).total_seconds() if earlier and later else None
    except (TypeError, ValueError):
        return None


def _tokens(origin):
    return str(origin or "").split(":")


def _event_linkage(sig, origin, attempt_origin):
    """How a funded position relates to one source buy: `origin_event_id` / `attempt_id` when that id
    carries `sig` as a whole ':' token; `other_event` when the first id that carries any signature
    carries another one; `unknown` when neither id carries a signature (NULL, act-<uuid>, a hex id)."""
    for name, value in (("origin_event_id", origin), ("attempt_id", attempt_origin)):
        tokens = _tokens(value)
        if sig and sig in tokens:
            return name
        if any(_SIGNATURE.match(t) for t in tokens):
            return "other_event"
    return "unknown"


def _attach_source_times(out, ledger, wallet):
    """S2 (Codex review 2026-10-07): each coin's source buy carries its CHAIN time (`source_at`, from the
    stored on-chain detection's block_time) beside our `detected_at`, and `source_time_basis` says which
    one exists. A buy with no stored block time is "unknown", never the detection time; an unreadable
    ledger is "unavailable". `source.at` stays the detection time, as before."""
    from sniper.persistence import db_memecoin as m
    try:
        chain = m.detection_block_times(ledger, wallet, [c["mint"] for c in out["coins"]])
        basis_missing = "unknown"
    except (sqlite3.Error, ValueError):
        chain, basis_missing = {}, "unavailable"
    for c in out["coins"]:
        src = c["source"]
        bt = chain.get(src["signature"])
        src["detected_at"] = src["at"]
        src["source_at"] = (datetime.fromtimestamp(int(bt), timezone.utc).isoformat()
                            if isinstance(bt, (int, float)) and bt > 0 else None)
        src["source_time_basis"] = "chain_block_time" if src["source_at"] else basis_missing


def _attach_funded(out, ledger, wallet):
    """D3: our funded results on each supporting coin and the wallet's funded block. `entered` = a
    funded position of THIS source event (see _event_linkage); other positions on the same wallet and
    coin sit in `other_positions_same_coin`, dated. A coin without one is `not_entered`, with a reason
    ONLY from an attempt row whose origin carries that coin's source signature; otherwise "no attempt
    recorded". A ledger read failure is `unavailable` on every coin, never `not_entered`."""
    from sniper.persistence import db_memecoin as m
    out["funded_scope"] = FUNDED_SCOPE
    try:
        positions = m.real_positions_for_wallet(ledger, wallet, "solana")
        attempts = m.entry_attempts_for_coins(ledger, wallet, "solana", [c["mint"] for c in out["coins"]])
        stats = m.real_stats_by_wallet(ledger, "solana")
    except (sqlite3.Error, ValueError):
        out["funded"] = {"state": "unavailable"}
        for c in out["coins"]:
            c["funded"] = {"state": "unavailable"}
        return
    out["funded"] = (dict(state="ok", **stats[wallet]) if wallet in stats
                     else {"state": "none", "open_count": 0, "closed_count": 0})
    by_mint = collections.defaultdict(list)
    for p in positions:
        by_mint[p["mint"]].append(p)
    attempt_origin = {a["attempt_id"]: a["origin_event_id"] for rows in attempts.values() for a in rows}
    for c in out["coins"]:
        sig, at = c["source"]["signature"], c["source"]["at"]
        # EXACT-EVENT ATTRIBUTION (Alpha on D3): a position is THIS coin's entry only when its own
        # origin, else its attempt's, carries this source buy's signature as a whole token. Every
        # other position on the same coin is listed beside it, dated, never counted as entered.
        entered, others = [], []
        for p in by_mint.get(c["mint"], []):
            # Chain time and detection time apart (S2): a late detection never reads as a lead.
            row = dict(_funded_position(p),
                       opened_vs_source_s=_seconds_between(c["source"].get("source_at"), p["opened_at"]),
                       opened_vs_detected_s=_seconds_between(at, p["opened_at"]))
            row["linkage"] = _event_linkage(sig, p["origin_event_id"], attempt_origin.get(p["attempt_id"]))
            (entered if row["linkage"] in ("origin_event_id", "attempt_id") else others).append(row)
        if entered:
            c["funded"] = {"state": "entered", "positions": entered, "other_positions_same_coin": others}
            continue
        mine = [a for a in attempts.get(c["mint"], []) if sig and sig in _tokens(a["origin_event_id"])]
        last = mine[-1] if mine else None
        c["funded"] = {"state": "not_entered", "other_positions_same_coin": others, "attempts": len(mine),
                       "reason": last["terminal_reason"] if last else "no attempt recorded",
                       "attempt": None if last is None else {
                           "attempt_id": last["attempt_id"], "status": last["terminal_status"],
                           "at": last["decision_started_at"], "origin_event_id": last["origin_event_id"],
                           "lane": last["delivery_lane"]}}


def wallet_gaps(view):
    """D4: each gap in a wallet view's evidence with what would fill it, from fields D1-D3 already
    return. Keys are stable for the page. A gap is named, never left to read as a zero. The dossier's
    admission gap is NOT here: the page reads it from /wallet-map/dossier (entries[].codes)."""
    state = view.get("graph_state") or (view.get("stamp") or {}).get("graph_state")
    if state == "not_measured":
        return [{"key": "graph_not_measured", "what": "Relationships are not measured on this chain.",
                 "fill": "A relationship graph for this chain; the link graph covers Solana only."}]
    if state == "unavailable":
        return [{"key": "graph_unavailable", "what": "The relationship graph could not be read.",
                 "fill": "A readable data/memecoin_wallet_graph.db with a finished run (hourly cron)."}]
    gaps = []
    if state == "stale":
        gaps.append({"key": "graph_stale", "what": "The relationship graph is older than 3 hours.",
                     "fill": "The next hourly wallet-link-graph run.",
                     "since": (view.get("stamp") or {}).get("computed_at")})
    n = (view.get("wallet") or {}).get("n") or 0
    if n < REG_N:
        gaps.append({"key": "small_sample", "n": n,
                     "what": "Only %d read first-buy coins; repeat buyers are named from 10." % n,
                     "fill": "At least %d read first-buy coins for this wallet." % REG_N})
    funded = view.get("funded") or {}
    if funded.get("state") == "unavailable":
        gaps.append({"key": "funded_unavailable", "what": "Our funded record could not be read.",
                     "fill": "A readable ledger."})
        return gaps
    if not funded.get("closed_count"):
        gaps.append({"key": "no_funded_closes", "what": "No funded closes for this wallet on this chain.",
                     "fill": "Funded closes; only wallets we armed have them."})
    missing = sum(1 for c in view.get("coins") or []
                  if (c.get("funded") or {}).get("state") == "not_entered" and not c["funded"].get("attempts"))
    if missing:
        listed = len(view.get("coins") or [])
        scope = " of the newest %d" % listed if view.get("coins_omitted") else ""
        gaps.append({"key": "coins_without_attempt", "n": missing,
                     "what": "%d supporting coins%s have no recorded entry attempt of ours." % (missing, scope),
                     "fill": "A recorded entry attempt for those source buys (arming, or the trace)."})
    return gaps


def build_wallet_view(graph, ledger, *, wallet, now_iso, own_addresses, first_sigs=None):
    """{stamp, wallet, before, herd, sells_into_it, it_sells_into, coins, patterns, details}: one herder
    read left to right in time (spec page two). `before` stays "not measured yet" until the herder-keyed
    upstream read (pre-registration B) has run."""
    st = _state(graph, ledger, now_iso=now_iso, own_addresses=own_addresses, first_sigs=first_sigs)
    out = {"stamp": st["stamp"], "wallet": None, "before": {"state": "not_measured_yet"}, "herd": None,
           "sells_into_it": [], "it_sells_into": [], "coins": [], "patterns": None, "details": None}
    if st["stamp"]["error"]:
        out["gaps"] = wallet_gaps(out)
        return out
    handles, reg = st["handles"], st["reg"]
    row = next((r for r in _herders_payload(st, ledger)["herders"] if r["id"] == wallet), None)
    out["wallet"] = row or {"id": wallet, "handle": handles.get(wallet),
                            "follow": follow_states(ledger, [wallet])[wallet], "n": 0}
    if row is not None:
        mine = reg.get(wallet, {})
        out["herd"] = {
            "regulars": sorted(({"id": m, "handle": handles.get(m), "k": k, "n": row["n"],
                                 "other_herds": sum(1 for h, ms in reg.items() if h != wallet and m in ms)}
                                for m, k in mine.items()), key=lambda x: (-x["k"], x["id"])),
            "others": row["members"] - len(mine)}
    for etype, su, du, n, lag in st["edges"]:
        if etype != "dumps_on":
            continue
        if du == wallet:
            out["sells_into_it"].append({"id": su, "handle": handles.get(su), "n_coins": n, "lag_p50_s": lag})
        if su == wallet:
            out["it_sells_into"].append({"id": du, "handle": handles.get(du), "n_coins": n, "lag_p50_s": lag})
    mine_reads = sorted((r for r in st["chosen"] if r["source_user_id"] == wallet),
                        key=lambda r: r["detected_at"], reverse=True)
    regs = set(reg.get(wallet, {}))
    # D2 (spec 2026-10-07-wallet-map-overhaul-design.md): each coin names its source first buy and
    # the buyers after it in that slot. A source read with no slot index has an UNKNOWN order, so
    # its followers are None, never 0. Newest COINS_MAX coins; the rest are counted.
    out["coins_total"], out["coins_omitted"] = len(mine_reads), max(0, len(mine_reads) - COINS_MAX)
    out["notes"] = {"after_signature": "not stored by the slot read"}
    for r in mine_reads[:COINS_MAX]:
        idx = r.get("index")
        after = None if idx is None else sorted(
            ({"trader": c["trader"], "address": c.get("address"), "index": c["index"], "signature": None,
              "repeat_buyer": c["trader"] in regs}
             for c in r["companions"]
             if c.get("index") is not None and c["index"] > idx and c["trader"] != wallet),
            key=lambda c: (c["index"], str(c["trader"])))
        traders = None if after is None else {c["trader"] for c in after}
        out["coins"].append({"mint": r["mint"], "at": r["detected_at"],
                             "followers": None if traders is None else len(traders),
                             "regulars": None if traders is None else sorted(traders & regs),
                             "source": {"signature": r["signature"], "slot": r.get("slot"), "index": idx,
                                        "at": r["detected_at"]},
                             "after": after})
    _attach_source_times(out, ledger, wallet)
    _attach_funded(out, ledger, wallet)
    out["patterns"] = patterns(ledger, graph, wallet, st["window_from"])
    out["details"] = {"out_of_n": (row or {}).get("out_of_n", {}),
                      "undecoded": sum(r.get("undecoded") or 0 for r in mine_reads)}
    out["gaps"] = wallet_gaps(out)
    return out


# ---------------------------------------------------------------- the follow pipeline, live
# The funnel file is written hourly; the Wallet Map's buttons change the roster, arming and trials
# NOW (operator 2026-10-02: "arm it directly and see it in Armed"). The route lays the ledger's live
# state over the file on every read, so a row moves tab on the next refresh, not the next hour.
CLIP_CHAINS = ("solana", "4663", "56", "8453")


def funnel_clips(cfg):
    """{chain: {trial, full_max}} for the confirm dialogs: the trial clip, and the full clip's
    ceiling (the full clip itself is bankroll-sized at entry time; None = no ceiling set)."""
    from sniper.services.memecoin_risk import solana_above_nano_ceiling_usd, solana_flat_clip_ceiling_usd
    out = {}
    flat = solana_flat_clip_ceiling_usd()
    for chain in CLIP_CHAINS:
        full = (cfg.MEMECOIN_SOLANA_MAX_CLIP_USD if chain == "solana" else cfg.evm_max_clip_usd_for(chain))
        # THRESHOLD 0 (2026-10-08): every Solana entry sizes under the raised ceiling.
        if chain == "solana" and full and flat is not None:
            full = max(float(full), flat)
        out[chain] = {"trial": cfg.trial_clip_usd_for(chain), "full_max": float(full) if full else None}
        # ABOVE NANO (2026-10-08): a Solana coin over the threshold sizes under the raised ceiling.
        raised = solana_above_nano_ceiling_usd() if chain == "solana" and full and flat is None else None
        out[chain]["full_max_above_nano"] = max(float(full), raised) if raised else None
    return out


def funnel_live_overlay(body, conn, cfg, now_iso):
    """`body` (the funnel file) with each row's live arming, open trials (and what stopped one),
    pause and stage; a candidate now on the roster moves into the pipeline rows; a retired wallet
    leaves every list. Reads only (memecoin_wallets, real_chains, memecoin_trial_arms/_verdicts)."""
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_trial_ops import stop_of
    roster = {r[0]: {"handle": r[1], "paused": bool(r[2]), "excluded": bool(r[3])} for r in conn.execute(
        "SELECT source_user_id, handle, COALESCE(paused,0), COALESCE(excluded,0) FROM memecoin_wallets")}
    trial_chains, trial_stops = collections.defaultdict(set), collections.defaultdict(dict)
    try:
        for t in m.open_trials(conn):
            trial_chains[t["source_user_id"]].add(str(t["chain"]))
            stop = stop_of(conn, t)
            if stop:                            # the grader stopped it: Keep armed must name this
                trial_stops[t["source_user_id"]][str(t["chain"])] = stop
    except sqlite3.OperationalError:            # trial tables absent: no trial is open
        pass

    def live(uid):
        armed = m.wallet_armed_chains(conn, uid) if uid in roster else []
        return armed, sorted(trial_chains.get(uid, ()))

    effective = {chain: m.real_approved_user_ids(conn, network_id=chain)
                 for chain in {c for uid in roster for c in m.wallet_armed_chains(conn, uid)}}

    def stage_of(row, armed, tchains):
        wallet = roster.get(row.get('uid')) or {}
        if wallet.get('paused') or wallet.get('excluded'):
            return 'curated'
        armed = [c for c in armed if row.get('uid') in effective.get(c, set())]
        if tchains:
            return "trial"
        if armed:
            return "armed"
        if body.get('stamp', {}).get('mode') == 'live_authority':
            return 'paper'  # configured watch-only; no performance or delivery inference
        stage = row.get("stage")
        return stage if stage in ("paper", "tracked", "curated") else (
            "paper" if row.get("paper_n") else "curated")

    rows, seen = [], set()
    for r in body.get("funnel") or []:
        w = roster.get(r["uid"])
        if w and w["excluded"]:
            continue
        armed, tchains = live(r["uid"])
        rows.append(dict(r, armed=armed, trial=bool(tchains), trial_chains=tchains,
                         trial_stops=dict(trial_stops.get(r["uid"], {})),
                         paused=bool(w and w["paused"]), stage=stage_of(r, armed, tchains)))
        seen.add(r["uid"])

    def promote(uid, handle, chain):
        """True when the candidate leaves its list: retired, already a pipeline row, or moved there."""
        w = roster.get(uid)
        if not w:
            return False
        if w["excluded"] or uid in seen:
            return True
        armed, tchains = live(uid)
        rows.append({"uid": uid, "handle": w["handle"] or handle, "stage": stage_of({'uid': uid}, armed, tchains),
                     "armed": armed, "trial": bool(tchains), "trial_chains": tchains,
                     "trial_stops": dict(trial_stops.get(uid, {})), "paused": w["paused"], "paper_n": 0, "paper_ret": None, "paper_cens_share": None,
                     "gated_n": 0, "gated_ret": None, "gated_cens_share": None, "funded_n": 0,
                     "funded_ret": None, "feasible_by_chain": {}, "feasible_n": None,
                     "from_candidate": chain})
        seen.add(uid)
        return True

    body["solana_candidates"] = [c for c in body.get("solana_candidates") or []
                                 if not promote("pump:" + c["address"], c["address"][:8], "solana")]
    # NO PROVEN ADDRESS, LIVE (whatever the file's vintage): the trial gate's own source, the
    # watcher's proven addresses, read once. An EVM candidate without one gets no actions.
    try:
        from sniper.services.wallet_observation import observation_owners
        proven = {(str(net), uid) for (net, _a), (uid, _h) in observation_owners(conn,set(roster)).items()}
    except Exception:                                   # failed proof read stays unavailable
        proven = set()
        body.setdefault('stamp', {})['proof_state'] = 'unavailable'
    for chain, block in (body.get("evm_candidates") or {}).items():
        block["candidates"] = [c for c in block.get("candidates") or []
                               if not promote(c["uid"], c.get("handle"), chain)]
        for c in block["candidates"]:
            if (str(chain), c["uid"]) not in proven:
                c["commands"], c["no_commands"] = [], "no proven %s address" % chain
    for uid, wallet in roster.items():
        if wallet['excluded'] or uid in seen:
            continue
        promote(uid, wallet['handle'], None)
        rows[-1].update(paper_n=None, gated_n=None, funded_n=None,
                        measurement_state='not_generated')
    for row in rows:
        row['effective_chains'] = [c for c in row['armed']
                                   if row['uid'] in effective.get(c, set())]
        row.setdefault('measurement_state', 'ok')
    body.setdefault('stamp', {})['authority_state'] = 'ok'
    body["rotation"] = [dict(r, armed=live(r["uid"])[0]) for r in body.get("rotation") or []
                        if not (roster.get(r["uid"]) or {}).get("excluded")]
    for row in rows:
        row['address_chains'] = sorted(net for net, uid in proven if uid == row['uid'])
        row['proof_state'] = body.get('stamp', {}).get('proof_state', 'ok')
    body['excluded_uids'] = [uid for uid,w in roster.items() if w['excluded']]
    body["funnel"] = rows
    body["clips"] = funnel_clips(cfg)
    body.setdefault("stamp", {})["live_at"] = now_iso
    if body['stamp'].get('mode') == 'live_authority':
        authority_fields = {'uid', 'handle', 'stage', 'armed', 'effective_chains', 'trial',
                            'trial_chains', 'trial_stops', 'paused', 'address_chains', 'proof_state'}
        body['funnel'] = [{k: v for k, v in row.items() if k in authority_fields} for row in rows]
        body['solana_candidates'], body['evm_candidates'], body['rotation'] = [], {}, []
        body['publication'] = 'authority_only'
    return body


UNIVERSE_MAX_BYTES = 16 * 1024 * 1024  # Real Solana evidence is ~10 MiB; keep the read bounded.
UNIVERSE_WALLET_EDGE_CAP = 120          # per wallet, strongest first; the live max was 100 (2026-10-07)
GRAPH_CHAINS = ("solana",)              # the link graph measures Solana only
ASSOCIATIONS = ("shared_funder", "co_buy_slot")    # no order claim; every other type is ordered


def _live_edge(row):
    (kind, src, dst, src_uid, dst_uid, n_coins, n_consistent, lag50, lag90, null_p, null_p99, flag,
     first_seen, last_seen, coverage, evidence, method_version, computed_at) = row
    ev = json.loads(evidence)
    coins = list(dict.fromkeys(str(c["mint"]) for c in ev.get("coins") or []
                               if isinstance(c, dict) and c.get("mint")))
    return {"kind": kind, "direction": "association" if kind in ASSOCIATIONS else "ordered",
            "src": src, "dst": dst, "src_uid": src_uid or src, "dst_uid": dst_uid or dst,
            "src_in_roster": src_uid is not None, "dst_in_roster": dst_uid is not None,
            "n_coins": n_coins, "n_consistent": n_consistent, "lag_p50_s": lag50, "lag_p90_s": lag90,
            "null_p": null_p, "null_p99": null_p99, "flag": flag,
            "first_seen": first_seen, "last_seen": last_seen, "coins": coins,
            # The job stores at most EVIDENCE_MAX coins per edge; a type without coins (shared_funder)
            # has nothing to omit.
            "coins_omitted": max(0, int(n_coins or 0) - len(coins)) if "coins" in ev else None,
            "coverage": json.loads(coverage),       # moved to coverage_by_ref by live_universe_edges
            "provenance": {"method_version": method_version, "computed_at": computed_at}}


def _edge_family(conn, chain):
    """(latest finished run (method_version, finished_at) or None, its edge rows on `chain`): the run's
    method version and its ':'-suffixed variants (the slot method's ':cov1'), never an older one."""
    run = conn.execute("SELECT method_version, finished_at FROM memecoin_wallet_graph_runs "
                       "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1").fetchone()
    if run is None:
        return None, []
    rows = conn.execute(
        "SELECT edge_type, src, dst, src_user_id, dst_user_id, n_coins, n_consistent, lag_p50_s, "
        "lag_p90_s, null_p, null_p99, flag, first_seen, last_seen, coverage, evidence, method_version, "
        "computed_at FROM memecoin_wallet_edges WHERE network_id=?", (chain,)).fetchall()
    return run, [r for r in rows if r[16] == run[0] or str(r[16]).startswith(run[0] + ":")]


def shared_with(graph, wallet, other, *, now_iso, chain="solana"):
    """The coins `wallet` and `other` share under each measured edge type (D2), with each coin's stored
    evidence (times, lags, slot indexes) exactly as the graph job kept it. No edge between them is
    `measured_link: False` with an empty list and the coverage of what WAS measured, never a bare 0.
    `graph` None or unreadable is `state: unavailable`."""
    out = {"other": other, "state": "unavailable", "measured_link": None, "edges": [], "shared_coins": [],
           "coverage_by_ref": {}}
    if graph is None:
        return out
    try:
        run, family = _edge_family(graph, chain)
        pair = []
        for row in family:
            e = _live_edge(row)
            src, dst = {e["src"], e["src_uid"]}, {e["dst"], e["dst_uid"]}
            if (wallet in src and other in dst) or (other in src and wallet in dst):
                e["coins"] = [c for c in json.loads(row[15]).get("coins") or [] if isinstance(c, dict)]
                pair.append(e)
        scope = [_live_edge(r) for r in family] if not pair else pair
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return out
    if run is None:
        return out
    age = (_parse(now_iso) - _parse(run[1])).total_seconds()
    refs = _dedupe_coverage(scope)
    out.update(state="stale" if age > STALE_AFTER_S else "ok", measured_link=bool(pair),
               edges=pair, coverage_by_ref=refs,
               shared_coins=sorted({c["mint"] for e in pair for c in e["coins"] if c.get("mint")}))
    return out


def _dedupe_coverage(edges):
    """{ref: coverage}, with each edge's `coverage` replaced by `coverage_ref`. The job writes one
    coverage per type per run, repeated on every edge (~630 chars x 859 slot edges, 2026-10-07), so
    the ref is the type; a coverage that really differs within a type gets `type#2`, `type#3`."""
    refs, seen = {}, {}
    for e in edges:
        cov = e.pop("coverage")
        key = (e["kind"], json.dumps(cov, sort_keys=True))
        if key not in seen:
            n = sum(1 for k in seen if k[0] == e["kind"])
            seen[key] = e["kind"] if n == 0 else "%s#%d" % (e["kind"], n + 1)
            refs[seen[key]] = cov
        e["coverage_ref"] = seen[key]
    return refs


def live_universe_edges(path, *, chain, now_iso, excluded=()):
    """{graph, edges, link_counts, coverage_by_ref} for the universe route, from the live link graph (D1, spec
    2026-10-07-wallet-map-overhaul-design.md). Read-only, one connection, closed before return.

    `graph.state` is ok / stale / not_measured / unavailable and `graph.reason` names why it is not
    ok, so a graph we could not read is never served as a measured graph with no links. Edges are
    the latest finished run's method version and its ':'-suffixed variants (the slot method's
    ':cov1'). `link_counts` = {wallet: {type: n}} over every edge of that family, before the
    per-wallet cap; None unless the graph was read."""
    graph = {"state": "unavailable", "reason": None, "run_finished_at": None, "computed_at": None,
             "method_version": None, "age_s": None, "stale_after_s": STALE_AFTER_S,
             "edges_total": None, "edges_served": 0, "omitted_by_wallet": {}}
    out = {"graph": graph, "edges": [], "link_counts": None, "coverage_by_ref": {}}
    if chain not in GRAPH_CHAINS:
        graph.update(state="not_measured", reason="chain_not_measured")
        return out
    try:
        conn = open_graph_readonly(path)
    except sqlite3.Error:
        graph["reason"] = "open_failed"
        return out
    if conn is None:
        graph["reason"] = "absent"
        return out
    try:
        run, family = _edge_family(conn, chain)
        edges = [e for e in map(_live_edge, family)
                 if e["src_uid"] not in excluded and e["dst_uid"] not in excluded]
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        graph["reason"] = "unreadable"
        return out
    finally:
        conn.close()
    if run is None:
        graph["reason"] = "no_finished_run"
        return out
    age = (_parse(now_iso) - _parse(run[1])).total_seconds()
    graph.update(state="stale" if age > STALE_AFTER_S else "ok", run_finished_at=run[1], age_s=age,
                 method_version=run[0], edges_total=len(edges),
                 computed_at=max((e["provenance"]["computed_at"] for e in edges), default=None))
    counts = collections.defaultdict(collections.Counter)
    for e in edges:
        counts[e["src_uid"]][e["kind"]] += 1
        if e["dst_uid"] != e["src_uid"]:
            counts[e["dst_uid"]][e["kind"]] += 1
    # THE CAP COUNTS BOTH ENDPOINTS (Gamma on D1): strongest first, an edge is served while neither
    # wallet holds the cap. An edge left out is missing from BOTH wallets' views, so both count it.
    served, held = [], collections.Counter()
    omitted = graph["omitted_by_wallet"]
    for e in sorted(edges, key=lambda e: (e["null_p"] is None, e["null_p"] or 0.0, -(e["n_coins"] or 0),
                                          e["kind"], str(e["src_uid"]), str(e["dst_uid"]))):
        ends = {e["src_uid"], e["dst_uid"]}
        if all(held[w] < UNIVERSE_WALLET_EDGE_CAP for w in ends):
            served.append(e)
            held.update(ends)
        else:
            for w in ends:
                omitted[w] = omitted.get(w, 0) + 1
    refs = _dedupe_coverage(served)
    if len(json.dumps(served)) + len(json.dumps(refs)) > UNIVERSE_MAX_BYTES:
        graph.update(state="unavailable", reason="oversize")
        return out
    graph["edges_served"] = len(served)
    out.update(edges=served, link_counts={w: dict(c) for w, c in counts.items()}, coverage_by_ref=refs)
    return out


def universe_projection(*, chain, wallets, edges, generated_at, graph_state):
    """Offline chain partition. Missing measurements are unknown, never zero."""
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(chain)
    if chain is None:
        raise ValueError('unknown chain')
    selected, seen = [], set()
    for wallet in wallets:
        if canonical_chain_id(wallet['chain']) != chain or wallet['uid'] in seen:
            continue
        selected.append(dict({'paper': None, 'funded': None, 'strict_policy': None, 'setups': [],
                              'coverage': {}}, **wallet))
        seen.add(wallet['uid'])
    links = []
    for edge in edges:
        if canonical_chain_id(edge['chain']) != chain:
            continue
        coins = list(dict.fromkeys(str(c.get('mint')) if isinstance(c, dict) else str(c)
                                   for c in edge.get('coins', []) if c))
        links.append(dict(edge, coins=coins, direction=(
            'association' if edge['kind'] in ('shared_funder', 'co_buy_slot') else 'ordered')))
    return dict(version=1, chain=chain, generated_at=generated_at, graph_state=graph_state,
                wallets=selected, edges=links)


def build_universe(ledger, graph, *, chain, generated_at, observation=None,quotes=None,captures=None):
    """Existing facts only; called offline. Each input DB owns an independent snapshot."""
    from sniper.services.memecoin_chains import canonical_chain_id, resolve_chain
    from sniper.persistence import db_memecoin as m
    from sniper.services.axiom_address_verify import watcher_address_owners
    from sniper.services.memecoin_paper_report import wallet_arm_cells_from_forward_cohort,CHAIN_IDS
    from sniper.services.memecoin_copy_arm import select_from_cells
    from sniper.settings import memecoin as cfg
    from scripts.research.wallet_follow_candidates import pooled
    chain = canonical_chain_id(chain)
    if chain is None:
        raise ValueError('unknown chain')
    roster = {r['source_user_id']: dict(r) for r in ledger.execute(
        'SELECT source_user_id,handle,paused,excluded FROM memecoin_wallets')}
    proof_state = 'ok'
    try:
        from sniper.services.wallet_observation import observation_owners
        owners = observation_owners(ledger,set(roster))
    except sqlite3.Error:
        owners, proof_state = {}, 'unavailable'
    addresses = collections.defaultdict(list)
    for (net, addr), (uid, handle) in owners.items():
        if canonical_chain_id(net) == chain:
            addresses[uid].append(addr)
    p = pooled(ledger, "execution_mode='paper' AND strategy='funded_clone' AND opened_at >= ?",
               ('2026-09-01',))
    f = pooled(ledger, "execution_mode='real' AND opened_at >= ?", ('2026-09-01',))
    edges, graph_state = [], 'not_collected'
    if chain == 'solana':
        graph_state = 'unavailable' if graph is None else 'ok'
        if graph is not None:
            try:
                for row in graph.execute('SELECT * FROM memecoin_wallet_edges WHERE network_id=?', (chain,)):
                    e = dict(row)
                    raw = json.loads(e.get('evidence') or '{}')
                    edges.append(dict(chain=chain, src_uid=e.get('src_user_id') or e['src'],
                                      dst_uid=e.get('dst_user_id') or e['dst'], kind=e['edge_type'],
                                      coins=raw.get('coins', []), coverage=e.get('coverage'),
                                      provenance=dict(method=e.get('method_version'), at=e.get('computed_at'),
                                                      lag_p50_s=e.get('lag_p50_s'), lag_p90_s=e.get('lag_p90_s'),
                                                      evidence=raw)))
            except (sqlite3.Error, ValueError, AttributeError):
                edges, graph_state = [], 'unavailable'
    ids = set(roster) | set(addresses) | {uid for uid, ch in set(p) | set(f) if ch == chain and uid}
    for e in edges:
        ids.update([e['src_uid'], e['dst_uid']])
    arms = list(cfg.MEMECOIN_ARMS)
    selected = {}
    if arms and resolve_chain(chain) in CHAIN_IDS:
        selected = {uid: select_from_cells(cells, chain=chain) for uid, cells in
                    wallet_arm_cells_from_forward_cohort(ledger, arms, resolve_chain(chain)).items()}
    def metric(value):
        return None if value is None else dict(priced=value[0], survivor_return=value[1],
                                               censored=value[3], label='survivor diagnostic')
    from sniper.services.wallet_setup_evidence import project_setup_evidence, project_blockers
    setups = project_setup_evidence(ledger, chain=chain, arms=arms,
                                   cutoff='2026-09-01T00:00:00+00:00') if resolve_chain(chain) in CHAIN_IDS else {}
    blockers = project_blockers(ledger, chain=chain, cutoff='2026-09-01T00:00:00+00:00',capture_db=captures)
    from sniper.services.wallet_observation import project_research_evidence
    research=project_research_evidence(observation,quotes,chain=chain) if observation is not None else {}
    ids.update(research)
    from sniper.services.memecoin_chains import network_ids_for
    networks=network_ids_for(resolve_chain(chain));marks=','.join('?' for _ in networks)
    source_rows=ledger.execute('SELECT source_user_id,mint,signature,detected_at,block_time,detect_latency_s '
        'FROM onchain_wallet_detections WHERE network_id IN ('+marks+') '
        'ORDER BY detected_at DESC LIMIT 10000',networks).fetchall()
    last_buys={}
    for r in source_rows:
        last_buys.setdefault(r[0],dict(mint=r[1],signature=r[2],detected_at=r[3],block_time=r[4],latency_s=r[5]))
    position_evidence=collections.defaultdict(lambda:collections.defaultdict(list))
    contracts={}
    for r in ledger.execute('SELECT id,source_user_id,mint,origin_event_id,execution_mode,strategy,is_open,'
        'realized_pnl,entry_notional,opened_at,funded_contract_hash,funded_contract_json FROM memecoin_positions '
        'WHERE network_id IN ('+marks+') AND opened_at>=? ORDER BY opened_at,id',(*networks,'2026-09-01')):
        mode=r[4]
        if mode=='paper' and r[5]!='funded_clone': continue
        if mode=='real': contracts[r[1]]=dict(position_id=r[0],hash=r[10],contract=r[11])
        position_evidence[r[1]][mode].append(dict(position_id=r[0],mint=r[2],origin=r[3],open=bool(r[6]),
            pnl=r[7],notional=r[8],opened_at=r[9],funded_contract_hash=r[10] if mode=='real' else None))
    rows = []
    for uid in sorted(ids - {None, ''}):
        w = roster.get(uid, {})
        if w.get('excluded'):
            continue
        rows.append(dict(uid=uid, chain=chain, handle=w.get('handle') or uid,
                         paused=bool(w.get('paused')), intake='roster' if uid in roster else 'measured_neighbor',
                         paper=metric(p.get((uid, chain))), funded=metric(f.get((uid, chain))),
                         strict_policy=selected.get(uid),research=research.get(uid),last_buy=last_buys.get(uid),
                         supporting_positions=dict(position_evidence.get(uid,{})),latest_funded_contract=contracts.get(uid),
                         setups=setups.get(uid, []), blockers=blockers.get(uid, []),
                         current_override=m.wallet_exit_override(ledger, uid, network_id=chain),
                         gate_exemptions=sorted(m.wallet_gate_exemptions(ledger, uid)),
                         coverage=dict(address=dict(state='unavailable' if proof_state != 'ok' else ('ok' if addresses[uid] else 'not_proven'),
                                                    value=addresses[uid], as_of=generated_at, source='verified registry'),
                                       subscription=dict(state='unknown', value=None, as_of=None, source=None))))
    return universe_projection(chain=chain, wallets=rows, edges=edges,
                               generated_at=generated_at, graph_state=graph_state)
