"""Materialize point-in-time V6 observations and enumerate V6 capture targets.

``build_observation`` is chain-neutral: it calls the whole-bag executable sell quote once, merges
point-in-time chain facts computed directly from the read-only chain tape, records provenance, and
fingerprints the canonical payload. The materialized exit-state DB is only a fallback for injected
callers without a tape. It NEVER chooses an action — a missing fact stays ``None`` and surfaces in
``missing_fields`` so the engine turns it into a ``data_insufficient`` HOLD. It must not import V5,
the exit engine, the real runner, or legacy bracket rails.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sniper.services.memecoin_v6_engine import (
    POLICY_ID, TIME_EXPIRY_S, EvidenceIdentity, _missing_capture, fingerprint, make_observation,
    semantic_observation_id,
)
from sniper.services.memecoin_quotes import (
    QuoteFailure, QuoteStatus, quote_exit, quote_exit_outcome,
)

CAPTURE_VERSION = "v6-direct-v1"
_MAX_TICKS = 5000
_SOL_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_SOL_WSOL_MINT = "So11111111111111111111111111111111111111112"
_ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
_NATIVE_EVM = "0x0000000000000000000000000000000000000000"


class ObservationSourceError(RuntimeError):
    """A required tape/state source is unavailable or structurally invalid."""


def _age_s(opened_at: str, now_iso: str) -> float:
    start = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
    now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    return (now - start).total_seconds()


def _float_or_none(value):
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        try:
            parsed = float(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _event_epoch(value):
    parsed = _float_or_none(value)
    if parsed is not None:
        return parsed
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _quote_asset_unit(network_id, asset):
    """Normalize only assets with a proven 1:1 unit identity; never infer USD equivalence."""
    if asset is None:
        return None
    value = str(asset).strip().lower()
    if str(network_id) == "solana":
        if value in {"usdc", _SOL_USDC_MINT.lower()}:
            return "usdc"
        if value in {"sol", "wsol", _SOL_WSOL_MINT.lower()}:
            return "wsol"
    if str(network_id) == "4663":
        if value in {"weth", "eth", _ROBINHOOD_WETH.lower(), _NATIVE_EVM}:
            return "weth"
    return value


def _active_tape(tape_conn, network_id):
    if isinstance(tape_conn, Mapping):
        return tape_conn.get(network_id)
    return tape_conn


def _pool_for_position(tc, position):
    network_id = str(position["network_id"])
    mint = str(position["mint"])
    if network_id == "solana":
        capture_id = position.get("capture_position_id")
        if capture_id is not None:
            row = tc.execute(
                "SELECT pool FROM sol_position_pools WHERE position_id=? AND mint=?",
                (capture_id, mint)).fetchone()
            if row:
                return str(row[0])
        rows = tc.execute(
            "SELECT pool FROM pool_meta WHERE chain_id='solana' AND token0=?", (mint,)).fetchall()
        return str(rows[0][0]) if len(rows) == 1 else None
    route = position.get("route_binding")
    if isinstance(route, Mapping) and route.get("market_pool"):
        pool = str(route["market_pool"]).lower()
        if len(pool) == 42 and pool.startswith("0x"):
            return pool
    if isinstance(route, Mapping) and route.get("protocol") == "uniswap_v4" \
            and isinstance(route.get("pool_key"), Mapping):
        from sniper.services.memecoin_evm_v4_events import v4_pool_id
        try:
            return v4_pool_id(route["pool_key"])
        except ValueError:
            return None
    rows = tc.execute(
        "SELECT pool FROM pool_meta WHERE chain_id=? AND quote_side IS NOT NULL "
        "AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL "
        "AND ((quote_side=0 AND LOWER(token1)=?) OR (quote_side=1 AND LOWER(token0)=?))",
        (network_id, mint.lower(), mint.lower())).fetchall()
    return str(rows[0][0]).lower() if len(rows) == 1 else None


def _direct_tape_facts(position, *, now_iso, tape_conn, state_conn) -> dict:
    """Compute this V6 position's own causal market facts directly from its read-only tape.

    Funded ``memecoin_exit_market_state`` rows are keyed to funded position ids and therefore cannot
    cover an independent V6 paper fill. Reusing those rows by mint made V6 silently dependent on a
    coincident funded bag. The direct path reuses the same pure ``compute_state`` feature code over
    the V6-held route's tape without writing any shared database.
    """
    network_id = str(position["network_id"])
    tc = _active_tape(tape_conn, network_id)
    if tc is None:
        return {}
    try:
        pool = _pool_for_position(tc, position)
        if pool is None:
            return {"capture_status": "pool_unresolved", "chain_vol_p90":
                    _float_or_none(position.get("chain_vol_p90"))}
        kinds = {row[0] for row in tc.execute(
            "SELECT DISTINCT kind FROM pool_ticks WHERE chain_id=? AND pool=?",
            (network_id, pool))}
        if kinds & {"sol_swap", "sol_sync", "sol_curve_swap", "sol_curve_sync"}:
            pool_kind = "sol"
        elif "v2_swap" in kinds or "v2_sync" in kinds:
            pool_kind = "v2"
        elif "v3_swap" in kinds:
            pool_kind = "v3"
        else:
            pool_kind = None
        meta = tc.execute(
            "SELECT quote_side FROM pool_meta WHERE chain_id=? AND pool=?",
            (network_id, pool)).fetchone()
        quote_side = meta[0] if meta else None
        market_quote_asset = None
        if network_id == "solana":
            quote_row = tc.execute(
                "SELECT quote_mint FROM sol_pool_vaults WHERE pool=?", (pool,)).fetchone()
            market_quote_asset = quote_row[0] if quote_row else None
        else:
            token_row = tc.execute(
                "SELECT token0,token1 FROM pool_meta WHERE chain_id=? AND pool=?",
                (network_id, pool)).fetchone()
            if token_row and quote_side in (0, 1):
                market_quote_asset = token_row[int(quote_side)]
        rows = tc.execute(
            "SELECT kind, block_no, tx_sig, log_index, maker, amount0, amount1, tick, "
            "tick_lower, tick_upper, liquidity, sqrt_price_x96, block_hash FROM pool_ticks "
            "WHERE chain_id=? AND pool=? AND block_no IS NOT NULL "
            "ORDER BY block_no DESC, log_index DESC LIMIT ?",
            (network_id, pool, _MAX_TICKS)).fetchall()
        swaps = [dict(row) for row in rows]
        blocks = {row["block_no"] for row in swaps if row.get("block_no") is not None}
        from sniper.persistence import db_exit_state, db_ticks
        if network_id == "solana":
            block_times = db_ticks.block_ts_map(tc, network_id, blocks)
            cursor = tc.execute(
                "SELECT frontier_before FROM sol_sig_cursor WHERE vault=?", (pool,)).fetchone()
            tape_incomplete = bool(cursor and cursor[0])
        else:
            block_times = db_exit_state.merged_block_ts_map(tc, state_conn, network_id, blocks)
            tape_incomplete = False
        cutoff_ts = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00")).timestamp()
        # A shared tape can already contain a later block by the time this position is observed.
        # Keep unknown-time rows so compute_state fails closed; exclude only rows proven future.
        swaps = [row for row in swaps
                 if _event_epoch(block_times.get(row.get("block_no"))) is None
                 or _event_epoch(block_times.get(row.get("block_no"))) <= cutoff_ts]
        from sniper.services.memecoin_exit_market_state import compute_state
        from sniper.settings.memecoin_v6_paper import MEMECOIN_V6_V3_DEPTH_VERIFIED_CHAINS
        state = compute_state(
            swaps, block_times,
            {"chain_id": network_id, "mint": position["mint"], "pool": pool,
             "pool_kind": pool_kind, "quote_side": quote_side,
             "position_id": position["position_id"], "tape_incomplete": tape_incomplete,
             "engine_version": POLICY_ID,
             "v3_depth_verified": MEMECOIN_V6_V3_DEPTH_VERIFIED_CHAINS},
            now_ts=datetime.fromisoformat(str(now_iso).replace("Z", "+00:00")).timestamp())
    except sqlite3.Error as exc:
        raise ObservationSourceError(
            f"{network_id} tape schema/read failed for {position.get('position_id')}: {exc}") from exc

    buy_n, sell_n = state.buy_n, state.sell_n
    buy_share = None
    if buy_n is not None and sell_n is not None and buy_n + sell_n > 0:
        buy_share = buy_n / (buy_n + sell_n)
    serialized = state.to_dict()
    return {
        "price": _float_or_none(state.nominal_price),
        "prior_low": _float_or_none(state.prior_low),
        "net": _float_or_none(state.net_flow_quote),
        "arrival_rate": _float_or_none(state.arrival_rate),
        "buy_share": _float_or_none(buy_share),
        "realized_vol": _float_or_none(state.realized_vol),
        "chain_vol_p90": _float_or_none(position.get("chain_vol_p90")),
        "buy_n": buy_n, "sell_n": sell_n,
        "buy_vol_quote": state.buy_vol_quote, "sell_vol_quote": state.sell_vol_quote,
        "depth_pull_flag": state.depth_pull_flag,
        "liquidity_removed_fraction": state.liquidity_removed_fraction,
        "reserve_quote": state.reserve_quote, "reserve_delta": state.reserve_delta,
        "tape_complete": not tape_incomplete,
        "capture_status": state.state.value,
        "capture_version": f"{CAPTURE_VERSION}/exit-{state.feature_version}",
        "block_identity": state.latest_event_id,
        "source_event_time": state.source_event_time,
        "pool": pool,
        "market_quote_asset": market_quote_asset,
        "provenance": serialized.get("provenance"),
    }


def _persisted_state_facts(position, *, now_iso, state_conn) -> dict:
    """Best-effort point-in-time market facts for one position.

    Reads the latest ``memecoin_exit_market_state`` row for this mint with ``computed_at <= now_iso``
    (never a future row). Column facts (arrival_rate, realized_vol, drawdown, net) are read directly;
    prior_low and buy_share are pulled from the row's ``state_json`` when present.
    source_wallet_exited and wallet_tier_adjustment are carried on the position (the runner, which
    holds the source connection, enriches them); absent -> None / neutral 0. Anything unavailable
    stays absent so the engine HOLDs on ``data_insufficient`` rather than acting on a guess.
    This compatibility path is not used by the dedicated worker, which always supplies chain tapes.
    """
    facts: dict = {
        "source_wallet_exited": position.get("source_wallet_exited"),
        "wallet_tier_adjustment": position.get("wallet_tier_adjustment"),
    }
    facts["chain_vol_p90"] = _float_or_none(position.get("chain_vol_p90"))
    if state_conn is None:
        return facts
    try:
        prior = state_conn.row_factory
        state_conn.row_factory = sqlite3.Row
        try:
            row = state_conn.execute(
                "SELECT * FROM memecoin_exit_market_state WHERE mint=? AND chain_id=? "
                "AND computed_at <= ? ORDER BY computed_at DESC LIMIT 1",
                (position["mint"], position["network_id"], now_iso)).fetchone()
        finally:
            state_conn.row_factory = prior
    except sqlite3.Error as exc:
        raise ObservationSourceError(f"exit-state read failed: {exc}") from exc
    if row is None:
        return facts
    keys = row.keys()
    facts["arrival_rate"] = _float_or_none(row["arrival_rate"]) if "arrival_rate" in keys else None
    facts["realized_vol"] = _float_or_none(row["realized_vol"]) if "realized_vol" in keys else None
    facts["drawdown"] = _float_or_none(row["drawdown"]) if "drawdown" in keys else None
    facts["net"] = _float_or_none(row["net_flow_quote"]) if "net_flow_quote" in keys else None
    if "state_json" in keys and row["state_json"]:
        try:
            sj = json.loads(row["state_json"])
        except (TypeError, ValueError):
            sj = {}
        facts["prior_low"] = _float_or_none(sj.get("prior_low"))
        facts["price"] = _float_or_none(sj.get("nominal_price"))
        buy_n, sell_n = sj.get("buy_n"), sj.get("sell_n")
        if isinstance(buy_n, int) and isinstance(sell_n, int) and buy_n + sell_n > 0:
            facts["buy_share"] = buy_n / (buy_n + sell_n)
        else:
            facts["buy_share"] = _float_or_none(sj.get("buy_share"))
        if facts["net"] is None:
            facts["net"] = _float_or_none(sj.get("net_flow_quote"))
        facts["block_identity"] = sj.get("latest_event_id")
        facts["provenance"] = sj.get("provenance")
    facts["tape_complete"] = position.get("tape_complete")
    version = row["feature_version"] if "feature_version" in keys else None
    facts["capture_version"] = f"persisted-exit-{version}" if version else None
    if not facts.get("block_identity"):
        facts["block_identity"] = row["source_event_time"] if "source_event_time" in keys else None
    return facts


def _default_facts(position, *, now_iso, tape_conn, state_conn) -> dict:
    facts = {
        "source_wallet_exited": position.get("source_wallet_exited"),
        "source_status": position.get("source_status"),
        "source_evidence_id": position.get("source_evidence_id"),
        "wallet_tier_adjustment": position.get("wallet_tier_adjustment"),
    }
    if _active_tape(tape_conn, position["network_id"]) is not None:
        facts.update(_direct_tape_facts(position, now_iso=now_iso, tape_conn=tape_conn,
                                        state_conn=state_conn))
        return facts
    facts.update(_persisted_state_facts(position, now_iso=now_iso, state_conn=state_conn))
    return facts


def build_observation(position, *, now_iso, tape_conn, state_conn, quote_fn=quote_exit,
                      facts_fn=None, pool_facts=None):
    """Freeze one point-in-time observation for an open V6 position. Returns a V6Observation whose
    payload carries the executable quote, position economics, and chain facts; missing facts stay
    None and are listed under ``missing_fields``. Raises only on genuinely malformed input."""
    network_id = position["network_id"]
    mint = position["mint"]
    position_id = str(position.get("position_id") or position.get("entry_id") or "").strip()
    if not position_id:
        raise ValueError("position_id is required for a V6 observation")
    remaining = int(position["remaining_qty_atomic"])

    # 1. One whole-bag executable sell quote. Failure is sellability evidence, not an exception.
    executable_price = whole_bag_proceeds = quote_freshness_s = None
    route_binding = None
    route_degraded = False
    sellable = False
    observation_at = now_iso
    quote_evidence_id = None
    quote_status = QuoteStatus.PROVIDER_UNAVAILABLE
    quote_failure_code = None
    quote_out_asset = quote_provider = None
    healthy_quote_semantics = None
    outcome = quote_exit_outcome(
        position, remaining, adapter=quote_fn, observed_at=now_iso)
    if not isinstance(outcome, QuoteFailure):
        fill = outcome
        executable_price = float(Decimal(fill.unit_price))
        whole_bag_proceeds = fill.quote_out_atomic
        route_binding = dict(fill.route_binding)
        quote_freshness_s = float(getattr(fill, "quote_age_s", 0.0))
        quote_out_asset = getattr(fill, "quote_out_asset", None)
        quote_provider = getattr(fill, "quote_provider", None)
        quote_observed_at = getattr(fill, "observed_at", None)
        if quote_observed_at:
            quote_dt = datetime.fromisoformat(str(quote_observed_at).replace("Z", "+00:00"))
            cutoff_dt = datetime.fromisoformat(str(observation_at).replace("Z", "+00:00"))
            if quote_dt > cutoff_dt:
                observation_at = str(quote_observed_at)
        route_degraded = False
        sellable = True
        quote_status = QuoteStatus.HEALTHY
        healthy_quote_semantics = {
            "network_id": network_id,
            "provider": getattr(fill, "quote_provider", None),
            "route_binding": route_binding,
            "input_atomic": remaining,
            "output_atomic": whole_bag_proceeds,
            "output_asset": quote_out_asset,
        }
        entry_price = position.get("entry_unit_price")
        try:
            is_catastrophic = (entry_price is not None and Decimal(str(executable_price))
                               <= Decimal(str(entry_price)) * Decimal("0.20"))
        except (ArithmeticError, ValueError):
            is_catastrophic = False
        if is_catastrophic:
            # Two independent healthy requests must be able to confirm a catastrophic quote even
            # when their executable economics are identical. Replaying one fill keeps one check.
            healthy_quote_semantics["catastrophe_check_at"] = (
                str(quote_observed_at or observation_at))
    else:
        quote_status = outcome.status
        route_degraded = quote_status is QuoteStatus.ROUTE_ABSENT
        quote_failure_code = outcome.detail_code
        quote_evidence_id = outcome.evidence_id

    held_s = max(0.0, _age_s(position["opened_at"], observation_at))
    if healthy_quote_semantics is not None:
        # Provider timestamps and opaque payload fingerprints change on every refresh even when
        # the executable economics do not. They are transport evidence, not a new engine fact.
        # Preserve the one quiet-time semantic boundary the engine must still observe.
        quote_evidence_id = "quote-" + fingerprint({
            **healthy_quote_semantics,
            "time_expired": held_s >= TIME_EXPIRY_S,
        })

    # 2. Chain facts (injectable; default reads materialized market-state point-in-time).
    if pool_facts is not None and facts_fn is not None:
        raise ValueError("pool_facts and facts_fn are mutually exclusive")
    if pool_facts is not None:
        if str(pool_facts.network_id) != str(network_id) \
                or str(pool_facts.mint).lower() != str(mint).lower():
            raise ValueError("pool_facts do not belong to this position")
        facts = pool_facts.to_observation_facts()
        facts.update({
            "source_wallet_exited": position.get("source_wallet_exited"),
            "source_status": position.get("source_status"),
            "source_evidence_id": position.get("source_evidence_id"),
            "wallet_tier_adjustment": position.get("wallet_tier_adjustment"),
        })
        # PoolMeta is long-lived. This cross-position trailing value is recomputed each decision.
        facts["chain_vol_p90"] = _float_or_none(position.get("chain_vol_p90"))
    else:
        facts = (facts_fn or _default_facts)(
            position, now_iso=observation_at, tape_conn=tape_conn, state_conn=state_conn)
    price = _float_or_none(facts.get("price"))
    prior_low = facts.get("prior_low")
    market_quote_asset = facts.get("market_quote_asset")
    comparable_quote_asset = None
    if market_quote_asset is not None and quote_out_asset is not None:
        comparable_quote_asset = (
            _quote_asset_unit(network_id, market_quote_asset)
            == _quote_asset_unit(network_id, quote_out_asset)
        )
    if price is None and comparable_quote_asset is True:
        # The whole-bag route quote is the freshest price the position can actually realize. A
        # continuously checked quiet pool with no structural level can use it. Once structural
        # history exists, unlike quote assets must never be compared.
        price = executable_price
    net = facts.get("net")

    # 3. is_break derived per the seed's definition: below the prior confirmed low with negative
    # decoded flow. No confirmed low means no confirmed break (False), not unavailable evidence.
    is_break = None
    if price is not None and net is not None:
        is_break = bool(prior_low is not None and price < prior_low and net < 0)

    prior_high_water = _float_or_none(position.get("executable_high_water"))
    high_water = prior_high_water
    if executable_price is not None:
        high_water = max(executable_price, prior_high_water or executable_price)
    drawdown = facts.get("drawdown")
    if executable_price is not None and high_water is not None and high_water > 0:
        drawdown = 1.0 - executable_price / high_water

    market_evidence_id = None
    if facts.get("block_identity") is not None:
        market_evidence_id = "market-" + fingerprint({
            "network_id": network_id,
            "pool": facts.get("pool"),
            "capture_version": facts.get("capture_version"),
            "block_identity": facts.get("block_identity"),
        })
    source_evidence_id = facts.get("source_evidence_id")
    if source_evidence_id is not None:
        source_evidence_id = "source-" + fingerprint({
            "network_id": network_id, "source_evidence_id": source_evidence_id,
        })
    evidence = EvidenceIdentity(
        market=market_evidence_id, quote=quote_evidence_id, source=source_evidence_id)

    payload = {
        # engine-scored evidence
        "executable_price": executable_price, "price": price, "held_s": held_s,
        "is_break": is_break, "prior_low": prior_low, "net": net,
        "arrival_rate": facts.get("arrival_rate"), "buy_share": facts.get("buy_share"),
        "realized_vol": facts.get("realized_vol"), "chain_vol_p90": facts.get("chain_vol_p90"),
        "route_degraded": route_degraded,
        "quote_status": quote_status.value,
        "quote_failure_code": quote_failure_code,
        "quote_out_asset": quote_out_asset,
        "market_quote_asset": market_quote_asset,
        "quote_units_comparable": comparable_quote_asset,
        "quote_provider": quote_provider,
        "source_wallet_exited": facts.get("source_wallet_exited"),
        "source_status": facts.get("source_status"),
        "wallet_tier_adjustment": facts.get("wallet_tier_adjustment"),
        # provenance / diagnostics (engine ignores these keys)
        "remaining_qty_atomic": remaining, "whole_bag_proceeds_atomic": whole_bag_proceeds,
        "quote_freshness_s": quote_freshness_s, "sellable": sellable,
        "route_binding": route_binding,
        "executable_high_water": high_water,
        "drawdown": drawdown,
        "cost_basis_quote_atomic": position.get("cost_basis_quote_atomic"),
        "realized_pnl_quote_atomic": position.get("realized_pnl_quote_atomic"),
        "tape_complete": facts.get("tape_complete"),
        "capture_version": facts.get("capture_version"),
        "block_identity": facts.get("block_identity"),
        "source_event_time": facts.get("source_event_time"),
        "buy_n": facts.get("buy_n"), "sell_n": facts.get("sell_n"),
        "buy_vol_quote": facts.get("buy_vol_quote"),
        "sell_vol_quote": facts.get("sell_vol_quote"),
        "depth_pull_flag": facts.get("depth_pull_flag"),
        "depth_provenance": (
            ((facts.get("provenance") or {}).get("depth") or {}).get("quality")
            if isinstance(facts.get("provenance"), Mapping) else None
        ),
        "liquidity_removed_fraction": facts.get("liquidity_removed_fraction"),
        "reserve_quote": facts.get("reserve_quote"), "reserve_delta": facts.get("reserve_delta"),
        "capture_status": facts.get("capture_status"), "pool": facts.get("pool"),
        "market_event_kind": facts.get("market_event_kind"),
        "provenance": facts.get("provenance"),
        "network_id": network_id,
        "market_evidence_id": evidence.market,
        "quote_evidence_id": evidence.quote,
        "source_evidence_id": evidence.source,
    }
    payload["missing_fields"] = _missing_capture(payload)

    observation_id = semantic_observation_id(
        position_id, evidence, sequence=position.get("observation_sequence"))
    return make_observation(observation_id=observation_id, observed_at=observation_at,
                            network_id=network_id, payload=payload, evidence=evidence)


def v6_capture_position_id(entry_id: str) -> int:
    """Stable negative INTEGER namespace for the Solana tape's position-pool cache."""
    return -(int(fingerprint({"v6_entry_id": entry_id})[:15], 16) or 1)


def v6_capture_positions(v6_conn, network_id) -> list[dict]:
    """Open V6 paper positions on ``network_id`` as tape targets: ``{id, mint, network_id,
    opened_at}``. Read-only over the V6 book; the tape resolves the vault from the mint."""
    prior = v6_conn.row_factory
    v6_conn.row_factory = sqlite3.Row
    try:
        rows = v6_conn.execute(
            "SELECT p.entry_id, p.mint, p.network_id, e.fill_ts, e.route_binding_json "
            "FROM v6_paper_positions p JOIN v6_paper_entries e ON e.entry_id = p.entry_id "
            "WHERE p.status='open' AND p.network_id=? ORDER BY p.entry_id", (network_id,)).fetchall()
    finally:
        v6_conn.row_factory = prior
    out = []
    for row in rows:
        # sol_position_pools.position_id is INTEGER PRIMARY KEY. A V6 entry id is text, which makes
        # the tape's otherwise-valid resolution cache insert fail with SQLITE_MISMATCH. Bind the
        # separate book through a deterministic negative integer namespace; funded ids are positive.
        capture_id = v6_capture_position_id(row["entry_id"])
        try:
            route_binding = json.loads(row["route_binding_json"])
        except (TypeError, json.JSONDecodeError):
            route_binding = None
        out.append({"id": capture_id, "entry_id": row["entry_id"], "mint": row["mint"],
                    "network_id": row["network_id"], "opened_at": row["fill_ts"],
                    "route_binding": route_binding})
    return out
