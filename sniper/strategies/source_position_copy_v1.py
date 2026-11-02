"""Source-position copy v1 observation store."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .base import (
    RESOLUTION_LOSS,
    RESOLUTION_WIN,
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)

PLUGIN_ID = "source_position_copy_v1"
SURFACE_VERSION = "source_position_copy_v1"
DEFAULT_TRIGGER_VERSION = "swissmiss_filtered_v1"
SWISSMISS_WALLET = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"
VALID_COPY_STATES = {"tracking", "rejected", "entered", "source_reduced", "source_exited", "resolved"}
REJECTED_TAG_PRIORITY = {
    "iran": 0,
    "middle east": 1,
    "geopolitics": 2,
    "elections": 3,
    "macro": 4,
    "politics": 5,
}

DEFAULTS: dict[str, Any] = {
    "target_wallets": [SWISSMISS_WALLET],
    "accepted_tags": ["sports", "ufc", "cfb", "mlb"],
    "rejected_tags": ["geopolitics", "middle east", "iran", "elections", "macro", "politics"],
    "entry_price_min": 0.30,
    "source_commitment_threshold_usdc": 25000.0,
    "source_commitment_window_hours": 6,
    "source_commitment_mode": "net_buy_cost",
    "max_threshold_age_minutes": 20,
    "max_price_drift_from_source_vwap_pct": 10.0,
    "paper_size_usdc": 25.0,
    "max_concurrent": 15,
    "exit_policy": "hold_to_resolution",
    "material_source_exit_share": 0.50,
    "execution_mode": "paper",
    "trigger_version": DEFAULT_TRIGGER_VERSION,
}


@dataclass(frozen=True)
class SourceObservation:
    activity_key: str
    wallet: str
    condition_id: str
    asset_id: str
    outcome: str
    side: str
    price: float
    size: float
    usdc_size: float
    observed_ts: int
    title: str
    event_slug: str
    event_id: str
    tags: list[str]
    source_endpoint: str
    raw_json: dict[str, Any]


@dataclass
class SourcePositionState:
    wallet: str
    event_key: str
    condition_id: str
    asset_id: str
    buy_cost: float = 0.0
    sell_proceeds: float = 0.0
    buy_shares: float = 0.0
    sell_shares: float = 0.0
    first_buy_price: float | None = None
    last_buy_price: float | None = None
    buy_count: int = 0
    first_observed_ts: int | None = None
    last_observed_ts: int | None = None
    observations: list[SourceObservation] | None = None

    @property
    def net_buy_cost(self) -> float:
        return self.buy_cost - self.sell_proceeds

    @property
    def net_shares(self) -> float:
        return self.buy_shares - self.sell_shares

    @property
    def buy_vwap(self) -> float:
        if self.buy_shares <= 0:
            return 0.0
        return self.buy_cost / self.buy_shares

    @property
    def build_style(self) -> str:
        if self.buy_count <= 1 or self.first_buy_price is None or self.last_buy_price is None:
            return "single_buy"
        if self.last_buy_price > self.first_buy_price:
            return "averaged_up"
        if self.last_buy_price < self.first_buy_price:
            return "averaged_down"
        return "flat"


@dataclass(frozen=True)
class ThresholdTrigger:
    should_enter: bool
    reason: str
    cross_ts: int | None = None
    cross_price: float | None = None
    source_vwap: float = 0.0
    net_buy_cost: float = 0.0
    net_shares: float = 0.0


def ensure_source_position_copy_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_position_observations (
            activity_key    TEXT PRIMARY KEY,
            wallet          TEXT NOT NULL,
            condition_id    TEXT NOT NULL,
            asset_id        TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            side            TEXT NOT NULL,
            price           REAL NOT NULL,
            size            REAL NOT NULL,
            usdc_size       REAL NOT NULL,
            observed_ts     INTEGER NOT NULL,
            title           TEXT NOT NULL,
            event_slug      TEXT NOT NULL,
            event_id        TEXT NOT NULL,
            tags_json       TEXT NOT NULL,
            source_endpoint TEXT NOT NULL,
            raw_json        TEXT NOT NULL,
            created_ts      INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_position_observations_wallet_time
          ON source_position_observations(wallet, observed_ts)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_position_observations_position
          ON source_position_observations(wallet, event_id, condition_id, asset_id, observed_ts)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_position_copy_state (
            id                         INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id                INTEGER NOT NULL,
            wallet                     TEXT NOT NULL,
            event_id                   TEXT NOT NULL,
            condition_id               TEXT NOT NULL,
            asset_id                   TEXT NOT NULL,
            trigger_version            TEXT NOT NULL,
            state                      TEXT NOT NULL,
            reason                     TEXT NOT NULL DEFAULT '',
            threshold_cross_ts         INTEGER,
            threshold_cross_price      REAL,
            source_vwap_at_cross       REAL,
            source_net_cost_at_cross   REAL,
            source_net_shares_at_cross REAL,
            entered_position_id        TEXT,
            updated_ts                 INTEGER NOT NULL,
            UNIQUE(strategy_id, wallet, condition_id, asset_id, trigger_version)
        )
        """
    )
    conn.commit()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_params(strategy: Any) -> dict[str, Any]:
    params: Any = {}
    if isinstance(strategy, dict):
        params = strategy.get("params", {})
    else:
        params = getattr(strategy, "params", {})

    if isinstance(params, str):
        params = json.loads(params or "{}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise TypeError("strategy params must be a dict or JSON object")

    merged = dict(DEFAULTS)
    merged.update(params)
    return merged


def _normalized_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    return [str(tag).lower().strip() for tag in tags if str(tag).strip()]


def _matches_target_filter(obs: SourceObservation, params: dict[str, Any]) -> tuple[bool, str]:
    tags = _normalized_tags(obs.tags)
    if not tags:
        return False, "missing_tags"

    rejected_tags = set(_normalized_tags(params.get("rejected_tags")))
    matched_rejected_tags = [tag for tag in tags if tag in rejected_tags]
    if matched_rejected_tags:
        rejected_tag = sorted(
            matched_rejected_tags,
            key=lambda tag: (REJECTED_TAG_PRIORITY.get(tag, 10_000), tag),
        )[0]
        return False, f"rejected_tag:{rejected_tag}"

    accepted_tags = set(_normalized_tags(params.get("accepted_tags")))
    if accepted_tags and not accepted_tags.intersection(tags):
        return False, "missing_accepted_tag"

    if not str(obs.asset_id or "").strip():
        return False, "missing_asset"

    entry_price_min = float(params.get("entry_price_min", 0.0))
    if float(obs.price) < entry_price_min:
        return False, f"entry_price_below_min:{float(obs.price):g}"

    return True, ""


def _position_key(obs: SourceObservation) -> tuple[str, str, str, str]:
    event_key = str(obs.event_id or obs.event_slug or "").strip()
    return (
        str(obs.wallet).lower().strip(),
        event_key,
        str(obs.condition_id).strip(),
        str(obs.asset_id).strip(),
    )


def _build_source_positions(
    observations: list[SourceObservation],
) -> dict[tuple[str, str, str, str], SourcePositionState]:
    positions: dict[tuple[str, str, str, str], SourcePositionState] = {}
    for obs in sorted(observations, key=lambda row: (int(row.observed_ts), str(row.activity_key))):
        key = _position_key(obs)
        state = positions.setdefault(
            key,
            SourcePositionState(
                wallet=key[0],
                event_key=key[1],
                condition_id=key[2],
                asset_id=key[3],
                observations=[],
            ),
        )
        assert state.observations is not None
        state.observations.append(obs)
        state.first_observed_ts = (
            int(obs.observed_ts)
            if state.first_observed_ts is None
            else min(state.first_observed_ts, int(obs.observed_ts))
        )
        state.last_observed_ts = (
            int(obs.observed_ts)
            if state.last_observed_ts is None
            else max(state.last_observed_ts, int(obs.observed_ts))
        )

        side = str(obs.side).upper().strip()
        if side == "BUY":
            state.buy_cost += float(obs.usdc_size)
            state.buy_shares += float(obs.size)
            state.buy_count += 1
            if state.first_buy_price is None:
                state.first_buy_price = float(obs.price)
            state.last_buy_price = float(obs.price)
        elif side == "SELL":
            state.sell_proceeds += float(obs.usdc_size)
            state.sell_shares += float(obs.size)
    return positions


def _detect_threshold_crossing(
    state: SourcePositionState,
    params: dict[str, Any],
    *,
    now_ts: int,
    copy_price: float | None = None,
) -> ThresholdTrigger:
    threshold = float(params.get("source_commitment_threshold_usdc", 0.0))
    max_window_seconds = float(params.get("source_commitment_window_hours", 0.0)) * 60 * 60
    max_age_seconds = float(params.get("max_threshold_age_minutes", 0.0)) * 60
    max_drift_pct = float(params.get("max_price_drift_from_source_vwap_pct", 0.0))

    buy_cost = 0.0
    sell_proceeds = 0.0
    buy_shares = 0.0
    sell_shares = 0.0
    first_buy_ts: int | None = None

    observations = state.observations or []
    for obs in sorted(observations, key=lambda row: (int(row.observed_ts), str(row.activity_key))):
        side = str(obs.side).upper().strip()
        if side == "BUY":
            buy_cost += float(obs.usdc_size)
            buy_shares += float(obs.size)
            if first_buy_ts is None:
                first_buy_ts = int(obs.observed_ts)
        elif side == "SELL":
            sell_proceeds += float(obs.usdc_size)
            sell_shares += float(obs.size)

        net_cost = buy_cost - sell_proceeds
        net_shares = buy_shares - sell_shares
        if net_cost < threshold:
            continue

        cross_ts = int(obs.observed_ts)
        cross_price = float(copy_price) if copy_price is not None else float(obs.price)
        source_vwap = buy_cost / buy_shares if buy_shares > 0 else 0.0
        if first_buy_ts is not None and max_window_seconds > 0:
            window_seconds = cross_ts - first_buy_ts
            if window_seconds > max_window_seconds:
                return ThresholdTrigger(
                    should_enter=False,
                    reason=f"source_commitment_window_exceeded:{window_seconds / 3600:.2f}h",
                    cross_ts=cross_ts,
                    cross_price=cross_price,
                    source_vwap=source_vwap,
                    net_buy_cost=net_cost,
                    net_shares=net_shares,
                )

        crossing_age_seconds = int(now_ts) - cross_ts
        if max_age_seconds > 0 and crossing_age_seconds > max_age_seconds:
            return ThresholdTrigger(
                should_enter=False,
                reason=f"threshold_crossing_stale:{crossing_age_seconds / 60:.2f}m",
                cross_ts=cross_ts,
                cross_price=cross_price,
                source_vwap=source_vwap,
                net_buy_cost=net_cost,
                net_shares=net_shares,
            )

        if source_vwap > 0 and max_drift_pct > 0:
            drift_pct = abs(cross_price - source_vwap) / source_vwap * 100
            if drift_pct > max_drift_pct:
                return ThresholdTrigger(
                    should_enter=False,
                    reason=f"price_drift_exceeded:{drift_pct:.2f}%",
                    cross_ts=cross_ts,
                    cross_price=cross_price,
                    source_vwap=source_vwap,
                    net_buy_cost=net_cost,
                    net_shares=net_shares,
                )

        return ThresholdTrigger(
            should_enter=True,
            reason="threshold_crossed",
            cross_ts=cross_ts,
            cross_price=cross_price,
            source_vwap=source_vwap,
            net_buy_cost=net_cost,
            net_shares=net_shares,
        )

    return ThresholdTrigger(should_enter=False, reason="threshold_not_crossed")


def insert_source_observation(
    conn: sqlite3.Connection,
    *,
    activity_key: str,
    wallet: str,
    condition_id: str,
    asset_id: str,
    outcome: str,
    side: str,
    price: float,
    size: float,
    usdc_size: float,
    observed_ts: int,
    title: str,
    event_slug: str,
    event_id: str,
    tags: list[str],
    source_endpoint: str,
    raw_json: dict[str, Any],
) -> int | None:
    ensure_source_position_copy_tables(conn)
    try:
        cur = conn.execute(
            """
            INSERT INTO source_position_observations
              (activity_key, wallet, condition_id, asset_id, outcome, side, price,
               size, usdc_size, observed_ts, title, event_slug, event_id, tags_json,
               source_endpoint, raw_json, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(activity_key),
                str(wallet).lower().strip(),
                str(condition_id),
                str(asset_id),
                str(outcome).upper(),
                str(side).upper(),
                float(price),
                float(size),
                float(usdc_size),
                int(observed_ts),
                str(title or ""),
                str(event_slug or ""),
                str(event_id or ""),
                _json_dumps([str(t).lower().strip() for t in tags if str(t).strip()]),
                str(source_endpoint or ""),
                _json_dumps(raw_json or {}),
                int(time.time()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def record_copy_state(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    wallet: str,
    event_id: str,
    condition_id: str,
    asset_id: str,
    trigger_version: str,
    state: str,
    reason: str,
    trigger: ThresholdTrigger | None = None,
    entered_position_id: str | None = None,
) -> None:
    state = str(state).strip()
    if state not in VALID_COPY_STATES:
        raise ValueError(f"invalid source-position copy state: {state}")

    ensure_source_position_copy_tables(conn)
    conn.execute(
        """
        INSERT INTO source_position_copy_state
          (strategy_id, wallet, event_id, condition_id, asset_id, trigger_version,
           state, reason, threshold_cross_ts, threshold_cross_price, source_vwap_at_cross,
           source_net_cost_at_cross, source_net_shares_at_cross, entered_position_id, updated_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_id, wallet, condition_id, asset_id, trigger_version)
        DO UPDATE SET
          state=excluded.state,
          reason=excluded.reason,
          threshold_cross_ts=excluded.threshold_cross_ts,
          threshold_cross_price=excluded.threshold_cross_price,
          source_vwap_at_cross=excluded.source_vwap_at_cross,
          source_net_cost_at_cross=excluded.source_net_cost_at_cross,
          source_net_shares_at_cross=excluded.source_net_shares_at_cross,
          entered_position_id=COALESCE(
            excluded.entered_position_id,
            source_position_copy_state.entered_position_id
          ),
          updated_ts=excluded.updated_ts
        """,
        (
            int(strategy_id),
            str(wallet).lower().strip(),
            str(event_id or ""),
            str(condition_id),
            str(asset_id),
            str(trigger_version),
            state,
            str(reason),
            trigger.cross_ts if trigger else None,
            trigger.cross_price if trigger else None,
            trigger.source_vwap if trigger else None,
            trigger.net_buy_cost if trigger else None,
            trigger.net_shares if trigger else None,
            entered_position_id,
            int(time.time()),
        ),
    )
    conn.commit()


def _already_entered(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    wallet: str,
    condition_id: str,
    asset_id: str,
    trigger_version: str,
) -> bool:
    ensure_source_position_copy_tables(conn)
    row = conn.execute(
        """
        SELECT 1
        FROM source_position_copy_state
        WHERE strategy_id=?
          AND wallet=?
          AND condition_id=?
          AND asset_id=?
          AND trigger_version=?
          AND state IN ('entered', 'source_reduced', 'source_exited', 'resolved')
        LIMIT 1
        """,
        (
            int(strategy_id),
            str(wallet).lower().strip(),
            str(condition_id),
            str(asset_id),
            str(trigger_version),
        ),
    ).fetchone()
    return row is not None


def _has_opposite_side_conflict(observations: list[SourceObservation]) -> tuple[bool, str]:
    condition_to_assets: dict[str, set[str]] = {}
    for obs in observations:
        if str(obs.side).upper().strip() != "BUY":
            continue
        if float(obs.usdc_size) <= 0:
            continue
        condition_to_assets.setdefault(str(obs.condition_id), set()).add(str(obs.asset_id))

    if any(len(asset_ids) > 1 for asset_ids in condition_to_assets.values()):
        return True, "opposite_side_conflict"
    return False, ""


def _load_observations(conn: sqlite3.Connection, wallets: list[str]) -> list[SourceObservation]:
    ensure_source_position_copy_tables(conn)
    target_wallets = [str(wallet).lower().strip() for wallet in wallets if str(wallet).strip()]
    if not target_wallets:
        return []

    placeholders = ",".join("?" for _ in target_wallets)
    rows = conn.execute(
        f"""
        SELECT activity_key, wallet, condition_id, asset_id, outcome, side, price,
               size, usdc_size, observed_ts, title, event_slug, event_id,
               tags_json, source_endpoint, raw_json
        FROM source_position_observations
        WHERE wallet IN ({placeholders})
        ORDER BY observed_ts ASC, activity_key ASC
        """,
        tuple(target_wallets),
    ).fetchall()

    observations: list[SourceObservation] = []
    for row in rows:
        observations.append(
            SourceObservation(
                activity_key=str(row["activity_key"]),
                wallet=str(row["wallet"]).lower().strip(),
                condition_id=str(row["condition_id"]),
                asset_id=str(row["asset_id"]),
                outcome=str(row["outcome"]),
                side=str(row["side"]),
                price=float(row["price"]),
                size=float(row["size"]),
                usdc_size=float(row["usdc_size"]),
                observed_ts=int(row["observed_ts"]),
                title=str(row["title"] or ""),
                event_slug=str(row["event_slug"] or ""),
                event_id=str(row["event_id"] or ""),
                tags=json.loads(row["tags_json"] or "[]"),
                source_endpoint=str(row["source_endpoint"] or ""),
                raw_json=json.loads(row["raw_json"] or "{}"),
            )
        )
    return observations


def _open_position_count(conn: sqlite3.Connection, strategy_id: int) -> int:
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM strategy_positions WHERE strategy_id=? AND state='open'",
            (int(strategy_id),),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


class SourcePositionCopyV1Strategy(StrategyPlugin):
    plugin_id = PLUGIN_ID
    display_name = "SwissMiss Copy - Filtered"
    description = "Paper-only source-position copy after source commitment threshold crossing."
    strategy_family = "paper_copy"
    supported_execution_modes = ("paper",)
    required_tables = ("source_position_observations", "source_position_copy_state")
    default_loop_seconds = 60

    def validate_parameters(self, parameters: dict[str, Any]) -> list[str]:
        params = dict(DEFAULTS)
        params.update(parameters or {})
        errors: list[str] = []
        if str(params.get("execution_mode") or "").lower() != "paper":
            errors.append("execution_mode must be paper")
        if float(params.get("source_commitment_threshold_usdc") or 0.0) <= 0:
            errors.append("source_commitment_threshold_usdc must be > 0")
        if float(params.get("paper_size_usdc") or 0.0) <= 0:
            errors.append("paper_size_usdc must be > 0")
        return errors

    def discover(self, conn: sqlite3.Connection, strategy: dict[str, Any]) -> list[StrategySignal]:
        ensure_source_position_copy_tables(conn)
        params = _parse_params(strategy)
        strategy_id = int(strategy.get("id") or 0)
        trigger_version = str(params.get("trigger_version") or DEFAULT_TRIGGER_VERSION)
        observations = _load_observations(conn, params.get("target_wallets") or [])

        positions = _build_source_positions(observations)
        condition_buy_groups: dict[tuple[str, str], list[SourceObservation]] = {}
        for obs in observations:
            if str(obs.side).upper().strip() == "BUY":
                condition_buy_groups.setdefault((obs.wallet, obs.condition_id), []).append(obs)

        now_ts = int(time.time())
        signals: list[StrategySignal] = []
        for state in positions.values():
            buy_rows = [obs for obs in state.observations or [] if str(obs.side).upper().strip() == "BUY"]
            if not buy_rows:
                continue
            first_buy = min(buy_rows, key=lambda obs: (int(obs.observed_ts), str(obs.activity_key)))
            passes, reason = _matches_target_filter(first_buy, params)
            if not passes:
                if strategy_id:
                    record_copy_state(
                        conn,
                        strategy_id=strategy_id,
                        wallet=state.wallet,
                        event_id=state.event_key,
                        condition_id=state.condition_id,
                        asset_id=state.asset_id,
                        trigger_version=trigger_version,
                        state="rejected",
                        reason=reason,
                    )
                continue

            condition_obs = condition_buy_groups.get((state.wallet, state.condition_id), [])
            conflict, reason = _has_opposite_side_conflict(condition_obs)
            if conflict:
                if strategy_id:
                    record_copy_state(
                        conn,
                        strategy_id=strategy_id,
                        wallet=state.wallet,
                        event_id=state.event_key,
                        condition_id=state.condition_id,
                        asset_id=state.asset_id,
                        trigger_version=trigger_version,
                        state="rejected",
                        reason=reason,
                    )
                continue

            if strategy_id and _already_entered(
                conn,
                strategy_id=strategy_id,
                wallet=state.wallet,
                condition_id=state.condition_id,
                asset_id=state.asset_id,
                trigger_version=trigger_version,
            ):
                continue

            trigger = _detect_threshold_crossing(state, params, now_ts=now_ts)
            if not trigger.should_enter:
                if strategy_id:
                    record_copy_state(
                        conn,
                        strategy_id=strategy_id,
                        wallet=state.wallet,
                        event_id=state.event_key,
                        condition_id=state.condition_id,
                        asset_id=state.asset_id,
                        trigger_version=trigger_version,
                        state="tracking" if trigger.reason == "threshold_not_crossed" else "rejected",
                        reason=trigger.reason,
                        trigger=trigger,
                    )
                continue

            latest_buy = max(buy_rows, key=lambda obs: (int(obs.observed_ts), str(obs.activity_key)))

            signals.append(
                StrategySignal(
                    wallet=state.wallet,
                    condition_id=state.condition_id,
                    outcome=latest_buy.outcome,
                    title=latest_buy.title,
                    current_price=float(latest_buy.price),
                    position_value=float(trigger.net_buy_cost),
                    analyzed_ts=int(trigger.cross_ts or latest_buy.observed_ts),
                    metadata={
                        "asset_id": state.asset_id,
                        "event_id": latest_buy.event_id,
                        "event_slug": latest_buy.event_slug,
                        "trigger_version": trigger_version,
                        "threshold_cross_ts": trigger.cross_ts,
                        "threshold_cross_price": trigger.cross_price,
                        "source_vwap": trigger.source_vwap,
                        "source_net_cost": trigger.net_buy_cost,
                        "source_net_shares": trigger.net_shares,
                        "source_position_mode": str(params.get("source_commitment_mode")),
                        "price_source": "source_price_proxy",
                        "surface_version": SURFACE_VERSION,
                    },
                )
            )

        return signals

    def enter(
        self,
        conn: sqlite3.Connection,
        strategy: dict[str, Any],
        signal: StrategySignal,
        open_positions: list[dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        if str(params.get("execution_mode") or "").lower() != "paper":
            return StrategyEntryDecision(False, "non_paper_execution_rejected")

        strategy_id = int(strategy.get("id") or 0)
        max_concurrent = int(params.get("max_concurrent") or 0)
        if strategy_id and max_concurrent > 0 and _open_position_count(conn, strategy_id) >= max_concurrent:
            return StrategyEntryDecision(False, f"max_concurrent={max_concurrent}_reached")

        trigger_version = str(signal.metadata.get("trigger_version") or params.get("trigger_version") or DEFAULT_TRIGGER_VERSION)
        asset_id = str(signal.metadata.get("asset_id") or "")
        event_id = str(signal.metadata.get("event_id") or "")
        trigger = ThresholdTrigger(
            should_enter=True,
            reason="threshold_crossed",
            cross_ts=signal.metadata.get("threshold_cross_ts"),
            cross_price=signal.metadata.get("threshold_cross_price"),
            source_vwap=float(signal.metadata.get("source_vwap") or 0.0),
            net_buy_cost=float(signal.metadata.get("source_net_cost") or signal.position_value or 0.0),
            net_shares=float(signal.metadata.get("source_net_shares") or 0.0),
        )
        if strategy_id:
            record_copy_state(
                conn,
                strategy_id=strategy_id,
                wallet=signal.wallet,
                event_id=event_id,
                condition_id=signal.condition_id,
                asset_id=asset_id,
                trigger_version=trigger_version,
                state="entered",
                reason="source_position_threshold_crossed",
                trigger=trigger,
            )

        return StrategyEntryDecision(
            should_enter=True,
            reason="source_position_threshold_crossed",
            size_usdc=float(params.get("paper_size_usdc") or DEFAULTS["paper_size_usdc"]),
            entry_price=float(signal.current_price),
            execution_mode="paper",
            metadata={
                "asset_id": asset_id,
                "event_id": event_id,
                "trigger_version": trigger_version,
                "source_wallet": signal.wallet,
                "source_net_cost": trigger.net_buy_cost,
                "source_net_shares": trigger.net_shares,
                "source_vwap": trigger.source_vwap,
            },
        )

    def manage(
        self,
        conn: sqlite3.Connection,
        strategy: dict[str, Any],
        position: dict[str, Any],
    ) -> StrategyManageState:
        current_price = position.get("current_price")
        return StrategyManageState(
            current_price=float(current_price) if current_price is not None else None,
            signal_open=True,
            metadata={},
        )

    def exit(
        self,
        conn: sqlite3.Connection,
        strategy: dict[str, Any],
        position: dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        current_price = (
            state.current_price
            if state and state.current_price is not None
            else float(position.get("current_price") or 0.0)
        )

        if current_price >= RESOLUTION_WIN:
            return StrategyExitDecision(True, "resolution_win", exit_price=float(current_price), status="resolution_win")
        if current_price <= RESOLUTION_LOSS:
            return StrategyExitDecision(True, "resolution_loss", exit_price=float(current_price), status="resolution_loss")

        if str(params.get("exit_policy") or "") == "source_exit":
            source_exit_share = float((state.metadata or {}).get("source_exit_share") or 0.0)
            material_share = float(params.get("material_source_exit_share") or 0.0)
            if material_share > 0 and source_exit_share >= material_share:
                return StrategyExitDecision(True, "source_material_exit", exit_price=float(current_price), status="source_exit")

        return StrategyExitDecision(False, "holding", exit_price=float(current_price), status="open")

    def report(
        self,
        conn: sqlite3.Connection,
        strategy: dict[str, Any],
        positions: list[dict[str, Any]],
    ) -> StrategyReport:
        strategy_id = int(strategy.get("id") or 0)
        paper_count = _open_position_count(conn, strategy_id) if strategy_id else 0
        state_counts: dict[str, int] = {}
        source_positions = 0
        if strategy_id:
            ensure_source_position_copy_tables(conn)
            rows = conn.execute(
                """
                SELECT state, reason, COUNT(*) AS count
                FROM source_position_copy_state
                WHERE strategy_id=?
                GROUP BY state, reason
                ORDER BY state, reason
                """,
                (strategy_id,),
            ).fetchall()
            for row in rows:
                count = int(row["count"])
                state_counts[f"{row['state']}:{row['reason']}"] = count
                source_positions += count

        return StrategyReport(
            summary={
                "plugin_id": PLUGIN_ID,
                "source_positions": source_positions,
                "state_counts": state_counts,
                "paper_positions": paper_count,
            }
        )
