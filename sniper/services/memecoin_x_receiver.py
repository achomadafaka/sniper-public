"""Local receiver for X source-graph card capture (display-only, read-only lane).

A userscript running in the operator's own logged-in browser POSTs raw card HTML here; this
service sanitizes -> parses -> normalizes -> persists through the already-tested pipeline,
behind the clock-health gate, recording one capture-run per delivered batch. It never follows,
posts, trades, sizes, vetoes, or touches funded execution.

Why a userscript instead of Playwright/CDP: X blocks automation logins, Chrome refuses a debug
port on a logged-in profile, and extension tools redact post ids. A userscript in the real
browser has the live session, no automation flag, and no redaction.
"""
from __future__ import annotations

import sqlite3
import time
import re
from datetime import datetime, timezone

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.analysis.memecoin_x.dom_fixture import sanitize_card_html
from sniper.analysis.memecoin_x.dom_parser import parse_article_html
from sniper.analysis.memecoin_x.normalize import PARSER_VERSION, normalize_card
from sniper.analysis.memecoin_x import roster as _roster
from sniper.persistence import db_memecoin_x as X


MAX_CARDS = 50
MAX_CARD_BYTES = 256 * 1024
MAX_DIAGNOSTICS = 20
MAX_SENTINEL_LENGTH = 128
MAX_SCANNED_CARDS = 1000
RUN_LEASE_SECONDS = 60
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CLIENT_STATUSES = frozenset({
    "ok", "no_new", "coverage_gap", "wrong_feed", "logged_out", "challenge",
    "rate_limited", "page_unready", "parser_error",
})
_NON_CAPTURE_STATUSES = frozenset({
    "wrong_feed", "logged_out", "challenge", "rate_limited", "page_unready", "parser_error",
})
_CLIENT_DIAGNOSTIC_STATUS = {
    "wrong_feed": "wrong_feed",
    "unauthenticated": "logged_out",
    "challenge": "challenge",
    "rate_limited": "rate_limited",
    "page_unready": "page_unready",
    "parser_error": "parser_error",
    "coverage_gap": "coverage_gap",
}
_STOP_REASONS = frozenset({
    "initial_pass", "sentinel_found", "card_limit", "scroll_limit",
    "wrong_feed", "logged_out", "challenge", "rate_limited", "parser_error",
    "page_unready",
})
_CADENCE_BUCKETS = frozenset({
    "new_5_8m", "idle_10_15m", "recovery_5_8m", "gap_10_15m",
    "page_unready_15m", "frozen", "delivery_retry_60s",
})
_RELOAD_REASONS = frozenset({"none", "healthy_ack"})
_PARSER_DIAGNOSTIC_MAP = {
    "no_article": "missing_article",
    "no_canonical_id": "missing_post_id",
    "repost_actor_unresolved": "missing_actor",
    "reply_referenced_pending": "missing_referenced_post_id",
    "quote_referenced_not_visible": "missing_referenced_post_id",
    "repost_contains_nested_quote": "missing_referenced_post_id",
}


def _host_iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def _run_result(conn, run_id, *, duplicate=False):
    row = conn.execute("""
        SELECT run_id, started_at, cards_seen, cards_scanned, stop_reason, new_posts,
               new_relationships, parse_failures, clock_domain, status, prior_sentinel,
               newest_sentinel, sentinel_found
        FROM memecoin_x_capture_runs WHERE run_id=?
    """, (run_id,)).fetchone()
    if not row:
        return None
    result = {
        "run_id": row["run_id"], "started_at": row["started_at"],
        "cards_seen": row["cards_seen"] or 0,
        "cards_scanned": row["cards_scanned"] or 0, "stop_reason": row["stop_reason"],
        "new_posts": row["new_posts"] or 0,
        "new_relationships": row["new_relationships"] or 0,
        "parse_failures": row["parse_failures"] or 0,
        "clock_domain": row["clock_domain"], "status": row["status"],
        "prior_sentinel": row["prior_sentinel"], "newest_sentinel": row["newest_sentinel"],
        "sentinel_found": (None if row["sentinel_found"] is None
                           else bool(row["sentinel_found"])),
    }
    if duplicate:
        result["duplicate"] = True
    return result


def _diagnostic_code(code):
    if code in X.CAPTURE_DIAGNOSTIC_CODES:
        return code
    return _PARSER_DIAGNOSTIC_MAP.get(code, "parser_error")


def _error_detail(exc):
    return f"{type(exc).__name__}: {exc}"[:240]


def _validate_pass_metadata(status, diagnostics, *, has_cards):
    if status not in _CLIENT_STATUSES:
        raise ValueError("invalid capture status")
    if any(code not in _CLIENT_DIAGNOSTIC_STATUS for code in diagnostics):
        raise ValueError("invalid client capture diagnostic")
    if any(_CLIENT_DIAGNOSTIC_STATUS[code] != status for code in diagnostics):
        raise ValueError("capture status and diagnostics disagree")
    expected = {code for code, expected_status in _CLIENT_DIAGNOSTIC_STATUS.items()
                if expected_status == status}
    if expected and not expected.intersection(diagnostics):
        raise ValueError("capture status is missing its diagnostic")
    if status in _NON_CAPTURE_STATUSES and has_cards:
        raise ValueError("non-capture status cannot include cards")


def _validate_pacing_metadata(values):
    cadence_bucket = values.get("cadence_bucket")
    if cadence_bucket is not None and cadence_bucket not in _CADENCE_BUCKETS:
        raise ValueError("invalid cadence_bucket")
    scroll_count = values.get("scroll_count")
    if (scroll_count is not None and (
            isinstance(scroll_count, bool) or not isinstance(scroll_count, int)
            or not 0 <= scroll_count <= 3)):
        raise ValueError("invalid scroll_count")
    dom_wait_ms = values.get("dom_wait_ms")
    if (dom_wait_ms is not None and (
            isinstance(dom_wait_ms, bool) or not isinstance(dom_wait_ms, int)
            or not 0 <= dom_wait_ms <= 30000)):
        raise ValueError("invalid dom_wait_ms")
    reload_reason = values.get("reload_reason")
    if reload_reason is not None and reload_reason not in _RELOAD_REASONS:
        raise ValueError("invalid reload_reason")
    for key in ("recovery_used", "operator_postponed"):
        value = values.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"invalid {key}")


def process_batch(conn, raw_cards, *, run_id, now=None, offset_path=None, pass_meta=None) -> dict:
    """Sanitize/parse/normalize/persist one delivered batch of raw card HTML. One clock
    snapshot per batch; unhealthy clock -> legacy_unknown (stored, but excluded from forward
    correlation downstream). Returns per-batch counts."""
    pass_meta = dict(pass_meta or {})
    requested = pass_meta.get("status", "ok")
    diagnostics = list(pass_meta.get("diagnostics") or [])
    _validate_pass_metadata(requested, diagnostics, has_cards=bool(raw_cards))
    _validate_pacing_metadata(pass_meta)
    cards_scanned = pass_meta.get("cards_scanned", len(raw_cards))
    stop_reason = pass_meta.get("stop_reason")
    if (isinstance(cards_scanned, bool) or not isinstance(cards_scanned, int)
            or cards_scanned < len(raw_cards) or cards_scanned > MAX_SCANNED_CARDS):
        raise ValueError("invalid cards_scanned")
    if stop_reason is not None and stop_reason not in _STOP_REASONS:
        raise ValueError("invalid stop_reason")

    existing = _run_result(conn, run_id)
    retry_error = bool(existing and existing["status"] == "error")
    if existing and existing["status"] not in {"running", "error"}:
        return {**existing, "duplicate": True}

    now = time.time() if now is None else float(now)
    observed_at, health = x_observed_at_or_none(now=now, path=offset_path)
    if health["healthy"]:
        clock_domain = "observed_real_utc"
    else:
        clock_domain = "legacy_unknown"
        observed_at = _host_iso(now)

    retry_running = False
    if existing and existing["status"] == "running":
        try:
            comparable_now = (observed_at if (
                existing["clock_domain"] == "observed_real_utc" and health["healthy"])
                else (_host_iso(now) if existing["clock_domain"] == "legacy_unknown" else None))
            lease_age = (datetime.fromisoformat(comparable_now)
                         - datetime.fromisoformat(existing["started_at"])).total_seconds()
        except (TypeError, ValueError):
            lease_age = 0
        if lease_age <= RUN_LEASE_SECONDS:
            return {**existing, "duplicate": True}
        retry_running = True

    snapshot = {**health, "clock_domain": clock_domain}
    if retry_error or retry_running:
        conn.execute("""
            UPDATE memecoin_x_capture_runs
            SET parser_version=?, started_at=?, finished_at=NULL, clock_offset_s=?,
                clock_offset_age_s=?, clock_domain=?, cards_seen=0, cards_scanned=0,
                stop_reason=NULL, new_posts=0, new_relationships=0, parse_failures=0,
                newest_sentinel=NULL, sentinel_found=NULL, status='running', error_detail=NULL
            WHERE run_id=? AND status=? AND started_at IS ?
        """, (PARSER_VERSION, observed_at, health.get("offset_s"), health.get("offset_age_s"),
              clock_domain, run_id, existing["status"], existing["started_at"]))
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            conn.rollback()
            return {**_run_result(conn, run_id), "duplicate": True}
        conn.commit()
    elif not existing:
        X.start_capture_run(
            conn, run_id=run_id, parser_version=PARSER_VERSION, started_at=observed_at,
            clock_snapshot=snapshot, prior_sentinel=pass_meta.get("prior_sentinel"))

    new_posts = new_rels = failures = parsed_facts = 0
    try:
        for code in pass_meta.get("diagnostics") or []:
            X.record_capture_diagnostic(
                conn, capture_run_id=run_id, observed_at=observed_at,
                clock_domain=clock_domain, diagnostic_code=_diagnostic_code(str(code)),
                parser_version=PARSER_VERSION, details={"source_code": str(code)},
                commit=False)
        for card_index, raw in enumerate(raw_cards):
            try:
                clean = sanitize_card_html(raw)      # raises on secret material / non-tweet
            except Exception as exc:
                failures += 1
                X.record_capture_diagnostic(
                    conn, capture_run_id=run_id, observed_at=observed_at,
                    clock_domain=clock_domain, diagnostic_code="capture_error",
                    parser_version=PARSER_VERSION, card_index=card_index,
                    details={"error": _error_detail(exc)}, commit=False)
                continue
            batch = parse_article_html(clean)
            diagnostic_post_id = (
                batch.cards[0].get("canonical_post_id") if batch.cards else None)
            for code in batch.diagnostics:
                X.record_capture_diagnostic(
                    conn, capture_run_id=run_id, observed_at=observed_at,
                    clock_domain=clock_domain, diagnostic_code=_diagnostic_code(code),
                    parser_version=PARSER_VERSION, card_index=card_index,
                    canonical_post_id=diagnostic_post_id,
                    details={"source_code": code}, commit=False)
            if not batch.cards:
                if set(batch.diagnostics) == {"promoted_card"}:
                    parsed_facts += 1  # expected quarantine; no retry or eligible fact
                else:
                    failures += 1
                continue
            for card in batch.cards:
                if not card.get("text_complete"):
                    X.record_capture_diagnostic(
                        conn, capture_run_id=run_id, observed_at=observed_at,
                        clock_domain=clock_domain, diagnostic_code="incomplete_text",
                        parser_version=PARSER_VERSION, card_index=card_index,
                        canonical_post_id=card.get("canonical_post_id"), raw_card=card,
                        commit=False)
                result = X.persist_card(conn, normalize_card(card), observed_at=observed_at,
                                        clock_domain=clock_domain, capture_run_id=run_id,
                                        now_iso=observed_at, commit=False)
                new_posts += 1 if result["new_post"] else 0
                new_rels += 1 if result["new_relationship"] else 0
                parsed_facts += 1 if result["new_observation"] else 0

        if requested not in X.CAPTURE_RUN_STATUSES or requested in {"running", "error"}:
            raise ValueError(f"invalid pass status: {requested}")
        if raw_cards and parsed_facts == 0:
            status = "parser_error"
        elif not health["healthy"] and (raw_cards or requested in {"ok", "no_new"}):
            status = "clock_unhealthy"
        elif requested == "ok" and not raw_cards:
            status = "no_new"
        else:
            status = requested
        X.finish_capture_run(
            conn, run_id=run_id, finished_at=observed_at, status=status,
            cards_seen=len(raw_cards), new_posts=new_posts, new_relationships=new_rels,
            parse_failures=failures, newest_sentinel=pass_meta.get("newest_sentinel"),
            sentinel_found=pass_meta.get("sentinel_found"), cards_scanned=cards_scanned,
            stop_reason=stop_reason, cadence_bucket=pass_meta.get("cadence_bucket"),
            scroll_count=pass_meta.get("scroll_count"), dom_wait_ms=pass_meta.get("dom_wait_ms"),
            reload_reason=pass_meta.get("reload_reason"),
            recovery_used=pass_meta.get("recovery_used"),
            operator_postponed=pass_meta.get("operator_postponed"))
    except Exception as exc:
        conn.rollback()
        try:
            X.record_capture_diagnostic(
                conn, capture_run_id=run_id, observed_at=observed_at,
                clock_domain=clock_domain, diagnostic_code="capture_error",
                parser_version=PARSER_VERSION, details={"error": _error_detail(exc)})
            X.finish_capture_run(
                conn, run_id=run_id, finished_at=observed_at, status="error",
                cards_seen=len(raw_cards), new_posts=new_posts, new_relationships=new_rels,
                parse_failures=failures + 1, newest_sentinel=pass_meta.get("newest_sentinel"),
                sentinel_found=pass_meta.get("sentinel_found"), error_detail=_error_detail(exc),
                cards_scanned=cards_scanned, stop_reason=stop_reason,
                cadence_bucket=pass_meta.get("cadence_bucket"),
                scroll_count=pass_meta.get("scroll_count"), dom_wait_ms=pass_meta.get("dom_wait_ms"),
                reload_reason=pass_meta.get("reload_reason"),
                recovery_used=pass_meta.get("recovery_used"),
                operator_postponed=pass_meta.get("operator_postponed"))
        except Exception:
            pass
        raise
    return _run_result(conn, run_id)


def _open_db(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    X.ensure_memecoin_x_tables(conn)
    X.seed_roster(conn, _roster.INITIAL_ROSTER, now_iso=_host_iso(time.time()))
    return conn


def initialize_store(db_path="data/memecoin_x.db"):
    """Apply idempotent schema migrations and seed the roster before serving traffic."""
    conn = _open_db(db_path)
    conn.close()


def create_app(db_path="data/memecoin_x.db", *, offset_path=None):
    """Flask app exposing POST /x/cards {cards:[html,...]} and GET /health. Bind to 127.0.0.1
    only. Isolated store (its own db); no trading imports."""
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "lane": "memecoin-x-source-graph", "display_only": True})

    @app.post("/x/cards")
    def cards():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON object required"}), 400
        raw = body.get("cards", [])
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            return jsonify({"error": "valid run_id is required"}), 400
        if not isinstance(raw, list) or len(raw) > MAX_CARDS:
            return jsonify({"error": f"cards must be a list of at most {MAX_CARDS}"}), 400
        if any(not isinstance(card, str) for card in raw):
            return jsonify({"error": "every card must be a string"}), 400
        if any(len(card.encode("utf-8")) > MAX_CARD_BYTES for card in raw):
            return jsonify({"error": f"each card must be at most {MAX_CARD_BYTES} bytes"}), 400
        status = body.get("status", "ok")
        if status not in _CLIENT_STATUSES:
            return jsonify({"error": "invalid capture status"}), 400
        diagnostics = body.get("diagnostics", [])
        if (not isinstance(diagnostics, list) or len(diagnostics) > MAX_DIAGNOSTICS
                or any(not isinstance(code, str) or code not in _CLIENT_DIAGNOSTIC_STATUS
                       for code in diagnostics)):
            return jsonify({"error": "invalid capture diagnostics"}), 400
        try:
            _validate_pass_metadata(status, diagnostics, has_cards=bool(raw))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        for key in ("prior_sentinel", "newest_sentinel"):
            value = body.get(key)
            if value is not None and (not isinstance(value, str)
                                      or len(value) > MAX_SENTINEL_LENGTH):
                return jsonify({"error": f"invalid {key}"}), 400
        sentinel_found = body.get("sentinel_found")
        if sentinel_found is not None and not isinstance(sentinel_found, bool):
            return jsonify({"error": "sentinel_found must be boolean or null"}), 400
        cards_scanned = body.get("cards_scanned", len(raw))
        if (isinstance(cards_scanned, bool) or not isinstance(cards_scanned, int)
                or cards_scanned < len(raw) or cards_scanned > MAX_SCANNED_CARDS):
            return jsonify({"error": "invalid cards_scanned"}), 400
        stop_reason = body.get("stop_reason")
        if stop_reason is not None and stop_reason not in _STOP_REASONS:
            return jsonify({"error": "invalid stop_reason"}), 400
        try:
            _validate_pacing_metadata(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        pass_meta = {key: body.get(key) for key in (
            "status", "prior_sentinel", "newest_sentinel", "sentinel_found", "diagnostics",
            "cards_scanned", "stop_reason", "cadence_bucket", "scroll_count", "dom_wait_ms",
            "reload_reason", "recovery_used", "operator_postponed")
            if key in body}
        conn = _open_db(db_path)
        try:
            res = process_batch(
                conn, raw, run_id=run_id, offset_path=offset_path, pass_meta=pass_meta)
        finally:
            conn.close()
        return jsonify(res), (422 if res["status"] == "parser_error" else 200)

    return app
