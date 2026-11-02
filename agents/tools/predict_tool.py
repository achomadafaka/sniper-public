#!/usr/bin/env python3
"""
Self-improvement tool for strategy agents.

Agents make verifiable predictions, grade past predictions against reality,
and extract lessons that persist across all future runs.

Usage:
    # Make a prediction (will be graded on a future run)
    python3 agents/tools/predict_tool.py predict \
        --agent insider_copy \
        --prediction "Wallet 0xde9f will maintain insider_score >= 70 for 7 days" \
        --condition "Check user_analysis.insider_score for wallet 0xde9f7f4e" \
        --confidence high \
        --verify-hours 168 \
        --query "SELECT insider_score FROM user_analysis WHERE wallet LIKE '0xde9f7f4e%'"

    # Grade all pending predictions (run at start of every agent session)
    python3 agents/tools/predict_tool.py grade \
        --agent insider_copy

    # Read active lessons (evolved insights from past mistakes)
    python3 agents/tools/predict_tool.py lessons \
        --agent insider_copy

    # Read accuracy stats
    python3 agents/tools/predict_tool.py stats \
        --agent insider_copy

    # Log a run (cost, findings, accuracy)
    python3 agents/tools/predict_tool.py log-run \
        --agent insider_copy \
        --model claude-opus-4-8 \
        --cost 0.42 \
        --turns 38 \
        --duration 120 \
        --summary "3 active strategies healthy, flagged wallet 0xABC"

Outputs JSON to stdout.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent.parent.parent / "sniper_tracker.db"))

# Tables that live in Supabase only — not present in the local SQLite DB.
# verification_query strings referencing these cannot be auto-graded via SQLite.
SUPABASE_ONLY_TABLES = frozenset({
    "world_events",
    "agent_predictions",
    "agent_lessons",
    "agent_runs",
})

_EXPLICIT_VERIFY_PATTERNS = (
    r"\beod\b",
    r"\bend[-\s]+of[-\s]+day\b",
    r"\bthrough\s+(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b",
    r"\buntil\s+(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+\d{1,2}(?:,\s*\d{4})?\b",
    r"\bbefore\s+(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+\d{1,2}(?:,\s*\d{4})?\b",
    r"\bby\s+(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+\d{1,2}(?:,\s*\d{4})?\b",
    r"\bon\s+(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\s+\d{1,2}(?:,\s*\d{4})?\b",
)
_EXPLICIT_VERIFY_RE = re.compile("|".join(_EXPLICIT_VERIFY_PATTERNS), re.IGNORECASE)


def _supabase_only_table(query: str):
    """Return the first Supabase-only table name found in *query*, or None."""
    q = query.lower()
    for tbl in SUPABASE_ONLY_TABLES:
        if tbl in q:
            return tbl
    return None


def _request(method, path, body=None):
    """Supabase REST API request."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        print(json.dumps({"error": f"HTTP {e.code}: {err_body[:200]}"}))
        sys.exit(1)


def _needs_explicit_verify_at(prediction_text: str, condition_text: str) -> bool:
    """Date/EOD predictions must use an exact verify timestamp, not a rough hour offset."""
    haystack = f"{prediction_text or ''}\n{condition_text or ''}"
    return bool(_EXPLICIT_VERIFY_RE.search(haystack))


def _parse_verify_at(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("--verify-at requires a non-empty ISO timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--verify-at must be a valid ISO timestamp") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _resolve_verify_after(args) -> str:
    if getattr(args, "verify_at", None):
        return _parse_verify_at(args.verify_at)
    if _needs_explicit_verify_at(args.prediction, args.condition):
        raise ValueError(
            "Prediction references a calendar deadline or EOD. Use --verify-at "
            "with an exact ISO timestamp after the true resolution window."
        )
    return (datetime.now(timezone.utc) + timedelta(hours=args.verify_hours)).isoformat()


def _validate_calibration_note(raw: str, confidence: str) -> str:
    """Parse + validate the envelope per spec §6.3. Returns the serialized JSON
    (re-stringified to guarantee canonical form) or raises SystemExit(2)."""
    try:
        env = json.loads(raw)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"calibration-note is not valid JSON: {exc}"}))
        sys.exit(2)
    if not isinstance(env, dict) or "schema_version" not in env or "calibration" not in env:
        print(json.dumps({"ok": False, "error": "calibration-note missing schema_version or calibration"}))
        sys.exit(2)
    cal = env.get("calibration")
    if not isinstance(cal, dict):
        print(json.dumps({"ok": False, "error": "calibration must be an object"}))
        sys.exit(2)
    if "saw_red_flags" not in cal or "verification_query_provided" not in cal:
        print(json.dumps({"ok": False,
                          "error": "calibration missing saw_red_flags or verification_query_provided"}))
        sys.exit(2)
    saw = cal.get("saw_red_flags") or []
    rationale_required = bool(saw) or confidence == "high"
    if rationale_required and not cal.get("confidence_rationale"):
        print(json.dumps({"ok": False,
                          "error": "confidence_rationale required when saw_red_flags non-empty or confidence=high"}))
        sys.exit(2)
    return json.dumps(env)


def cmd_predict(args):
    """Make a verifiable prediction. Writes run_id when --run-id is provided."""
    try:
        verify_at = _resolve_verify_after(args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(2)

    # Determine context_snapshot source:
    # - If --calibration-note provided, validate + use it (v1 wrapper-initiated path).
    # - Else, fall back to --context (legacy behavior).
    context_snapshot = None
    run_id = getattr(args, "run_id", None)
    cal_note = getattr(args, "calibration_note", None)

    if run_id and not cal_note:
        print(json.dumps({"ok": False, "error": "--calibration-note is required when --run-id is provided"}))
        sys.exit(2)

    if cal_note:
        context_snapshot = _validate_calibration_note(cal_note, args.confidence)
    else:
        context_snapshot = getattr(args, "context", None)

    row = {
        "agent_type": args.agent,
        "prediction": args.prediction,
        "condition": args.condition,
        "confidence": args.confidence,
        "verify_after": verify_at,
        "verification_query": args.query,
        "context_snapshot": context_snapshot,
    }
    if run_id:
        row["run_id"] = run_id

    result = _request("POST", "agent_predictions", row)
    pred_id = result[0]["id"] if result else "unknown"
    output = {
        "ok": True,
        "id": pred_id,
        "verify_after": verify_at,
        "message": f"Prediction logged. Will be graded after {verify_at}.",
    }
    if run_id:
        output["run_id"] = run_id
    if not args.query:
        output["manual_grade_required"] = True
        output["warning"] = "No --query provided. This prediction requires manual grading and cannot be auto-graded."
    print(json.dumps(output, indent=2))


def cmd_grade(args):
    """Grade all pending predictions whose verify_after has passed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fetch ungraded predictions past their verify date
    path = (
        f"agent_predictions?agent_type=eq.{args.agent}"
        f"&was_correct=is.null"
        f"&verify_after=lte.{now}"
        f"&order=created_at.asc"
        f"&limit=20"
    )
    pending = _request("GET", path)

    if not pending:
        print(json.dumps({"ok": True, "graded": 0, "message": "No predictions due for grading."}))
        return

    # Connect to SQLite for verification queries
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
    except Exception:
        pass

    graded = []
    for pred in pending:
        pred_id = pred["id"]
        query = pred.get("verification_query")
        result_data = None

        # Try to auto-verify via SQL query (skip Supabase-only tables)
        if query and conn:
            sb_table = _supabase_only_table(query)
            if sb_table:
                result_data = (
                    f"[skipped] verification_query references Supabase-only table "
                    f"'{sb_table}' — grade manually using current world_events data."
                )
            else:
                try:
                    rows = conn.execute(query).fetchall()
                    result_data = json.dumps([dict(r) for r in rows])
                except Exception as e:
                    result_data = f"Query failed: {e}"

        graded.append({
            "id": pred_id,
            "prediction": pred["prediction"],
            "condition": pred["condition"],
            "confidence": pred["confidence"],
            "created_at": pred["created_at"],
            "verification_result": result_data,
        })

    if conn:
        conn.close()

    # Output for the agent to grade (agent makes the was_correct judgment)
    print(json.dumps({
        "ok": True,
        "pending_count": len(graded),
        "predictions": graded,
        "instructions": (
            "For each prediction, determine if it was correct based on the verification_result. "
            "Then call: python3 agents/tools/predict_tool.py mark "
            "--id <prediction_id> --correct true/false --notes 'explanation'"
        ),
    }, indent=2))


def _auto_grade_prediction(prediction_text: str, query_result: list) -> tuple:
    """Attempt to auto-grade a prediction based on its text and query result.

    Returns (was_correct: bool, notes: str) or (None, reason: str) if can't auto-grade.
    """
    import re

    pred_lower = prediction_text.lower()

    # Rule 1: If query returned no rows, entity vanished -> incorrect
    if not query_result:
        return (False, "Verification query returned no results (entity vanished or condition not met)")

    # Rule 2: Extract numeric value from first row's first column
    first_row = query_result[0]
    if not first_row:
        return (False, "Verification query returned empty row")

    # Get the first numeric value from the result
    numeric_value = None
    value_column = None
    for col, val in first_row.items():
        if isinstance(val, (int, float)):
            numeric_value = float(val)
            value_column = col
            break
        if isinstance(val, str):
            try:
                numeric_value = float(val)
                value_column = col
                break
            except (ValueError, TypeError):
                continue

    if numeric_value is None:
        return (None, "No numeric value in query result — needs manual grading")

    # Rule 3a: Sanity-check for probability/price mismatch.
    # If the query returned a value in [0, 1] (likely a Polymarket contract
    # probability) but the prediction text references a threshold > 1 (e.g. a
    # commodity price like "$115"), the numeric comparison is invalid.
    import re as _re
    if 0.0 <= numeric_value <= 1.0:
        large = [float(n) for n in _re.findall(r'\b(\d+(?:\.\d+)?)\b', pred_lower)
                 if float(n) > 1.0]
        if large:
            return (
                None,
                f"Probability/price mismatch: query returned {value_column}={numeric_value} "
                f"(0–1 range, likely a contract probability) but prediction threshold is "
                f"{large[0]}+ (real-world value). Cannot auto-grade — mark manually with "
                f"real price data.",
            )

    # Rule 3: Pattern matching for threshold predictions
    # Patterns: "above X", "at least X", ">= X", "> X", "will maintain ... >= X",
    #           "stay above X", "remain above X", "keep above X"
    threshold_patterns = [
        r'(?:above|at\s+least|>=?|will\s+maintain.*?>=?|stay\s+above|remain\s+above|keep\s+above)\s*(\d+(?:\.\d+)?)',
        r'(?:insider_score|score|wr|win_rate|pnl).*?>=?\s*(\d+(?:\.\d+)?)',
    ]

    for pattern in threshold_patterns:
        match = re.search(pattern, pred_lower)
        if match:
            threshold = float(match.group(1))
            was_correct = numeric_value >= threshold
            notes = f"Value {value_column}={numeric_value} vs threshold {threshold}: {'met' if was_correct else 'not met'}"
            return (was_correct, notes)

    # Patterns: "below X", "under X", "< X", "<= X", "drop below X", "fall below X"
    below_patterns = [
        r'(?:below|under|<=?|drop\s+below|fall\s+below)\s*(\d+(?:\.\d+)?)',
    ]

    for pattern in below_patterns:
        match = re.search(pattern, pred_lower)
        if match:
            threshold = float(match.group(1))
            was_correct = numeric_value < threshold
            notes = f"Value {value_column}={numeric_value} vs threshold {threshold}: {'met' if was_correct else 'not met'}"
            return (was_correct, notes)

    # Patterns: "will have 0", "still have 0", "have 0 open", "0 positions", "zero signals"
    zero_patterns = [
        r'(?:will\s+(?:still\s+)?have|generate)\s+0\b',
        r'\b0\s+(?:open|positions?|signals?|trades?)',
        r'zero\s+(?:open|positions?|signals?|trades?)',
    ]
    for pattern in zero_patterns:
        if re.search(pattern, pred_lower):
            was_correct = numeric_value == 0
            notes = f"Value {value_column}={numeric_value}: {'is zero' if was_correct else 'not zero'}"
            return (was_correct, notes)

    # Patterns: "positive PnL", "positive pnl"
    if re.search(r'positive\s+pnl', pred_lower):
        was_correct = numeric_value > 0
        notes = f"Value {value_column}={numeric_value}: {'positive' if was_correct else 'not positive'}"
        return (was_correct, notes)

    # Patterns: "negative PnL"
    if re.search(r'negative\s+pnl', pred_lower):
        was_correct = numeric_value < 0
        notes = f"Value {value_column}={numeric_value}: {'negative' if was_correct else 'not negative'}"
        return (was_correct, notes)

    # Patterns: "will have >= N" / "will have N+" / "at least N" for count-type predictions
    count_patterns = [
        r'(?:will\s+have|have)\s+>=?\s*(\d+)',
        r'>=?\s*(\d+)\s+(?:open|qualifying|signals?|positions?)',
    ]
    for pattern in count_patterns:
        match = re.search(pattern, pred_lower)
        if match:
            threshold = float(match.group(1))
            was_correct = numeric_value >= threshold
            notes = f"Value {value_column}={numeric_value} vs count threshold {threshold}: {'met' if was_correct else 'not met'}"
            return (was_correct, notes)

    # Can't determine — needs manual grading
    return (None, f"Cannot auto-grade: no matching pattern in prediction text. Result: {value_column}={numeric_value}")


def cmd_auto_grade(args):
    """Auto-grade pending predictions by running verification queries and applying rules."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Fetch ungraded predictions past their verify date
    path = (
        f"agent_predictions?agent_type=eq.{args.agent}"
        f"&was_correct=is.null"
        f"&verify_after=lte.{now}"
        f"&order=created_at.asc"
        f"&limit=50"
    )
    pending = _request("GET", path)

    if not pending:
        print(json.dumps({"ok": True, "auto_graded": 0, "skipped": 0, "total": 0,
                          "message": "No predictions due for grading."}))
        return

    # Connect to SQLite for verification queries
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
    except Exception:
        pass

    auto_graded = 0
    skipped = 0
    skipped_items = []
    graded_items = []

    for pred in pending:
        pred_id = pred["id"]
        query = pred.get("verification_query")
        prediction_text = pred.get("prediction", "")

        # Skip if no verification query
        if not query or not conn:
            skipped += 1
            skipped_items.append({
                "id": pred_id,
                "prediction": prediction_text[:100],
                "reason": "No verification_query" if not query else "No DB connection",
            })
            continue

        # Skip queries that reference Supabase-only tables (not in local SQLite)
        sb_table = _supabase_only_table(query)
        if sb_table:
            skipped += 1
            skipped_items.append({
                "id": pred_id,
                "prediction": prediction_text[:100],
                "reason": (
                    f"verification_query references Supabase-only table '{sb_table}' — "
                    f"cannot auto-grade via SQLite. Use 'mark' to grade manually."
                ),
            })
            continue

        # Run the verification query
        try:
            rows = conn.execute(query).fetchall()
            query_result = [dict(r) for r in rows]
        except Exception as e:
            skipped += 1
            skipped_items.append({
                "id": pred_id,
                "prediction": prediction_text[:100],
                "reason": f"Query failed: {e}",
            })
            continue

        # Attempt auto-grading
        was_correct, notes = _auto_grade_prediction(prediction_text, query_result)

        if was_correct is None:
            # Can't auto-grade — skip for manual review
            skipped += 1
            skipped_items.append({
                "id": pred_id,
                "prediction": prediction_text[:100],
                "reason": notes,
                "verification_result": json.dumps(query_result)[:200],
            })
            continue

        # Grade it by calling mark internally
        mark_path = f"agent_predictions?id=eq.{pred_id}"
        _request("PATCH", mark_path, {
            "was_correct": was_correct,
            "actual_outcome": notes,
            "graded_at": now,
            "grading_notes": f"[auto-grade] {notes}",
        })

        auto_graded += 1
        graded_items.append({
            "id": pred_id,
            "prediction": prediction_text[:100],
            "was_correct": was_correct,
            "notes": notes,
        })

    if conn:
        conn.close()

    print(json.dumps({
        "ok": True,
        "auto_graded": auto_graded,
        "skipped": skipped,
        "total": len(pending),
        "graded": graded_items,
        "skipped_for_manual": skipped_items,
        "message": f"Auto-graded {auto_graded}/{len(pending)} predictions. {skipped} need manual grading.",
    }, indent=2))


def cmd_mark(args):
    """Mark a prediction as correct or incorrect."""
    now = datetime.now(timezone.utc).isoformat()
    correct = args.correct.lower() in ("true", "1", "yes")

    path = f"agent_predictions?id=eq.{args.id}"
    _request("PATCH", path, {
        "was_correct": correct,
        "actual_outcome": args.notes,
        "graded_at": now,
        "grading_notes": args.notes,
    })

    print(json.dumps({
        "ok": True,
        "id": args.id,
        "was_correct": correct,
        "message": f"Prediction marked as {'correct' if correct else 'incorrect'}.",
    }))


def cmd_lessons(args):
    """Read active lessons for an agent."""
    path = (
        f"agent_lessons?agent_type=eq.{args.agent}"
        f"&active=eq.true"
        f"&order=created_at.desc"
        f"&limit=15"
    )
    lessons = _request("GET", path)
    print(json.dumps({
        "ok": True,
        "count": len(lessons),
        "lessons": lessons,
    }, indent=2))


def cmd_learn(args):
    """Record a new lesson from self-grading."""
    row = {
        "agent_type": args.agent,
        "lesson": args.lesson,
        "evidence": args.evidence,
        "accuracy_at_discovery": args.accuracy,
        "predictions_graded": args.graded,
    }
    if args.supersedes:
        row["supersedes"] = args.supersedes
        # Mark old lesson as inactive
        _request("PATCH", f"agent_lessons?id=eq.{args.supersedes}", {"active": False})

    result = _request("POST", "agent_lessons", row)
    lesson_id = result[0]["id"] if result else "unknown"
    print(json.dumps({
        "ok": True,
        "id": lesson_id,
        "message": "Lesson recorded. Will be included in all future runs.",
    }))


def _load_calibration_settings():
    """Load CALIBRATION_* constants from web_settings. Importable lazily so
    tests that stub web_settings don't break the module at import time."""
    try:
        import sniper.settings.web as ws
    except ImportError:
        class _Defaults:
            CALIBRATION_WINDOW_DAYS = 30
            CALIBRATION_MIN_SAMPLE = 10
            CALIBRATION_CATASTROPHIC_THRESHOLD = 0.20
            CALIBRATION_COVERAGE_THRESHOLD = 0.60
            CALIBRATION_HIGH_CONF_THRESHOLD = 0.50
            CALIBRATION_ENABLED_AGENTS = ["insider_copy"]
        return _Defaults()
    return ws


def cmd_calibration_stats_fn(agent_type: str, window_days: int = 30) -> dict:
    """Compute calibration stats JIT from agent_predictions joined to agent_runs.

    Eligibility filter (spec §5.2):
      - agent_predictions.run_id IS NOT NULL
      - parent agent_runs.status = 'success'
      - parent agent_runs.turns > 0

    Returns the dict described in spec §5.3.
    """
    ws = _load_calibration_settings()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)

    # Use PostgREST embedding + filter on the embedded resource to apply the
    # eligibility filter. agent_runs!run_id is the FK relationship; the
    # inner-join modifier '!inner' narrows rows to those with matching parents.
    # We also filter on parent status and turns via dot syntax.
    path = (
        f"agent_predictions"
        f"?select=id,was_correct,confidence,verification_query,created_at,run_id,"
        f"agent_runs!inner(status,turns)"
        f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
        f"&run_id=not.is.null"
        f"&agent_runs.status=eq.success"
        f"&agent_runs.turns=gt.0"
        f"&created_at=gte.{urllib.parse.quote(window_start.isoformat())}"
        f"&order=created_at.desc"
        f"&limit=1000"
    )
    rows = _request("GET", path) or []

    # Aggregate
    total = len(rows)
    graded_rows = [r for r in rows if r.get("was_correct") is not None]
    graded = len(graded_rows)
    ungraded = total - graded
    correct = sum(1 for r in graded_rows if r.get("was_correct"))
    accuracy_pct = round(correct / graded * 100, 1) if graded else 0.0
    with_query = sum(1 for r in rows if r.get("verification_query"))
    verification_coverage_pct = round(with_query / total * 100, 1) if total else 0.0
    graded_coverage_pct = round(graded / total * 100, 1) if total else 0.0

    # Per-confidence breakdown
    by_conf: dict = {}
    for r in rows:
        c = r.get("confidence", "medium") or "medium"
        b = by_conf.setdefault(c, {"total": 0, "graded": 0, "correct": 0})
        b["total"] += 1
        if r.get("was_correct") is not None:
            b["graded"] += 1
            if r.get("was_correct"):
                b["correct"] += 1
    for c, b in by_conf.items():
        b["accuracy_pct"] = round(b["correct"] / b["graded"] * 100, 1) if b["graded"] else 0.0

    # Red flags
    red_flags: list[str] = []
    sample_warning = graded < ws.CALIBRATION_MIN_SAMPLE
    if sample_warning:
        red_flags.append("small_sample")
    if graded >= ws.CALIBRATION_MIN_SAMPLE and accuracy_pct < ws.CALIBRATION_CATASTROPHIC_THRESHOLD * 100:
        red_flags.append("catastrophic_accuracy")
    if total >= ws.CALIBRATION_MIN_SAMPLE and verification_coverage_pct < ws.CALIBRATION_COVERAGE_THRESHOLD * 100:
        red_flags.append("low_verification_coverage")

    # high_confidence_miscalibrated: monotonicity failure
    hc = by_conf.get("high")
    if hc and hc["graded"] >= 5 and hc["accuracy_pct"] < ws.CALIBRATION_HIGH_CONF_THRESHOLD * 100:
        mc = by_conf.get("medium")
        if mc and mc["graded"] >= 1:
            if hc["accuracy_pct"] <= mc["accuracy_pct"]:
                red_flags.append("high_confidence_miscalibrated")
        elif hc["accuracy_pct"] <= accuracy_pct:
            red_flags.append("high_confidence_miscalibrated")

    return {
        "agent_type": agent_type,
        "window_days": window_days,
        "window_start": window_start.isoformat(),
        "computed_at": now.isoformat(),
        "total": total,
        "graded": graded,
        "ungraded": ungraded,
        "graded_coverage_pct": graded_coverage_pct,
        "verification_coverage_pct": verification_coverage_pct,
        "accuracy_pct": accuracy_pct,
        "by_confidence": by_conf,
        "red_flags": red_flags,
        "sample_warning": sample_warning,
    }


def cmd_calibration_stats(args):
    """CLI wrapper: emit JSON to stdout."""
    result = cmd_calibration_stats_fn(args.agent, args.window_days)
    print(json.dumps(result, indent=2))


def _format_calibration_block(stats: dict) -> str:
    """Render the calibration stats as the human-readable block the agent sees.

    Format is spec §6.2. Plain text, grep-able.
    """
    lines = []
    lines.append("## CALIBRATION STATE (last %d days)" % stats.get("window_days", 30))
    lines.append(
        "Predictions: %d total | %d graded (%.1f%%) | %d pending"
        % (stats.get("total", 0), stats.get("graded", 0),
           stats.get("graded_coverage_pct", 0.0),
           stats.get("total", 0) - stats.get("graded", 0))
    )
    lines.append("Overall accuracy: %.1f%%" % stats.get("accuracy_pct", 0.0))
    lines.append("Verification coverage: %.1f%%" % stats.get("verification_coverage_pct", 0.0))
    lines.append("")
    by_conf = stats.get("by_confidence", {}) or {}
    if by_conf:
        lines.append("By confidence:")
        for c in ("high", "medium", "low"):
            b = by_conf.get(c)
            if b:
                lines.append(
                    "  %-6s %d graded, %d correct (%.1f%%)"
                    % (c, b.get("graded", 0), b.get("correct", 0), b.get("accuracy_pct", 0.0))
                )
        lines.append("")

    flags = stats.get("red_flags", []) or []
    if flags:
        lines.append("RED FLAGS:")
        flag_rationales = {
            "catastrophic_accuracy":
                "%.1f%% is worse than useful; predictions likely hallucinated or grading broken"
                % stats.get("accuracy_pct", 0.0),
            "low_verification_coverage":
                "only %.1f%% auto-gradable; calibration itself is fragile"
                % stats.get("verification_coverage_pct", 0.0),
            "high_confidence_miscalibrated":
                'your "high" label does not monotonically beat other buckets',
            "small_sample":
                "only %d graded predictions — stats untrustable" % stats.get("graded", 0),
        }
        for f in flags:
            lines.append("  %-33s — %s" % (f, flag_rationales.get(f, "see spec")))
        lines.append("")
        lines.append("Before you make new predictions:")
        lines.append("  1. Provide verification_query for EVERY prediction (auto-gradable only)")
        lines.append("  2. Downgrade confidence unless you have stronger evidence than last time")
        lines.append("  3. If confidence=high, include calibration_note explaining why this is different")
    else:
        lines.append("RED FLAGS: none")
        lines.append("")
        lines.append("Before you make new predictions:")
        lines.append("  1. Provide verification_query for EVERY prediction (auto-gradable only)")
        lines.append("  2. Apply confidence monotonically: high > medium > low accuracy")
        lines.append("  3. If confidence=high, include calibration_note explaining why")

    return "\n".join(lines)


def cmd_start_run(args):
    """Open an agent_runs row with status='running' and calibration_context populated.

    Returns (via stdout JSON) the run UUID and the formatted calibration block
    for the wrapper to prepend to its prompt.
    """
    stats = cmd_calibration_stats_fn(args.agent, getattr(args, "window_days", 30))
    block = _format_calibration_block(stats)

    row = {
        "agent_type": args.agent,
        "status": "running",
        "calibration_context": stats,
    }
    if getattr(args, "model", None):
        row["model"] = args.model

    result = _request("POST", "agent_runs", row)
    run_id = result[0]["id"] if result else "unknown"

    print(json.dumps({
        "ok": True,
        "run_id": run_id,
        "calibration_block": block,
        "calibration_stats": stats,
    }, indent=2))


_TERMINAL_STATUSES = {"success", "error", "timeout", "budget_exceeded"}


def cmd_finish_run(args):
    """Close an agent_runs row opened by start-run with a terminal status.

    Does NOT write accuracy_pct (schema-drift column; see spec §6.4 + §9 risk 6).
    """
    if args.status not in _TERMINAL_STATUSES:
        print(json.dumps({
            "ok": False,
            "error": f"status must be one of {sorted(_TERMINAL_STATUSES)}, got {args.status!r}",
        }))
        sys.exit(2)

    # Fetch the row's created_at so we can compute duration_seconds server-side.
    duration_seconds = None
    parent = _request("GET", f"agent_runs?select=created_at&id=eq.{args.run_id}&limit=1") or []
    if parent:
        created_raw = parent[0].get("created_at")
        try:
            created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
            duration_seconds = (datetime.now(timezone.utc) - created).total_seconds()
        except Exception:
            duration_seconds = None

    body = {"status": args.status}
    if args.turns is not None:
        body["turns"] = args.turns
    if args.cost_usd is not None:
        body["cost_usd"] = args.cost_usd
    if args.findings_summary is not None:
        body["findings_summary"] = args.findings_summary
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    else:
        body["duration_seconds"] = None

    _request("PATCH", f"agent_runs?id=eq.{args.run_id}", body)
    print(json.dumps({"ok": True, "run_id": args.run_id, "status": args.status}, indent=2))


def cmd_stats(args):
    """Show accuracy statistics for an agent."""
    # All graded predictions
    path = (
        f"agent_predictions?agent_type=eq.{args.agent}"
        f"&was_correct=not.is.null"
        f"&order=created_at.desc"
        f"&limit=100"
    )
    graded = _request("GET", path)

    if not graded:
        print(json.dumps({"ok": True, "total": 0, "message": "No graded predictions yet."}))
        return

    total = len(graded)
    correct = sum(1 for p in graded if p["was_correct"])
    accuracy = correct / total if total > 0 else 0

    # By confidence level
    by_conf = {}
    for p in graded:
        c = p.get("confidence", "medium")
        if c not in by_conf:
            by_conf[c] = {"total": 0, "correct": 0}
        by_conf[c]["total"] += 1
        if p["was_correct"]:
            by_conf[c]["correct"] += 1

    for c in by_conf:
        by_conf[c]["accuracy"] = round(by_conf[c]["correct"] / by_conf[c]["total"] * 100, 1)

    print(json.dumps({
        "ok": True,
        "total_graded": total,
        "correct": correct,
        "accuracy_pct": round(accuracy * 100, 1),
        "by_confidence": by_conf,
        "calibration_note": (
            "Well-calibrated means: high confidence predictions are correct more often than medium, "
            "which are correct more often than low. If that's not the case, adjust your confidence levels."
        ),
    }, indent=2))


def cmd_cleanup(args):
    """Expire old predictions that can never be auto-graded."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Find old predictions with no verification query
    path = (
        f"agent_predictions?agent_type=eq.{args.agent}"
        f"&was_correct=is.null"
        f"&verification_query=is.null"
        f"&verify_after=lt.{seven_days_ago}"
        f"&select=id,prediction"
    )
    expired = _request("GET", path) or []

    for pred in expired:
        _request("PATCH",
            f"agent_predictions?id=eq.{pred['id']}",
            body={"was_correct": False, "graded_at": now,
                  "grading_notes": "expired: no verification_query, past verify_after + 7d grace period"},
        )

    print(json.dumps({
        "ok": True, "expired": len(expired),
        "message": f"Expired {len(expired)} ungradeable predictions for {args.agent}"
    }))


def cmd_log_run(args):
    """Log an agent run with metadata."""
    row = {
        "agent_type": args.agent,
        "model": args.model,
        "cost_usd": args.cost,
        "turns": args.turns,
        "duration_seconds": args.duration,
        "findings_summary": args.summary,
        "status": args.status or "success",
    }
    result = _request("POST", "agent_runs", row)
    run_id = result[0]["id"] if result else "unknown"
    print(json.dumps({"ok": True, "id": run_id}))


def main():
    parser = argparse.ArgumentParser(description="Agent self-improvement tool")
    sub = parser.add_subparsers(dest="command")

    p_predict = sub.add_parser("predict", help="Make a verifiable prediction")
    p_predict.add_argument("--agent", required=True)
    p_predict.add_argument("--prediction", required=True)
    p_predict.add_argument("--condition", required=True, help="How to verify this prediction")
    p_predict.add_argument("--confidence", default="medium", choices=["low", "medium", "high"])
    p_predict.add_argument("--verify-hours", type=int, default=48, help="Hours until verification")
    p_predict.add_argument(
        "--verify-at",
        default=None,
        help="Exact ISO timestamp for verification. Required for date/EOD predictions.",
    )
    p_predict.add_argument("--query", default=None, help="SQL query to auto-check prediction")
    p_predict.add_argument("--context", default=None, help="Context snapshot at prediction time")
    p_predict.add_argument("--run-id", dest="run_id", default=None,
                           help="Parent agent_runs UUID. Required for wrapper-initiated predictions.")
    p_predict.add_argument("--calibration-note", dest="calibration_note", default=None,
                           help="JSON envelope (spec §6.3). Required when --run-id is provided.")

    p_grade = sub.add_parser("grade", help="Grade pending predictions")
    p_grade.add_argument("--agent", required=True)

    p_auto_grade = sub.add_parser("auto-grade", help="Auto-grade predictions using verification queries")
    p_auto_grade.add_argument("--agent", required=True)

    p_mark = sub.add_parser("mark", help="Mark a prediction as correct/incorrect")
    p_mark.add_argument("--id", required=True)
    p_mark.add_argument("--correct", required=True, help="true or false")
    p_mark.add_argument("--notes", default="", help="Grading explanation")

    p_lessons = sub.add_parser("lessons", help="Read active lessons")
    p_lessons.add_argument("--agent", required=True)

    p_learn = sub.add_parser("learn", help="Record a new lesson")
    p_learn.add_argument("--agent", required=True)
    p_learn.add_argument("--lesson", required=True)
    p_learn.add_argument("--evidence", required=True)
    p_learn.add_argument("--accuracy", type=float, default=None)
    p_learn.add_argument("--graded", type=int, default=None)
    p_learn.add_argument("--supersedes", default=None, help="UUID of lesson this replaces")

    p_stats = sub.add_parser("stats", help="Show accuracy statistics")
    p_stats.add_argument("--agent", required=True)

    p_cleanup = sub.add_parser("cleanup", help="Expire old ungradeable predictions")
    p_cleanup.add_argument("--agent", required=True)

    p_cal = sub.add_parser("calibration-stats", help="Compute calibration stats JIT")
    p_cal.add_argument("--agent", required=True)
    p_cal.add_argument("--window-days", type=int, default=30)

    p_start = sub.add_parser("start-run", help="Open an agent run with calibration snapshot")
    p_start.add_argument("--agent", required=True)
    p_start.add_argument("--model", default=None)
    p_start.add_argument("--window-days", type=int, default=30)

    p_finish = sub.add_parser("finish-run", help="Close an agent run with terminal status")
    p_finish.add_argument("--run-id", required=True, dest="run_id")
    p_finish.add_argument("--status", required=True,
                          choices=["success", "error", "timeout", "budget_exceeded"])
    p_finish.add_argument("--turns", type=int, default=None)
    p_finish.add_argument("--cost-usd", type=float, default=None, dest="cost_usd")
    p_finish.add_argument("--findings-summary", default=None, dest="findings_summary")

    p_log = sub.add_parser("log-run", help="Log an agent run")
    p_log.add_argument("--agent", required=True)
    p_log.add_argument("--model", default=None)
    p_log.add_argument("--cost", type=float, default=None)
    p_log.add_argument("--turns", type=int, default=None)
    p_log.add_argument("--duration", type=float, default=None)
    p_log.add_argument("--summary", default=None)
    p_log.add_argument("--status", default="success")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        "calibration-stats": cmd_calibration_stats,
        "start-run": cmd_start_run,
        "finish-run": cmd_finish_run,
        "predict": cmd_predict,
        "grade": cmd_grade,
        "auto-grade": cmd_auto_grade,
        "mark": cmd_mark,
        "lessons": cmd_lessons,
        "learn": cmd_learn,
        "stats": cmd_stats,
        "cleanup": cmd_cleanup,
        "log-run": cmd_log_run,
    }[args.command](args)


if __name__ == "__main__":
    main()
