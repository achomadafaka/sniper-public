"""Native CU evidence and the pure post-CU bid callback."""
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def no_demotions():
    curve.CU_DEMOTED.clear()
    yield
    curve.CU_DEMOTED.clear()


@pytest.mark.parametrize('module_name', ['test_pump_curve_buy', 'test_pumpswap_buy'])
@pytest.mark.parametrize('price', [0, 1234567])
def test_native_builder_calls_bid_after_cu_and_signs_exact_price(monkeypatch, module_name, price):
    import importlib
    t = importlib.import_module(module_name)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    calls = []
    bid = {'micro_lamports_per_cu': price, 'source': 'cache', 'sample_age_s': 0.5}
    def select(**kwargs):
        calls.append(kwargs)
        return bid
    builder = t._builder(t._Clock())
    builder.priority_fee_fn = select
    message, validity = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000)
    assert message is not None, validity
    pool = (curve.curve_route_target(t._buy().mint) if module_name == 'test_pump_curve_buy'
            else t._buy().pool)
    assert calls == [{'pool': pool, 'cu_limit': validity['cu_limit'], 'tip_lamports': 0}]
    assert int.from_bytes(bytes(message.instructions[1].data)[1:], 'little') == price
    assert validity['priority_bid'] == bid
    assert builder.last_build['priority_bid'] == bid


@pytest.mark.parametrize('bad', [None, {}, {'micro_lamports_per_cu': -1},
                                  {'micro_lamports_per_cu': True},
                                  {'micro_lamports_per_cu': 1.5}])
def test_malformed_bid_refuses_before_signing(monkeypatch, bad):
    import test_pump_curve_buy as t
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    builder = t._builder(t._Clock(), priority_fee_fn=lambda **kwargs: bad)
    message, reason = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000)
    assert message is None and reason == 'priority_bid_invalid'


def test_callback_sees_tip_and_explicit_cu(monkeypatch):
    import test_pump_curve_buy as t
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    calls = []
    builder = t._builder(t._Clock(), compute_unit_limit=333000,
                         priority_fee_fn=lambda **kw: calls.append(kw) or {'micro_lamports_per_cu': 321})
    message, validity = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000,
                                     tip=(t.LIVE['accounts'][0][0], 12345))
    assert message is not None, validity
    assert calls[0]['cu_limit'] == 333000 and calls[0]['tip_lamports'] == 12345


def test_pumpswap_constructor_accepts_callback():
    from sniper.execution.pumpswap_buy import SlotLanePumpSwapBuilder
    select = lambda **kw: {'micro_lamports_per_cu': 1}
    builder = SlotLanePumpSwapBuilder(user='11111111111111111111111111111111', rpc=lambda *a: None, priority_fee_fn=select)
    assert builder.priority_fee_fn is select


@pytest.mark.parametrize('module_name', ['test_pump_curve_buy', 'test_pumpswap_buy'])
def test_leg_builds_select_after_the_measured_cu_lookup(monkeypatch, module_name):
    import importlib
    t = importlib.import_module(module_name)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    builder = t._builder(t._Clock())
    builder._leg['quote'] = {'routePlan': [{'swapInfo': {'label': 'BisonFi'}}]}
    calls = []
    builder.priority_fee_fn = lambda **kw: calls.append(kw) or {'micro_lamports_per_cu': 333}
    message, validity = builder.build(t._buy(), 5., t.SNAP)
    assert message is not None, validity
    assert calls[0]['cu_limit'] == validity['cu_limit'] < cfg.MEMECOIN_SLOT_LANE_CU_LIMIT
    assert int.from_bytes(bytes(message.instructions[1].data)[1:], 'little') == 333


@pytest.mark.parametrize('venue', ['pump_curve', 'pump_amm'])
def test_exotic_bid_uses_raised_final_cu_and_coin_facing_pool(monkeypatch, venue):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 30.)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', frozenset({venue}))
    if venue == 'pump_curve':
        import test_slot_lane_quote_curve as t
        builder, buy = t._builder(), t._slot_buy(t.EXACT_IN)
        assert builder.ondemand_quote(buy)[2] is None
        leg = t._leg()
        quote = (8, 100. / (118090 / 1e8), 0, 0)
        clip, pool = 100., curve.curve_route_target(buy.mint)
    else:
        import test_slot_lane_pump_nonsol as t
        builder, buy = t._builder(t.SPCX), t._slot_buy(t.SPCX)
        leg = t._leg(t.SPCX['quote_mint'], 40000)
        quote, clip, pool = (6, 250., 0, 0), 10., t.SPCX['pool']
    leg['quote'] = {'routePlan': [{'swapInfo': {'label': 'Meteora DLMM'}}] * 3}
    calls = []
    builder.priority_fee_fn = lambda **kw: calls.append(kw) or {'micro_lamports_per_cu': 222}
    builder.rpc = lambda *a, **kw: pytest.fail('no build RPC')
    message, validity = builder.build(buy, clip, t.SNAP, leg=leg, quote=quote)
    assert message is not None, validity
    assert validity['cu_limit'] > cfg.MEMECOIN_SLOT_LANE_CU_LIMIT
    assert calls == [{'pool': pool, 'cu_limit': validity['cu_limit'], 'tip_lamports': 0}]
    assert int.from_bytes(bytes(message.instructions[1].data)[1:], 'little') == 222


def test_native_pumpamm_26_token2022_class_uses_receipt_bound_and_demotes(monkeypatch):
    import test_pumpswap_buy as t
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    builder = t._builder(t._Clock())
    facts = builder.pools._pools[t._buy().pool]
    facts['base_token_program'] = curve.TOKEN_2022_PROGRAM
    cls = ('pump_amm', 'native', '26_accounts')
    message, validity = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000)
    assert validity['cu_class'] == cls
    assert validity['cu_limit'] == int.from_bytes(bytes(message.instructions[0].data)[1:5], 'little') == 152000
    assert validity['cu_key'] == (*cls, curve.TOKEN_2022_PROGRAM)
    curve.demote_cu_class(cls)
    message, validity = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000)
    assert validity['cu_class'] is None and validity['cu_limit'] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT


@pytest.mark.parametrize('program,creator', [(curve.TOKEN_PROGRAM, 'known'),
                                           (curve.TOKEN_2022_PROGRAM, curve.SYSTEM_PROGRAM),
                                           (curve.TOKEN_2022_PROGRAM, None)])
def test_unsampled_native_token_or_layout_keeps_default(monkeypatch, program, creator):
    import test_pumpswap_buy as t
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    builder = t._builder(t._Clock())
    facts = builder.pools._pools[t._buy().pool]
    facts['base_token_program'] = program
    if creator != 'known':
        facts['coin_creator'] = creator
    # Missing facts may refuse; they never pick a measured class.
    if creator is None:
        limit, cls, _key = curve.build_cu_limit(builder, 'pump_amm', 'native',
                                               token_program=program, native_variant=None)
        assert limit == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT and cls is None
    else:
        message, validity = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000)
        assert message is not None, validity
        assert validity['cu_class'] is None and validity['cu_limit'] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT


def test_pump_curve_native_sparse_sample_and_explicit_override_stay_unchanged(monkeypatch):
    import test_pump_curve_buy as t
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    for explicit in (None, 333000):
        builder = t._builder(t._Clock(), compute_unit_limit=explicit)
        message, validity = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000)
        assert validity['cu_class'] is None
        assert validity['cu_limit'] == (explicit or cfg.MEMECOIN_SLOT_LANE_CU_LIMIT)


def test_native_class_is_bound_to_successful_receipts_and_proved_ata_upper_bound():
    import json
    from pathlib import Path
    evidence = json.loads((Path(__file__).parent / 'fixtures' / 'memecoin' /
                           'slot_lane_native_cu_evidence.json').read_text())
    rows = [r for r in evidence['successful_receipts']
            if r['atas'][1]['program'] == curve.TOKEN_2022_PROGRAM]
    assert all(r['err'] is None and r['amm'] == [{'accounts': 26, 'data': 'c62e1552b4d9e870'}]
               for r in rows)
    cls = ('pump_amm', 'native', '26_accounts')
    assert cfg.SLOT_LANE_CU_CLASSES[cls] == (len(rows), max(r['cu'] for r in rows),
                                          frozenset({curve.TOKEN_2022_PROGRAM}))
    assert len(rows) >= cfg.SLOT_LANE_CU_MIN_SENDS
    assert {r['atas'][1]['create'] for r in rows} == {True, False}
    sim = evidence['ata_simulation']
    assert sim['err'] is None and sim['signed'] is False and sim['broadcast'] is False
    import re
    measured = [int(v) for v in re.findall(
        r'Program ' + curve.ATA_PROGRAM + r' consumed (\d+) of', '\n'.join(sim['logs']))]
    assert measured == [sim['wsOL_create_cu'], sim['wsOL_reuse_cu'],
                        sim['token2022_create_cu'], sim['token2022_reuse_cu']]
    assert sim['wsOL_reuse_cu'] < sim['wsOL_create_cu']
    assert sim['token2022_reuse_cu'] < sim['token2022_create_cu']
    assert cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS[cls] == cfg.slot_lane_cu_class_limit(max(r['cu'] for r in rows))


def test_explicit_pumpamm_cu_override_beats_the_native_class(monkeypatch):
    import test_pumpswap_buy as t
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 10.)
    builder = t._builder(t._Clock())
    builder._cu_explicit, builder.compute_unit_limit = True, 333000
    builder.pools._pools[t._buy().pool]['base_token_program'] = curve.TOKEN_2022_PROGRAM
    message, validity = builder.build(t._buy(), 5., t.SNAP, funding_lamports=28000000)
    assert validity['cu_class'] is None and validity['cu_limit'] == 333000
