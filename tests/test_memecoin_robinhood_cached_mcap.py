"""Robinhood source-price bands from verified, fresh local supply; never inline RPC."""
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.services import memecoin_volume_writer as w
from sniper.services import memecoin_pons_metadata as pm
from sniper.services.memecoin_pons_metadata import read_cached_supply
from sniper.settings import memecoin as cfg

MINT = '0xa5127fae2d0986a4cb6619b9c4ec53461726454b'
PRICE = 1.595246538195589e-05


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_ALLOWLIST', {'4663'})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_OWN_MCAP_CHAINS', '56')
    monkeypatch.setattr(cfg, 'MEMECOIN_DEX_BLIND_CHAINS', {'4663'})
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MCAP_ASSUME_MICRO', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_MCAP_USD', 250000.)
    monkeypatch.setattr(cfg, 'MEMECOIN_AUTO_TEMPLATES', 'off')
    monkeypatch.setattr(pm, '_LIVE_SUPPLY', OrderedDict())
    book = sqlite3.connect(':memory:'); book.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(book); m.add_wallet(book,'u','seralberttrades')
    m.set_wallet_real_approved(book,'u',True,chains=['4663'],force=True)
    path = tmp_path / 'volume.db'; cache = w.connect(path)
    cache.execute('INSERT INTO volume_token_supply VALUES (?,?,?,?,?)', ('4663',MINT,18,str(10**27),2000))
    cache.commit()
    seen, fallback, marks = {}, [], []
    def lookup(chain, mint):
        return read_cached_supply(chain, mint, path=path, now=2001)
    monkeypatch.setattr(rr, 'read_cached_supply', lookup, raising=False)
    def spy(conn, ev, contexts, **kw):
        seen.update(kw); return {'status':'filled'}
    monkeypatch.setattr(rr,'real_entry_hook',spy)
    monkeypatch.setattr(rr,'_alert_entry_result',lambda *a,**k:None)
    monkeypatch.setattr(rr,'_mark_evm_own_supply',lambda trace,name,**fields: marks.append((name,fields)))
    def blind(chain, mint):
        fallback.append((chain,mint)); return None
    hook = rr.make_real_hook(None,{'4663':object()},now_fn=lambda:datetime.now(timezone.utc).isoformat(),
                             trade_size=10., max_age_s=None, stats_fn=lambda *a,**k:{'market_cap':14219.,'price':PRICE,'liquidity':3.23},
                             blind_mcap_fn=blind)
    ev = SimpleNamespace(side='buy',source_user_id='u',mint=MINT,network_id='4663',their_market_cap=0.,their_price=PRICE)
    yield book,cache,hook,ev,seen,fallback,marks
    cache.close(); book.close()


def test_fresh_supply_sets_own_mcap_without_any_provider_lookup(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    from sniper.services import memecoin_evm_price as ep
    monkeypatch.setattr(ep,'evm_total_supply',lambda *a:pytest.fail('inline supply RPC'))
    assert hook(book,ev)['status'] == 'filled'
    ctx = seen['classification_context']
    assert ctx['effective_mcap'] == pytest.approx(15952.46538195589)
    assert ctx['mcap_basis'] == 'evm_source_cached'
    assert not ctx['assumed_micro']
    assert fallback == []
    assert marks[-1][0] == 'evm_cached_supply' and marks[-1][1]['outcome'] == 'ok'


@pytest.mark.parametrize('kind',['missing','stale','malformed','wrong_chain'])
def test_cache_miss_preserves_existing_blind_fallback_and_unknown_band(setup, kind):
    book,cache,hook,ev,seen,fallback,marks = setup
    if kind == 'missing': cache.execute('DELETE FROM volume_token_supply')
    elif kind == 'stale': cache.execute('UPDATE volume_token_supply SET observed=1000')
    elif kind == 'malformed': cache.execute("UPDATE volume_token_supply SET total_supply='0'")
    else: cache.execute("UPDATE volume_token_supply SET chain_id='56'")
    cache.commit()
    assert hook(book,ev)['status'] == 'filled'
    ctx = seen['classification_context']
    assert ctx['mcap_basis'] == 'assumed' and ctx['effective_mcap'] is None
    assert fallback == [('4663',MINT)]
    assert marks[-1][1]['outcome'] in {'missing','stale','malformed'}


def test_known_source_cap_keeps_its_band_when_cached_cap_is_under_ceiling(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    from sniper.services import memecoin_evm_price as ep
    monkeypatch.setattr(ep,'evm_total_supply',lambda *a:pytest.fail('inline supply RPC'))
    ev.their_market_cap = 40000.
    assert hook(book,ev)['status'] == 'filled'
    assert seen['classification_context']['mcap_basis'] == 'event'
    assert seen['classification_context']['effective_mcap'] == 40000.
    assert marks[-1][1]['mcap'] == pytest.approx(15952.46538195589)
    assert fallback == []


def test_op_feed_cap_cannot_bypass_verified_supply_ceiling(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    from sniper.services import memecoin_evm_price as ep
    monkeypatch.setattr(ep,'evm_total_supply',lambda *a:pytest.fail('inline supply RPC'))
    ev.their_market_cap, ev.their_price = 168705., 0.000554322
    out = hook(book,ev)
    assert out['status'] == 'skipped_mcap'
    assert out['mcap'] == pytest.approx(554322.)
    assert out['reason'] == 'mcap $554,322 over ceiling $250,000 (over_ceiling)'
    assert not seen and fallback == []


@pytest.mark.parametrize('kind',['missing','stale','malformed','wrong_chain','unavailable'])
def test_known_cap_preserves_admission_when_cached_supply_is_unknown(setup, monkeypatch, kind):
    book,cache,hook,ev,seen,fallback,marks = setup
    ev.their_market_cap = 40000.
    if kind == 'missing': cache.execute('DELETE FROM volume_token_supply')
    elif kind == 'stale': cache.execute('UPDATE volume_token_supply SET observed=1000')
    elif kind == 'malformed': cache.execute("UPDATE volume_token_supply SET total_supply='0'")
    elif kind == 'wrong_chain': cache.execute("UPDATE volume_token_supply SET chain_id='56'")
    else:
        monkeypatch.setattr(rr,'read_cached_supply',lambda *a:dict(supply=None,outcome='unavailable',observed=None))
    cache.commit()
    assert hook(book,ev)['status'] == 'filled'
    assert seen['classification_context']['effective_mcap'] == 40000.
    assert seen['classification_context']['mcap_basis'] == 'event'
    assert fallback == []


def test_cached_crosscheck_respects_disabled_ceiling(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_MCAP_USD',0.)
    ev.their_market_cap, ev.their_price = 168705., 0.000554322
    assert hook(book,ev)['status'] == 'filled'
    assert seen['classification_context']['effective_mcap'] == 168705.


def test_history_cap_cannot_bypass_verified_supply_ceiling(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    monkeypatch.setattr(m,'resolve_mint_mcap',lambda *a,**kw:168705.)
    ev.their_price = 0.000554322
    assert hook(book,ev)['status'] == 'skipped_mcap'
    assert not seen and fallback == []


def test_cached_crosscheck_preserves_wallet_ceiling(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET',{'4663:u':600000.})
    ev.their_market_cap, ev.their_price = 168705., 0.000554322
    assert hook(book,ev)['status'] == 'filled'
    assert seen['classification_context']['effective_mcap'] == 168705.


def test_cached_crosscheck_preserves_explicit_scalp_admission(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    monkeypatch.setattr(cfg,'MEMECOIN_AUTO_TEMPLATES','on')
    monkeypatch.setattr(cfg,'MEMECOIN_SCALP_MCAP_USD',300000.)
    ev.their_market_cap, ev.their_price = 168705., 0.000554322
    assert hook(book,ev)['status'] == 'filled'


@pytest.mark.parametrize('lifted',[None,500000.])
def test_cached_crosscheck_uses_verified_cap_for_existing_bigcap_lift(setup, monkeypatch, lifted):
    book,cache,hook,ev,seen,fallback,marks = setup
    ev.their_market_cap, ev.their_price = 168705., 0.0004
    calls = []
    def lift(ev,chain,mcap,ceiling,**kw):
        calls.append((chain,mcap,ceiling)); return lifted
    monkeypatch.setattr(rr,'_bigcap_lift',lift)
    out = hook(book,ev)
    assert calls == [('4663',400000.,250000.)]
    assert out['status'] == ('filled' if lifted else 'skipped_mcap')


def test_under_floor_feed_is_not_promoted_by_the_cached_cap(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MIN_MCAP_USD',50000.)
    monkeypatch.setattr(rr,'read_cached_supply',lambda *a:pytest.fail('unneeded cache read'))
    ev.their_market_cap, ev.their_price = 40000., 0.0001
    assert hook(book,ev)['status'] == 'skipped_mcap'
    assert not seen


def test_big_cached_cap_still_enforces_existing_ceiling(setup):
    book,cache,hook,ev,seen,fallback,marks = setup
    ev.their_price = 0.004
    out = hook(book,ev)
    assert out['status'] == 'skipped_mcap' and out['reason'].endswith('(over_ceiling)')
    assert not seen and fallback == []


@pytest.mark.parametrize('price',[None,0,-1,float('nan'),float('inf'),'bad'])
def test_missing_or_invalid_price_never_creates_a_cached_cap(setup, price):
    book,cache,hook,ev,seen,fallback,marks = setup
    ev.their_price = price
    assert hook(book,ev)['status'] == 'filled'
    assert seen['classification_context']['effective_mcap'] is None
    assert fallback == [('4663',MINT)]
    assert marks[-1][1]['outcome'] == 'no_source_price'


# 4663 fast-lane misses C (2026-10-14): 73809 0xdetweiler. The rh fast lane refused at $831,550 from
# verified supply; volume_token_supply holds Pons curves only, so the guard read "missing" and the
# on-chain path admitted the same buy at a feed-history $7,980 (readings imply 1B of a ~100B coin).
DETWEILER_PRICE = 7.97957543258506e-06


def test_lane_verified_supply_refuses_history_cap_when_volume_cache_misses(setup, monkeypatch):
    book,cache,hook,ev,seen,fallback,marks = setup
    cache.execute('DELETE FROM volume_token_supply'); cache.commit()
    monkeypatch.setattr(m,'resolve_mint_mcap',lambda *a,**kw:7979.575432585059)
    assert pm.remember_live_supply('4663', MINT.upper().replace('0X', '0x'), 18, 100 * 10**27)
    ev.their_price = DETWEILER_PRICE
    out = hook(book,ev)
    assert out['status'] == 'skipped_mcap'
    assert out['mcap'] == pytest.approx(797957.543258506)
    assert not seen and fallback == []
    assert ('evm_cached_supply', 'lane_verified') in [(n, f['outcome']) for n, f in marks]


def test_old_lane_supply_still_refuses_over_ceiling_but_never_sets_the_band(setup, monkeypatch):
    # 73517 shape: the lane's read was 79 min old. The guard refuses on it; the band source may not.
    book,cache,hook,ev,seen,fallback,marks = setup
    cache.execute('DELETE FROM volume_token_supply'); cache.commit()
    from sniper.util.clock import real_utc_now
    pm.remember_live_supply('4663', MINT, 18, 100 * 10**27, now=real_utc_now() - 79 * 60)
    monkeypatch.setattr(m,'resolve_mint_mcap',lambda *a,**kw:7979.575432585059)
    ev.their_price = DETWEILER_PRICE
    assert hook(book,ev)['status'] == 'skipped_mcap'
    monkeypatch.setattr(m,'resolve_mint_mcap',lambda *a,**kw:None)
    ev.their_price = 1e-7                                    # $10k at 100B: under the ceiling
    seen.clear(); fallback.clear()
    assert hook(book,ev)['status'] == 'filled'
    assert seen['classification_context']['mcap_basis'] == 'assumed'   # not the old lane read
    assert fallback == [('4663', MINT)]


@pytest.mark.parametrize('kind', ['stale', 'other_chain', 'never_published'])
def test_unusable_lane_supply_keeps_todays_admission(setup, monkeypatch, kind):
    book,cache,hook,ev,seen,fallback,marks = setup
    cache.execute('DELETE FROM volume_token_supply'); cache.commit()
    monkeypatch.setattr(m,'resolve_mint_mcap',lambda *a,**kw:7979.575432585059)
    from sniper.util.clock import real_utc_now
    if kind == 'stale':
        pm.remember_live_supply('4663', MINT, 18, 100 * 10**27, now=real_utc_now() - 86401)
    elif kind == 'other_chain':
        pm.remember_live_supply('56', MINT, 18, 100 * 10**27)
    ev.their_price = DETWEILER_PRICE
    assert hook(book,ev)['status'] == 'filled'
    assert seen['classification_context']['mcap_basis'] == 'history'


@pytest.mark.parametrize('decimals,atomic', [(18, 0), (-1, 10**27), (37, 10**27), (18, '1000'),
                                             (True, 10**27), (18, 1 << 256)])
def test_live_supply_refuses_invalid_reads(decimals, atomic, monkeypatch):
    monkeypatch.setattr(pm, '_LIVE_SUPPLY', OrderedDict())
    assert pm.remember_live_supply('4663', MINT, decimals, atomic) is False
    assert pm.read_live_supply('4663', MINT)['outcome'] == 'missing'


def test_supply_refresh_keeps_verified_value_visible_until_atomic_replacement(monkeypatch):
    writing, release = threading.Event(), threading.Event()

    class PausedReplacement(OrderedDict):
        def __setitem__(self, key, value):
            if value[1] == 2_000_000:
                writing.set()
                assert release.wait(1)
            super().__setitem__(key, value)

    monkeypatch.setattr(pm, '_LIVE_SUPPLY', PausedReplacement())
    assert pm.remember_live_supply('4663', MINT, 0, 1_000_000, now=100)
    thread = threading.Thread(target=lambda: pm.remember_live_supply(
        '4663', MINT, 0, 2_000_000, now=101))
    thread.start()
    try:
        assert writing.wait(1)
        assert pm.read_live_supply('4663', MINT, now=101)['supply'] == 1_000_000
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    assert pm.read_live_supply('4663', MINT, now=101)['supply'] == 2_000_000


def test_supply_refresh_preserves_recently_updated_value_at_eviction(monkeypatch):
    monkeypatch.setattr(pm, '_LIVE_SUPPLY', OrderedDict())
    monkeypatch.setattr(pm, 'LIVE_SUPPLY_CAP', 2)
    b, c = '0x' + 'bb' * 20, '0x' + 'cc' * 20
    for token in (MINT, b, MINT, c):
        assert pm.remember_live_supply('4663', token, 0, 1, now=100)
    assert pm.read_live_supply('4663', MINT, now=100)['supply'] == 1
    assert pm.read_live_supply('4663', c, now=100)['supply'] == 1
    assert pm.read_live_supply('4663', b, now=100)['outcome'] == 'missing'
