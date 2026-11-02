"""Start, end and list trial arms: ONE implementation for the operator script
(scripts/memecoin_trial_arm.py) and the Wallet Map's buttons (POST /api/memecoin/wallets/<uid>/trial).
Spec docs/superpowers/specs/2026-10-01-memecoin-trial-arm-design.md sections 5.1 and 5.6; rule
3a0b141ee.

START refuses when the chain has no trial clip (MEMECOIN_REAL_TRIAL_CLIP_USD_<chain>), the wallet is
excluded, already armed on that chain, already has an open trial there, has no proven address on that
chain (our on-chain detection could not see its buys), or shows no on-chain buy on that chain in the
last 7 days (the trial would only expire). J7 is out of the wallet path (operator 2026-10-02): the
evidence is our own, never a J7 delivery: onchain_wallet_detections at its addresses, and on Solana
the Wallet Map tape (it bought before one of our sources, or in the same slot as one of our sources).
Every check runs BEFORE any write, so a refused start writes nothing. With `add_if_missing` (the map's
"Trial" on a candidate) a wallet not yet on the roster is added only after every check passed. It
writes the trial row, then arms the chain with force (several candidates fail the copy-PnL promote
gate; that is why they are trials). A refused arm deletes the trial row; a wallet added for it stays
on the roster (watch-only) and the result says so. END needs exactly one of keep_armed / disarm, so
ending a trial never silently leaves a wallet on the full clip.

DB authority only: nothing here calls a funded seam or the network. The runner picks a new arm up at
its next roster refresh.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_trial as T
from sniper.services.memecoin_chains import canonical_chain_id
from sniper.settings import memecoin as cfg

SIGNAL_DAYS = 7
GRAPH_PATH = None        # the Wallet Map db; None = data/memecoin_wallet_graph.db under the state root

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def valid_roster_uid(uid):
    """A Fomo uuid, `pump:<Solana address>` or `evm:<0x lowercase address>`; anything else is
    refused (a typo must not add a row)."""
    if _UUID.match(uid or ""):
        return True
    from sniper.services.axiom_address_verify import evm_roster_address
    if evm_roster_address(uid):
        return True
    return bool(uid and uid.startswith("pump:") and _SOLANA.match(uid[len("pump:"):]))


def graph_path():
    if GRAPH_PATH:
        return str(GRAPH_PATH)
    from sniper.util.state_paths import state_path
    return str(state_path("data", "memecoin_wallet_graph.db"))


def wallet_by_handle(conn, handle):
    """(source_user_id, excluded) of the one wallet with this handle, else None."""
    rows = conn.execute("SELECT source_user_id, COALESCE(excluded,0) FROM memecoin_wallets "
                        "WHERE handle=?", (handle,)).fetchall()
    return tuple(rows[0]) if len(rows) == 1 else None


def solana_addresses(conn, uid):
    """The addresses the slot lane would watch once `uid` is armed: armed_verified_owners, the ONE
    definition of watched (registry-verified addresses plus the address a pump.fun id names, unless
    refuted)."""
    from sniper.services.memecoin_address_registry import armed_verified_owners
    owners = armed_verified_owners(conn, armed={uid}, network_id="solana")
    if owners is None:                                   # no registry table: the pump id alone
        addr = uid[5:] if uid.startswith("pump:") else ""
        return {addr} if addr else set()
    return set(owners)


def evm_addresses(conn, uid, chain):
    """The addresses the EVM watcher would subscribe once `uid` is armed on `chain` (its proven
    addresses, the input of evm_watched_owners)."""
    from sniper.services.axiom_address_verify import watcher_address_owners
    try:
        owners = watcher_address_owners(conn)
    except Exception:                                    # proof tables absent: no proven address
        return set()
    return {str(a).lower() for (net, a), (u, _h) in owners.items() if str(net) == chain and u == uid}


def onchain_evidence(conn, uid, chain, now_iso):
    """{addresses, detections_7d, tape_buys_7d} from OUR OWN on-chain records (never a J7 delivery).
    tape_buys_7d is None when not read: not Solana, or no Wallet Map db (unknown, never 0)."""
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    since = now - timedelta(days=SIGNAL_DAYS)
    addrs = sorted(solana_addresses(conn, uid) if chain == "solana" else evm_addresses(conn, uid, chain))
    out = {"addresses": len(addrs), "detections_7d": 0, "tape_buys_7d": None}
    if not addrs:
        return out
    marks = ",".join("?" for _ in addrs)
    out["detections_7d"] = conn.execute(
        "SELECT COUNT(*) FROM onchain_wallet_detections WHERE network_id=? AND detected_at >= ? "
        "AND (source_user_id=? OR address IN (%s))" % marks,
        (chain, since.isoformat(), uid, *addrs)).fetchone()[0]
    path = graph_path()
    if chain == "solana" and Path(path).exists():
        g = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            early = g.execute(
                "SELECT COUNT(*) FROM memecoin_wallet_early_buys e JOIN memecoin_wallet_upstream_reads u "
                "ON u.signature = e.signature WHERE u.status='ok' AND e.trader IN (%s) "
                "AND u.t0 - COALESCE(e.lead_s, 0) >= ?" % marks, (*addrs, since.timestamp())).fetchone()[0]
            same_slot = sum(g.execute(
                "SELECT COUNT(*) FROM memecoin_wallet_slot_reads WHERE status='ok' AND detected_at >= ? "
                "AND instr(companions, ?) > 0", (since.isoformat(), '"%s"' % a)).fetchone()[0] for a in addrs)
        finally:
            g.close()
        out["tape_buys_7d"] = early + same_slot
    return out


def tables_missing(conn):
    """A reason when the trial tables are absent: the runner creates them when it starts on a
    release that carries the trial arm, so before that restart nothing can be read or written."""
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memecoin_trial_arms'")}
    return None if have else ("trial tables are absent: the runner has not restarted on the "
                              "trial release yet")


def start(conn, chain, *, uid, confirm, now_iso, handle=None, add_if_missing=False, actor="operator",
          evidence_ack=None, rule=None):
    """Start a trial of `uid` on `chain`; a dry run unless `confirm`. Returns the result dict
    (ok, written, reason, clip_usd, evidence, added, would_add, trial_id).

    `evidence_ack` (operator text, 2026-10-07): starts a trial whose wallet shows no on-chain buy in
    SIGNAL_DAYS because we could not watch it before it was armed (china_sniper_1). The proven
    address is still required; the ack is written into the arm reason. `tp-two-of-three-v1` selects
    the operator's automatic two-TP/best-of-three rule at a fixed $50 cap; the default is unchanged."""
    chain = canonical_chain_id(chain) or str(chain)
    rule = T.RULE if rule is None else rule
    clip = 50.0 if rule == T.TP_TWO_OF_THREE_RULE else cfg.trial_clip_usd_for(chain)
    out = {"ok": False, "written": False, "handle": handle, "chain": chain, "clip_usd": clip,
           "source_user_id": uid, "added": False}
    if rule not in (T.RULE, T.TP_TWO_OF_THREE_RULE):
        return {**out, "reason": "unknown trial rule"}
    missing = tables_missing(conn)
    if missing:
        return {**out, "reason": missing}
    row = conn.execute("SELECT handle, COALESCE(excluded,0) FROM memecoin_wallets WHERE source_user_id=?",
                       (uid,)).fetchone()
    if row is None and not add_if_missing:
        return {**out, "reason": "not on the roster"}
    if row is None:
        if not valid_roster_uid(uid):
            return {**out, "reason": "%r is neither a Fomo uuid, pump:<Solana address> nor "
                                     "evm:<0x lowercase address>" % uid}
        if not handle:
            return {**out, "reason": "a new wallet needs a handle"}
    else:
        out["handle"] = handle = row[0] or handle
    if clip is None:
        return {**out, "reason": "no trial clip on %s (MEMECOIN_REAL_TRIAL_CLIP_USD_%s)" % (
            chain, chain.upper())}
    if row is not None and row[1]:
        return {**out, "reason": "wallet is excluded (restore first)"}
    trial = m.open_trial(conn, uid, chain)
    if trial is not None:
        return {**out, "reason": "already has open trial #%d on %s" % (trial["id"], chain)}
    if chain in m.wallet_armed_chains(conn, uid):
        return {**out, "reason": "already armed on %s at the full clip; a trial starts unarmed" % chain}
    ev = onchain_evidence(conn, uid, chain, now_iso)
    out.update(ev)
    if not ev["addresses"]:
        return {**out, "reason": "no proven %s address: our on-chain detection could not see its buys"
                                 % chain}
    ack = str(evidence_ack or "").strip()
    if not ev["detections_7d"] and not ev["tape_buys_7d"] and not ack:
        return {**out, "reason": "no on-chain buy on %s in %d days (detections 0, Wallet Map tape %s): "
                                 "the trial would only expire"
                                 % (chain, SIGNAL_DAYS, "not read" if ev["tape_buys_7d"] is None else 0)}
    if ack:
        out["evidence_ack"] = ack[:200]
    if not confirm:
        return {**out, "ok": True, "would_add": row is None, "reason": "dry run"}
    if row is None:
        m.add_wallet(conn, uid, handle, "follow funnel")
        conn.commit()
        out["added"] = True
    try:
        tid = m.start_trial(conn, source_user_id=uid, chain=chain, clip_usd=clip, rule=rule,
                            actor=actor, now_iso=now_iso)
    except sqlite3.IntegrityError:          # a concurrent start won the partial UNIQUE index (Gamma)
        # The failed INSERT opened an implicit transaction that holds the ledger's write lock: the
        # runner's writes would wait on it. add_wallet committed before it, so this undoes nothing else.
        conn.rollback()
        return {**out, "reason": "%salready has an open trial on %s" % (
            "added to the roster (watch-only); " if out["added"] else "", chain)}
    res = m.set_wallet_chain_armed(conn, uid, chain, True, actor=actor, force=True,
                                   reason="trial %s #%d%s" % (rule, tid, (
                                       "; evidence ack: %s" % out["evidence_ack"])
                                       if out.get("evidence_ack") else ""))
    if not res.get("ok"):
        m.delete_trial(conn, tid)
        return {**out, "reason": "%sarm refused, trial row deleted: %s" % (
            "added to the roster (watch-only); " if out["added"] else "", res.get("reason"))}
    return {**out, "ok": True, "written": True, "trial_id": tid, "reason": "started"}


def stop_of(conn, trial):
    """None while the trial takes entries; else what stopped it: {stopped_at, verdict, mean_r, closes}
    from its last verdict row, verdict 'STOPPED' when the stop has no verdict row."""
    if not trial.get("stopped_at"):
        return None
    v = conn.execute("SELECT verdict, mean_r, closes FROM memecoin_trial_verdicts WHERE trial_id=? "
                     "ORDER BY id DESC LIMIT 1", (trial["id"],)).fetchone()
    return {"stopped_at": trial["stopped_at"], "verdict": v[0] if v else "STOPPED",
            "mean_r": v[1] if v else None, "closes": v[2] if v else None}


def end(conn, chain, *, uid, keep_armed, disarm, confirm, now_iso, handle=None, actor="operator",
        ack_stop=None):
    """End `uid`'s open trial on `chain`, keeping the chain armed at the full clip or disarming it.
    Keeping armed a trial the grader stopped on anything but PROMOTE needs `ack_stop` equal to that
    stop's verdict (Gamma L1 on 8ae21ddfd). Rule 3a0b141ee makes every verdict a recommendation to
    the operator, so the override is allowed, never blocked, but only when the request names it."""
    chain = canonical_chain_id(chain) or str(chain)
    out = {"ok": False, "written": False, "handle": handle, "chain": chain, "source_user_id": uid}
    if bool(keep_armed) == bool(disarm):
        return {**out, "reason": "give exactly one of keep-armed or disarm"}
    missing = tables_missing(conn)
    if missing:
        return {**out, "reason": missing}
    trial = m.open_trial(conn, uid, chain) if uid else None
    if trial is None:
        return {**out, "reason": "no open trial for %r on %s" % (handle or uid, chain)}
    out["trial_id"], out["stop"] = trial["id"], stop_of(conn, trial)
    stop = out["stop"]
    if keep_armed and stop and stop["verdict"] != "PROMOTE" and ack_stop != stop["verdict"]:
        mean = "n/a" if stop["mean_r"] is None else "%+.1f%%" % (100.0 * stop["mean_r"])
        return {**out, "reason": "the grader stopped trial #%d at %s: %s, mean r %s over %s closes; keeping "
                                 "it armed at the FULL clip overrides that, so the request must name %s" % (
                                     trial["id"], stop["stopped_at"], stop["verdict"], mean,
                                     "n/a" if stop["closes"] is None else stop["closes"], stop["verdict"])}
    if not confirm:
        return {**out, "ok": True, "reason": "dry run"}
    if disarm:
        res = m.set_wallet_chain_armed(conn, trial["source_user_id"], chain, False, actor=actor,
                                       reason="trial %s #%d ended by the operator" % (T.RULE, trial["id"]))
        if not res.get("ok"):
            return {**out, "reason": "disarm refused, trial left open: %s" % res.get("reason")}
    m.end_trial(conn, trial["id"], now_iso=now_iso,
                reason="operator_disarm" if disarm else "operator_keep_armed")
    return {**out, "ok": True, "written": True, "reason": "ended"}


def status(conn):
    """(open trials, None), or ([], the reason) when the trial tables are absent."""
    missing = tables_missing(conn)
    if missing:
        return [], missing
    rows = []
    for t in m.open_trials(conn):
        done = conn.execute("SELECT checkpoint, verdict, action FROM memecoin_trial_verdicts "
                            "WHERE trial_id=? ORDER BY id", (t["id"],)).fetchall()
        rows.append({**t, "closes": len(m.trial_closes(conn, t)),
                     "verdicts": [tuple(r) for r in done]})
    return rows, None
