"""PumpSwap U crossings from explicit causal facts; missing metadata keeps the worker capture-only."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U


def _stamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def state_from_print(*, event, mapping, metadata, slot, signature, ordinal, tx_index, now, received_at, sol_usd,
                     sol_usd_at, reserve_max_age_s, sol_max_age_s, metadata_optional=False):
    """The exact pool state, mcap and liquidity a proven PumpSwap BUY print gives (identity, clocks, virtual quote,
    fees, metadata, causal FX). Shared by PoolBook (band emissions) and the desk intake book; ValueError(reason) on
    anything unproven. Returns (pool, mint, state, mcap, liq, at).
    metadata_optional (the desk INTAKE book only, Alpha F3 2026-10-27): no metadata means supply/decimals and the
    metadata clocks are None and mcap is None; the hard filter then takes supply/decimals from the assembler's own
    mint read. The band PoolBook never passes it."""
    pool = event.get('pool') if isinstance(event, dict) else None
    if not pool or not isinstance(mapping, dict):
        raise ValueError('unmapped_pool')
    if not isinstance(metadata, dict) and not (metadata_optional and metadata is None):
        raise ValueError('metadata_not_yet_known')
    if (not _integer(slot, 1) or not _integer(ordinal)
            or (tx_index is not None and not _integer(tx_index))
            or len(U.b58decode(signature)) != 64 or not _stamp(now)):
        raise ValueError('unknown_facts')
    if not _stamp(received_at) or not 0 <= now - received_at <= reserve_max_age_s:
        raise ValueError('reserve_stale')
    if (not _stamp(sol_usd) or sol_usd == 0 or not _stamp(sol_usd_at)
            or not 0 <= now - sol_usd_at <= sol_max_age_s):
        raise ValueError('sol_stale')
    at = datetime.fromtimestamp(now, timezone.utc)
    now = at.timestamp()
    mint = mapping['mint']
    if (mapping['pool'] != pool or mapping['canonical'] is not True
            or pool != S.pumpswap_pool_address(mint) or mapping['quote_mint'] != K.WSOL_MINT
            or not _stamp(mapping['known_at']) or mapping['known_at'] > received_at
            or not _integer(mapping['slot']) or mapping['slot'] > slot
            or (metadata is not None and (any(metadata[k] != value for k, value in (
                ('pool', pool), ('mint', mint), ('quote_mint', K.WSOL_MINT)))
                or not _integer(metadata['slot'])))):
        raise ValueError('unknown_identity_or_price')
    if metadata is not None and metadata['slot'] > slot:
        raise ValueError('metadata_after_reserve')
    if metadata is not None and (not _stamp(metadata['known_at']) or metadata['known_at'] >= received_at):
        raise ValueError('metadata_not_yet_known')
    meta = metadata or {}
    if (event.get('kind') != 'buy' or event.get('virtual_quote_atomic') is None
            or event.get('virtual_quote_source') != 'pumpswap_grpc:buy_event_virtual_quote'):
        raise ValueError('virtual_quote_unproven')
    if (type(event.get('cashback_fee_bps')) is not int or event['cashback_fee_bps'] != 0
            or any(type(event.get(k)) is not int or not 0 <= event[k] < 10000
                   for k in ('lp_fee_bps','protocol_fee_bps','creator_fee_bps'))):
        raise ValueError('fee_unknown')
    state = {
        'base_reserve_atomic': event['post_base_reserve_atomic'],
        'quote_reserve_atomic': event['post_quote_reserve_atomic'], 'quote_mint': K.WSOL_MINT,
        'base_decimals': meta.get('base_decimals'), 'supply_atomic': meta.get('supply_atomic'),
        'virtual_quote_atomic': event['virtual_quote_atomic'],
        'lp_fee_bps': event['lp_fee_bps'], 'protocol_fee_bps': event['protocol_fee_bps'],
        'creator_fee_bps': event['creator_fee_bps'], 'slot': slot,
        'map_known_at': mapping['known_at'], 'metadata_known_at': meta.get('known_at'),
        'metadata_slot': meta.get('slot'), 'metadata_source': meta.get('source'),
        'reserve_known_at': received_at, 'sol_usd': sol_usd, 'sol_age_s': now - sol_usd_at,
        'virtual_quote_known_at': received_at, 'virtual_quote_slot': slot,
        'virtual_quote_source': event['virtual_quote_source'],
    }
    if metadata_optional and mapping.get('map_source') is not None:   # intake: the binding's proof travels (Delta)
        state['map_source'] = mapping['map_source']
    if not K._pool_state(state, complete=metadata is not None):
        raise ValueError('unknown_pool_state')
    if metadata is None:                       # intake: only the metadata keys may be unknown, never a reserve or fee
        if any(state[k] is None for k in state if k not in ('base_decimals', 'supply_atomic', 'metadata_known_at',
                                                             'metadata_slot', 'metadata_source')):
            raise ValueError('unknown_pool_state')
        mcap = None
    else:
        price_sol = ((int(state['quote_reserve_atomic']) + int(state['virtual_quote_atomic'])) / 1e9
                     / (int(state['base_reserve_atomic']) / 10**state['base_decimals']))
        mcap = price_sol * (int(state['supply_atomic']) / 10**state['base_decimals']) * sol_usd
    liq = int(state['quote_reserve_atomic']) / 1e9 * sol_usd
    if not all(math.isfinite(v) and v > 0 for v in ((liq,) if mcap is None else (mcap, liq))):
        raise ValueError('unknown_price')
    return pool, mint, state, mcap, liq, at


def candidate_from_state(*, pool, mint, state, mcap, liq, at, slot, signature, ordinal, ticket_id, extra=None):
    """The PumpSwap candidate envelope for a state_from_print result (extra: e.g. the desk intake claim)."""
    now = at.timestamp()
    source = 'pumpswap_grpc:pool_reserves'
    partial = state.get('supply_atomic') is None              # intake without metadata (F3): supply comes later
    fields = {
        'pool_state': U.Field(state, source, now, slot, 'partial' if partial else 'complete'),
        'mcap_usd': (U.Field(None, 'pumpswap_grpc:supply_unknown', now, None, 'absent') if mcap is None
                     else U.Field(mcap, source, now, slot, 'complete')),
        'liq_usd': U.Field(liq, source, now, slot, 'complete'),
        **{name: U.Field(None, 'pumpswap_grpc:unavailable', now, None, 'absent')
           for name in ('curve_pct', 'age_s', 'creator_prior_launches', 'flow_60s',
                        'price_vs_60s_low_pct', 'price_vs_60s_high_pct')},
    }
    return {
        'schema': K.PUMPSWAP_SCHEMA, 'venue': 'pumpswap', 'producer': 'U', 'network_id': 'solana',
        'ticket_id': ticket_id, 'mint': mint, 'pool': pool, 'band_entry_slot': slot, 'trigger_signature': signature,
        'trigger_ordinal': ordinal, 'emitted_at': at.isoformat(), 'gate_state': None,
        'snapshot': U.build_snapshot(emission_at=now, fields=fields), **(extra or {}),
    }


class PoolBook:
    def __init__(self, *, band, cooldown_s, sol_max_age_s, reserve_max_age_s=60):
        lo, hi = band
        if (not all(_stamp(v) for v in (lo, hi, cooldown_s, sol_max_age_s, reserve_max_age_s))
                or not 0 < lo <= hi or reserve_max_age_s <= 0):
            raise ValueError('pumpswap_band')
        self.lo, self.hi = lo, hi
        self.cooldown_s, self.sol_max_age_s = cooldown_s, sol_max_age_s
        self.reserve_max_age_s, self.last_reason = reserve_max_age_s, 'not_evaluated'
        self._inside, self._observed, self._emitted = {}, {}, {}

    def mark_unknown(self, _reason):
        self._inside.clear()
        self._observed.clear()

    def on_swap(self, *, event, mapping, metadata, slot, signature, ordinal, tx_index, now, received_at,
                sol_usd, sol_usd_at):
        pool = event.get('pool') if isinstance(event, dict) else None
        try:
            pool, mint, state, mcap, liq, at = state_from_print(
                event=event, mapping=mapping, metadata=metadata, slot=slot, signature=signature, ordinal=ordinal,
                tx_index=tx_index, now=now, received_at=received_at, sol_usd=sol_usd, sol_usd_at=sol_usd_at,
                reserve_max_age_s=self.reserve_max_age_s, sol_max_age_s=self.sol_max_age_s)
            now = at.timestamp()
        except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
            self.last_reason = str(exc) if isinstance(exc, ValueError) else 'unknown_facts'
            self._inside.pop(pool, None)
            self._observed.pop(pool, None)
            return None
        previous = self._observed.get(pool)
        order = (slot, tx_index, signature, ordinal, now)
        self._observed[pool] = order
        if previous is not None:
            old_slot, old_index, old_sig, old_ordinal, old_at = previous
            ordered = slot > old_slot or (slot == old_slot and (
                signature == old_sig and ordinal > old_ordinal
                or signature != old_sig and tx_index is not None and old_index is not None
                and tx_index > old_index))
            if not ordered or now < old_at:
                self._inside.pop(pool, None)
                self.last_reason = 'ordering_unknown'
                return None
        inside, was = self.lo <= mcap <= self.hi, self._inside.get(pool)
        self._inside[pool] = inside
        self.last_reason = 'outside_band' if not inside else 'seed' if was is None else 'no_crossing'
        if not inside or was is not False:
            return None
        if now - self._emitted.get(pool, -math.inf) < self.cooldown_s:
            self.last_reason = 'cooldown'
            return None
        candidate = candidate_from_state(pool=pool, mint=mint, state=state, mcap=mcap, liq=liq, at=at, slot=slot,
                                         signature=signature, ordinal=ordinal,
                                         ticket_id=S.pumpswap_ticket_id('solana', mint, pool, slot, signature, ordinal))
        K.validate_candidate(candidate)
        self._emitted[pool] = now
        self.last_reason = 'emitted'
        return candidate
