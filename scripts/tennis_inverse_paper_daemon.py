#!/usr/bin/env python3
"""Paper-only live runner for inverse tennis panic-dip entries."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from sniper.services.stink_bid_runner import _fetch_gamma_tennis_h2h

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class InverseCandidate:
    condition_id: str
    title: str
    favorite_label: str
    favorite_token_id: str
    favorite_price: float
    underdog_label: str
    underdog_token_id: str
    underdog_price: float
    ref_price: float


@dataclass(frozen=True)
class InverseDecision:
    enter: bool
    reason: str
    entry_label: str = ""
    entry_token_id: str = ""
    entry_price: float = 0.0


def _parse_json_or_list(value) -> list:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return value if isinstance(value, list) else []


def candidate_from_pair(
    pair: dict,
    *,
    min_ref: float,
    max_ref: float,
    saved: Optional[dict] = None,
) -> Optional[InverseCandidate]:
    market = pair.get("market") or {}
    event = pair.get("event") or {}
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
    title = str(market.get("question") or event.get("title") or "")
    outcomes = [str(item) for item in _parse_json_or_list(market.get("outcomes"))]
    token_ids = [str(item) for item in _parse_json_or_list(market.get("clobTokenIds"))]
    try:
        prices = [float(item) for item in _parse_json_or_list(market.get("outcomePrices"))]
    except (TypeError, ValueError):
        return None
    if not condition_id or len(outcomes) < 2 or len(token_ids) < 2 or len(prices) < 2:
        return None
    if saved:
        favorite_token_id = str(saved.get("favorite_token_id") or "")
        if favorite_token_id not in token_ids:
            return None
        fav_idx = token_ids.index(favorite_token_id)
        saved_underdog_token_id = str(saved.get("underdog_token_id") or "")
        if saved_underdog_token_id in token_ids:
            dog_idx = token_ids.index(saved_underdog_token_id)
        else:
            dog_idx = 1 - fav_idx
        ref_price = float(saved.get("ref_price") or prices[fav_idx])
    else:
        fav_idx = max(range(len(prices[:2])), key=lambda idx: prices[idx])
        dog_idx = 1 - fav_idx
        ref_price = float(prices[fav_idx])
    if ref_price < min_ref or ref_price > max_ref:
        return None
    return InverseCandidate(
        condition_id=condition_id,
        title=title,
        favorite_label=str(saved.get("favorite_label") or outcomes[fav_idx]) if saved else outcomes[fav_idx],
        favorite_token_id=token_ids[fav_idx],
        favorite_price=float(prices[fav_idx]),
        underdog_label=str(saved.get("underdog_label") or outcomes[dog_idx]) if saved else outcomes[dog_idx],
        underdog_token_id=token_ids[dog_idx],
        underdog_price=float(prices[dog_idx]),
        ref_price=ref_price,
    )


def should_enter_inverse(
    candidate: InverseCandidate,
    *,
    mode: str,
    drop_pct: float,
    max_entry_price: float,
    slippage: float = 0.0,
) -> InverseDecision:
    entry_price = min(candidate.underdog_price + slippage, 0.999)
    if entry_price > max_entry_price:
        return InverseDecision(False, "entry_price_above_max")
    if mode == "favorite_flip_fade":
        if candidate.favorite_price < 0.50:
            return InverseDecision(
                True,
                "favorite_flip_fade_entry",
                candidate.underdog_label,
                candidate.underdog_token_id,
                entry_price,
            )
        return InverseDecision(False, "no_favorite_flip")
    if mode == "favorite_drop_fade":
        if candidate.favorite_price <= candidate.ref_price * (1.0 - drop_pct):
            return InverseDecision(
                True,
                "favorite_drop_fade_entry",
                candidate.underdog_label,
                candidate.underdog_token_id,
                entry_price,
            )
        return InverseDecision(False, "no_drop_trigger")
    raise ValueError(f"unsupported mode: {mode}")


def insert_paper_position(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    candidate: InverseCandidate,
    entry_price: float,
    size_usdc: float,
    max_attempts: int = 3,
    retry_sleep_seconds: float = 2.0,
) -> Optional[int]:
    if entry_price <= 0:
        raise ValueError("entry_price must be > 0")
    now_ts = int(time.time())
    wallet = f"paper:tennis-inverse:{strategy_id}"
    shares = size_usdc / entry_price
    attempts = max(int(max_attempts), 1)
    for attempt in range(1, attempts + 1):
        try:
            cur = conn.execute(
                """
                INSERT INTO strategy_positions (
                    wallet, condition_id, outcome, strategy_id, state,
                    token_id, entry_price, avg_entry_price,
                    entry_shares, filled_entry_shares,
                    current_price, current_value, size_usdc,
                    opened_ts, updated_ts, instance_tag, version
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    wallet,
                    candidate.condition_id,
                    candidate.underdog_label,
                    str(strategy_id),
                    candidate.underdog_token_id,
                    entry_price,
                    entry_price,
                    shares,
                    shares,
                    entry_price,
                    size_usdc,
                    size_usdc,
                    now_ts,
                    now_ts,
                    "tennis-inverse-paper",
                ),
            )
            try:
                conn.execute(
                    """
                    UPDATE strategies
                    SET trades_triggered = COALESCE(trades_triggered, 0) + 1,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (strategy_id,),
                )
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
            conn.commit()
            return int(cur.lastrowid or 0)
        except sqlite3.IntegrityError:
            conn.rollback()
            return None
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" not in str(exc).lower() or attempt >= attempts:
                raise
            time.sleep(retry_sleep_seconds)
    return None


def _load_strategy_params(db_path: str, strategy_id: int) -> dict:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT parameters FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    try:
        return json.loads(row["parameters"] or "{}")
    except json.JSONDecodeError:
        return {}


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def run_once(
    *,
    db_path: str,
    strategy_id: int,
    state_path: str | Path,
    mode: str,
    drop_pct: float,
    min_ref: float,
    max_ref: float,
    slippage: float,
    max_entry_price: float,
    position_size_usdc: float,
    session: Optional[requests.Session] = None,
    dry_run: bool = False,
) -> dict:
    session = session or requests.Session()
    pairs = _fetch_gamma_tennis_h2h(session)
    state_file = Path(state_path)
    state = _load_state(state_file)
    opened = []
    would_open = []
    skipped = {}
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        for pair in pairs:
            market = pair.get("market") or {}
            condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
            saved = state.get(condition_id) if condition_id else None
            candidate = candidate_from_pair(pair, min_ref=min_ref, max_ref=max_ref, saved=saved)
            if candidate is None:
                skipped["not_candidate"] = skipped.get("not_candidate", 0) + 1
                continue
            saved = state.setdefault(
                candidate.condition_id,
                {
                    "ref_price": candidate.ref_price,
                    "favorite_label": candidate.favorite_label,
                    "favorite_token_id": candidate.favorite_token_id,
                    "underdog_label": candidate.underdog_label,
                    "underdog_token_id": candidate.underdog_token_id,
                    "created_ts": int(time.time()),
                },
            )
            decision = should_enter_inverse(
                candidate,
                mode=mode,
                drop_pct=drop_pct,
                max_entry_price=max_entry_price,
                slippage=slippage,
            )
            if not decision.enter:
                skipped[decision.reason] = skipped.get(decision.reason, 0) + 1
                continue
            open_row = {
                "condition_id": candidate.condition_id,
                "title": candidate.title,
                "outcome": decision.entry_label,
                "entry_price": decision.entry_price,
                "reason": decision.reason,
            }
            if dry_run:
                would_open.append(open_row)
                continue
            position_id = insert_paper_position(
                conn,
                strategy_id=strategy_id,
                candidate=candidate,
                entry_price=decision.entry_price,
                size_usdc=position_size_usdc,
            )
            if position_id is None:
                skipped["existing_position"] = skipped.get("existing_position", 0) + 1
                continue
            opened.append(
                {
                    **open_row,
                    "position_id": position_id,
                }
            )
    finally:
        conn.close()
    _save_state(state_file, state)
    return {
        "pairs_seen": len(pairs),
        "opened": opened,
        "would_open": would_open,
        "skipped": skipped,
        "state_path": str(state_file),
        "dry_run": dry_run,
    }


async def run_loop(args) -> None:
    while True:
        try:
            out = run_once(
                db_path=args.db,
                strategy_id=args.strategy_id,
                state_path=args.state,
                mode=args.mode,
                drop_pct=args.drop_pct,
                min_ref=args.min_ref,
                max_ref=args.max_ref,
                slippage=args.slippage,
                max_entry_price=args.max_entry_price,
                position_size_usdc=args.position_size_usdc,
                dry_run=False,
            )
            LOG.info("tennis_inverse cycle %s", json.dumps(out, sort_keys=True))
        except Exception:
            LOG.exception("tennis_inverse cycle failed")
        await asyncio.sleep(args.discover_seconds)


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--strategy-id", type=int, required=True)
    parser.add_argument("--state", default="data/live_shadow/tennis_inverse_state.json")
    parser.add_argument("--mode", choices=["favorite_drop_fade", "favorite_flip_fade"], default="favorite_drop_fade")
    parser.add_argument("--drop-pct", type=float, default=0.30)
    parser.add_argument("--min-ref", type=float, default=0.60)
    parser.add_argument("--max-ref", type=float, default=0.90)
    parser.add_argument("--slippage", type=float, default=0.03)
    parser.add_argument("--max-entry-price", type=float, default=0.98)
    parser.add_argument("--position-size-usdc", type=float, default=25.0)
    parser.add_argument("--discover-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    params = _load_strategy_params(args.db, args.strategy_id)
    for key in ("mode", "drop_pct", "min_ref", "max_ref", "slippage", "max_entry_price", "position_size_usdc"):
        if key in params:
            setattr(args, key.replace("-", "_"), params[key])
    if args.once:
        print(
            json.dumps(
                run_once(
                    db_path=args.db,
                    strategy_id=args.strategy_id,
                    state_path=args.state,
                    mode=args.mode,
                    drop_pct=args.drop_pct,
                    min_ref=args.min_ref,
                    max_ref=args.max_ref,
                    slippage=args.slippage,
                    max_entry_price=args.max_entry_price,
                    position_size_usdc=args.position_size_usdc,
                    dry_run=args.dry_run,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return
    asyncio.run(run_loop(args))


if __name__ == "__main__":
    main()
