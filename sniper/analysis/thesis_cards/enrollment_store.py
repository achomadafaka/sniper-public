"""Supabase-backed durable store for forward-paper enrollments (D3).

Same `upsert(enrollment) -> PaperEnrollment` shape as InMemoryPaperEnrollmentStore, so
`enroll_profile(..., store=...)` works against either. Persistence only — NO live
trading, NO sizing, NO paper evaluation, NO verdict mutation. State advancement +
forward_metric/verdict are the D4 forward-tracking job's concern; this store never
writes them.

Idempotency: insert-if-absent on the unique key (wallet, card_id, card_version,
profile_run_id) via PostgREST `resolution=ignore-duplicates`. On a key collision the
existing row is returned UNCHANGED — a re-run never resets a row that D4 has already
advanced to active/completed/killed/promoted.

The HTTP transport is injectable (`request=`) so tests run with a fake backend and no
network; the default uses `bot.supabase_helper._request` (service key + retry).
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

from sniper.analysis.thesis_cards.enrollment import PaperEnrollment, _reachable

_TABLE = "paper_card_enrollments"
_KEY_COLS = ("wallet", "card_id", "card_version", "profile_run_id")

Request = Callable[..., Any]   # (method, path, body=None, extra_headers=None) -> json


def _default_request(method: str, path: str, body: Any = None, extra_headers: dict | None = None) -> Any:
    from bot.supabase_helper import _request   # lazy: keep this module import-clean / env-free
    return _request(method, path, body, extra_headers)


class SupabasePaperEnrollmentStore:
    """Durable enrollment store behind the same `.upsert()` shape as the in-memory one."""

    def __init__(self, request: Request | None = None) -> None:
        self._request = request or _default_request

    @staticmethod
    def _row(e: PaperEnrollment) -> dict[str, Any]:
        # NB: no live-trade fields; forward_metric/verdict left to D4.
        row = {
            "wallet": e.wallet,
            "card_id": e.card_id,
            "card_version": e.card_version,
            "profile_run_id": e.profile_run_id,
            "rank": e.rank,
            "q_value": e.q_value,
            "evidence_state": e.evidence_state,
            "data_tier": e.data_tier,
            "forward_test_design": dict(e.forward_test_design),
            "enrolled_effect": e.enrolled_effect,
            "state": e.state,
        }
        if e.enrolled_at is not None:                    # backtest (as-of T); else DB default now()
            row["enrolled_at"] = datetime.fromtimestamp(
                int(e.enrolled_at), tz=timezone.utc).isoformat()
        return row

    @staticmethod
    def _to_enrollment(row: dict[str, Any]) -> PaperEnrollment:
        return PaperEnrollment(
            wallet=row["wallet"],
            card_id=row["card_id"],
            card_version=row["card_version"],
            profile_run_id=row["profile_run_id"],
            rank=row["rank"],
            q_value=row.get("q_value"),
            evidence_state=row["evidence_state"],
            data_tier=row.get("data_tier"),
            forward_test_design=row["forward_test_design"],
            state=row.get("state", "queued"),
            enrolled_effect=row.get("enrolled_effect"),
            enrolled_at=row.get("enrolled_at"),
            forward_metric=row.get("forward_metric") or {},
            verdict=row.get("verdict"),
        )

    def _fetch(self, e: PaperEnrollment) -> dict[str, Any] | None:
        filters = "&".join(f"{c}=eq.{urllib.parse.quote(str(getattr(e, c)))}" for c in _KEY_COLS)
        rows = self._request("GET", f"{_TABLE}?select=*&{filters}&limit=1")
        return rows[0] if rows else None

    def upsert(self, enrollment: PaperEnrollment) -> PaperEnrollment:
        """Insert if absent; on key collision return the EXISTING row unchanged."""
        conflict = ",".join(_KEY_COLS)
        inserted = self._request(
            "POST", f"{_TABLE}?on_conflict={conflict}", [self._row(enrollment)],
            {"Prefer": "resolution=ignore-duplicates,return=representation"},
        )
        if inserted:                                  # newly inserted -> its representation
            return self._to_enrollment(inserted[0])
        existing = self._fetch(enrollment)            # collision -> existing, state preserved
        return self._to_enrollment(existing) if existing else enrollment

    # --- D4 forward-tracking surface ---
    def iter_active(self) -> list[dict]:
        """Rows in state queued/active, as D4 evaluation row dicts (enrolled_at -> unix)."""
        rows = self._request("GET", f"{_TABLE}?select=*&state=in.(queued,active)") or []
        return [self._tracking_row(r) for r in rows]

    def update_tracking(self, *, key: dict, expected_state: str, new_state: str,
                        forward_metric: dict, verdict: str | None) -> bool:
        """Conditional advance: PATCH only where state = expected_state (concurrency-safe)."""
        if not _reachable(expected_state, new_state):
            raise ValueError(f"illegal paper state transition: {expected_state} -> {new_state}")
        filters = "&".join(f"{c}=eq.{urllib.parse.quote(str(key[c]))}" for c in _KEY_COLS)
        body = {"state": new_state, "forward_metric": forward_metric, "verdict": verdict,
                "updated_at": datetime.now(timezone.utc).isoformat()}
        updated = self._request(
            "PATCH", f"{_TABLE}?{filters}&state=eq.{expected_state}", body,
            {"Prefer": "return=representation"},
        )
        return bool(updated)

    @staticmethod
    def _tracking_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "wallet": row["wallet"], "card_id": row["card_id"],
            "card_version": row["card_version"], "profile_run_id": row["profile_run_id"],
            "state": row["state"], "enrolled_at": _iso_to_unix(row.get("enrolled_at")),
            "enrolled_effect": row.get("enrolled_effect"),
            "forward_test_design": row.get("forward_test_design") or {},
            "forward_metric": row.get("forward_metric") or {}, "verdict": row.get("verdict"),
        }


def _iso_to_unix(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
