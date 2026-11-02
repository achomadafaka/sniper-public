"""Office Task 8: the counterfactual fill. SKIP, veto, uncovered and null-arm outcomes must be graded under THE SAME FILL MODEL as the live Office book (prereg: pinned paper-engine model and version), so this
does not re-implement fills: it runs the real ledger engine (`sniper.office.ledger`: same ps_paper quotes, same rails, same modeled costs, same causal-FX rules) on a SCRATCH in-memory book with ample
capital, ticking `manage` through [t_decision, t_decision + horizon_s] and closing at the horizon if no rail fired. Nothing touches the real ledger or any file.
`tick_s` is the live paper-watcher cadence (a frozen input, never defaulted) and must stay inside the ledger's TICK_GRACE_S or the replay would book outages the live book would not.
-> {"pnl_usd", "entry_t", "exit_t", "exit_reason"} | {"censored": reason}. A censored coin is never a zero: unquotable entry => the quote's reason; unquotable exit => 'exit_unquotable'.
Execution-cost bound B (prereg section 5) is a separate adverse USD-per-fill input subtracted once per fill and exit via `bound_usd_per_fill` (required from the frozen registry; no implicit zero)."""
from pathlib import Path
import math

from sniper.office import ledger as L

CAPITAL_USD = 10_000_000.0
EXIT_GRACE_S = 120.0


def counterfactual(policy, desk_dir, *, network_id, mint, pool, t_decision, horizon_s, sol_usd_fn, tick_s, clock_sample_fn, bound_usd_per_fill):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (t_decision, horizon_s, tick_s, bound_usd_per_fill)) or horizon_s <= 0 or bound_usd_per_fill < 0:
        raise ValueError('invalid_counterfactual_inputs')
    if not 0 < tick_s <= L.TICK_GRACE_S:
        raise ValueError("tick_s must be the live watcher tick and within the ledger grace %s s" % L.TICK_GRACE_S)
    conn = L.open_ledger(":memory:")
    try:
        L.open_epoch(conn, epoch_id="counterfactual", clip_usd=policy.clip_usd, rule_version="frozen-counterfactual", actor="replay", note="scratch", now=t_decision - 2)
        L.open_book(conn, book_id="counterfactual", epoch_id="counterfactual", kind="discovery", actor="replay", note="scratch", now=t_decision - 2)
        L.fund(conn, book_id="counterfactual", usd=CAPITAL_USD, kind="initial", actor="replay", note="scratch counterfactual capital", now=t_decision - 1)
        fx, fx_at = sol_usd_fn(t_decision - 1)
        oid = "cf:%s:%s:%d" % (network_id, mint, int(t_decision))
        pol = policy
        r = L.submit(conn, pol, book_id="counterfactual", order_id=oid, network_id=network_id, mint=mint, pool=pool, decision_bundle_hash="0" * 64, now=t_decision, sol_usd=fx, sol_at=fx_at)
        if r.state != "pending":
            return {"censored": r.reason or r.state}
        end = t_decision + horizon_s
        now, asked = t_decision, False
        samples, identities = [], {}
        while now <= end + EXIT_GRACE_S + pol.entry_timeout_s:
            fx, fx_at = sol_usd_fn(now - tick_s)                     # an observation at or before every due time inside this tick: FX stays causal for the fill or exit it prices
            sample = clock_sample_fn(now)
            if not isinstance(sample, dict) or not isinstance(sample.get("clock_sample_id"), str) or not sample["clock_sample_id"] or L._offset(sample, now) is None:
                return {"censored": "clock_unknown"}
            identity = (sample["offset_s"], sample["sampled_at"])
            key = sample["clock_sample_id"]
            if key in identities and identities[key] != identity:
                return {"censored": "clock_sample_changed"}
            identities[key] = identity
            samples.append(dict(sample, corrected_cut=now, host_cut=now + sample["offset_s"]))
            L.manage(conn, pol, desk_dir=Path(desk_dir), now=now, clock_sample=sample, sol_usd=fx, sol_at=fx_at)
            row = conn.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()
            if row["state"] == "censored":
                return {"censored": row["reason"] or "censored"}
            if row["state"] == "closed":
                pnl = conn.execute("SELECT COALESCE(SUM(usd), 0) FROM cashflows WHERE order_id=?", (oid,)).fetchone()[0] - 2 * bound_usd_per_fill
                return {"pnl_usd": pnl, "entry_t": row["filled_at"], "exit_t": row["closed_at"], "exit_reason": row["exit_reason"] or "rail", "clock_samples": samples}
            if row["state"] == "open" and not asked and now >= end:
                L.request_exit(conn, order_id=oid, reason="horizon", requested_by="replay", now=now)
                asked = True
            if asked and now > end + EXIT_GRACE_S:
                return {"censored": "exit_unquotable"}
            now += tick_s
        return {"censored": "exit_unquotable" if asked else "entry_unfilled"}
    finally:
        conn.close()
