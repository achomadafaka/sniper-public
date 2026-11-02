"""Unsigned DFlow references become usable only after the existing chain reads."""
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import queue

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.execution import pumpswap_buy as amm
from sniper.services import memecoin_shyft_prepare as prep


FX = json.loads((Path(__file__).parent / 'fixtures/memecoin/dflow_preparation_quackd_20261013.json').read_text())


def packet():
    source = FX['source']
    update = pb.SubscribeUpdate()
    update.transaction.slot = source['slot']
    info = update.transaction.transaction
    tx = source['transaction']
    info.signature = base58.b58decode(tx['signatures'][0])
    info.transaction.signatures.extend(base58.b58decode(s) for s in tx['signatures'])
    msg = info.transaction.message
    raw = tx['message']
    for src, dst in [('numRequiredSignatures', 'num_required_signatures'),
                     ('numReadonlySignedAccounts', 'num_readonly_signed_accounts'),
                     ('numReadonlyUnsignedAccounts', 'num_readonly_unsigned_accounts')]:
        setattr(msg.header, dst, raw['header'][src])
    msg.account_keys.extend(base58.b58decode(k) for k in raw['accountKeys'])
    msg.recent_blockhash = base58.b58decode(raw['recentBlockhash'])
    msg.versioned = True
    for ix in raw['instructions']:
        msg.instructions.add(program_id_index=ix['programIdIndex'], accounts=bytes(ix['accounts']),
                             data=base58.b58decode(ix['data']))
    return update


def candidate(update=None, now=0):
    return prep.preparation_candidate(update or packet(), {FX['owner']: ('uid', 'ser_albert')},
                                      recv_monotonic=now, generation=1)


def test_quackd_dflow_without_metadata_prepares_references_without_authority(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unsigned preparation reached execution')
    for symbol in ['sniper.services.memecoin_risk.reserve_entry',
                   'sniper.execution.solana_real_seams.sign_swap_tx',
                   'sniper.execution.submission_wiring.make_solana_send_fn']:
        monkeypatch.setattr(symbol, forbidden)
    got = candidate()
    assert got is not None
    target = prep.preparation_target(got)
    assert target['venue'] == 'pump_amm'
    assert FX['pool'] in target['candidate_accounts']
    assert len(target['candidate_accounts']) == len(set(target['candidate_accounts'])) <= 64
    assert not {'pool', 'mint', 'source_price', 'clip', 'min_out', 'source_initiation'} & target.keys()
    assert got['recv_monotonic'] == 0


def test_action_widths_and_header_match_authoritative_onchain_idl():
    idl = FX['idl']
    assert bytes(idl['swap']['discriminator']) == bytes.fromhex('f8c69e91e17587c8')
    expected = {12: 'PumpFunAmmBuy', 31: 'DFlowDynamicRouteV1', 37: 'RecordId',
                38: 'RecordId2', 49: 'SetMinimumLegPrices', 50: 'SetSponsor',
                54: 'DeriverseSwap', 60: 'InitAtaIdempotent',
                78: 'SetSponsorIntermediateCloseAuthority', 82: 'PumpFunAmmBuyV2'}
    assert all(idl['action_variants'][index] == name for index, name in expected.items())
    assert idl['dynamic_variants'][2] == 'TesseraV' and idl['dynamic_variants'][8] == 'BisonFi'
    assert idl['types']['PumpFunAmmBuyV2Options']['fields'][0]['type'] == 'u64'
    assert idl['types']['OrchestratorFlags']['fields'] == [{'name': 'flags', 'type': 'u8'}]
    assert idl['types']['RecordIdOptions']['fields'][0]['type'] == {'array': ['u8', 76]}


@pytest.mark.parametrize('bad', ['unknown_action', 'truncated', 'trailing', 'huge_action_count',
    'second_amm_buy', 'no_amm_buy', 'wrong_actor', 'wrong_program', 'wrong_event_authority',
    'wrong_token_program', 'wrong_ata_program', 'wrong_system_program', 'nonsigner', 'failed'])
def test_unparseable_or_unbound_dflow_never_prepares(bad):
    update = packet()
    info = update.transaction.transaction
    msg = info.transaction.message
    ix = msg.instructions[0]
    data = bytearray(ix.data)
    accounts = bytearray(ix.accounts)
    if bad == 'unknown_action': data[12] = 255
    if bad == 'truncated': data = data[:-1]
    if bad == 'trailing': data += b'\x00'
    if bad == 'huge_action_count': data[8:12] = (10000).to_bytes(4, 'little')
    if bad == 'second_amm_buy':
        data[8:12] = (10).to_bytes(4, 'little')
        data[-12:-12] = bytes([82]) + bytes(9)
    if bad == 'no_amm_buy': data[-22] = 54
    if bad == 'wrong_actor': accounts[3] = 0
    if bad == 'wrong_program': accounts[5] = 0
    if bad == 'wrong_event_authority': accounts[4] = 0
    if bad == 'wrong_token_program': accounts[0] = 0
    if bad == 'wrong_ata_program': accounts[1] = 0
    if bad == 'wrong_system_program': accounts[2] = 0
    if bad == 'nonsigner':
        msg.account_keys[1], msg.account_keys[3] = msg.account_keys[3], msg.account_keys[1]
    if bad == 'failed': info.meta.err.err = b'failed'
    ix.data, ix.accounts = bytes(data), bytes(accounts)
    assert candidate(update) is None


def test_dflow_reference_batch_is_offered_without_reading_or_inferred_offsets():
    seen = []
    pools = SimpleNamespace(offer_candidates=lambda refs, **kw: seen.append((refs, kw)))
    pending = queue.Queue(maxsize=128)
    pending.put(candidate(now=1))
    stats = {}
    prep.drain_preparations(pending, {'pump_amm': SimpleNamespace(pools=pools)}, stats, now=1.01)
    assert len(seen) == 1 and seen[0][1]['received_at'] == 1
    assert FX['pool'] in seen[0][0]
    assert stats['delivered'] == 1 and stats['errors'] == 0


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def monotonic(self): return self.now


def cache_fixture(clock, *, pool_file=None, corrupt=None):
    accounts = copy.deepcopy(FX['static_read']['accounts'])
    if corrupt == 'pool_owner': accounts[FX['pool']]['owner'] = amm.SYSTEM_PROGRAM
    if corrupt == 'pool_layout': accounts[FX['pool']]['data'][0] = base64.b64encode(b'bad').decode()
    if corrupt == 'mint_owner': accounts[FX['mint']]['owner'] = amm.SYSTEM_PROGRAM
    if corrupt == 'missing_pool': accounts[FX['pool']] = None
    if corrupt == 'wrong_base_mint':
        raw = bytearray(base64.b64decode(accounts[FX['pool']]['data'][0]))
        raw[43:75] = bytes([211]) * 32
        accounts[FX['pool']]['data'][0] = base64.b64encode(raw).decode()
    calls = []
    def rpc(method, params):
        assert method == 'getMultipleAccounts'
        calls.append((clock.now, tuple(params[0])))
        assert len(params[0]) <= 100 and params[1]['commitment'] == 'confirmed'
        return {'value': [accounts.get(key, {'owner': amm.SYSTEM_PROGRAM, 'data': ['', 'base64']})
                          for key in params[0]]}
    cache = amm.PumpSwapPoolCache(rpc=rpc, clock=clock, store_path=pool_file)
    return cache, calls


def test_reference_batch_publishes_only_after_pool_and_mint_chain_validation():
    clock = Clock()
    cache, calls = cache_fixture(clock)
    refs = prep.preparation_target(candidate())['candidate_accounts']
    assert cache.offer_candidates(refs, received_at=0)
    assert cache.cached(FX['pool']) is None and not calls
    cache.learn_pending()
    facts = cache.cached(FX['pool'])
    assert facts['base_mint'] == FX['mint']
    assert facts['base_token_program'] == amm.TOKEN_2022_PROGRAM
    assert facts['quote_token_program'] == amm.TOKEN_PROGRAM
    assert facts['base_decimals'] == 6
    assert len(calls) == 2 and cache.stats['candidate_stored'] == 1
    before = len(calls)
    for _ in range(100): assert cache.cached(FX['pool']) is facts
    assert len(calls) == before  # the ready lane reads local facts only


@pytest.mark.parametrize('corrupt', ['pool_owner', 'pool_layout', 'mint_owner', 'missing_pool', 'wrong_base_mint'])
def test_reference_batch_cannot_publish_wrong_or_unknown_chain_facts(corrupt):
    clock = Clock()
    cache, _ = cache_fixture(clock, corrupt=corrupt)
    assert cache.offer_candidates(prep.preparation_target(candidate())['candidate_accounts'], received_at=0)
    cache.learn_pending()
    assert cache.cached(FX['pool']) is None


def test_candidate_read_spacing_shares_persisted_warm_budget(tmp_path):
    clock = Clock()
    path = tmp_path / 'pools.json'
    path.write_text(json.dumps([FX['pool']]))
    cache, calls = cache_fixture(clock, pool_file=str(path))
    cache.learn_pending()
    assert len(calls) == 2
    clock.now = .1
    assert cache.offer_candidates(prep.preparation_target(candidate())['candidate_accounts'], received_at=.1)
    cache.learn_pending()
    assert len(calls) == 2
    clock.now = .99
    cache.learn_pending()
    assert len(calls) == 2
    clock.now = 1
    cache.learn_pending()
    assert len(calls) == 3


def test_failed_second_read_still_spends_both_attempted_calls_in_warm_budget():
    clock = Clock()
    cache, calls = cache_fixture(clock)
    original = cache.rpc
    def rpc(method, params):
        result = original(method, params)
        if len(calls) == 2:
            raise TimeoutError('mint read unavailable')
        return result
    cache.rpc = rpc
    assert cache.offer_candidates(prep.preparation_target(candidate())['candidate_accounts'], received_at=0)
    cache.learn_pending()
    assert len(calls) == 2 and cache.cached(FX['pool']) is None
    clock.now = .6
    cache.learn_pending()
    assert len(calls) == 2
    clock.now = 1
    cache.learn_pending()
    assert len(calls) == 4 and cache.cached(FX['pool']) is not None


def test_candidate_duplicates_do_not_renew_ttl_or_increase_bounded_queue():
    clock = Clock()
    cache, calls = cache_fixture(clock)
    refs = prep.preparation_target(candidate())['candidate_accounts']
    assert cache.offer_candidates(refs, received_at=0)
    clock.now = 1.9
    assert not cache.offer_candidates(refs, received_at=1.9)
    clock.now = 2
    cache.learn_pending()
    assert calls == [] and cache.stats['candidate_expired'] == 1
    assert cache.stats['candidate_duplicate'] == 1


def test_candidate_queue_and_each_reference_batch_are_bounded():
    clock = Clock()
    cache, calls = cache_fixture(clock)
    def refs(n): return tuple(base58.b58encode(bytes([k]) * 32).decode() for k in n)
    assert not cache.offer_candidates(refs(range(65)), received_at=0)
    for k in range(64): assert cache.offer_candidates(refs([k]), received_at=0)
    assert not cache.offer_candidates(refs([65]), received_at=0)
    assert cache.stats['candidate_overflow'] == 1 and calls == []


class Event:
    def __init__(self, clock, arrivals): self.clock, self.arrivals, self.waits = clock, list(arrivals), []
    def wait(self, timeout):
        self.waits.append(timeout)
        if self.arrivals and self.arrivals[0] <= self.clock.now + timeout:
            self.clock.now = self.arrivals.pop(0)
            return True
        self.clock.now += timeout
        return False
    def clear(self): pass


def test_idle_preparation_wake_does_not_spend_the_half_second_sleep():
    clock = Clock()
    event = Event(clock, [.001])
    assert prep.wait_for_preparation(event, clock=clock, not_before=0)
    assert clock.now == .001


def test_burst_wakes_do_not_restart_prefetch_or_raise_preparation_cadence():
    clock = Clock()
    event = Event(clock, [.01, .02, .03])
    assert prep.wait_for_preparation(event, clock=clock, not_before=.5)
    assert clock.now == .5
    assert len(event.waits) == 4


def test_absent_hint_retains_periodic_prefetch_tick():
    clock = Clock()
    assert not prep.wait_for_preparation(Event(clock, []), clock=clock, not_before=0)
    assert clock.now == .5
