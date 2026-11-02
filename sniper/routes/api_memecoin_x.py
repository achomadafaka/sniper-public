"""Isolated read-only Flask API for persisted memecoin X evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3

from flask import Blueprint, jsonify, request

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.analysis.memecoin_x.normalize import is_valid_network_address, normalize_address
from sniper.analysis.memecoin_x.read_models import build_source_graph
from sniper.persistence.db_core import get_readonly_connection
from sniper.services.memecoin_x_llm_queue import enqueue_on_request_job
from sniper.routes.memecoin_csrf import guard_mutations


CAPTURE_STALE_S = 1200
_WINDOWS = frozenset({"24h", "7d", "30d"})
_NETWORK = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_REQUIRED_TABLES = frozenset({
    "memecoin_x_capture_runs", "memecoin_x_capture_diagnostics",
    "memecoin_x_card_observations", "memecoin_x_forward_posts",
    "memecoin_x_relationships", "memecoin_x_coin_mentions",
    "memecoin_x_forward_links", "memecoin_x_fomo_buyer_snapshots",
    "memecoin_x_signal_events",
    "memecoin_x_llm_jobs", "memecoin_x_llm_annotations",
    "memecoin_x_api_state", "memecoin_x_api_resources",
})


def _aware(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.utcoffset() is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


def _payload(**values):
    return {"display_only": True, **values}


def _unavailable(reason, *, detail=None, collection=None):
    body = _payload(available=False, reason=reason)
    if detail:
        body["detail"] = " ".join(str(detail).split())[:160]
    if collection:
        body[collection] = []
    return body


def _window_arg():
    value = request.args.get("window", "24h")
    if value not in _WINDOWS:
        raise ValueError("window must be 24h, 7d, or 30d")
    return value


def _network_arg():
    value = request.args.get("network_id")
    if value is not None and not _NETWORK.fullmatch(value):
        raise ValueError("invalid network_id")
    return value


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def create_api_memecoin_x_blueprint(ctx):
    bp = Blueprint("api_memecoin_x", __name__)
    # The LLM analysis queue spends money per request: same CSRF guard as api_memecoin (2026-09-27).
    guard_mutations(bp, enforce=ctx.get("csrf_enforce_all"))
    db_path = ctx["db_path"]
    offset_path = ctx.get("clock_offset_path")
    clock_fn = ctx.get("clock_fn", x_observed_at_or_none)
    llm_enabled = bool(ctx.get("llm_enabled", False))
    llm_provider = ctx.get("llm_provider")
    llm_model = ctx.get("llm_model")
    llm_versions = ctx.get("llm_versions") or {}
    enqueue_on_request_fn = ctx.get("enqueue_on_request_fn", enqueue_on_request_job)

    def availability():
        as_of, clock = clock_fn(path=offset_path)
        if as_of is None:
            return None, _unavailable("clock_unhealthy", detail=clock.get("reason"))
        try:
            conn = get_readonly_connection(db_path)
        except (OSError, sqlite3.Error) as exc:
            return None, _unavailable("database_unavailable", detail=exc)
        try:
            tables = {row[0] for row in conn.execute("""
                SELECT name FROM sqlite_master WHERE type='table'
            """)}
            if not _REQUIRED_TABLES.issubset(tables):
                conn.close()
                return None, _unavailable("schema_unavailable")
            api_state = conn.execute(
                "SELECT * FROM memecoin_x_api_state WHERE singleton=1").fetchone()
            if api_state is not None and api_state["state"] != "stopped":
                api_state = dict(api_state)
                last_healthy = api_state.get("last_healthy_at")
                age = None
                if last_healthy:
                    age = (_aware(as_of) - _aware(last_healthy)).total_seconds()
                utc_day = _aware(as_of).date().isoformat()
                resources = conn.execute(
                    "SELECT COUNT(*) FROM memecoin_x_api_resources WHERE utc_day=?",
                    (utc_day,)).fetchone()[0]
                degraded = api_state["state"] != "healthy" or age is None or age > CAPTURE_STALE_S
                return conn, _payload(
                    available=True, as_of=_aware(as_of).isoformat(), clock_health=clock,
                    capture_status=api_state["state"], capture_health=(
                        "degraded" if degraded else "healthy"), capture_degraded=degraded,
                    last_capture_at=api_state.get("updated_at"),
                    last_healthy_at=last_healthy, capture_age_s=age,
                    last_recovery_at=api_state.get("last_recovery_at"),
                    resources_today=int(resources),
                    recovery_next_token=api_state.get("recovery_next_token"),
                    bounded_detail=api_state.get("bounded_detail"))
            latest = conn.execute("""
                SELECT run_id, status, finished_at, clock_domain
                FROM memecoin_x_capture_runs
                WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1
            """).fetchone()
            healthy = conn.execute("""
                SELECT run_id, status, finished_at
                FROM memecoin_x_capture_runs
                WHERE finished_at IS NOT NULL AND clock_domain='observed_real_utc'
                  AND status IN ('ok','no_new')
                ORDER BY id DESC LIMIT 1
            """).fetchone()
            if healthy is None:
                conn.close()
                return None, _unavailable("capture_unavailable")
            age = (_aware(as_of) - _aware(healthy["finished_at"])).total_seconds()
            if age < 0 or age > CAPTURE_STALE_S:
                conn.close()
                return None, _unavailable("capture_stale", detail=f"age_s={max(0, age):.0f}")
            state = {
                "available": True,
                "display_only": True,
                "as_of": _aware(as_of).isoformat(),
                "clock_health": clock,
                "capture_status": latest["status"] if latest else None,
                "last_capture_at": latest["finished_at"] if latest else None,
                "last_healthy_run": healthy["run_id"],
                "last_healthy_at": healthy["finished_at"],
                "capture_age_s": age,
            }
            capture_degraded = bool(
                latest is None
                or latest["status"] not in {"ok", "no_new"}
                or latest["clock_domain"] != "observed_real_utc"
            )
            state["capture_degraded"] = capture_degraded
            state["capture_health"] = "degraded" if capture_degraded else "healthy"
            return conn, state
        except sqlite3.Error as exc:
            conn.close()
            return None, _unavailable("schema_unavailable", detail=exc)

    def bad(exc):
        return jsonify(_unavailable("invalid_request", detail=exc)), 400

    def model(conn, as_of):
        return build_source_graph(conn, as_of=as_of)

    @bp.get("/api/intel/x/status")
    @bp.get("/api/memecoin/x/status")
    def status():
        conn, state = availability()
        if conn is None:
            return jsonify(state)
        try:
            window = model(conn, state["as_of"])["windows"]["24h"]
            state["coverage"] = window["coverage"]
            return jsonify(state)
        finally:
            conn.close()

    @bp.get("/api/intel/x/sources")
    @bp.get("/api/memecoin/x/sources")
    def sources():
        try:
            window_name, network = _window_arg(), _network_arg()
        except ValueError as exc:
            return bad(exc)
        conn, state = availability()
        if conn is None:
            state["sources"] = []
            return jsonify(state)
        try:
            window = model(conn, state["as_of"])["windows"][window_name]
            rows = window["sources"]
            if network is not None:
                rows = [row for row in rows if row["network_id"] == network]
            return jsonify(_payload(
                available=True, window=window_name, network_id=network,
                sources=rows, coverage=window["coverage"], as_of=state["as_of"]))
        finally:
            conn.close()

    @bp.get("/api/intel/x/events")
    @bp.get("/api/memecoin/x/events")
    def events():
        try:
            network = _network_arg()
            limit = int(request.args.get("limit", 100))
            if not 1 <= limit <= 500:
                raise ValueError("limit must be in 1..500")
        except (TypeError, ValueError) as exc:
            return bad(exc)
        conn, state = availability()
        if conn is None:
            state["events"] = []
            return jsonify(state)
        try:
            where = "WHERE network_id=?" if network is not None else ""
            args = (network, limit) if network is not None else (limit,)
            rows = conn.execute(f"""
                SELECT * FROM memecoin_x_signal_events {where}
                ORDER BY id DESC LIMIT ?
            """, args).fetchall()
            result = []
            for raw in rows:
                row = dict(raw)
                row["evidence_observation_ids"] = _json_list(
                    row.pop("evidence_observation_ids_json"))
                row["actor_handles"] = _json_list(row.pop("actor_handles_json"))
                row["relationship_counts"] = _json_dict(
                    row.pop("relationship_counts_json"))
                row["display_only"] = True
                row["x_observed_at"] = row["triggered_at"]
                evidence_ids = row["evidence_observation_ids"]
                link = None
                if evidence_ids:
                    marks = ",".join("?" for _ in evidence_ids)
                    link = conn.execute(f"""
                        SELECT l.fomo_observed_at, l.lead_seconds, l.evidence_lane,
                               l.buyer_relationship
                        FROM memecoin_x_card_observations o
                        JOIN memecoin_x_forward_links l
                          ON l.canonical_post_id=o.canonical_post_id
                        WHERE o.id IN ({marks}) AND l.network_id=? AND l.mint=?
                          AND l.eligible=1
                        ORDER BY l.fomo_observed_at LIMIT 1
                    """, (*evidence_ids, row["network_id"], row["mint"])).fetchone()
                row["fomo_observed_at"] = link["fomo_observed_at"] if link else None
                row["lead_seconds"] = link["lead_seconds"] if link else None
                row["evidence_lane"] = link["evidence_lane"] if link else None
                row["buyer_relationship"] = link["buyer_relationship"] if link else None
                row["coverage_state"] = "linked" if link else "unlinked"
                result.append(row)
            return jsonify(_payload(
                available=True, events=result, network_id=network, as_of=state["as_of"]))
        finally:
            conn.close()

    def separated(collection):
        try:
            window_name = _window_arg()
        except ValueError as exc:
            return bad(exc)
        conn, state = availability()
        if conn is None:
            state[collection] = []
            return jsonify(state)
        try:
            window = model(conn, state["as_of"])["windows"][window_name]
            return jsonify(_payload(
                available=True, window=window_name, as_of=state["as_of"],
                **{collection: window[collection]}))
        finally:
            conn.close()

    @bp.get("/api/intel/x/suggestions")
    @bp.get("/api/memecoin/x/suggestions")
    def suggestions():
        return separated("suggestions")

    @bp.get("/api/intel/x/context")
    @bp.get("/api/memecoin/x/context")
    def context():
        return separated("official_context")

    @bp.get("/api/intel/x/annotations")
    @bp.get("/api/memecoin/x/annotations")
    def annotations():
        try:
            limit = int(request.args.get("limit", 50))
            if not 1 <= limit <= 200:
                raise ValueError("limit must be in 1..200")
        except (TypeError, ValueError) as exc:
            return bad(exc)
        conn, state = availability()
        if conn is None:
            state["annotations"] = []
            return jsonify(state)
        try:
            rows = conn.execute("""
                SELECT a.*,j.mode,j.logical_key,j.network_id,j.mint,j.as_of
                FROM memecoin_x_llm_annotations a
                JOIN memecoin_x_llm_jobs j ON j.id=a.job_id
                WHERE j.state='accepted' AND a.display_only=1
                ORDER BY a.id DESC LIMIT ?
            """, (limit,)).fetchall()
            result = []
            for raw in rows:
                row = dict(raw)
                row["risk_concerns"] = _json_list(row.pop("risk_concerns_json"))
                row["claims"] = _json_list(row.pop("claims_json"))
                row["display_only"] = True
                row["label"] = "LLM, display-only"
                result.append(row)
            return jsonify(_payload(
                available=True, annotations=result, as_of=state["as_of"]))
        finally:
            conn.close()

    @bp.post("/api/intel/x/analysis")
    @bp.post("/api/memecoin/x/analysis")
    def queue_analysis():
        if not llm_enabled:
            return jsonify(_unavailable("llm_disabled")), 403
        conn, state = availability()
        if conn is None:
            return jsonify(state), 503
        conn.close()
        body = request.get_json(silent=True) or {}
        try:
            request_id = str(body.get("request_id") or "")
            frozen_as_of = _aware(body.get("as_of"))
            if frozen_as_of > _aware(state["as_of"]):
                raise ValueError("as_of cannot be in the future")
            window = str(body.get("window") or "")
            if window not in _WINDOWS:
                raise ValueError("window must be 24h, 7d, or 30d")
            network = body.get("network_id")
            if network is not None:
                network = str(network)
                if not _NETWORK.fullmatch(network):
                    raise ValueError("invalid network_id")
            mint = body.get("mint")
            if mint is not None:
                if network is None:
                    raise ValueError("mint requires network_id")
                mint = str(mint)
                if not is_valid_network_address(mint, network):
                    raise ValueError("invalid mint for network_id")
                mint = normalize_address(mint, network)
        except (TypeError, ValueError) as exc:
            return bad(exc)
        write_conn = sqlite3.connect(db_path, timeout=30)
        write_conn.row_factory = sqlite3.Row
        write_conn.execute("PRAGMA busy_timeout=30000")
        try:
            job_id = enqueue_on_request_fn(
                write_conn, request_id=request_id,
                as_of=frozen_as_of.isoformat(), window=window,
                network_id=network, mint=mint, provider=llm_provider,
                model=llm_model, versions=llm_versions)
            job = write_conn.execute(
                "SELECT state FROM memecoin_x_llm_jobs WHERE id=?", (job_id,)).fetchone()
        except ValueError as exc:
            write_conn.close()
            if "conflicting" in str(exc):
                return jsonify(_unavailable("request_id_conflict", detail=exc)), 409
            return bad(exc)
        except sqlite3.Error as exc:
            write_conn.close()
            return jsonify(_unavailable("queue_unavailable", detail=exc)), 503
        finally:
            if write_conn:
                write_conn.close()
        return jsonify(_payload(
            available=True, job_id=job_id, request_id=request_id,
            state=job["state"])), 202

    @bp.get("/api/intel/x/analysis/<request_id>")
    @bp.get("/api/memecoin/x/analysis/<request_id>")
    def analysis_status(request_id):
        conn, state = availability()
        if conn is None:
            return jsonify(state)
        try:
            row = conn.execute("""
                SELECT * FROM memecoin_x_llm_jobs WHERE logical_key=?
            """, (f"on_request:{request_id}",)).fetchone()
            if row is None:
                return jsonify(_unavailable("analysis_not_found")), 404
            job = dict(row)
            for key in ("input_json", "raw_response_json"):
                job.pop(key, None)
            job["evidence_ids"] = _json_list(job.pop("evidence_ids_json"))
            job["generation"] = _json_dict(job.pop("generation_json"))
            annotation = conn.execute(
                "SELECT * FROM memecoin_x_llm_annotations WHERE job_id=?", (job["id"],)
            ).fetchone()
            annotation_data = None
            if annotation is not None:
                annotation_data = dict(annotation)
                annotation_data["risk_concerns"] = _json_list(
                    annotation_data.pop("risk_concerns_json"))
                annotation_data["claims"] = _json_list(annotation_data.pop("claims_json"))
                annotation_data["display_only"] = True
                annotation_data["label"] = "LLM, display-only"
            return jsonify(_payload(
                available=True, job=job, annotation=annotation_data,
                as_of=state["as_of"]))
        finally:
            conn.close()

    return bp
