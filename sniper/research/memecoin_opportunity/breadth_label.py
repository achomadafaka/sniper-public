"""`radar-buyer-breadth-v1` — an immutable decision label. It is never an arm.

The label records the buyer-breadth class of a launch. It stores the class at the moment
the engine decided that launch. It takes no slot. It emits no receipt. It changes no
behaviour.

**Why a label and not an arm.** An arm on `eligible_buyer_count >= 9` would take the
control's slot. It would take that slot on the launches under test. The control cohort
would then hold only low-breadth launches. The matched comparison would measure the slot
rule instead of the market. `pump-creator-prior-v1` was retired for that failure.

**Why the label runs before arm selection.** The engine builds no control arm when a
confirmation or an adaptive arm fires. Those arms need a high buyer count. A label bound
to the control receipt would therefore drop most `pass` rows. The label runs once per
launch, ahead of arm selection, so the class does not depend on which arm won.

**Why the thresholds are frozen here.** The study that motivated the label chose 9 from
terciles of the corpus that then scored it. That number is in-sample. A forward clock is
honest only if the threshold cannot move. So the threshold is a constant in a file with
one purpose. The contract names it:
`docs/superpowers/specs/2026-08-19-radar-buyer-breadth-preregistration.md`.

**Why pump.fun only.** `docs/superpowers/specs/2026-08-07-radar-venue-scoring-design.md`
forbids shared controls across venues. Bags and Pons need their own rule versions and
their own clocks. This rule labels one venue.

The label is paper research. It never signs. It never submits. It never reserves. It
never informs funded authority.
"""

from __future__ import annotations

import sqlite3

from .identity import content_fingerprint, require_utc_iso


RULE_VERSION = "radar-buyer-breadth-v1"

# FROZEN 2026-08-19. Do not retune. A change needs a new rule_version and a new clock.
PASS_MIN_BUYERS = 9
CONTROL_MAX_BUYERS = 3

# FROZEN. One venue per rule version. See the venue scoring design.
LABELLED_VENUE_ID = "pumpfun_solana"

CLASS_PASS = "pass"
CLASS_MIDDLE = "middle"
CLASS_CONTROL = "control"
CLASS_CENSORED = "censored"

CENSOR_UNKNOWN_BUYER_COUNT = "unknown_buyer_count"


def classify(eligible_buyer_count: int | None) -> str:
    """Return the frozen breadth class. An unknown count censors."""
    if eligible_buyer_count is None:
        return CLASS_CENSORED
    count = int(eligible_buyer_count)
    if count >= PASS_MIN_BUYERS:
        return CLASS_PASS
    if count <= CONTROL_MAX_BUYERS:
        return CLASS_CONTROL
    return CLASS_MIDDLE


def record_breadth_label(
    conn: sqlite3.Connection,
    *,
    launch_id: str,
    network_id: str,
    venue_id: str,
    decision_snapshot_id: str,
    features: dict[str, object],
    observed_at: str,
) -> str | None:
    """Freeze the breadth class of one launch. The first write wins.

    A censored launch gets a row with an explicit reason. An absent row would be
    ambiguous: it could mean an unknown count, an old schema, or a lost write. The gate
    must be able to measure censor coverage, so the censor is a fact, not a gap.

    Returns the class, or None when the venue is out of scope. A failure here must never
    change a decision. The label observes a decision the engine already made.
    """
    if str(venue_id) != LABELLED_VENUE_ID:
        return None
    raw = features.get("eligible_buyer_count")
    breadth_class = classify(raw)
    censored = breadth_class == CLASS_CENSORED
    buyer_count = None if censored else int(raw)
    payload = {
        "launch_id": str(launch_id),
        "rule_version": RULE_VERSION,
        "breadth_class": breadth_class,
        "eligible_buyer_count": buyer_count,
        "pass_min_buyers": PASS_MIN_BUYERS,
        "control_max_buyers": CONTROL_MAX_BUYERS,
    }
    conn.execute(
        "INSERT OR IGNORE INTO op_breadth_labels("
        "launch_id,network_id,venue_id,decision_snapshot_id,rule_version,breadth_class,"
        "censor_reason,eligible_buyer_count,entity_buyer_count,launch_mode,"
        "decision_age_ms,progress_bps,modeled_round_trip_cost_bps,observed_at,"
        "row_fingerprint,inserted_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(launch_id), str(network_id), str(venue_id), str(decision_snapshot_id),
            RULE_VERSION, breadth_class,
            CENSOR_UNKNOWN_BUYER_COUNT if censored else None,
            buyer_count,
            # A shadow measurement only. Point-in-time entity edges do not exist. A map
            # built from a whole corpus would look ahead, so this column stays NULL.
            None,
            features.get("launch_mode"),
            _optional_int(features.get("decision_age_ms")),
            _optional_int(features.get("progress_bps")),
            _optional_int(features.get("modeled_round_trip_cost_bps")),
            require_utc_iso(observed_at),
            content_fingerprint(payload),
            require_utc_iso(observed_at),
        ),
    )
    return breadth_class


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
