"""Daily desk report (operator order 2026-10-23 via Alpha): one UTC day of the LLM desk on one page.

READ-ONLY and report-only: no ledger write, no config, nothing on the money path, nothing here feeds a decision. Every
source is optional: a missing file or table is the string 'unavailable' in its section, never a zero. Counts, percentiles
and enums are computed here; the per-trade GPT review (memecoin_desk_trade_review) is appended by the script.

collect() takes already-open READ-ONLY sqlite connections (or None) so the tests can hand it fixtures.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

UNAVAILABLE = "unavailable"
CLIP_LOSS_USD = 25.0 * 0.18               # desk_reentry_tracker: one clip loss at the frozen stop
TRIPWIRE_MIN_REENTRIES = 10
RAIL_REASONS = {"real_tp": "rail:tp", "real_trail": "rail:trail", "b_stop": "rail:stop", "catastrophe": "rail:catastrophe"}


def day_bounds(day):
    """(iso_start, iso_end, epoch_start, epoch_end) of the UTC day 'YYYY-MM-DD'."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat(), start.timestamp(), end.timestamp()


def pct(values, q):
    """Nearest-rank percentile (q in 0..1) of a list, None when empty."""
    xs = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))]


def exit_class(reason):
    """(class, detail): 'model' for desk_risk_exit, 'rail:<kind>' for deterministic rails, else 'other:<reason>'."""
    r = str(reason or "")
    if r == "desk_risk_exit":
        return "model", r
    if r in RAIL_REASONS:
        return RAIL_REASONS[r], r
    return ("open" if not r else "other:" + r), r


def refusal_bucket(reason):
    r = str(reason or "").lower()
    if r.startswith("desk verdict price"):
        return "verdict_cap"
    if "no quote" in r or "no_route" in r or "route_not_found" in r:
        return "no_route"
    if "bankroll" in r:
        return "bankroll"
    if "revert" in r or "slippage" in r or r.startswith("chain_error"):
        return "revert_or_slippage"
    if "kill_switch" in r:
        return "kill_switch"
    if "receipt" in r:
        return "receipt_unreadable"
    return "other:" + (r[:40] if r else "none")


def _q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return None


def _positions(ledger, iso_start, iso_end):
    rows = _q(ledger, "SELECT id, mint, ticker, entry_price, entry_notional, opened_at, closed_at, realized_pnl, exit_reason, "
                      "is_open, network_id FROM memecoin_positions WHERE signal_source='desk' AND execution_mode='real' "
                      "AND ((opened_at >= ? AND opened_at < ?) OR (closed_at >= ? AND closed_at < ?)) ORDER BY opened_at",
              (iso_start, iso_end, iso_start, iso_end))
    if rows is None:
        return None
    out = []
    for r in rows:
        (pid, mint, ticker, entry_price, notional, opened, closed, pnl, reason, is_open, network_id) = tuple(r)
        hold = None
        try:
            if opened and closed:
                hold = (datetime.fromisoformat(closed) - datetime.fromisoformat(opened)).total_seconds()
        except ValueError:
            hold = None
        cls, _ = exit_class(reason if closed else None)
        out.append({"id": pid, "mint": mint, "network_id": network_id, "ticker": ticker, "entry_price": entry_price, "entry_notional": notional,
                    "opened_at": opened, "closed_at": closed, "realized_pnl": pnl, "exit_reason": reason, "exit_class": cls,
                    "hold_s": hold, "is_open": bool(is_open),
                    "pnl_pct": (pnl / notional * 100.0) if isinstance(pnl, (int, float)) and notional else None,
                    "opened_today": bool(opened and iso_start <= opened < iso_end),
                    "closed_today": bool(closed and iso_start <= closed < iso_end)})
    return out


def positions_section(ledger, iso_start, iso_end):
    if ledger is None:
        return UNAVAILABLE
    rows = _positions(ledger, iso_start, iso_end)
    if rows is None:
        return UNAVAILABLE
    closed = [r for r in rows if r["closed_today"] and r["realized_pnl"] is not None]
    wins = [r for r in closed if r["realized_pnl"] > 0]
    by_class = {}
    for r in closed:
        by_class.setdefault(r["exit_class"], []).append(r["realized_pnl"])
    return {"positions": rows, "entries": sum(r["opened_today"] for r in rows), "closes": len(closed),
            "open_now": sum(r["is_open"] for r in rows), "pnl_usd": sum(r["realized_pnl"] for r in closed),
            "wins": len(wins), "losses": len(closed) - len(wins),
            "by_exit_class": {k: {"n": len(v), "pnl_usd": sum(v)} for k, v in sorted(by_class.items())}}


def funnel_section(desk, iso_start, iso_end):
    """Desk tickets of the day by stage/verdict/reason (the desk DB's own ledger of what each coin did)."""
    if desk is None:
        return UNAVAILABLE
    rows = _q(desk, "SELECT stage, verdict, reason, COUNT(*) FROM desk_tickets WHERE ts >= ? AND ts < ? GROUP BY 1,2,3",
              (iso_start, iso_end))
    if rows is None:
        return UNAVAILABLE
    stages = {}
    for stage, verdict, reason, n in map(tuple, rows):
        stages.setdefault(stage, {}).setdefault(verdict, {})[reason] = n
    return stages


def refusals_section(ledger, iso_start, iso_end):
    """Entry attempts the funded leg refused/failed, bucketed by cause."""
    if ledger is None:
        return UNAVAILABLE
    rows = _q(ledger, "SELECT terminal_status, terminal_reason, COUNT(*) FROM memecoin_entry_attempts WHERE signal_source='desk' "
                      "AND decision_started_at >= ? AND decision_started_at < ? GROUP BY 1,2", (iso_start, iso_end))
    if rows is None:
        return UNAVAILABLE
    # The ledger's entry-attempt table holds the attempts that did not end in a fill (refused / aborted / anomaly / failed);
    # fills are counted from the positions. A row without a reason is bucketed 'other:none', never dropped.
    out = {"attempts": 0, "by_status": {}, "by_cause": {}}
    for status, reason, n in map(tuple, rows):
        out["attempts"] += n
        out["by_status"][str(status)] = out["by_status"].get(str(status), 0) + n
        b = refusal_bucket(reason)
        out["by_cause"][b] = out["by_cause"].get(b, 0) + n
    return out


def _verdict_of(text):
    try:
        return json.loads(text).get("v")
    except (TypeError, ValueError, AttributeError):
        return None


def judge_section(desk, iso_start, iso_end):
    """JUDGE calls of the day: model decisions, ENTER rate, latency p50/p90 by transport, non-decisions by cause."""
    if desk is None:
        return UNAVAILABLE
    rows = _q(desk, "SELECT result_json FROM desk_judge_calls WHERE state='responded' AND call_started_at >= ? AND call_started_at < ?",
              (iso_start.replace("+00:00", ""), iso_end.replace("+00:00", "")))
    if rows is None:
        return UNAVAILABLE
    decisions, errors, lat = {}, {}, {}
    n = 0
    for (rj,) in map(tuple, rows):
        n += 1
        try:
            r = json.loads(rj or "{}")
        except ValueError:
            continue
        if r.get("call_error"):
            errors[r["call_error"]] = errors.get(r["call_error"], 0) + 1
            continue
        v = _verdict_of(r.get("text"))
        if v:
            decisions[v] = decisions.get(v, 0) + 1
        t = r.get("timing") or {}
        if isinstance(t.get("wall_ms"), (int, float)):
            lat.setdefault(t.get("transport") or "cold", []).append(t["wall_ms"] / 1000.0)
    total = sum(decisions.values())
    return {"calls": n, "decisions": decisions, "enter_rate": (decisions.get("E", 0) / total) if total else None,
            "non_decisions": errors,
            "latency_s": {k: {"n": len(v), "p50": pct(v, .5), "p90": pct(v, .9)} for k, v in sorted(lat.items())}}


def risk_section(risk, ledger, epoch_start, epoch_end, iso_start, iso_end):
    out = {}
    if risk is None:
        out["decisions"] = UNAVAILABLE
    else:
        rows = _q(risk, "SELECT action, COUNT(*) FROM risk_decisions WHERE live=1 AND COALESCE(decided_at, cycle_at) >= ? "
                        "AND COALESCE(decided_at, cycle_at) < ? GROUP BY 1", (epoch_start, epoch_end))
        out["decisions"] = UNAVAILABLE if rows is None else {a: n for a, n in map(tuple, rows)}
    if ledger is None:
        out["applies"] = UNAVAILABLE
    else:
        rows = _q(ledger, "SELECT source, action, status, COUNT(*) FROM memecoin_desk_risk_applies WHERE applied_at >= ? "
                          "AND applied_at < ? GROUP BY 1,2,3", (iso_start, iso_end))
        out["applies"] = UNAVAILABLE if rows is None else [
            {"source": s, "action": a, "status": st, "n": n} for s, a, st, n in map(tuple, rows)]
        if rows is not None:
            out["auto_tightens"] = sum(n for s, a, st, n in map(tuple, rows)
                                       if s == "attention_auto" and st in ("applied", "applied_partial"))
    return out


def reentry_section(ledger):
    """Re-entries (2nd+ desk position on one mint) and the operator's tripwire, over ALL desk history (as desk_reentry_tracker)."""
    if ledger is None:
        return UNAVAILABLE
    rows = _q(ledger, "SELECT id, mint, opened_at, closed_at, realized_pnl FROM memecoin_positions WHERE signal_source='desk' "
                      "AND execution_mode='real' ORDER BY opened_at")
    if rows is None:
        return UNAVAILABLE
    by = {}
    for r in map(tuple, rows):
        by.setdefault(r[1], []).append(r)
    firsts = [v[0][4] for v in by.values() if v[0][3] and v[0][4] is not None]
    re_after_loss = []
    re_all = 0
    for ps in by.values():
        for prev, cur in zip(ps, ps[1:]):
            if prev[4] is None:
                continue
            re_all += 1
            if prev[4] < 0 and cur[3] and cur[4] is not None:
                re_after_loss.append(cur[4])
    first_mean = sum(firsts) / len(firsts) if firsts else None
    re_mean = sum(re_after_loss) / len(re_after_loss) if re_after_loss else None
    if len(re_after_loss) < TRIPWIRE_MIN_REENTRIES or first_mean is None:
        status = "insufficient (%d of %d closed re-entries after a losing close)" % (len(re_after_loss), TRIPWIRE_MIN_REENTRIES)
    elif re_mean < first_mean - CLIP_LOSS_USD:
        status = "TRIPPED: re-entry mean %.2f < first-entry mean %.2f - %.2f" % (re_mean, first_mean, CLIP_LOSS_USD)
    else:
        status = "clear"
    return {"reentries": re_all, "closed_after_loss": len(re_after_loss), "reentry_mean_usd": re_mean,
            "first_entry_mean_usd": first_mean, "tripwire": status}


def checker_section(checker, day):
    if checker is None:
        return UNAVAILABLE
    rows = _q(checker, "SELECT COALESCE(defect, 'ERR:' || call_error), COUNT(*) FROM checks WHERE day=? GROUP BY 1", (day,))
    return UNAVAILABLE if rows is None else {k: n for k, n in map(tuple, rows)}


def gpt_section(gpt, day):
    if gpt is None:
        return UNAVAILABLE
    rows = _q(gpt, "SELECT live_text, gpt_text, parse_ok, valid, call_error, guard_void FROM gpt_judge WHERE day=?", (day,))
    if rows is None:
        return UNAVAILABLE
    out = {"rows": 0, "answered": 0, "agree": 0, "gpt_enter": 0, "live_enter": 0, "faults": 0}
    for live, text, parse_ok, valid, err, guard in map(tuple, rows):
        out["rows"] += 1
        if err or guard:
            out["faults"] += 1
            continue
        a, b = _verdict_of(live), _verdict_of(text)
        if a and b:
            out["answered"] += 1
            out["agree"] += int(a == b)
            out["gpt_enter"] += int(b == "E")
            out["live_enter"] += int(a == "E")
    out["agreement"] = (out["agree"] / out["answered"]) if out["answered"] else None
    return out


def infra_section(release_dirs, gaps_conn, epoch_start, epoch_end, stale_snapshots):
    """deploys = release directories created in the day (.runtime/memecoin/releases/*, one per deploy);
    universe_restarts = worker_start rows in the PumpSwap tape's gaps; stale_snapshots from the JUDGE section."""
    out = {"deploys": UNAVAILABLE, "universe_restarts": UNAVAILABLE, "stale_snapshots": stale_snapshots}
    if release_dirs is not None:
        n = 0
        for path in release_dirs:
            try:
                if epoch_start <= os.stat(path).st_mtime < epoch_end:
                    n += 1
            except OSError:
                continue
        out["deploys"] = n
    if gaps_conn is not None:
        rows = _q(gaps_conn, "SELECT COUNT(*) FROM gaps_ps WHERE cause='worker_start' AND at >= ? AND at < ?", (epoch_start, epoch_end))
        out["universe_restarts"] = UNAVAILABLE if rows is None else rows[0][0]
    return out


def collect(day, *, ledger=None, desk=None, checker=None, gpt=None, risk=None, gaps=None, deploy_logs=None):
    iso_start, iso_end, e0, e1 = day_bounds(day)
    judge = judge_section(desk, iso_start, iso_end)
    stale = UNAVAILABLE if judge == UNAVAILABLE else judge["non_decisions"].get("stale_snapshot", 0)
    return {"day": day, "positions": positions_section(ledger, iso_start, iso_end),
            "funnel": funnel_section(desk, iso_start, iso_end), "refusals": refusals_section(ledger, iso_start, iso_end),
            "judge": judge,
            "risk": risk_section(risk, ledger, e0, e1, iso_start, iso_end), "reentry": reentry_section(ledger),
            "checker": checker_section(checker, day), "gpt_shadow": gpt_section(gpt, day),
            "infra": infra_section(deploy_logs, gaps, e0, e1, stale)}


# ---------------------------------------------------------------- rendering (no model text ever reaches Telegram)
def _fmt(x, nd=2):
    return "n/a" if x is None else (("%.*f" % (nd, x)) if isinstance(x, float) else str(x))


def _md_escape(text):
    """Model/free text inside a table cell or paragraph: printable only, no markup that breaks the page."""
    s = "".join(ch for ch in str(text) if ch.isprintable() or ch in "\n ")
    for ch in ("|", "`", "<", ">"):
        s = s.replace(ch, " ")
    return " ".join(s.split())


def render_markdown(rep, reviews=None):
    d = rep["day"]
    L = ["# Desk daily report %s (UTC)" % d, "",
         "_Report only. Nothing here feeds a decision; an adopted change ships as a new cohort or prompt version._", ""]
    p = rep["positions"]
    L.append("## Trades")
    if p == UNAVAILABLE:
        L.append("ledger %s" % UNAVAILABLE)
    else:
        L.append("entries %d, closes %d, open now %d, realized **%s USD** (%d wins / %d losses)" % (
            p["entries"], p["closes"], p["open_now"], _fmt(p["pnl_usd"]), p["wins"], p["losses"]))
        L.append("exit paths: " + (", ".join("%s n=%d pnl=%s" % (k, v["n"], _fmt(v["pnl_usd"])) for k, v in p["by_exit_class"].items()) or "none"))
        L += ["", "| pos | ticker | mint | opened | hold s | notional | exit path | pnl USD | pnl % |", "|---|---|---|---|---|---|---|---|---|"]
        for r in p["positions"]:
            L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                r["id"], _md_escape(r["ticker"] or ""), str(r["mint"])[:8], str(r["opened_at"])[11:19], _fmt(r["hold_s"], 0),
                _fmt(r["entry_notional"]), _md_escape(r["exit_class"] if r["closed_at"] else "open"), _fmt(r["realized_pnl"]),
                _fmt(r["pnl_pct"], 1)))
    L += ["", "## Funnel (desk tickets)"]
    f = rep["funnel"]
    if f == UNAVAILABLE:
        L.append("desk DB %s" % UNAVAILABLE)
    else:
        for stage, verdicts in f.items():
            for verdict, reasons in verdicts.items():
                L.append("- %s %s: %s" % (stage, verdict, ", ".join("%s %d" % (k, v) for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))))
    L += ["", "## Entry refusals (funded leg)"]
    r = rep["refusals"]
    if r == UNAVAILABLE:
        L.append("ledger %s" % UNAVAILABLE)
    else:
        L.append("attempts that did not fill %d (%s); by cause: %s" % (
            r["attempts"], ", ".join("%s %d" % kv for kv in sorted(r["by_status"].items())) or "none",
            ", ".join("%s %d" % kv for kv in sorted(r["by_cause"].items(), key=lambda kv: -kv[1])) or "none"))
    L += ["", "## JUDGE"]
    j = rep["judge"]
    if j == UNAVAILABLE:
        L.append("desk DB %s" % UNAVAILABLE)
    else:
        L.append("calls %d, decisions %s, ENTER rate %s, non-decisions %s" % (
            j["calls"], j["decisions"], _fmt(None if j["enter_rate"] is None else j["enter_rate"] * 100, 1) + "%", j["non_decisions"] or "none"))
        for k, v in j["latency_s"].items():
            L.append("- latency %s: n=%d p50 %s s p90 %s s" % (k, v["n"], _fmt(v["p50"]), _fmt(v["p90"])))
    L += ["", "## RISK"]
    k = rep["risk"]
    L.append("decisions: %s" % k.get("decisions"))
    L.append("applies: %s" % ("; ".join("%s/%s/%s n=%d" % (a["source"], a["action"], a["status"], a["n"]) for a in k["applies"])
                              if isinstance(k.get("applies"), list) else k.get("applies")))
    if "auto_tightens" in k:
        L.append("attention auto-tightens applied: %d" % k["auto_tightens"])
    L += ["", "## Re-entries and tripwire"]
    e = rep["reentry"]
    L.append(e if e == UNAVAILABLE else "re-entries %d (closed after a losing close %d); tripwire: **%s**" % (e["reentries"], e["closed_after_loss"], e["tripwire"]))
    L += ["", "## CHECKER findings", str(rep["checker"]), "", "## GPT shadow seat", str(rep["gpt_shadow"]), "", "## Infra", str(rep["infra"])]
    if reviews is not None:
        L += ["", "## Per-trade GPT review (report only, never feeds a decision)", ""]
        L += reviews if isinstance(reviews, list) else [str(reviews)]
    return "\n".join(L) + "\n"


def render_telegram(rep, tags=None, path=None):
    """One short message: counts and enums only (no model text)."""
    p = rep["positions"]
    parts = ["desk %s" % rep["day"]]
    if p != UNAVAILABLE:
        parts.append("entries %d closes %d pnl %s USD (%dW/%dL)" % (p["entries"], p["closes"], _fmt(p["pnl_usd"]), p["wins"], p["losses"]))
    r = rep["refusals"]
    if r != UNAVAILABLE and r["by_cause"]:
        parts.append("refused " + ", ".join("%s %d" % kv for kv in sorted(r["by_cause"].items(), key=lambda kv: -kv[1])[:3]))
    j = rep["judge"]
    if j != UNAVAILABLE and j["enter_rate"] is not None:
        parts.append("JUDGE %d calls ENTER %s%%" % (j["calls"], _fmt(j["enter_rate"] * 100, 0)))
    k = rep["risk"].get("decisions")
    if isinstance(k, dict) and k:
        parts.append("RISK " + " ".join("%s %d" % kv for kv in sorted(k.items())))
    e = rep["reentry"]
    if e != UNAVAILABLE:
        parts.append("tripwire " + e["tripwire"].split(" (")[0].split(":")[0])
    if tags:
        parts.append("review tags " + " ".join("%s %d" % kv for kv in sorted(tags.items())))
    if path:
        parts.append(path)
    return " | ".join(parts)
