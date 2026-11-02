#!/usr/bin/env python3
"""Run the Hyperliquid microstructure live-paper engine.

Public market-data WebSocket only. Paper execution only. No private keys and no exchange order calls.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.hl_microstructure.book import BookState  # noqa: E402
from sniper.algos.hl_microstructure.catalysts import match_catalysts  # noqa: E402
from sniper.algos.hl_microstructure.experts import build_expert_signals  # noqa: E402
from sniper.algos.hl_microstructure.features import RollingMarketState  # noqa: E402
from sniper.algos.hl_microstructure.fusion import FusionContext, evaluate_fusion_policy  # noqa: E402
from sniper.algos.hl_microstructure.policies_gated import evaluate_fusion_gated_policy  # noqa: E402
from sniper.algos.hl_microstructure.liquidation import stress_from_positions  # noqa: E402
from sniper.algos.hl_microstructure.paper import PaperConfig, PaperEngine  # noqa: E402
from sniper.algos.hl_microstructure.policies import PolicyConfig, evaluate_policies  # noqa: E402
from sniper.algos.hl_microstructure.radar import rank_markets  # noqa: E402
from sniper.algos.hl_microstructure import persistence as P  # noqa: E402
from sniper.persistence.db_core import DEFAULT_DB_PATH as DEFAULT_CONTEXT_DB  # noqa: E402


HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "data/hl_microstructure_paper.sqlite"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _assets(raw: str) -> list[str]:
    return [a.strip().upper() for a in (raw or "").split(",") if a.strip()]


def _subscribe(ws, assets: list[str]) -> None:
    for coin in assets:
        for sub in (
            {"type": "l2Book", "coin": coin},
            {"type": "trades", "coin": coin},
            {"type": "bbo", "coin": coin},
            {"type": "activeAssetCtx", "coin": coin},
        ):
            ws.send(json.dumps({"method": "subscribe", "subscription": sub}))


def _unsubscribe(ws, assets: list[str]) -> None:
    for coin in assets:
        for sub in (
            {"type": "l2Book", "coin": coin},
            {"type": "trades", "coin": coin},
            {"type": "bbo", "coin": coin},
            {"type": "activeAssetCtx", "coin": coin},
        ):
            ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))


def _fetch_universe() -> list[dict]:
    req = urllib.request.Request(
        HL_INFO_URL,
        data=json.dumps({"type": "metaAndAssetCtxs"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        meta, ctxs = json.loads(resp.read().decode("utf-8"))
    return [{**m, "ctx": c} for m, c in zip(meta.get("universe") or [], ctxs or [])]


def _fixture_universe() -> list[dict]:
    return [
        {"name": "BTC", "ctx": {"midPx": "100", "dayNtlVlm": "1000000000", "openInterest": "30000", "funding": "0.000001", "premium": "0.0"}},
        {"name": "HOT", "ctx": {"midPx": "2", "dayNtlVlm": "6000000", "openInterest": "90000000", "funding": "-0.00020", "premium": "-0.004"}},
        {"name": "SLEEPY", "ctx": {"midPx": "1", "dayNtlVlm": "1000", "openInterest": "10", "funding": "0.0", "premium": "0.0"}},
    ]


def _select_assets(conn, universe: list[dict], *, decision_ts_ms: int, max_assets: int) -> list[str]:
    candidates = rank_markets(universe, max_assets=max_assets)
    P.record_radar_snapshot(conn, decision_ts_ms=decision_ts_ms, candidates=candidates)
    return [c.asset for c in candidates]


def _iso_to_ms(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        value = raw.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return None


def _load_context(context_db_path: str, assets: list[str], now_ms: int) -> FusionContext:
    if not context_db_path or not os.path.exists(context_db_path):
        return FusionContext()
    symbols = set(assets)
    news = []
    whale_rows = []
    try:
        conn = sqlite3.connect(f"file:{context_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            try:
                rows = conn.execute("""
                    SELECT *
                    FROM news_items
                    LIMIT 200
                """).fetchall()
                for r in rows:
                    item = dict(r)
                    ts = (
                        _iso_to_ms(item.get("created_at"))
                        or _iso_to_ms(item.get("published_at"))
                        or _iso_to_ms(item.get("inserted_at"))
                        or _iso_to_ms(item.get("stored_at"))
                    )
                    news.append({
                        "title": item.get("title") or item.get("headline") or "",
                        "source": item.get("source") or item.get("source_name") or "unknown",
                        "received_ts_ms": ts,
                    })
            except sqlite3.Error:
                pass
            try:
                rows = conn.execute("""
                    SELECT asset, side, position_value_usd, distance_pct, margin_type, captured_at
                    FROM hl_whale_positions
                    WHERE asset IN ({})
                """.format(",".join("?" for _ in assets)), tuple(assets)).fetchall()
                for r in rows:
                    whale_rows.append({
                        "asset": r["asset"],
                        "side": r["side"],
                        "notional_usdc": r["position_value_usd"],
                        "distance_to_liquidation_pct": r["distance_pct"],
                        "margin_mode": r["margin_type"],
                        "updated_ts_ms": _iso_to_ms(r["captured_at"]),
                    })
            except sqlite3.Error:
                pass
        finally:
            conn.close()
    except sqlite3.Error:
        return FusionContext()
    return FusionContext(
        catalysts=match_catalysts(news, symbols, now_ms=now_ms),
        liquidation_stress=stress_from_positions(whale_rows, now_ms=now_ms),
    )


def _record_and_apply(conn, state: RollingMarketState, msg: dict, receive_ts_ms: int) -> None:
    channel = msg.get("channel")
    data = msg.get("data")
    if channel == "subscriptionResponse":
        P.record_raw_event(conn, channel=channel, asset=None, exchange_ts_ms=None,
                           receive_ts_ms=receive_ts_ms, payload=msg)
        return
    if channel == "l2Book" and isinstance(data, dict):
        asset = str(data.get("coin") or "").upper()
        P.record_raw_event(conn, channel=channel, asset=asset, exchange_ts_ms=data.get("time"),
                           receive_ts_ms=receive_ts_ms, payload=data)
        state.update_book(BookState.from_l2(asset, data.get("levels") or [[], []],
                                            exchange_ts_ms=data.get("time"), receive_ts_ms=receive_ts_ms))
    elif channel == "trades" and isinstance(data, list):
        for trade in data:
            asset = str(trade.get("coin") or "").upper()
            P.record_raw_event(conn, channel=channel, asset=asset, exchange_ts_ms=trade.get("time"),
                               receive_ts_ms=receive_ts_ms, payload=trade)
            state.ingest_trade(asset, side=trade.get("side"), price=float(trade.get("px") or 0),
                               size=float(trade.get("sz") or 0), exchange_ts_ms=trade.get("time"),
                               receive_ts_ms=receive_ts_ms)
    elif channel == "bbo" and isinstance(data, dict):
        asset = str(data.get("coin") or "").upper()
        P.record_raw_event(conn, channel=channel, asset=asset, exchange_ts_ms=data.get("time"),
                           receive_ts_ms=receive_ts_ms, payload=data)
    elif channel == "activeAssetCtx" and isinstance(data, dict):
        asset = str(data.get("coin") or "").upper()
        P.record_raw_event(conn, channel=channel, asset=asset, exchange_ts_ms=None,
                           receive_ts_ms=receive_ts_ms, payload=data)
        state.update_asset_ctx(asset, data.get("ctx") or {}, receive_ts_ms=receive_ts_ms)


GATED_ARMS = ("b1", "b2")


def _run_decisions(conn, state: RollingMarketState, engine: PaperEngine, assets: list[str],
                   policy_cfg: PolicyConfig, *, decision_ts_ms: int, context: FusionContext | None = None,
                   fusion_only: bool = False, enable_gated_arms: bool = True) -> int:
    count = 0
    for asset in assets:
        snapshot = state.snapshot(asset, decision_ts_ms=decision_ts_ms)
        book = state.books.get(asset)
        mid = snapshot.features.get("mid") if snapshot.features else None
        if mid is not None:
            P.settle_due_expert_predictions(conn, asset=asset, decision_ts_ms=decision_ts_ms, mid=mid)
        expert_signals = build_expert_signals(snapshot, reliability=P.expert_reliability(conn))
        P.record_expert_predictions(conn, snapshot, expert_signals)
        asset_context = _with_experts(context or FusionContext(), asset, expert_signals)
        if not fusion_only:
            for decision in evaluate_policies(snapshot, policy_cfg):
                engine.on_decision(decision, snapshot, book)
                count += 1
            continue
        control = evaluate_fusion_policy(snapshot, asset_context)
        engine.on_decision(control, snapshot, book)
        count += 1
        if enable_gated_arms:
            count += _run_gated_arms(conn, engine, snapshot, book, asset, control, asset_context)
    return count


def _run_gated_arms(conn, engine: PaperEngine, snapshot, book, asset: str, control, asset_context) -> int:
    """Shadow A/B arms: entry differs from control, but an open arm position exits with the
    SAME control decision (relabeled to the arm's policy_id) so the experiment isolates entry."""
    count = 0
    for arm in GATED_ARMS:
        arm_id = f"fusion_gated_{arm}"
        open_pos = P.open_position_for(conn, policy_id=arm_id, asset=asset)
        if open_pos is not None:
            decision = dataclasses.replace(control, policy_id=arm_id)
        else:
            decision = evaluate_fusion_gated_policy(snapshot, asset_context, arm=arm)
        engine.on_decision(decision, snapshot, book)
        count += 1
    return count


def _with_experts(context: FusionContext, asset: str, signals) -> FusionContext:
    expert_signals = dict(context.expert_signals or {})
    expert_signals[asset] = list(signals or [])
    return FusionContext(
        catalysts=context.catalysts,
        liquidation_stress=context.liquidation_stress,
        radar_candidates=context.radar_candidates,
        expert_signals=expert_signals,
    )


def _run_fixture(conn, args) -> int:
    now = 1_800_000_000_000
    assets = _assets(args.assets)
    if args.auto_select_assets:
        assets = _select_assets(conn, _fixture_universe(), decision_ts_ms=now, max_assets=args.max_assets)
    state = RollingMarketState()
    engine = PaperEngine(conn, PaperConfig(notional_usdc=args.notional_usdc, fee_rate=args.fee_rate))
    policy_cfg = PolicyConfig(max_spread_bps=args.max_spread_bps)
    base = now

    for i in range(6):
        ts = base + i * 1000
        for asset in assets:
            base_px = 2.0 if asset == "HOT" else 100.0
            bid = base_px + i * (0.01 if asset == "HOT" else 0.05)
            ask = bid + (0.001 if asset == "HOT" else 0.05)
            state.update_book(BookState.from_l2(
                asset,
                [[{"px": str(bid), "sz": "5000"}], [{"px": str(ask), "sz": "2000"}]],
                exchange_ts_ms=ts,
                receive_ts_ms=ts,
            ))
            state.ingest_trade(asset, side="B", price=ask, size=0.2 + i * 0.05,
                               exchange_ts_ms=ts, receive_ts_ms=ts)
            state.update_asset_ctx(asset, {"funding": "-0.00020" if asset == "HOT" else "0.0", "premium": "-0.004" if asset == "HOT" else "0.0"}, receive_ts_ms=ts)
        context = FusionContext() if not args.auto_select_assets else _load_context(args.context_db_path, assets, ts)
        _run_decisions(conn, state, engine, assets, policy_cfg, decision_ts_ms=ts,
                       context=context, fusion_only=args.auto_select_assets,
                       enable_gated_arms=args.gated_arms)
    return 0


def _run_live(conn, args) -> int:
    import websocket

    closed_exc = getattr(websocket, "WebSocketConnectionClosedException", None)
    assets = _assets(args.assets)
    if args.auto_select_assets:
        try:
            assets = _select_assets(conn, _fetch_universe(), decision_ts_ms=_now_ms(), max_assets=args.max_assets)
        except Exception as e:
            print(f"asset auto-select failed, falling back to --assets: {e}", file=sys.stderr)
    state = RollingMarketState()
    engine = PaperEngine(conn, PaperConfig(notional_usdc=args.notional_usdc, fee_rate=args.fee_rate))
    policy_cfg = PolicyConfig(max_spread_bps=args.max_spread_bps)
    ws = websocket.create_connection(HL_WS_URL, timeout=10)
    _subscribe(ws, assets)
    start = time.monotonic()
    last_decision = 0
    last_asset_refresh = time.monotonic()
    decisions = 0
    try:
        while True:
            if args.duration_seconds > 0 and time.monotonic() - start >= args.duration_seconds:
                break
            ws.settimeout(1)
            try:
                raw = ws.recv()
                if not raw:
                    print("websocket closed; exiting for restart: empty frame", file=sys.stderr)
                    return 2
                receive_ts = _now_ms()
                _record_and_apply(conn, state, json.loads(raw), receive_ts)
            except TimeoutError:
                receive_ts = _now_ms()
            except Exception as e:
                if (closed_exc is not None and isinstance(e, closed_exc)) or "socket is already closed" in str(e).lower():
                    print(f"websocket closed; exiting for restart: {e}", file=sys.stderr)
                    return 2
                receive_ts = _now_ms()
            if receive_ts - last_decision >= args.decision_interval_ms:
                if args.auto_select_assets and time.monotonic() - last_asset_refresh >= args.asset_refresh_seconds:
                    try:
                        new_assets = _select_assets(conn, _fetch_universe(), decision_ts_ms=receive_ts, max_assets=args.max_assets)
                        if new_assets != assets:
                            old = [a for a in assets if a not in new_assets]
                            add = [a for a in new_assets if a not in assets]
                            if old:
                                _unsubscribe(ws, old)
                            if add:
                                _subscribe(ws, add)
                            assets = new_assets
                        last_asset_refresh = time.monotonic()
                    except Exception as e:
                        print(f"asset refresh failed: {e}", file=sys.stderr)
                context = _load_context(args.context_db_path, assets, receive_ts) if args.auto_select_assets else FusionContext()
                decisions += _run_decisions(conn, state, engine, assets, policy_cfg, decision_ts_ms=receive_ts,
                                            context=context, fusion_only=args.auto_select_assets,
                                            enable_gated_arms=args.gated_arms)
                last_decision = receive_ts
    finally:
        try:
            ws.close()
        except Exception:
            pass
    print(f"hl_microstructure_paper decisions_recorded={decisions}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hyperliquid microstructure live-paper engine")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--assets", default="BTC,ETH,SOL")
    ap.add_argument("--duration-seconds", type=int, default=0, help="0 means run until interrupted")
    ap.add_argument("--decision-interval-ms", type=int, default=1000)
    ap.add_argument("--notional-usdc", type=float, default=25.0)
    ap.add_argument("--fee-rate", type=float, default=0.00045)
    ap.add_argument("--max-spread-bps", type=float, default=12.0)
    ap.add_argument("--auto-select-assets", action="store_true")
    ap.add_argument("--max-assets", type=int, default=6)
    ap.add_argument("--asset-refresh-seconds", type=int, default=300)
    ap.add_argument("--context-db-path", default=DEFAULT_CONTEXT_DB)
    ap.add_argument("--gated-arms", action=argparse.BooleanOptionalAction, default=True,
                    help="run the fusion_gated_b1/b2 forward A/B arms alongside fusion_radar (fusion path only)")
    ap.add_argument("--fixture-mode", action="store_true", help="deterministic no-network smoke")
    args = ap.parse_args(argv)

    conn = P.connect(args.db_path)
    try:
        P.ensure_schema(conn)
        if args.fixture_mode:
            return _run_fixture(conn, args)
        return _run_live(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
