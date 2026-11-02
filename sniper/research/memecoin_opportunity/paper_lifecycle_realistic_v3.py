"""`radar-realistic-v3`: the stop measures the token, not the cost of getting in.

Under `radar-realistic-v2` both the +1,200 target and the -800 stop read the same
number: the round trip against the cash entry. That number does not start at zero. The
first executable quote on `pumpfun_solana` has a median of **-681 bps** (p25 -761,
p10 -1523), because a $25 round trip on a new pool pays fees and impact on both legs.
A stop 800 bps below the cash entry therefore sits about 120 bps under the median
STARTING position. 37% of v6 positions were at or below it on their first look, and
`stop_first` fired on 56% of them for a mean of -2,519 while the same alerts' mark-based
MAE median was -250. The stop was measuring admission drag.

v3 keeps every v2 number recoverable and changes one thing: the stop is measured from
the position's own first executable quote.

  baseline            the scheduled 5 s executable quote, and only that one
  target      +1,200  against the CASH entry, unchanged from v2
  stop          -800  against the BASELINE
  censor              when the baseline is absent, or completes late

The baseline must be ON TIME. A first quote that lands after
`MAX_BASELINE_AGE_MS` would let a slow provider retroactively forgive a loss the
position had already taken, so a late baseline censors the position instead of rebasing
it. `MAX_BASELINE_AGE_MS` stays `None` until the paper sampler's 24-hour window closes
and the operator preregisters the measured value here; activation fails closed until
then, because any number chosen before the measurement is a guess.

v3 owns no quote stream. It reads the observation series `radar-realistic-v2` already
writes for the identical positions, so the two contracts are scored on exactly the same
quotes.

It is NOT free. A second stream was avoided; provider load still rises. v2 used to stop
quoting a position at its own outcome, and 56% of v6 positions stopped early. To give v3
a series to read, `_active_entries` now keeps a decided position in the collection loop
until the 90 s terminal age, so those positions consume up to 17 further buckets each.
Any sampler window recorded before that change measures the old load and cannot set
`MAX_BASELINE_AGE_MS`.

v3 is paper-only. It never signs, submits, or informs funded authority.
"""

from __future__ import annotations

import json
import sqlite3

from .identity import canonical_json, content_fingerprint, require_utc_iso
from .paper_lifecycle import (
    EXECUTION_HAIRCUT_BPS,
    EXIT_AGE_MS,
    QUOTE_CADENCE_MS,
    TERMINAL_AGE_MS,
    TERMINAL_GRACE_MS,
    _active_entries,
    _parse,
    _sqlite_atomic,
)
from .paper_lifecycle import LIFECYCLE_VERSION as SOURCE_LIFECYCLE_VERSION
from .store import ImmutableConflict


LIFECYCLE_VERSION = "radar-realistic-v3"
BASELINE_AGE_MS = QUOTE_CADENCE_MS
TARGET_BPS = 1_200
STOP_MOVE_BPS = -800

# PREREGISTERED 2026-08-19 from the SEALED `paper-sampler-v2` window, by the rule that
# was frozen before that window existed. Derived, never chosen:
#
#   window        2026-08-11T20:21:56Z .. 2026-08-12T20:21:56Z (24h)
#   eligible      38
#   with_baseline 35
#   coverage      9,211 bps
#   p90           14,032 ms  ->  MAX_BASELINE_AGE_MS = 15,000
#
# Reproduce read-only: `python3 scripts/radar_v3_baseline_age_preregistration.py`.
#
# Known limit, accepted by the operator: the window predates the 2026-08-19 cutover, so
# it measures the previous release's load. The alternative was a new sampler identity and
# a fresh window, which costs a day. A window SEALS permanently, so waiting could never
# have produced a second v2 measurement.
#
# Never re-derive this from an outcome. A different number needs a new sampler identity
# and a selection rule frozen BEFORE its window.
MAX_BASELINE_AGE_MS: int | None = 15_000


class BaselineAgeNotPreregistered(RuntimeError):
    """v3 cannot open a cohort before the sampler measures the baseline age."""


class ActivationNotAuthorised(RuntimeError):
    """The preregistered constant may ship while the cohort stays closed.

    Setting `MAX_BASELINE_AGE_MS` used to be the activation, so shipping the measured
    integer opened an immutable cohort at the next worker start with no separate
    operator step. The control below restores that step.
    """


def activation_authorised(env=None) -> bool:
    """Default OFF. Opening the cohort is a deliberate, separate operator action."""
    import os

    source = os.environ if env is None else env
    return str(source.get("MEMECOIN_RADAR_REALISTIC_V3_ACTIVATE", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _config() -> dict[str, object]:
    return {
        "clip_usd_micros": 25_000_000,
        "target_bps": TARGET_BPS,
        "target_reference": "cash_entry",
        "stop_move_bps": STOP_MOVE_BPS,
        "stop_reference": "on_time_first_quote",
        "baseline_age_ms": BASELINE_AGE_MS,
        "max_baseline_age_ms": MAX_BASELINE_AGE_MS,
        "quote_cadence_ms": QUOTE_CADENCE_MS,
        "exit_age_ms": EXIT_AGE_MS,
        "terminal_grace_ms": TERMINAL_GRACE_MS,
        "observation_source_lifecycle": SOURCE_LIFECYCLE_VERSION,
        "execution_haircut_bps_per_leg": EXECUTION_HAIRCUT_BPS,
        "paper_only": True,
    }


def stored_config(conn: sqlite3.Connection) -> dict[str, object] | None:
    """The config frozen INTO the activation row, or None before activation.

    The scorer must read this, never the current module constants. Otherwise the
    immutable row can claim one contract while the scoring uses another.
    """
    row = conn.execute(
        "SELECT config_json FROM op_paper_lifecycle_activations WHERE lifecycle_version=?",
        (LIFECYCLE_VERSION,),
    ).fetchone()
    if row is None or not row["config_json"]:
        return None
    return json.loads(row["config_json"])


def activate_realistic_v3(
    conn: sqlite3.Connection,
    *,
    now_iso: str,
    authorised: bool | None = None,
) -> str:
    """Create the immutable v3 cohort boundary once, or refuse.

    An EXISTING cohort keeps running whatever the control says: activation is one-way,
    so turning the control back off never closes a cohort or changes its contract.
    """
    if MAX_BASELINE_AGE_MS is None:
        raise BaselineAgeNotPreregistered(
            "set MAX_BASELINE_AGE_MS from the paper sampler window before activating v3"
        )
    current = _config()
    existing = stored_config(conn)
    if existing is not None:
        # A repeated activation must prove the contract has not drifted. The row is
        # immutable, so a changed constant would otherwise score an old cohort by new
        # thresholds without any signal.
        if existing != current:
            differing = sorted(
                k for k in set(existing) | set(current)
                if existing.get(k) != current.get(k)
            )
            raise ImmutableConflict(
                "realistic-v3 activation config drifted from the stored cohort contract: "
                f"{differing!r} (stored={ {k: existing.get(k) for k in differing} !r}, "
                f"current={ {k: current.get(k) for k in differing} !r})"
            )
        return LIFECYCLE_VERSION
    if not (activation_authorised() if authorised is None else authorised):
        raise ActivationNotAuthorised(
            "set MEMECOIN_RADAR_REALISTIC_V3_ACTIVATE=1 to open the realistic-v3 cohort"
        )
    activated_at = require_utc_iso(now_iso)
    values = {
        "lifecycle_version": LIFECYCLE_VERSION,
        "activated_at": activated_at,
        "config_json": canonical_json(_config()),
    }
    fingerprint = content_fingerprint(values)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO op_paper_lifecycle_activations "
        "(lifecycle_version,activated_at,config_json,row_fingerprint,inserted_at) "
        "VALUES (?,?,?,?,?)",
        (LIFECYCLE_VERSION, activated_at, values["config_json"], fingerprint, activated_at),
    )
    if cursor.rowcount == 0:
        row = conn.execute(
            "SELECT row_fingerprint FROM op_paper_lifecycle_activations "
            "WHERE lifecycle_version=?",
            (LIFECYCLE_VERSION,),
        ).fetchone()
        if row is None:
            raise ImmutableConflict("paper lifecycle activation disappeared")
    return LIFECYCLE_VERSION


def _observations(conn: sqlite3.Connection, request_id: str):
    """The v2 executable series for one position, in scheduled order."""
    return conn.execute(
        "SELECT observation_id,scheduled_age_ms,return_bps,floor_out_atomic,"
        "quote_completed_at FROM op_paper_exit_observations "
        "WHERE request_id=? AND lifecycle_version=? AND status='executable' "
        "ORDER BY scheduled_age_ms",
        (request_id, SOURCE_LIFECYCLE_VERSION),
    ).fetchall()


def _append_outcome(
    conn: sqlite3.Connection,
    *,
    entry,
    status: str,
    result_code: str,
    observation_id: str | None,
    exit_floor_atomic: int | None,
    return_bps: int | None,
    baseline_return_bps: int | None,
    baseline_move_bps: int | None,
    reason_code: str | None,
    outcome_at: str,
) -> None:
    values = {
        "outcome_id": content_fingerprint({
            "request_id": entry["request_id"],
            "lifecycle_version": LIFECYCLE_VERSION,
        }),
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_VERSION,
        "status": status,
        "result_code": result_code,
        "terminal_observation_id": observation_id,
        "entry_quote_atomic": int(entry["quote_in_atomic"]),
        "exit_floor_atomic": exit_floor_atomic,
        "return_bps": return_bps,
        "baseline_return_bps": baseline_return_bps,
        "baseline_move_bps": baseline_move_bps,
        "outcome_at": outcome_at,
        "exit_reason_codes_json": (
            canonical_json([reason_code]) if reason_code else None
        ),
    }
    fingerprint = content_fingerprint(values)
    stored = {
        **values,
        "entry_quote_atomic": _sqlite_atomic(values["entry_quote_atomic"]),
        "exit_floor_atomic": _sqlite_atomic(values["exit_floor_atomic"]),
    }
    conn.execute(
        "INSERT INTO op_paper_outcomes "
        f"({','.join(values)},row_fingerprint,inserted_at) "
        f"VALUES ({','.join('?' for _ in values)},?,?)",
        (*stored.values(), fingerprint, outcome_at),
    )


def _result_code(
    scheduled_age_ms: int, return_bps: int, move_bps: int, config: dict[str, object]
) -> str | None:
    """Score against the cohort's STORED contract, never the current constants.

    The activation row is immutable; the module constants are not. Reading the constants
    here let a live cohort be scored by thresholds it never agreed to.
    """
    if scheduled_age_ms >= int(config["exit_age_ms"]):
        return "time_exit"
    if return_bps >= int(config["target_bps"]):
        return "target_first"
    if move_bps <= int(config["stop_move_bps"]):
        return "stop_first"
    return None


def drive_realistic_v3(conn: sqlite3.Connection, *, now_iso: str) -> dict[str, int]:
    """Score every open v3 position against the v2 observation series."""
    now = require_utc_iso(now_iso)
    activation = conn.execute(
        "SELECT activated_at FROM op_paper_lifecycle_activations WHERE lifecycle_version=?",
        (LIFECYCLE_VERSION,),
    ).fetchone()
    counters = {"eligible": 0, "completed": 0, "censored": 0, "waiting": 0}
    if activation is None:
        return counters
    cohort_config = stored_config(conn)
    if cohort_config is None:
        return counters
    max_baseline_age_ms = int(cohort_config["max_baseline_age_ms"])
    for entry in _active_entries(
        conn, activation["activated_at"], LIFECYCLE_VERSION,
    ):
        counters["eligible"] += 1
        elapsed_ms = max(
            0,
            int((_parse(now) - _parse(entry["receipt_at"])).total_seconds() * 1_000),
        )
        terminal = elapsed_ms >= TERMINAL_AGE_MS
        rows = _observations(conn, entry["request_id"])
        baseline = next(
            (row for row in rows if row["scheduled_age_ms"] == BASELINE_AGE_MS), None,
        )

        def censor(reason: str) -> None:
            _append_outcome(
                conn,
                entry=entry,
                status="censored",
                result_code="route_censored",
                observation_id=None,
                exit_floor_atomic=None,
                return_bps=None,
                baseline_return_bps=None,
                baseline_move_bps=None,
                reason_code=reason,
                outcome_at=now,
            )
            counters["censored"] += 1

        if baseline is None:
            # Not absent yet -- the 5 s quote may still be in flight.
            if terminal:
                censor("baseline_absent")
            else:
                counters["waiting"] += 1
            continue
        baseline_age_ms = max(
            0,
            int(
                (
                    _parse(baseline["quote_completed_at"]) - _parse(entry["receipt_at"])
                ).total_seconds()
                * 1_000
            ),
        )
        if baseline_age_ms > max_baseline_age_ms:
            # A late first quote must never rebase away a loss the position already took.
            censor("baseline_late")
            continue
        baseline_return_bps = int(baseline["return_bps"])
        for row in rows:
            return_bps = int(row["return_bps"])
            move_bps = return_bps - baseline_return_bps
            code = _result_code(
                int(row["scheduled_age_ms"]), return_bps, move_bps, cohort_config
            )
            if code is None:
                continue
            _append_outcome(
                conn,
                entry=entry,
                status="valid",
                result_code=code,
                observation_id=row["observation_id"],
                exit_floor_atomic=int(row["floor_out_atomic"]),
                return_bps=return_bps,
                baseline_return_bps=baseline_return_bps,
                baseline_move_bps=move_bps,
                reason_code=None,
                outcome_at=now,
            )
            counters["completed"] += 1
            break
        else:
            if terminal:
                censor("no_terminal_observation")
            else:
                counters["waiting"] += 1
    return counters
