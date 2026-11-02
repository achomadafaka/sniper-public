"""The token EPISODE is the experimental unit: the first eligible trusted BUY on a (chain, token)
opens one; later signals while it's open bump consensus but create no new sample. Structural dedup."""
from __future__ import annotations
import hashlib
import math
from datetime import datetime


EPISODE_QUIET_GAP_S = 3600.0


def _episode_id(chain_id: str, token_address: str, started_ts: str) -> str:
    raw = f"{chain_id}:{token_address}:{started_ts}".encode()
    return "ep_" + hashlib.sha1(raw).hexdigest()[:16]


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def resolve_episode(conn, chain_id, token_address, signal_id, now_iso, *,
                    source_price=None, executable_price=None,
                    quiet_gap_s=EPISODE_QUIET_GAP_S):
    """Resolve exact-coin episode membership once per upstream event.

    A greater-than-one-hour quiet gap closes the old episode. Invalid/mixed timestamps never
    force a rollover; the evidence stays in the existing episode instead of inventing order.
    """
    prior_signal = conn.execute(
        "SELECT episode_id FROM memecoin_episode_signals WHERE upstream_event_id=?",
        (signal_id,),
    ).fetchone()
    if prior_signal is not None:
        return prior_signal[0], False
    row = conn.execute(
        "SELECT episode_id, started_ts, last_signal_ts, first_signal_id "
        "FROM memecoin_token_episodes "
        "WHERE chain_id=? AND token_address=? AND status='open'",
        (chain_id, token_address)).fetchone()
    if row is not None:
        eid = row[0]
        prior_at = _parse(row[2] or row[1])
        current_at = _parse(now_iso)
        gap = ((current_at - prior_at).total_seconds()
               if prior_at is not None and current_at is not None else None)
        if gap is not None and gap > float(quiet_gap_s):
            conn.execute(
                "UPDATE memecoin_token_episodes SET status='closed', ended_ts=? "
                "WHERE episode_id=? AND status='open'",
                (now_iso, eid),
            )
            row = None
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO memecoin_episode_signals(episode_id, upstream_event_id, "
                "observed_at, source_price, executable_price) VALUES(?,?,?,?,?)",
                (eid, signal_id, now_iso, _positive(source_price), _positive(executable_price)),
            )
            # Legacy rows predate exact membership. Seeding their first signal must not turn the
            # original count of one into two.
            increment = cur.rowcount == 1 and signal_id != row[3]
            if cur.rowcount == 1:
                conn.execute(
                    "UPDATE memecoin_token_episodes SET last_signal_ts=?, "
                    "consensus_cluster_count=consensus_cluster_count+? WHERE episode_id=?",
                    (now_iso, 1 if increment else 0, eid),
                )
            conn.commit()
            return eid, False
    eid = _episode_id(chain_id, token_address, now_iso)
    conn.execute("INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, "
                 "started_ts, first_signal_id, consensus_cluster_count, status, last_signal_ts, "
                 "first_source_price, first_executable_price) "
                 "VALUES(?,?,?,?,?,1,'open',?,?,?)",
                 (eid, chain_id, token_address, now_iso, signal_id, now_iso,
                  _positive(source_price), _positive(executable_price)))
    conn.execute(
        "INSERT INTO memecoin_episode_signals(episode_id, upstream_event_id, observed_at, "
        "source_price, executable_price) VALUES(?,?,?,?,?)",
        (eid, signal_id, now_iso, _positive(source_price), _positive(executable_price)),
    )
    conn.commit()
    return eid, True


def episode_facts(conn, episode_id, *, current_source_price=None,
                  current_executable_price=None):
    row = conn.execute(
        "SELECT first_signal_id, consensus_cluster_count, first_source_price, "
        "first_executable_price, started_ts, last_signal_ts FROM memecoin_token_episodes "
        "WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    if row is None:
        return {}
    first_source = _positive(row[2])
    first_executable = _positive(row[3])
    current_source = _positive(current_source_price)
    current_executable = _positive(current_executable_price)

    def runup(current, first):
        return ((current / first - 1.0) * 100.0
                if current is not None and first is not None else None)

    return {
        "episode_first_event_id": row[0],
        "episode_entry_sequence": int(row[1] or 0),
        "episode_first_source_price": first_source,
        "episode_first_executable_price": first_executable,
        "episode_source_runup_pct": runup(current_source, first_source),
        "episode_executable_runup_pct": runup(current_executable, first_executable),
        "episode_started_at": row[4],
        "episode_last_signal_at": row[5],
    }
