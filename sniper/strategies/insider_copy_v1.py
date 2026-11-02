from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

from sniper.filters import is_prop_market, market_exploitability

from sniper.settings.paper import (
    PAPER_CATEGORY_MAX_ENTRY,
    PAPER_ENTRY_MIN_INSIDER_SCORE,
    PAPER_MAX_ADVERSE_DRIFT,
    PAPER_MAX_CONCURRENT,
    PAPER_MAX_DAYS_TO_RESOLUTION,
    PAPER_MAX_EXPOSURE_PER_MARKET,
    PAPER_MAX_FAVORABLE_DRIFT,
    PAPER_MAX_POSITION_SIZE,
    PAPER_MIN_POSITION_SIZE,
    PAPER_POSITION_SIZE,
    PAPER_SIZING_ELITE_INSIDER_SCORE,
    PAPER_SIZING_ELITE_QROI,
    PAPER_SIZING_MIN_INSIDER_SCORE,
    PAPER_SIZING_MIN_QROI,
    PAPER_SKIP_WALLET_CLASSES,
    RESOLUTION_LOSS_THRESHOLD,
    RESOLUTION_WIN_THRESHOLD,
    SPECULATOR_BOT_MIN_QUALIFYING_ROI,
    SPECULATOR_BOT_MIN_WIN_RATE,
    STALE_DAYS,
)

from sniper.filters import days_to_resolution
from .base import (
    StrategyEntryDecision,
    StrategyExitDecision,
    StrategyManageState,
    StrategyPlugin,
    StrategyReport,
    StrategySignal,
)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    import json
    params = strategy.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(params, dict):
        return {}
    return params


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_telegram_notification(message: str) -> bool:
    """Thin wrapper — delegates to strategy_runner to avoid circular imports."""
    try:
        from sniper.services.strategy_runner import send_telegram_notification as _send
        return _send(message)
    except Exception:
        return False


DEFAULTS: Dict[str, Any] = {
    "resolution_entry_cutoff_days": 3,   # don't enter within this many days of resolution
    "resolution_stop_cutoff_days": 5,    # don't fire stop-loss within this many days of resolution
    "cascade_exit_mode": "follow",       # if cascade detected AND source wallet has exited, exit
}


class InsiderCopyV1Strategy(StrategyPlugin):
    plugin_id = "insider_copy_v1"
    display_name = "Insider Copy v1"
    description = "Paper-style insider signal copier with its own strategy ledger."

    strategy_family = "insider_copy"
    supported_execution_modes = ("paper", "real")
    required_feeds = ("polymarket",)
    default_loop_seconds = 30

    def replay_adapter(self):
        from sniper.replay.adapters import InsiderCopyReplayAdapter

        return InsiderCopyReplayAdapter()

    def discover(self, conn: sqlite3.Connection, strategy: Dict[str, Any]) -> List[StrategySignal]:
        wallet = str(strategy.get("wallet") or "").strip().lower()
        if not wallet:
            return []

        params = _parse_params(strategy)
        min_position_value = _to_float(params.get("min_position_value_usdc"), 500.0)

        rows = conn.execute(
            """
            SELECT
                ps.wallet,
                ps.condition_id,
                ps.outcome,
                ps.title,
                ps.current_price,
                ps.entry_price,
                ps.position_value,
                ps.analyzed_ts,
                ps.position_insider_score,
                ps.position_speculator_score,
                ps.position_classification,
                ps.end_date_iso,
                ua.win_rate,
                ua.qualifying_roi,
                ua.classification AS wallet_classification
            FROM position_signals ps
            LEFT JOIN user_analysis ua ON ua.wallet = ps.wallet
            WHERE ps.wallet = ?
              AND ps.is_open = 1
              AND ps.position_value >= ?
              AND ps.current_price > 0
            ORDER BY ps.position_value DESC, ps.analyzed_ts DESC
            """,
            (wallet, min_position_value),
        ).fetchall()

        signals: List[StrategySignal] = []
        for row in rows:
            signals.append(
                StrategySignal(
                    wallet=str(row["wallet"] or ""),
                    condition_id=str(row["condition_id"] or ""),
                    outcome=str(row["outcome"] or "").upper(),
                    title=str(row["title"] or ""),
                    current_price=_to_float(row["current_price"]),
                    position_value=_to_float(row["position_value"]),
                    analyzed_ts=int(row["analyzed_ts"] or 0),
                    metadata={
                        "position_insider_score": _to_float(row["position_insider_score"]),
                        "position_speculator_score": _to_float(row["position_speculator_score"]),
                        "position_classification": str(row["position_classification"] or ""),
                        "end_date_iso": str(row["end_date_iso"] or ""),
                        "win_rate": _to_float(row["win_rate"]),
                        "qualifying_roi": row["qualifying_roi"],
                        "wallet_classification": str(row["wallet_classification"] or ""),
                        "source_entry_price": _to_float(row["entry_price"]),
                    },
                )
            )
        return signals

    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        max_concurrent = int(params.get("max_concurrent_positions") or PAPER_MAX_CONCURRENT)
        max_entry_price = _to_float(params.get("max_entry_price"), 0.70)
        max_days_to_resolution = int(params.get("max_days_to_resolution") or PAPER_MAX_DAYS_TO_RESOLUTION)
        # Per-strategy position insider score override — lets high-WR wallets (e.g. near-certainty
        # buyers like tradecraft) bypass the global 55 floor since their style inherently yields
        # low per-position scores even though their win-rate is exceptional.
        # Support both param key names: DB historically used "min_insider_score" (per-wallet
        # degradation logic) while entry gate reads "min_position_insider_score".  Accept
        # either so that strategy params don't need a DB migration to take effect.
        min_pos_insider_score = _to_float(
            params.get("min_position_insider_score") or params.get("min_insider_score"),
            PAPER_ENTRY_MIN_INSIDER_SCORE,
        )
        insider_score = _to_float(signal.metadata.get("position_insider_score"))
        speculator_score = _to_float(signal.metadata.get("position_speculator_score"))
        win_rate = _to_float(signal.metadata.get("win_rate"))
        wallet_qroi = signal.metadata.get("qualifying_roi")
        wallet_qroi_val = None if wallet_qroi is None else _to_float(wallet_qroi)
        wallet_class = str(signal.metadata.get("wallet_classification") or "")

        if any(
            p.get("wallet") == signal.wallet
            and p.get("condition_id") == signal.condition_id
            and str(p.get("outcome") or "").upper() == signal.outcome.upper()
            for p in open_positions
        ):
            return StrategyEntryDecision(False, "already open for this wallet/market/outcome")

        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(False, f"max concurrent positions reached ({max_concurrent})")

        if signal.current_price <= 0:
            return StrategyEntryDecision(False, "invalid current price")

        if wallet_class in PAPER_SKIP_WALLET_CLASSES:
            return StrategyEntryDecision(False, f"excluded archetype: {wallet_class}")

        # ── Wallet quality gate (ADR-007) ─────────────────────────────────────
        # wallet_quality_score is for WALLET SELECTION — should this wallet be
        # copied at all?  Distinct from position_insider_score (entry timing).
        min_wq_score = _to_float(params.get("min_wallet_quality_score"), 30.0)
        try:
            wq_row = conn.execute(
                "SELECT wallet_quality_score, wallet_quality_eligible FROM user_analysis WHERE wallet = ? LIMIT 1",
                (signal.wallet,),
            ).fetchone()
            if wq_row is not None:
                wq_eligible = wq_row["wallet_quality_eligible"]
                wq_score = wq_row["wallet_quality_score"]
                # Hard reject: explicitly marked ineligible
                if wq_eligible is not None and int(wq_eligible) == 0:
                    return StrategyEntryDecision(
                        False,
                        f"wallet_quality_eligible=0 for {signal.wallet[:12]}",
                    )
                # Soft reject: score exists but below threshold
                if wq_score is not None and float(wq_score) < min_wq_score:
                    return StrategyEntryDecision(
                        False,
                        f"wallet_quality_score {float(wq_score):.1f} < {min_wq_score:.0f}",
                    )
                # wq_score is None → rollout grace period, allow through
        except Exception:
            # Column may not exist yet — graceful degradation
            logger.debug("insider_copy_v1 enter: wallet_quality_score query failed (non-blocking)", exc_info=True)

        if wallet_class == "speculator_bot":
            if win_rate < SPECULATOR_BOT_MIN_WIN_RATE or (wallet_qroi_val or 0.0) < SPECULATOR_BOT_MIN_QUALIFYING_ROI:
                return StrategyEntryDecision(
                    False,
                    f"speculator_bot below thresholds (wr={win_rate:.2f}, roi={(wallet_qroi_val or 0.0):.1f})",
                )

        # Prop/spread market gate: O/U, handicap, spread markets have no insider edge
        if is_prop_market(signal.title or ""):
            return StrategyEntryDecision(False, "prop/spread market — no insider edge")

        if signal.current_price > max_entry_price:
            return StrategyEntryDecision(False, f"entry price {signal.current_price:.3f} > {max_entry_price:.2f}")

        # Category-aware max entry price cap
        _, _, _cat = market_exploitability(signal.title or "", signal.metadata.get("market_slug", "") if signal.metadata else "")
        _cat_max = PAPER_CATEGORY_MAX_ENTRY.get(_cat, max_entry_price)
        if signal.current_price > _cat_max:
            return StrategyEntryDecision(False, f"entry {signal.current_price:.3f} > category cap {_cat_max} ({_cat})")

        if insider_score < min_pos_insider_score:
            return StrategyEntryDecision(False, f"insider score {insider_score:.0f} < {min_pos_insider_score:.0f}")

        if speculator_score >= 70:
            return StrategyEntryDecision(False, f"speculator score {speculator_score:.0f} >= 70")

        if wallet_qroi_val is None and insider_score < 90:
            return StrategyEntryDecision(False, "qualifying_roi missing and insider score < 90")
        if wallet_qroi_val is not None and wallet_qroi_val < 10.0:
            return StrategyEntryDecision(False, f"qualifying_roi {wallet_qroi_val:.1f} < 10.0")

        resolution_entry_cutoff = int(params.get("resolution_entry_cutoff_days", DEFAULTS["resolution_entry_cutoff_days"]))
        end_date_iso = str(signal.metadata.get("end_date_iso") or "")
        days_left = days_to_resolution(end_date_iso)
        if days_left is not None:
            if days_left > max_days_to_resolution:
                return StrategyEntryDecision(False, f"market resolves in {days_left}d > {max_days_to_resolution}d")
            if days_left < resolution_entry_cutoff:
                return StrategyEntryDecision(False, f"too close to resolution: {days_left}d < {resolution_entry_cutoff}d cutoff")

        # Per-market exposure cap: prevent concentration in a single market.
        max_exposure = _to_float(params.get("max_exposure_per_market_usdc"), PAPER_MAX_EXPOSURE_PER_MARKET)
        current_market_exposure = sum(
            _to_float(p.get("entry_shares"), 0) * _to_float(p.get("avg_entry_price"), 0)
            for p in open_positions
            if str(p.get("condition_id") or "").lower() == signal.condition_id.lower()
        )
        if current_market_exposure >= max_exposure:
            return StrategyEntryDecision(
                False,
                f"market exposure ${current_market_exposure:.0f} >= ${max_exposure:.0f} cap",
            )

        # Price drift gate: skip if current price drifted too far from source entry.
        source_entry = _to_float(signal.metadata.get("source_entry_price"), 0)
        max_adverse = _to_float(params.get("max_adverse_drift_pct"), PAPER_MAX_ADVERSE_DRIFT)
        max_favorable = _to_float(params.get("max_favorable_drift_pct"), PAPER_MAX_FAVORABLE_DRIFT)
        if source_entry > 0:
            drift_pct = ((signal.current_price - source_entry) / source_entry) * 100
            if drift_pct > max_adverse:
                return StrategyEntryDecision(
                    False,
                    f"adverse drift {drift_pct:+.1f}% > {max_adverse}% "
                    f"(source {source_entry:.3f} → now {signal.current_price:.3f})",
                )
            if drift_pct < -max_favorable:
                return StrategyEntryDecision(
                    False,
                    f"favorable drift {drift_pct:+.1f}% > {max_favorable}% "
                    f"(source {source_entry:.3f} → now {signal.current_price:.3f}, likely late)",
                )

        # Gate: no open position in paper_trades (cross-system dedup)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM paper_trades WHERE condition_id=? AND LOWER(outcome)=LOWER(?) AND status='open' AND duplicate_of IS NULL LIMIT 1",
                (signal.condition_id, signal.outcome)
            )
            if cur.fetchone():
                return StrategyEntryDecision(False, "paper_trades already open on this market/outcome")
        except Exception:
            logger.debug("insider_copy_v1 enter: paper_trades cross-dedup query failed", exc_info=True)

        # ── Reference price gate ───────────────────────────────────────────────
        # Establish an independent prior (Kalshi / calibration) before acting.
        # For insider_copy YES entries:
        #   poly > ref by >5¢ → insider is in an already-overpriced market → flag
        #   poly < ref by >5¢ → double confirmation → continue with confidence
        #   aligned (<2¢)     → both retail venues agree, no structural edge → skip
        _ref_meta: dict = {}
        try:
            import sys as _sys
            import os as _os
            _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
            from scripts.reference_price import get_reference_assessment
            from sniper.filters import market_exploitability as _mexp
            _, _, _cat = _mexp(signal.title or "", "")
            _ref = get_reference_assessment(
                market_title=signal.title or "",
                condition_id=signal.condition_id,
                outcome=signal.outcome,
                poly_price=signal.current_price,
                category=_cat,
            )
            _ref_meta = _ref
            if _ref.get("has_reference"):
                if _ref.get("recommendation") == "avoid":
                    logger.info(
                        "REFERENCE ALIGNED (insider_copy): %s poly=%.3f ref=%.3f div=%+.3f — skipping",
                        (signal.title or "")[:40],
                        signal.current_price,
                        _ref["reference_price"],
                        _ref["divergence"],
                    )
                    return StrategyEntryDecision(
                        False,
                        f"reference_aligned: poly={signal.current_price:.3f} ref={_ref['reference_price']:.3f} div={_ref['divergence']:+.3f}",
                    )
                _div = _ref.get("divergence", 0) or 0
                if abs(_div) > 0.06:
                    logger.info(
                        "REFERENCE DIVERGENCE (insider_copy): %+.3f — high confidence entry for %s",
                        _div,
                        (signal.title or "")[:40],
                    )
        except Exception as _ref_err:
            logger.debug("insider_copy_v1 reference check failed (non-blocking): %s", _ref_err)

        size_usdc = self._calculate_position_size(signal)

        # Clamp size to remaining market headroom
        market_headroom = max_exposure - current_market_exposure
        if size_usdc > market_headroom:
            size_usdc = market_headroom

        # Apply reference divergence size boost (mirrors paper_trader logic)
        _ref_div = (_ref_meta.get("divergence") or 0) if _ref_meta else 0
        if abs(_ref_div) > 0.06:
            size_usdc = min(size_usdc * 1.15, market_headroom)
        elif abs(_ref_div) > 0.04:
            size_usdc = min(size_usdc * 1.07, market_headroom)

        execution_mode = str(params.get("execution_mode") or "paper").strip().lower()
        entry_meta = {
            "source_signal_ts": signal.analyzed_ts,
            "signal_ts": signal.analyzed_ts,
            "entry_ts": int(time.time()),
        }
        if _ref_meta.get("has_reference"):
            entry_meta["reference_assessment"] = {
                "reference_price": _ref_meta.get("reference_price"),
                "divergence": _ref_meta.get("divergence"),
                "recommendation": _ref_meta.get("recommendation"),
                "sources_used": _ref_meta.get("sources_used", []),
            }
        return StrategyEntryDecision(
            True,
            "passes insider_copy_v1 filters",
            size_usdc=size_usdc,
            entry_price=signal.current_price,
            execution_mode=execution_mode,
            metadata=entry_meta,
        )

    def _check_wallet_degradation(
        self,
        conn,
        strategy: Dict[str, Any],
        min_insider_score: float,
        n_consecutive: int = 3,
    ) -> None:
        """Pause strategy if source wallet insider score has dropped below threshold
        for N consecutive analysis runs.  Sends a Telegram alert on first pause.
        Does NOT close open positions — they exit via normal resolution/staleness.
        Records paused_at and pause_reason for audit trail.
        """
        wallet = str(strategy.get("wallet") or "").strip().lower()
        if not wallet:
            return
        rows = conn.execute(
            """SELECT insider_score FROM score_history
               WHERE wallet = ?
               ORDER BY analyzed_ts DESC
               LIMIT ?""",
            (wallet, n_consecutive),
        ).fetchall()
        if len(rows) < n_consecutive:
            return
        if not all(_to_float(r["insider_score"]) < min_insider_score for r in rows):
            return
        strategy_id = strategy.get("id")
        now = _utc_now_iso()
        result = conn.execute(
            """UPDATE strategies
               SET status = 'paused', updated_at = ?, paused_at = ?, pause_reason = 'wallet_degradation'
               WHERE id = ? AND status = 'active'""",
            (now, now, strategy_id),
        )
        conn.commit()
        if result.rowcount > 0:
            current_score = _to_float(rows[0]["insider_score"])
            logger.warning(
                "strategy=%s wallet=%s auto-paused: insider_score=%.0f for %d consecutive runs "
                "(threshold=%.0f); open positions will exit normally",
                strategy_id, wallet, current_score, n_consecutive, min_insider_score,
            )
            try:
                send_telegram_notification(
                    f"\u26a0\ufe0f Strategy {strategy_id} auto-paused\n"
                    f"Wallet {wallet[:12]}... insider score dropped to {current_score:.0f} "
                    f"for {n_consecutive} consecutive runs (threshold: {min_insider_score:.0f})\n"
                    f"Open positions will exit via normal resolution/staleness."
                )
            except Exception:
                logger.exception("Failed to send Telegram alert for wallet degradation")

    def _check_wallet_recovery(
        self,
        conn,
        strategy: Dict[str, Any],
        min_insider_score: float,
        n_consecutive: int = 3,
    ) -> None:
        """Auto-resume a paused strategy if the source wallet insider score has
        recovered above threshold for N consecutive analysis runs.

        Only acts on strategies paused with pause_reason = 'wallet_degradation'
        (i.e. auto-paused by this bot — not manually paused by a human).
        Sends a Telegram alert on resume.
        """
        strategy_id = strategy.get("id")
        # Only auto-resume strategies that were paused by wallet degradation
        pause_reason = str(strategy.get("pause_reason") or "").strip()
        if pause_reason != "wallet_degradation":
            return

        wallet = str(strategy.get("wallet") or "").strip().lower()
        if not wallet:
            return

        rows = conn.execute(
            """SELECT insider_score FROM score_history
               WHERE wallet = ?
               ORDER BY analyzed_ts DESC
               LIMIT ?""",
            (wallet, n_consecutive),
        ).fetchall()
        if len(rows) < n_consecutive:
            return
        # All N most recent scores must be >= threshold to auto-resume
        if not all(_to_float(r["insider_score"]) >= min_insider_score for r in rows):
            return

        now = _utc_now_iso()
        result = conn.execute(
            """UPDATE strategies
               SET status = 'active', updated_at = ?, paused_at = NULL, pause_reason = NULL
               WHERE id = ? AND status = 'paused'""",
            (now, strategy_id),
        )
        conn.commit()
        if result.rowcount > 0:
            recovered_score = _to_float(rows[0]["insider_score"])
            logger.info(
                "strategy=%s wallet=%s auto-resumed: insider_score=%.0f recovered above %.0f "
                "for %d consecutive runs",
                strategy_id, wallet, recovered_score, min_insider_score, n_consecutive,
            )
            try:
                send_telegram_notification(
                    f"\u2705 Strategy {strategy_id} auto-resumed\n"
                    f"Wallet {wallet[:12]}... insider score recovered to {recovered_score:.0f} "
                    f"(threshold: {min_insider_score:.0f}) for {n_consecutive} consecutive runs."
                )
            except Exception:
                logger.exception("Failed to send Telegram alert for wallet recovery")

    def manage(self, conn, strategy: Dict[str, Any], position: Dict[str, Any]) -> StrategyManageState:
        params = _parse_params(strategy)
        min_insider_score = _to_float(params.get("min_insider_score"), PAPER_ENTRY_MIN_INSIDER_SCORE)
        n_consecutive = int(params.get("degradation_consecutive_runs") or 3)
        self._check_wallet_degradation(conn, strategy, min_insider_score, n_consecutive)

        row = conn.execute(
            """
            SELECT
                ps.is_open,
                ps.current_price,
                ps.position_value,
                ps.position_insider_score,
                ps.position_classification
            FROM position_signals ps
            WHERE ps.wallet = ?
              AND ps.condition_id = ?
              AND ps.outcome = ?
            ORDER BY ps.analyzed_ts DESC
            LIMIT 1
            """,
            (
                str(position.get("wallet") or "").lower(),
                str(position.get("condition_id") or "").lower(),
                str(position.get("outcome") or "").lower(),
            ),
        ).fetchone()

        if row is not None:
            return StrategyManageState(
                current_price=_to_float(row["current_price"], _to_float(position.get("avg_entry_price"))),
                signal_open=bool(int(row["is_open"] or 0)),
                metadata={
                    "position_value": _to_float(row["position_value"]),
                    "position_insider_score": _to_float(row["position_insider_score"]),
                    "position_classification": str(row["position_classification"] or ""),
                },
            )

        fp = conn.execute(
            """
            SELECT current_price, current_amount
            FROM followed_positions
            WHERE wallet = ?
              AND condition_id = ?
              AND outcome = ?
            ORDER BY last_synced_ts DESC
            LIMIT 1
            """,
            (
                str(position.get("wallet") or "").lower(),
                str(position.get("condition_id") or "").lower(),
                str(position.get("outcome") or "").lower(),
            ),
        ).fetchone()
        if fp is not None:
            return StrategyManageState(
                current_price=_to_float(fp["current_price"], _to_float(position.get("avg_entry_price"))),
                signal_open=False,
                metadata={"current_amount": _to_float(fp["current_amount"])},
            )

        return StrategyManageState(
            current_price=_to_float(position.get("avg_entry_price")),
            signal_open=False,
            metadata={},
        )

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        stale_days = int(params.get("stale_days") or STALE_DAYS)
        win_threshold = _to_float(params.get("resolution_win_threshold"), RESOLUTION_WIN_THRESHOLD)
        loss_threshold = _to_float(params.get("resolution_loss_threshold"), RESOLUTION_LOSS_THRESHOLD)
        resolution_stop_cutoff = int(params.get("resolution_stop_cutoff_days", DEFAULTS["resolution_stop_cutoff_days"]))
        cascade_active = bool(position.get("cascade_active", False))
        cascade_mode = params.get("cascade_exit_mode", DEFAULTS["cascade_exit_mode"])
        current_price = _to_float(state.current_price, _to_float(position.get("avg_entry_price")))
        opened_ts = int(position.get("opened_ts") or 0)
        age_days = max(0, int((_now_ts() - opened_ts) / 86400)) if opened_ts else 0

        days_left = days_to_resolution(position.get("end_date_iso"))

        # Resolution exits always fire regardless of cutoffs
        if current_price >= win_threshold:
            return StrategyExitDecision(True, "resolution_win", exit_price=current_price, status="closed")
        if current_price <= loss_threshold:
            return StrategyExitDecision(True, "resolution_loss", exit_price=current_price, status="closed")

        # Score decay exit: if insider score dropped ≥25 points from entry, exit
        entry_score = _to_float(position.get("entry_insider_score"), 0.0)
        current_score = _to_float(state.metadata.get("position_insider_score"), 0.0)
        if entry_score > 0 and current_score > 0 and (entry_score - current_score) >= 25:
            logger.info(
                "insider_copy_v1: score decayed from %.0f to %.0f — exiting %s",
                entry_score, current_score, position.get("condition_id", ""),
            )
            return StrategyExitDecision(True, "score_decay", exit_price=current_price, status="closed")

        # Cascade: hold_through suppresses price-based stops; follow uses normal logic
        if cascade_active and cascade_mode == "hold_through":
            logger.info(
                "insider_copy_v1: cascade_active but cascade_mode=hold_through — skipping stop-loss for %s",
                position.get("condition_id", ""),
            )
            # Skip source_exited and staleness stops during cascade; only resolution exits pass above
            return StrategyExitDecision(False, "hold_cascade", exit_price=current_price, status="open")

        # Resolution stop cutoff: don't fire source_exited stop near resolution
        if not state.signal_open:
            if days_left is not None and days_left < resolution_stop_cutoff:
                logger.debug(
                    "insider_copy_v1: suppressing source_exited stop — %dd to resolution < %dd cutoff",
                    days_left, resolution_stop_cutoff,
                )
            elif current_price < 0.10 and (days_left is None or days_left > 3):
                # Don't follow source exit on near-zero price — let it resolve
                logger.info(
                    "insider_copy_v1: suppressing source_exited — price %.3f < 0.10, %.1fd left, holding to resolution",
                    current_price, days_left if days_left is not None else -1,
                )
                # Fall through, don't exit
            else:
                return StrategyExitDecision(True, "source_exited", exit_price=current_price, status="closed")

        if age_days >= stale_days:
            return StrategyExitDecision(True, f"stale_{age_days}d", exit_price=current_price, status="closed")
        return StrategyExitDecision(False, "hold", exit_price=current_price, status="open")

    def report(self, conn, strategy: Dict[str, Any], positions: List[Dict[str, Any]]) -> StrategyReport:
        return StrategyReport(summary={"position_count": len(positions)})

    def _calculate_position_size(self, signal: StrategySignal) -> float:
        entry_price = signal.current_price
        win_rate = _to_float(signal.metadata.get("win_rate"), 0.5)
        if win_rate > 1.0 and win_rate <= 100.0:
            win_rate = win_rate / 100.0
        win_rate = max(0.0, min(1.0, win_rate))

        raw_qroi = signal.metadata.get("qualifying_roi")
        qualifying_roi = None if raw_qroi is None else _to_float(raw_qroi, PAPER_SIZING_MIN_QROI)
        insider_score = _to_float(signal.metadata.get("position_insider_score"), 0.0)

        base_size = PAPER_POSITION_SIZE
        min_size = min(PAPER_MIN_POSITION_SIZE, PAPER_MAX_POSITION_SIZE)
        max_size = max(PAPER_MIN_POSITION_SIZE, PAPER_MAX_POSITION_SIZE)

        upside_mult = max(0.70, min(1.35, 1.35 - (entry_price * 0.9)))

        qroi_range = max(1.0, PAPER_SIZING_ELITE_QROI - PAPER_SIZING_MIN_QROI)
        if qualifying_roi is None:
            roi_mult = 0.85
        else:
            qroi_progress = max(
                0.0,
                min(1.0, (qualifying_roi - PAPER_SIZING_MIN_QROI) / qroi_range),
            )
            roi_mult = 0.90 + (qroi_progress * 0.50)
            if qualifying_roi > PAPER_SIZING_ELITE_QROI:
                roi_mult += min(0.10, (qualifying_roi - PAPER_SIZING_ELITE_QROI) / 100.0)
        roi_mult = max(0.85, min(1.50, roi_mult))

        insider_range = max(1.0, PAPER_SIZING_ELITE_INSIDER_SCORE - PAPER_SIZING_MIN_INSIDER_SCORE)
        insider_progress = max(
            0.0,
            min(1.0, (insider_score - PAPER_SIZING_MIN_INSIDER_SCORE) / insider_range),
        )
        insider_mult = 0.90 + (insider_progress * 0.45)
        if insider_score > PAPER_SIZING_ELITE_INSIDER_SCORE:
            insider_mult += min(0.10, (insider_score - PAPER_SIZING_ELITE_INSIDER_SCORE) / 100.0)
        insider_mult = max(0.85, min(1.45, insider_mult))

        if win_rate >= 0.80:
            win_mult = 1.10
        elif win_rate >= 0.70:
            win_mult = 1.03
        elif win_rate >= 0.60:
            win_mult = 1.00
        elif win_rate >= 0.50:
            win_mult = 0.95
        else:
            win_mult = 0.90

        size = base_size * upside_mult * roi_mult * insider_mult * win_mult
        return max(min_size, min(max_size, size))
