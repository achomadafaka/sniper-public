"""Verified Pons metadata off the funded path; fresh supply reads from the local side-store."""
from __future__ import annotations

import math
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import closing
from pathlib import Path

from sniper.util.clock import real_utc_now
from sniper.util.state_paths import state_root

SUPPLY_REFRESH_S = 300.0
SUPPLY_MAX_AGE_S = 600.0
RETRY_S = 30.0
NATIVE = '0x' + '00' * 20
_ADDRESS = re.compile(r'0x[0-9a-f]{40}\Z')
_WORD = re.compile(r'0x[0-9a-fA-F]{64}\Z')


def _address(raw):
    if not isinstance(raw, str) or not _WORD.fullmatch(raw) or raw[2:26] != '0' * 24:
        return None
    return '0x' + raw[-40:].lower()


def _uint(raw):
    return int(raw, 16) if isinstance(raw, str) and _WORD.fullmatch(raw) else None


def _info_address(raw, word):
    if not isinstance(raw, str) or not raw.startswith('0x') or (len(raw) - 2) % 64:
        return None
    if word < 0 or len(raw) < 2 + (word + 1) * 64:
        return None
    return _address('0x' + raw[2 + word * 64:2 + (word + 1) * 64])


# A FAST LANE'S OWN SUPPLY READ, FOR THIS PROCESS (2026-10-14, 4663 fast-lane misses C). Only
# Pons curves land in volume_token_supply, so the funded 4663 ceiling guard read "missing" on every
# other coin and admitted 73809 at a feed mcap of $7,980 that the rh fast lane had just refused at
# $831,550 from verified supply. Fast lanes publish their decimals/totalSupply reads here (the EVM
# leg's put_token_facts_fn); one dict lookup reads it back. Writers hold the lock; readers never do.
_LIVE_SUPPLY = OrderedDict()     # (chain, token) -> (decimals, atomic int, observed epoch)
_LIVE_SUPPLY_LOCK = threading.Lock()
LIVE_SUPPLY_CAP = 4096
LIVE_SUPPLY_MAX_AGE_S = 300.0    # the fast lanes' own supply TTL (MEMECOIN_FAST_LANE_SUPPLY_TTL_S)
# The ceiling GUARD can only add a refusal, so it accepts an older read than a band source may: a
# launchpad coin's totalSupply does not move. 73517's lane read was 79 min old when the on-chain
# path admitted it at a blind $248,121 (lane $325,011). ponytail: a burn since the read overstates
# mcap and refuses a coin now under the ceiling; shorten this if a burn-heavy token shows up.
LIVE_SUPPLY_GUARD_MAX_AGE_S = 86400.0


def remember_live_supply(chain, mint, decimals, atomic, *, now=None):
    """Keep one verified (decimals, totalSupply) read. Invalid input is dropped, never stored."""
    token = str(mint).lower()
    if (not _ADDRESS.fullmatch(token) or token == NATIVE or type(decimals) is not int
            or not 0 <= decimals <= 36 or type(atomic) is not int or not 0 < atomic < 1 << 256):
        return False
    try:
        stamp = float(real_utc_now() if now is None else now)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(stamp) or stamp < 0:
        return False
    key = (str(chain), token)
    with _LIVE_SUPPLY_LOCK:
        _LIVE_SUPPLY[key] = (decimals, atomic, stamp)
        _LIVE_SUPPLY.move_to_end(key)
        while len(_LIVE_SUPPLY) > LIVE_SUPPLY_CAP:
            del _LIVE_SUPPLY[next(iter(_LIVE_SUPPLY))]
    return True


def read_live_supply(chain, mint, *, now=None, max_age_s=LIVE_SUPPLY_MAX_AGE_S):
    """read_cached_supply's shape for a fast lane's own read: no I/O, no lock. Unknown is never zero."""
    out = {'supply': None, 'outcome': 'missing', 'observed': None}
    got = _LIVE_SUPPLY.get((str(chain), str(mint).lower()))
    if got is None:
        return out
    decimals, atomic, stamp = got
    out['observed'] = stamp
    try:
        at = float(real_utc_now() if now is None else now)
    except (TypeError, ValueError):
        at = math.nan
    if not math.isfinite(at) or stamp > at or at - stamp > max_age_s:
        out['outcome'] = 'stale'
        return out
    out.update(supply=atomic / 10 ** decimals, outcome='ok')
    return out


def read_cached_supply(chain, mint, *, path=None, now=None):
    """One indexed read, no provider call or SQLite lock wait. Unknown is never zero."""
    out = {'supply': None, 'outcome': 'unavailable', 'observed': None}
    token = str(mint).lower()
    if not _ADDRESS.fullmatch(token) or token == NATIVE:
        out['outcome'] = 'malformed'
        return out
    try:
        file = Path(path or state_root() / 'data/memecoin_onchain_volume.db').resolve()
        with closing(sqlite3.connect(file.as_uri() + '?mode=ro', uri=True, timeout=0)) as conn:
            row = conn.execute('SELECT decimals,total_supply,observed FROM volume_token_supply '
                               'WHERE chain_id=? AND token=?', (str(chain), token)).fetchone()
    except (OSError, sqlite3.Error):
        return out
    if row is None:
        out['outcome'] = 'missing'
        return out
    decimals, atomic, observed = row
    try:
        at = float(real_utc_now() if now is None else now)
        stamp = float(observed)
        if (not isinstance(decimals, int) or not 0 <= decimals <= 36
                or not isinstance(atomic, str) or not re.fullmatch(r'[1-9][0-9]{0,77}', atomic)
                or int(atomic) >= 1 << 256 or not math.isfinite(stamp) or stamp < 0
                or not math.isfinite(at)):
            raise ValueError('invalid supply cache row')
        out['observed'] = stamp
        if stamp > at or at - stamp > SUPPLY_MAX_AGE_S:
            out['outcome'] = 'stale'
            return out
        supply = int(atomic) / 10 ** decimals
        if not math.isfinite(supply) or supply <= 0:
            raise ValueError('invalid whole-token supply')
    except (ValueError, TypeError, OverflowError):
        out['outcome'] = 'malformed'
        return out
    out.update(supply=supply, outcome='ok')
    return out


def resolve_pons_curves(conn, chain, rpc_call, now, limit=16):
    """Recover observed curves, verified against the configured factory's own registry.

    All network reads use the observer's private resolver socket. No transaction is held across
    a read. Failed/partial replies receive a bounded retry timestamp, never verified identity.
    """
    from sniper.services.memecoin_volume_writer import VolumeTape, iso
    from sniper.settings import memecoin as cfg
    chain = str(chain)
    factories = {str(e['address']).lower(): e
                 for e in cfg.MEMECOIN_EVM_V4_LAUNCHPADS.get(chain, ())}
    if not factories:
        return 0
    bound = max(0, min(16, int(limit)))
    # A launch can arrive before the first swap. Warm those coins immediately, and retry
    # incomplete reads even if no trade has arrived yet; the periodic tick scan fills missed launches.
    launches = [r[0] for r in conn.execute(
        "SELECT m.pool FROM pool_meta m LEFT JOIN volume_token_supply s "
        "ON s.chain_id=m.chain_id AND s.token=m.token1 WHERE m.chain_id=? "
        "AND m.resolve_state IN ('pons_launch_only','pons_unverified','pons_verified') "
        "AND m.token1 IS NOT NULL AND m.resolved_at>=? "
        "AND (m.resolve_state='pons_launch_only' OR m.resolved_at<=?) "
        "AND (m.resolve_state!='pons_verified' OR m.token0_decimals IS NULL "
        "OR m.token1_decimals IS NULL OR s.observed IS NULL) "
        "ORDER BY m.resolved_at,m.pool LIMIT ?",
        (chain,iso(now - 3900),iso(now - RETRY_S),bound))]
    swaps = [r[0] for r in conn.execute(
        "SELECT t.pool FROM pool_ticks t LEFT JOIN pool_meta m "
        "ON m.chain_id=t.chain_id AND m.pool=t.pool LEFT JOIN volume_token_supply s "
        "ON s.chain_id=m.chain_id AND s.token=m.token1 "
        "WHERE t.chain_id=? AND t.kind='pons_v2_swap' AND length(t.pool)=42 "
        "AND t.observed_at>=? AND (m.resolved_at IS NULL OR m.resolve_state='pons_launch_only' OR m.resolved_at<=?) "
        "AND (m.resolve_state IS NULL OR m.resolve_state!='pons_verified' "
        "OR m.token0_decimals IS NULL OR m.token1_decimals IS NULL "
        "OR s.observed IS NULL OR s.observed<=?) "
        "GROUP BY t.pool ORDER BY COALESCE(m.resolved_at,''),t.pool LIMIT ?",
        (chain, iso(now - 3900), iso(now - RETRY_S), now - SUPPLY_REFRESH_S,
         bound))]
    pools = list(dict.fromkeys(launches + swaps))[:bound]
    if len(pools) < bound:
        # Migrated coins trade on another registered pool; their old curve stops emitting swaps.
        # CROSS JOIN keeps the recent indexed ticks first, rather than scanning all old pools.
        active = [r[0] for r in conn.execute(
            "SELECT m.pool FROM pool_meta m LEFT JOIN volume_token_supply s "
            "ON s.chain_id=m.chain_id AND s.token=m.token1 WHERE m.chain_id=? "
            "AND m.resolve_state='pons_verified' AND m.resolved_at<=? "
            "AND (s.observed IS NULL OR s.observed<=?) AND m.token1 IN ("
            "SELECT DISTINCT p.mint FROM pool_ticks t INDEXED BY idx_pool_ticks_chain_time "
            "CROSS JOIN onchain_pool_protocols p WHERE t.chain_id=? AND t.observed_at>=? "
            "AND p.chain_id=t.chain_id AND p.pool=t.pool) "
            "ORDER BY m.resolved_at,m.pool LIMIT ?",
            (chain, iso(now - RETRY_S), now - SUPPLY_REFRESH_S, chain,
             iso(now - SUPPLY_REFRESH_S), bound))]
        pools = list(dict.fromkeys(pools + active))[:bound]
    pools = [p for p in pools if _ADDRESS.fullmatch(str(p)) and p != NATIVE]
    if not pools:
        return 0
    # Launch-only and legacy metadata never proved native currency. Invalidate that assumption
    # before reads; a failed verification must not keep an incorrect ETH volume valuation alive.
    with conn:
        for pool in pools:
            conn.execute('INSERT INTO pool_meta (chain_id,pool,resolved_at,resolve_state) '
                         'VALUES (?,?,?,?) ON CONFLICT(chain_id,pool) DO UPDATE SET '
                         'resolved_at=excluded.resolved_at, '
                         "token0=CASE WHEN pool_meta.resolve_state='pons_verified' THEN token0 ELSE NULL END, "
                         "quote_side=CASE WHEN pool_meta.resolve_state='pons_verified' THEN quote_side ELSE NULL END, "
                         "resolve_state=CASE WHEN pool_meta.resolve_state='pons_verified' "
                         "THEN resolve_state ELSE 'pons_unverified' END",
                         (chain, pool, iso(now), 'pons_unverified'))

    def calls(reqs):
        answers = rpc_call([('eth_call', [{'to': to, 'data': data}, 'latest'])
                            for to, data in reqs])
        # A truncated batch never shifts the next curve's answer into the current curve.
        return answers if isinstance(answers, (list, tuple)) and len(answers) == len(reqs) else [None] * len(reqs)

    answers = calls([(p, sel) for p in pools for sel in ('0xfc0c546a','0xc45a0155')])
    candidates = []
    for index, pool in enumerate(pools):
        mint, factory = _address(answers[index * 2]), _address(answers[index * 2 + 1])
        if mint and mint not in (NATIVE, pool) and factory in factories:
            candidates.append((pool, mint, factory, factories[factory]))
    infos = calls([(factory, '0x' + entry['info_selector'] + '0' * 24 + mint[2:])
                   for _, mint, factory, entry in candidates]) if candidates else []
    verified = []
    for (pool, mint, factory, entry), info in zip(candidates, infos):
        counter = _info_address(info, int(entry['counter_word']))
        if (_info_address(info, int(entry['coin_word'])) == mint
                and _info_address(info, 1) == pool and counter is not None
                and counter not in (mint, pool)):
            verified.append((pool, mint, counter))
    if not verified:
        return 0
    requests = set()
    counter_decimals = {NATIVE: 18}
    for _, mint, counter in verified:
        requests.update(((mint,'0x313ce567'), (mint,'0x18160ddd')))
        if counter not in counter_decimals:
            row = conn.execute('SELECT decimals FROM volume_token_decimals WHERE chain_id=? AND token=?',
                               (chain, counter)).fetchone()
            if row and isinstance(row[0], int) and 0 <= row[0] <= 36:
                counter_decimals[counter] = row[0]
            else:
                requests.add((counter,'0x313ce567'))
    requests = sorted(requests)
    quantities = dict(zip(requests, calls(requests)))
    tape = VolumeTape.__new__(VolumeTape)
    tape.conn, tape.chain = conn, chain
    with conn:
        for pool, mint, counter in verified:
            decimals = _uint(quantities.get((mint,'0x313ce567')))
            supply = _uint(quantities.get((mint,'0x18160ddd')))
            cd = counter_decimals.get(counter, _uint(quantities.get((counter,'0x313ce567'))))
            for token, dec in ((mint, decimals), (counter, cd)):
                if dec is not None and 0 <= dec <= 36 and token != NATIVE:
                    conn.execute('INSERT OR REPLACE INTO volume_token_decimals VALUES (?,?,?,?)',
                                 (chain, token, dec, now))
            if decimals is not None and 0 <= decimals <= 36 and supply is not None and supply > 0:
                conn.execute('INSERT OR REPLACE INTO volume_token_supply VALUES (?,?,?,?,?)',
                             (chain, mint, decimals, str(supply), now))
            tape.register_pons_curve(pool, mint, now, counter=counter)
    return len(verified)
