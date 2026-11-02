from __future__ import annotations

import json
from datetime import datetime, timedelta

from sniper.analysis.memecoin_thesis import thesis_stance

from .controls import select_past_control
from .evaluator import evaluate_stream, observation_order_key, process_observation
from .paper import open_candidate_position, sweep_candidate_positions
from .seeds import seed_v0_candidates
from .store import ensure_strategy_tables, list_candidates, record_evaluation


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("candidate live timestamps must be timezone aware")
    return parsed


def _number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def _observation(row):
    observation = dict(row)
    try:
        raw = json.loads(observation.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
    if observation.get("raw_type") == "thesis_created":
        score = _number(body.get("percentageUnrealizedPnl"))
        observation["thesis_score"] = score
        observation["thesis_stance"] = thesis_stance(score)
    else:
        observation["thesis_score"] = None
        observation["thesis_stance"] = None
    observation["liquidity"] = None
    observation["token_age_s"] = None
    mint = str(observation.get("mint") or "")
    observation["mint"] = mint.lower() if mint.lower().startswith("0x") else mint
    return observation


class CandidateLiveEngine:
    """Forward-only adapter over the pure evaluator and separate paper ledgers."""

    def __init__(self, conn, born_at):
        self.conn = conn
        ensure_strategy_tables(conn)
        seed_v0_candidates(conn, born_at)

    def _stage(self, candidate):
        if candidate.status != "promoted_first_class":
            return "candidate", candidate.discovery_cutoff
        row = self.conn.execute(
            "SELECT evidence_cutoff FROM memecoin_candidate_stages WHERE candidate_id=? "
            "AND stage_name='first_class_paper'", (candidate.candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("promoted candidate is missing its approved stage")
        return "first_class_paper", row[0]

    def _pending(self, candidate, now_iso, evidence_stage, cutoff):
        return [_observation(row) for row in self.conn.execute(
            "SELECT f.* FROM memecoin_feed_observations_v2 f "
            "WHERE f.capture_version>=? AND f.observed_at>=? AND f.observed_at<=? "
            "AND COALESCE(f.network_id,'')!='' AND COALESCE(f.mint,'')!='' "
            "AND NOT EXISTS (SELECT 1 FROM memecoin_candidate_signals s "
            " WHERE s.candidate_id=? AND s.evidence_stage=? "
            " AND s.upstream_observation_id=f.id) "
            "ORDER BY f.observed_at,COALESCE(f.source_ts,''),f.id LIMIT 1000",
            (candidate.capture_version_floor, cutoff, now_iso,
             candidate.candidate_id, evidence_stage),
        )]

    def _history(self, candidate, now_iso, pending, cutoff):
        lookback = max(
            int(candidate.predicate.get("window_s", 0)),
            int(candidate.predicate.get("freshness_s", 120)),
            int(candidate.controls.get("window_s", 0)),
        )
        earliest = min((_time(row["observed_at"]) for row in pending), default=_time(now_iso))
        latest = max((_time(row["observed_at"]) for row in pending), default=_time(now_iso))
        start = max(_time(cutoff), earliest - timedelta(seconds=lookback))
        return [_observation(row) for row in self.conn.execute(
            "SELECT * FROM memecoin_feed_observations_v2 "
            "WHERE capture_version>=? AND observed_at>=? AND observed_at<=? "
            "AND COALESCE(network_id,'')!='' AND COALESCE(mint,'')!='' "
            "ORDER BY observed_at, COALESCE(source_ts,''), id",
            (candidate.capture_version_floor, start.isoformat(), min(_time(now_iso), latest).isoformat()),
        )]

    def process_observations(self, now_iso):
        requests = {}
        matches = signals = 0
        for candidate in list_candidates(
            self.conn, statuses={"candidate_active", "promoted_first_class"},
        ):
            evidence_stage, cutoff = self._stage(candidate)
            pending = self._pending(candidate, now_iso, evidence_stage, cutoff)
            if not pending:
                continue
            history = self._history(candidate, now_iso, pending, cutoff)
            replay = evaluate_stream(candidate, history, "replay", now_iso)
            replay_by_id = {result.observation_id: result for result in replay}
            for observation in sorted(pending, key=observation_order_key):
                evaluation = process_observation(
                    candidate, observation, history, "live", now_iso,
                )
                control = None
                if evaluation.matched and candidate.controls.get("enabled"):
                    annotated = []
                    for row in history:
                        item = dict(row)
                        item["predicate_matched"] = replay_by_id[row.get("id")].matched
                        annotated.append(item)
                    control = select_past_control(
                        observation, annotated,
                        window_s=int(candidate.controls.get("window_s", 1800)),
                    )
                stored = record_evaluation(
                    self.conn, candidate, observation, evaluation, now_iso, control=control,
                    commit=False, evidence_stage=evidence_stage,
                )
                signals += 1
                if not stored["matched"]:
                    continue
                matches += 1
                expires = (_time(stored["observed_at"])
                           + timedelta(seconds=candidate.timecap_s)).isoformat()
                key = (stored["network_id"], stored["mint"], "candidate")
                requests[key] = {
                    "network_id": stored["network_id"], "mint": stored["mint"],
                    "context": "candidate", "observed_at": stored["observed_at"],
                    "expires_at": expires, "priority": 300,
                }
                if control is not None:
                    key = (control.network_id, control.mint, "control")
                    requests[key] = {
                        "network_id": control.network_id, "mint": control.mint,
                        "context": "control", "observed_at": stored["observed_at"],
                        "expires_at": expires, "priority": 250,
                    }
            self.conn.commit()
        return {"signals": signals, "matches": matches,
                "capture_requests": list(requests.values())}

    def _entry_quote(self, network_id, mint, signal_at, now_iso):
        mint = str(mint or "")
        mint = mint.lower() if mint.lower().startswith("0x") else mint
        row = self.conn.execute(
            "SELECT * FROM memecoin_quote_observations_v2 "
            "WHERE capture_version>=2 AND network_id=? AND mint=? AND quality='ok' "
            "AND observed_at>=? AND observed_at<=? AND price>0 "
            "ORDER BY observed_at,id LIMIT 1",
            (network_id, mint, signal_at, now_iso),
        ).fetchone()
        return dict(row) if row else None

    def _active_candidates(self):
        return {candidate.candidate_id: candidate for candidate in list_candidates(
            self.conn, statuses={"candidate_active", "promoted_first_class"},
        )}

    def _pending_open_signals(self, candidates, now_iso, limit=1000):
        if not candidates:
            return []
        max_freshness_s = max(
            int(candidate.predicate.get("freshness_s", 120))
            for candidate in candidates.values()
        )
        cutoff = (_time(now_iso) - timedelta(seconds=max_freshness_s)).isoformat()
        placeholders = ",".join("?" for _ in candidates)
        query = (
            "SELECT s.* FROM memecoin_candidate_signals s "
            f"WHERE s.candidate_id IN ({placeholders}) AND s.matched=1 "
            "AND s.observed_at>=? AND s.observed_at<=? AND ("
            "NOT EXISTS (SELECT 1 FROM memecoin_candidate_positions p "
            " WHERE p.candidate_id=s.candidate_id AND p.episode_key=s.episode_key "
            " AND p.control_for_signal_id IS NULL) OR ("
            "s.control_observation_id IS NOT NULL AND NOT EXISTS ("
            " SELECT 1 FROM memecoin_candidate_positions p "
            " WHERE p.candidate_id=s.candidate_id AND p.episode_key=s.episode_key "
            " AND p.control_for_signal_id=s.signal_id))) "
            "ORDER BY s.observed_at,s.signal_id LIMIT ?"
        )
        params = [*candidates, cutoff, now_iso, int(limit)]
        return [dict(row) for row in self.conn.execute(query, params)]

    def _open_pending(self, now_iso):
        candidates = self._active_candidates()
        opened = 0
        signals = self._pending_open_signals(candidates, now_iso)
        for signal in signals:
            candidate = candidates.get(signal["candidate_id"])
            if candidate is None:
                continue
            freshness_s = int(candidate.predicate.get("freshness_s", 120))
            if (_time(now_iso) - _time(signal["observed_at"])).total_seconds() > freshness_s:
                continue
            primary = self.conn.execute(
                "SELECT 1 FROM memecoin_candidate_positions WHERE candidate_id=? AND episode_key=? "
                "AND control_for_signal_id IS NULL",
                (candidate.candidate_id, signal["episode_key"]),
            ).fetchone()
            if primary is None:
                quote = self._entry_quote(
                    signal["network_id"], signal["mint"], signal["observed_at"], now_iso,
                )
                if quote:
                    open_candidate_position(self.conn, candidate, signal, quote, now_iso)
                    opened += 1
            if signal.get("control_observation_id") is None:
                continue
            control_open = self.conn.execute(
                "SELECT 1 FROM memecoin_candidate_positions WHERE candidate_id=? AND episode_key=? "
                "AND control_for_signal_id=?",
                (candidate.candidate_id, signal["episode_key"], signal["signal_id"]),
            ).fetchone()
            if control_open:
                continue
            control = self.conn.execute(
                "SELECT * FROM memecoin_feed_observations_v2 WHERE id=?",
                (signal["control_observation_id"],),
            ).fetchone()
            if control is None:
                continue
            control = dict(control)
            control_mint = str(control.get("mint") or "")
            control["mint"] = (control_mint.lower()
                               if control_mint.lower().startswith("0x") else control_mint)
            quote = self._entry_quote(
                control["network_id"], control["mint"], signal["observed_at"], now_iso,
            )
            if quote:
                control_signal = dict(signal)
                control_signal.update(
                    signal_id=f"{signal['signal_id']}-control-{control['id']}",
                    network_id=control["network_id"], mint=control["mint"],
                    control_for_signal_id=signal["signal_id"],
                )
                open_candidate_position(
                    self.conn, candidate, control_signal, quote, now_iso,
                )
                opened += 1
        return opened

    def sweep(self, now_iso):
        opened = self._open_pending(now_iso)
        quotes = [dict(row) for row in self.conn.execute(
            "SELECT q.* FROM memecoin_quote_observations_v2 q "
            "WHERE q.observed_at<=? AND EXISTS ("
            " SELECT 1 FROM memecoin_candidate_positions p WHERE p.status='open' "
            " AND p.network_id=q.network_id AND p.mint=q.mint) "
            "ORDER BY q.observed_at,q.id",
            (now_iso,),
        )]
        closed = sweep_candidate_positions(self.conn, quotes, now_iso)
        return {"opened": opened, "closed": len(closed), "closed_positions": closed}
