"""Office pods, org side (Task 18, Lambda): seats -> pods -> ledger books, and the scheduled frozen reallocation.
Pure rule and caps live in pods.py (Beta). Books are f"{epoch_id}:{pod}". The scheduled hook is NOT on the fill path: it runs once per UTC day, takes the ledger write lock only inside L.transfer, and
applies the frozen rule deterministically (no org change, no epoch). Anything unknown (FX, a score, a pod set) books nothing. A discretionary transfer, restaff, fire or hire is an org change (roster.py) and
breaks an open evaluated epoch there; this module offers no ledger path for a discretionary transfer."""
import json
from datetime import datetime, timezone

from sniper.office import ledger as L
from sniper.office import pods as P

DAY_S = 86400


def pod_ids(seats):
    """Sorted pods that hold seats; shared seats (pod None) belong to no pod book."""
    return sorted({s["pod"] for s in seats if s.get("pod")})


def period_key(now):
    """The UTC day of the caller's corrected clock, never the host clock."""
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def _check_frozen_rule(conn, rule, epoch_id):
    P.validate_rule(rule)
    frozen = json.dumps(rule, sort_keys=True, separators=(",", ":"), allow_nan=False)
    row = conn.execute("SELECT rule_json, rule_sha256 FROM epoch_pods_rules WHERE epoch_id=?", (epoch_id,)).fetchone()
    if row is None or tuple(row) != (frozen, L._hash(frozen)):
        raise ValueError("pods_rule_not_epoch_rule")


def open_pod_books(conn, rule, seats, *, epoch_id, clip_usd, total_usd, kind, actor, note, now):
    """One epoch with the rule frozen into it, one book per pod, initial capital split by pods.initial_capital. A rerun with the same values changes nothing; any difference raises pod_open_conflict."""
    pods = pod_ids(seats)
    caps = P.initial_capital(rule, pods, total_usd)
    with L._write(conn):
        e = conn.execute("SELECT clip_usd, rule_version FROM ledger_epochs WHERE epoch_id=?", (epoch_id,)).fetchone()
        if e is None:
            L.open_epoch(conn, epoch_id=epoch_id, clip_usd=clip_usd, rule_version=rule["version"], actor=actor, note=note, now=now, pods_rule=rule)
        elif tuple(e) != (clip_usd, rule["version"]):
            raise ValueError("pod_open_conflict")
        _check_frozen_rule(conn, rule, epoch_id)
        have = {r[0] for r in conn.execute("SELECT book_id FROM books WHERE epoch_id=?", (epoch_id,))}
        if have and have != {P.book_id(epoch_id, p) for p in pods}:          # the epoch's whole book set must match: a grown or shrunk pod set is another allocation
            raise ValueError("pod_open_conflict")
        for pod in pods:
            bid = P.book_id(epoch_id, pod)
            b = conn.execute("SELECT epoch_id, kind FROM books WHERE book_id=?", (bid,)).fetchone()
            if b is None:
                L.open_book(conn, book_id=bid, epoch_id=epoch_id, kind=kind, actor=actor, note=note, now=now)
            elif tuple(b) != (epoch_id, kind):
                raise ValueError("pod_open_conflict")
            c = conn.execute("SELECT usd FROM capital_flows WHERE book_id=? AND kind='initial'", (bid,)).fetchone()
            if c is None:
                L.fund(conn, book_id=bid, usd=caps[pod], kind="initial", actor=actor, note=note, now=now)
            elif c[0] != caps[pod]:
                raise ValueError("pod_open_conflict")
    return {pod: P.book_id(epoch_id, pod) for pod in pods}


def pod_scores(conn, rule, *, epoch_id, pods, now):
    """Realized only: open positions (bags) score as nothing until they close, so a pod holding losers looks no worse than a flat one (Gamma finding 2, accepted limit of the frozen rule).
    {pod: (realized_net_usd, closed_n)} over the rule's window of whole UTC days before today. Read-only. A pod with no closes scores (0.0, 0), which the rule treats as too few, not as zero."""
    day0 = now - now % DAY_S
    lo = day0 - rule["score"]["window_periods"] * DAY_S
    out = {}
    for pod in pods:
        usd, n = conn.execute("SELECT COALESCE(SUM(c.usd), 0), COUNT(DISTINCT o.order_id) FROM orders o JOIN cashflows c USING(order_id) "
                              "WHERE o.book_id=? AND o.state='closed' AND o.closed_at >= ? AND o.closed_at < ?",
                              (P.book_id(epoch_id, pod), lo, day0)).fetchone()
        out[pod] = (float(usd), int(n))
    return out


def reallocate(conn, rule, *, epoch_id, now, sol_usd, sol_at, actor="pods-rule", fx_max_age_s=120.0):
    """Apply the frozen rule once for today's period. -> list of (transfer_id, 'booked'|'replayed'). [] when the rule plans nothing or FX is unknown.
    Only an EVALUATED epoch's books are touched. A retry looks the transfer up first; a same-period conflict raises, never a second effect."""
    ep = conn.execute("SELECT rule_version FROM ledger_epochs WHERE epoch_id=?", (epoch_id,)).fetchone()
    if ep is None or ep[0] != rule["version"]:
        raise ValueError("rule_version_not_epoch_rule")
    _check_frozen_rule(conn, rule, epoch_id)
    rows = conn.execute("SELECT book_id FROM books WHERE epoch_id=? AND kind='evaluated'", (epoch_id,)).fetchall()
    pods = sorted(r[0][len(epoch_id) + 1:] for r in rows if r[0].startswith(epoch_id + ":"))
    if len(pods) < 2 or len(pods) != len(rows):
        return []
    note = json.dumps({"rule": rule["version"], "period": period_key(now)}, sort_keys=True)
    done = [r[0] for r in conn.execute("SELECT DISTINCT transfer_id FROM capital_flows WHERE kind='transfer_out' AND epoch_id=? AND rule_version=? AND note=?",
                                       (epoch_id, rule["version"], note))]
    if done:                                      # this period already booked (crash/retry): the booked transfer decides, the plan is not recomputed from post-transfer balances
        if len(done) != 1 or L.lookup_transfer(conn, done[0])["state"] != "booked":
            raise ValueError("transfer_id_conflict")
        return [(done[0], "replayed")]
    fx = L._fx(sol_usd, sol_at, now, fx_max_age_s)
    if fx is None:
        return []
    plan = P.plan_reallocation(rule, epoch_id=epoch_id, period_key=period_key(now),
                               scores=pod_scores(conn, rule, epoch_id=epoch_id, pods=pods, now=now),
                               capital={p: L.capital(conn, P.book_id(epoch_id, p)) for p in pods},
                               free_cash={p: L.free_cash(conn, fx, book_id=P.book_id(epoch_id, p)) for p in pods})
    return [(t["transfer_id"], L.transfer(conn, **t, actor=actor, note=note, now=now, sol_usd=sol_usd, sol_at=sol_at, fx_max_age_s=fx_max_age_s)) for t in plan]
