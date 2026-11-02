"""Wallet Analyzer API — browse persisted thesis profiles and drop a wallet to analyze.

BROWSE reads the durable JSON store (`runs/wallet_thesis_profiles/`, written by
winning_wallet_thesis store.persist_profile) — these survive DB wipes. DROP runs the
extractor in a background thread (it does live API calls and takes seconds-to-minutes),
tracked by an in-memory job registry the UI polls. No DB tables involved."""
from __future__ import annotations

import os
import re
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from sniper.markets import polymarket_api as P
from sniper.analysis.winning_wallet_thesis.store import STORE_DIR
from sniper.analysis.winning_wallet_thesis.ledger import CACHE_DIR as ANATOMY_DIR

import json as _json

_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _wallet_profile(wallet: str) -> dict:
    """Display profile (Polymarket username + avatar) for a wallet, extracted once from its
    cached activity (name/pseudonym/profileImage on every row) and cached to a small sidecar
    so the reports list stays fast. {} when unavailable."""
    w = wallet.lower()
    side = os.path.join(ANATOMY_DIR, f"{w}_profile.json")
    if os.path.exists(side):
        try:
            with open(side) as f:
                return _json.load(f)
        except Exception:  # noqa: BLE001
            pass
    prof: dict = {}
    act = os.path.join(ANATOMY_DIR, f"{w}_activity.json")
    if os.path.exists(act):
        try:
            with open(act) as f:
                rows = _json.load(f)
            for r in rows:
                nm = (r.get("name") or "").strip()
                ps = (r.get("pseudonym") or "").strip()
                img = (r.get("profileImage") or r.get("profileImageOptimized") or "").strip()
                if nm or ps or img:
                    prof = {"name": nm, "pseudonym": ps, "image": img}
                    break
        except Exception:  # noqa: BLE001
            prof = {}
    try:
        with open(side, "w") as f:
            _json.dump(prof, f)
    except Exception:  # noqa: BLE001
        pass
    return prof

# job_id -> {wallet, status: queued|running|done|error, started, finished, run_id, headline, error}
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
# A drop-a-wallet analysis runs the FULL live extractor (seconds-to-minutes, network + CPU) in a
# Flask thread. Cap concurrent runs so distinct wallets can't spawn unbounded heavy work and wedge
# the web process; a job older than _JOB_STALE_S no longer counts (its thread, if still alive, is on
# its own) so a single stuck run can't block the endpoint permanently.
_MAX_INFLIGHT = 2
_JOB_STALE_S = 1800


def _is_active(j: dict, now: float) -> bool:
    """A job counts as in-flight only while queued/running AND younger than _JOB_STALE_S — a
    job past that age no longer blocks the global cap OR same-wallet re-analysis (its thread,
    if still alive, is on its own)."""
    return j["status"] in ("queued", "running") and (now - (j.get("started") or now)) < _JOB_STALE_S


def _inflight_count(now: float) -> int:
    return sum(1 for j in _JOBS.values() if _is_active(j, now))


def _wallet_ok(w: str) -> bool:
    return bool(w and _WALLET_RE.match(w.strip()))


def _first(row: dict, *keys: str):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None



def _known_position_keys(wallet: str) -> set[tuple[str, int]]:
    """(condition_id, outcome_index) of positions the analyzer saw STILL OPEN (unresolved + still
    holding shares) at analysis time. A position the analyzer recorded as resolved or fully exited
    is NOT a current holding, so a live position on that market is a RE-ENTRY and must read as NEW,
    not 'known' — keying on every historical (cond,oidx) mislabeled re-buys. (review #MED 2026-06-17)"""
    from sniper.analysis.winning_wallet_thesis import db_store
    keys: set[tuple[str, int]] = set()
    for row in db_store.positions_for(wallet):
        cond = row.get("condition_id")
        oidx = _int_or_none(row.get("outcome_index"))
        if not cond or oidx is None:
            continue
        held = ((row.get("buy_shares") or 0.0) + (row.get("split_shares") or 0.0)
                + (row.get("convert_in_shares") or 0.0)      # negRisk conversion flow (else a
                - (row.get("sell_shares") or 0.0) - (row.get("merge_shares") or 0.0)
                - (row.get("convert_out_shares") or 0.0))    # conversion-held leg looks closed)
        if not row.get("resolved") and held > 1e-6:      # open & held at analysis time
            keys.add((str(cond), oidx))
    return keys


def _normalize_live_position(row: dict, known: set[tuple[str, int]]) -> dict:
    cond = _first(row, "conditionId", "condition_id", "condition")
    oidx = _int_or_none(_first(row, "outcomeIndex", "outcome_index"))
    key = (str(cond), oidx) if cond and oidx is not None else None
    known_to_analyzer = bool(key and key in known)
    return {
        "condition_id": str(cond or ""),
        "outcome_index": oidx,
        "outcome": _first(row, "outcome", "outcomeName") or "",
        "title": _first(row, "title", "marketTitle", "question") or "",
        "event_slug": _first(row, "eventSlug", "event_slug", "slug") or "",
        "asset": str(_first(row, "asset", "assetId", "token_id", "tokenId") or ""),
        "size": _float_or_none(_first(row, "size", "quantity")),
        "avg_price": _float_or_none(_first(row, "avgPrice", "averagePrice", "avg_price")),
        "current_price": _float_or_none(_first(row, "curPrice", "currentPrice", "current_price")),
        "current_value": _float_or_none(_first(row, "currentValue", "current_value")),
        "cash_pnl": _float_or_none(_first(row, "cashPnl", "cash_pnl", "pnl")),
        "percent_pnl": _float_or_none(_first(row, "percentPnl", "percent_pnl")),
        "known_to_analyzer": known_to_analyzer,
        "is_new_since_analysis": not known_to_analyzer,
    }


def _profile_summary(p: dict, *, mtime: float | None = None) -> dict:
    cr = p.get("capital_risk") or {}
    total = cr.get("total_realized_pnl_with_inventory")
    if total is None:
        total = cr.get("total_realized_pnl", 0.0)
    return {
        "wallet": p.get("wallet"),
        "run_id": p.get("run_id"),
        "headline": p.get("headline"),
        "n_edge_clusters": len(p.get("edge_clusters") or []),
        "n_loss_centers": len(p.get("loss_centers") or []),
        "total_realized_pnl": total,
        "reward_share_pct": p.get("reward_share_pct"),
        "n_data_gaps": len(p.get("data_gaps") or []),
        "analyzed_at": mtime,
    }


def _list_reports() -> list[dict]:
    """Latest profile per wallet, newest first, as summaries."""
    if not os.path.isdir(STORE_DIR):
        return []
    import json
    by_wallet: dict[str, tuple[float, dict]] = {}
    for name in os.listdir(STORE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(STORE_DIR, name)
        try:
            mtime = os.path.getmtime(path)
            with open(path) as f:
                p = json.load(f)
        except Exception:
            continue
        w = (p.get("wallet") or "").lower()
        if not w:
            continue
        if w not in by_wallet or mtime > by_wallet[w][0]:
            by_wallet[w] = (mtime, p)
    rows = [_profile_summary(p, mtime=mt) for mt, p in by_wallet.values()]
    rows.sort(key=lambda r: r.get("analyzed_at") or 0, reverse=True)
    return rows


def _load_full_report(wallet: str) -> dict | None:
    """Latest full profile dict for a wallet."""
    if not os.path.isdir(STORE_DIR):
        return None
    import json
    best: tuple[float, dict] | None = None
    wl = wallet.lower()
    for name in os.listdir(STORE_DIR):
        if not name.startswith(wl + "__") or not name.endswith(".json"):
            continue
        path = os.path.join(STORE_DIR, name)
        try:
            mtime = os.path.getmtime(path)
            with open(path) as f:
                p = json.load(f)
        except Exception:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best else None


def _run_analysis(job_id: str, wallet: str, refresh: bool = False) -> None:
    from sniper.analysis.winning_wallet_thesis.cli import run_extractor
    from sniper.analysis.winning_wallet_thesis.store import persist_profile
    with _JOBS_LOCK:
        _JOBS[job_id]["status"] = "running"
    try:
        # persist_store=True materializes the reconstructed data into the wallet's OWN
        # queryable SQLite file (runs/wallet_analysis/<wallet>.db) in addition to the JSON.
        # refresh=True forces a fresh live re-fetch (re-analyze); False reuses the cache.
        profile = run_extractor(wallet, refresh=refresh, incremental=refresh, persist_store=True)
        persist_profile(profile)
        with _JOBS_LOCK:
            _JOBS[job_id].update(status="done", finished=time.time(),
                                 run_id=profile.run_id, headline=profile.headline)
    except Exception as e:  # noqa: BLE001 — surface the failure to the UI, don't crash the thread
        with _JOBS_LOCK:
            _JOBS[job_id].update(status="error", finished=time.time(),
                                 error=f"{type(e).__name__}: {e}")


def _enrich_flags(conn, reports: list[dict]) -> None:
    """Attach favorite/deleted flags and the latest analyzer verdict/route to each report
    (display-only). Defensive: missing tables -> skipped."""
    if conn is None:
        return
    try:
        from sniper.persistence import db_wallet_flags as FL
        from sniper.persistence import db_wallet_reads as RD
    except Exception:  # noqa: BLE001
        return
    wallets = [r.get("wallet") or "" for r in reports]
    try:
        flags = FL.flags_for(conn, wallets)
    except Exception:  # noqa: BLE001
        flags = {}
    for r in reports:
        w = (r.get("wallet") or "").lower()
        f = flags.get(w, {})
        r["favorite"] = bool(f.get("favorite"))
        r["deleted"] = bool(f.get("deleted"))
        try:
            read = RD.latest_read(conn, w)
            if read:
                r["read_verdict"] = read.get("primary_verdict")
                route = read.get("operational_route")
                if route in {"strategy_candidate", "expedited_review", "monitor", "ignore"}:
                    r["read_route"] = route
                elif read.get("primary_verdict") in {
                    "established_directional_edge",
                    "emerging_directional_evidence",
                    "fresh_high_conviction",
                    "fresh_high_surprise",
                }:
                    r["read_route"] = "strategy_candidate"
                else:
                    r["read_route"] = None
                r["read_headline"] = read.get("headline")          # plain-English verdict sentence
                r["read_history_depth"] = read.get("history_depth")
                ev = _json.loads(read.get("evidence_json") or "{}")
                r["read_n_events"] = ev.get("n_independent_events")
                r["read_ex_post"] = (ev.get("fresh") or {}).get("ex_post_level")
                r["read_recent_anomaly"] = ev.get("recent_anomaly_level")
                el = _json.loads(read.get("edge_location_json") or "{}")
                r["read_domains"] = [d for d in (el.get("domains") or []) if d][:3]
                r["read_warnings"] = _json.loads(read.get("warnings_json") or "[]")
        except Exception:  # noqa: BLE001
            pass


def create_api_wallet_analyzer_blueprint(ctx=None) -> Blueprint:
    bp = Blueprint("api_wallet_analyzer", __name__)
    conn = (ctx or {}).get("conn") if ctx else None

    @bp.get("/api/wallet-analyzer/reports")
    def list_reports():
        reports = _list_reports()
        for r in reports:
            r["profile"] = _wallet_profile(r.get("wallet") or "")
        _enrich_flags(conn, reports)
        return jsonify({"reports": reports})

    @bp.get("/api/wallet-analyzer/report")
    def get_report():
        wallet = (request.args.get("wallet") or "").strip()
        if not _wallet_ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        report = _load_full_report(wallet)
        if report is None:
            return jsonify({"error": "no report for wallet"}), 404
        report["_profile"] = _wallet_profile(wallet)
        # Display-only plain-English synthesis (verdict + top patterns). Re-presents existing
        # fields; mutates no stored analysis.
        from sniper.analysis.winning_wallet_thesis import narrative as _narr
        report["narrative"] = _narr.synthesize(report)
        return jsonify(report)

    @bp.post("/api/wallet-analyzer/analyze")
    def analyze():
        data = request.get_json(silent=True) or {}
        wallet = (data.get("wallet") or "").strip()
        refresh = bool(data.get("refresh"))
        if not _wallet_ok(wallet):
            return jsonify({"error": "invalid wallet — expected 0x + 40 hex"}), 400
        wl = wallet.lower()
        with _JOBS_LOCK:
            now = time.time()
            # Reuse an ACTIVE job for the same wallet rather than double-running — but a stale
            # (stuck) job must NOT block re-analysis forever, so honor the same staleness window.
            for jid, j in _JOBS.items():
                if j["wallet"] == wl and _is_active(j, now):
                    return jsonify({"job_id": jid, "status": j["status"], "wallet": wl})
            inflight = _inflight_count(now)
            if inflight >= _MAX_INFLIGHT:
                return jsonify({"error": "analyzer busy — only %d concurrent analyses; retry shortly"
                                % _MAX_INFLIGHT, "inflight": inflight}), 429
            job_id = uuid.uuid4().hex[:12]
            _JOBS[job_id] = {"wallet": wl, "status": "queued", "started": time.time(),
                             "finished": None, "run_id": None, "headline": None, "error": None}
        threading.Thread(target=_run_analysis, args=(job_id, wl, refresh), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "queued", "wallet": wl})

    @bp.get("/api/wallet-analyzer/positions")
    def positions():
        wallet = (request.args.get("wallet") or "").strip()
        if not _wallet_ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        from sniper.analysis.winning_wallet_thesis import db_store
        return jsonify({"wallet": wallet.lower(), "positions": db_store.positions_for(wallet)})

    @bp.get("/api/wallet-analyzer/live-positions")
    def live_positions():
        wallet = (request.args.get("wallet") or "").strip()
        if not _wallet_ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        wl = wallet.lower()
        known = _known_position_keys(wl)
        try:
            rows, term = P.fetch_user_positions(None, wl, "positions")
        except Exception as e:  # noqa: BLE001 — surface source outage without breaking the UI
            return jsonify({"error": f"live position fetch failed: {type(e).__name__}: {e}"}), 502
        term_reason = getattr(term, "reason", None)
        if term_reason == "api_refused" and not rows:
            return jsonify({
                "error": "live position fetch refused",
                "status": getattr(term, "status", None),
                "offset": getattr(term, "offset", None),
            }), 502
        # Drop resolved-but-unredeemed positions (redeemable=True, curPrice=0 — e.g. a game that ended
        # weeks ago). They are not live/copyable bets; showing them floods the board with $0 resolved
        # losses (gardenshed's France/Bosnia). Same filter as the live-positions poller.
        live_rows = [r for r in rows if not r.get("redeemable")]
        resolved_hidden = len(rows) - len(live_rows)
        positions = [_normalize_live_position(r, known) for r in live_rows]
        return jsonify({
            "wallet": wl,
            "fetched_at": int(time.time()),
            "count": len(positions),
            "resolved_hidden": resolved_hidden,
            "known_count": sum(1 for p in positions if p["known_to_analyzer"]),
            "new_count": sum(1 for p in positions if p["is_new_since_analysis"]),
            "fetch_termination": term_reason,
            "positions": positions,
        })

    @bp.get("/api/wallet-analyzer/slices")
    def slices():
        wallet = (request.args.get("wallet") or "").strip()
        if not _wallet_ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        axis = (request.args.get("axis") or "").strip() or None
        from sniper.analysis.winning_wallet_thesis import db_store
        return jsonify({"wallet": wallet.lower(), "axis": axis, "slices": db_store.slices_for(wallet, axis=axis)})

    @bp.get("/api/wallet-analyzer/position-activity")
    def position_activity():
        wallet = (request.args.get("wallet") or "").strip()
        cond = (request.args.get("condition") or "").strip()
        if not _wallet_ok(wallet) or not cond:
            return jsonify({"error": "wallet + condition required"}), 400
        try:
            oidx = int(request.args.get("outcome"))
            epi = int(request.args.get("episode"))
        except (TypeError, ValueError):
            return jsonify({"error": "outcome + episode must be integers"}), 400
        from sniper.analysis.winning_wallet_thesis import db_store
        return jsonify({"activity": db_store.activity_for_position(wallet, cond, oidx, epi)})

    @bp.get("/api/wallet-analyzer/analysis-state")
    def analysis_state():
        wallet = (request.args.get("wallet") or "").strip()
        if not _wallet_ok(wallet):
            return jsonify({"error": "invalid wallet"}), 400
        from sniper.analysis.winning_wallet_thesis import db_store
        return jsonify({"wallet": wallet.lower(), "state": db_store.analysis_state(wallet)})

    @bp.get("/api/wallet-analyzer/jobs")
    def jobs():
        with _JOBS_LOCK:
            out = [{"job_id": jid, **j} for jid, j in _JOBS.items()]
        out.sort(key=lambda j: j.get("started") or 0, reverse=True)
        return jsonify({"jobs": out[:50]})

    return bp
