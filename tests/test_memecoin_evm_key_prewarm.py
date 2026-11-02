"""Signal-keyed V4 PoolKey warming: bounds, dedup, and the entry-path guarantee.

A stored PoolKey answers in ~10ms; a live resolve costs ~840ms and fails under load. Of 475 funded
4663 attempts that reserved capital in 48h, 64 filled and 153 died on "no quote". This warms the
key for the coin arriving NOW, ahead of the decision that needs it.
"""
import pytest

from sniper.services import memecoin_evm_key_prewarm as P


@pytest.fixture(autouse=True)
def _clean():
    P.reset_state()
    yield
    P.reset_state()


def test_it_queues_and_takes_in_fifo_order():
    assert P.enqueue("4663", "0xAAA", now=0.0) is True
    assert P.enqueue("4663", "0xBBB", now=0.0) is True
    assert P.take_next(1.0) == ("4663", "0xaaa")
    assert P.take_next(1.0) == ("4663", "0xbbb")
    assert P.take_next(1.0) is None


def test_the_mint_is_case_folded_so_one_coin_is_one_entry():
    """EVM addresses arrive in mixed case. Two spellings must not be two jobs."""
    assert P.enqueue("4663", "0xAbC", now=0.0) is True
    assert P.enqueue("4663", "0xabc", now=1.0) is False
    assert P.COUNTS["deduped"] == 1


def test_a_hot_mint_enqueued_every_poll_is_warmed_once():
    assert P.enqueue("4663", "0xAAA", now=0.0) is True
    for t in (1.0, 2.0, 60.0, 899.0):
        assert P.enqueue("4663", "0xAAA", now=t) is False
    P.take_next(900.0)                       # drain it; it is no longer pending
    assert P.enqueue("4663", "0xAAA", now=899.0) is False, "still inside the TTL"
    assert P.enqueue("4663", "0xAAA", now=901.0) is True, "the TTL must eventually allow a retry"


def test_a_mint_still_PENDING_is_not_requeued_even_past_the_ttl():
    """Two jobs for one coin is two resolves for one answer."""
    assert P.enqueue("4663", "0xAAA", now=0.0) is True
    assert P.enqueue("4663", "0xAAA", now=5000.0) is False
    assert P.take_next(5001.0) == ("4663", "0xaaa")
    assert P.take_next(5001.0) is None


def test_a_key_we_already_hold_is_never_queued():
    assert P.enqueue("4663", "0xAAA", now=0.0, has_key_fn=lambda c, m: True) is False
    assert P.take_next(1.0) is None


def test_an_unreadable_store_check_does_not_skip_the_warm():
    """A broken lookup is not evidence the key exists. Fail toward doing the work."""
    def boom(c, m):
        raise RuntimeError("store down")
    assert P.enqueue("4663", "0xAAA", now=0.0, has_key_fn=boom) is True


def test_a_signal_burst_cannot_become_an_rpc_burst():
    for i in range(200):
        P.enqueue("4663", "0x%03x" % i, now=0.0, max_pending=8)
    taken = [P.take_next(1.0, max_per_min=100) for _ in range(20)]
    assert sum(1 for t in taken if t) == 8, "queue must be bounded"
    assert P.COUNTS["dropped_full"] == 192


def test_the_overflow_drops_the_OLDEST():
    """The oldest pending mint has waited longest and its signal is the stalest."""
    P.enqueue("4663", "0xOLD", now=0.0, max_pending=2)
    P.enqueue("4663", "0xMID", now=0.0, max_pending=2)
    P.enqueue("4663", "0xNEW", now=0.0, max_pending=2)
    assert P.take_next(1.0) == ("4663", "0xmid")
    assert P.take_next(1.0) == ("4663", "0xnew")


def test_the_rate_bound_holds_and_then_releases():
    for i in range(5):
        P.enqueue("4663", "0x%02x" % i, now=0.0)
    assert P.take_next(0.0, max_per_min=2) is not None
    assert P.take_next(0.0, max_per_min=2) is not None
    assert P.take_next(0.0, max_per_min=2) is None, "third inside the same minute must be refused"
    assert P.COUNTS["dropped_rate"] == 1
    assert P.take_next(61.0, max_per_min=2) is not None, "the window must slide"


def test_a_rate_refusal_does_not_lose_the_job():
    P.enqueue("4663", "0xAAA", now=0.0)
    assert P.take_next(0.0, max_per_min=0) is None
    assert P.take_next(61.0, max_per_min=5) == ("4663", "0xaaa"), "the job was dropped, not deferred"


def test_failure_and_nothing_found_are_COUNTED_APART():
    """A provider that failed and a mint with no V4 pool are different facts. Folding them
    together makes an outage read as a chain with no pools."""
    P.record_outcome(None); P.record_outcome(0); P.record_outcome(2)
    assert (P.COUNTS["failed"], P.COUNTS["unresolved"], P.COUNTS["resolved"]) == (1, 1, 1)


def test_enqueue_never_raises_into_the_entry_path():
    """It is called from the observation path. Nothing here may reach a funded decision."""
    for bad in (None, "", 0):
        assert P.enqueue("4663", bad, now=0.0) is False
        assert P.enqueue(bad, "0xAAA", now=0.0) is False


def test_the_snapshot_exposes_queue_DEPTH_not_just_counters():
    """A queue that is permanently full or rate-bound must be visible, not inferred from an
    absence of effect."""
    P.enqueue("4663", "0xAAA", now=0.0)
    assert P.snapshot()["pending"] == 1
    assert "dropped_rate" in P.snapshot() and "dropped_full" in P.snapshot()


def test_the_two_chains_do_not_collide():
    P.enqueue("4663", "0xAAA", now=0.0)
    assert P.enqueue("8453", "0xAAA", now=0.0) is True, "same mint on another chain is another job"


def test_resolution_warms_families_before_discovery_in_the_same_process(monkeypatch):
    from sniper.execution import evm_v4_quote as v4
    from sniper.settings import memecoin as cfg
    calls = []
    monkeypatch.setattr(cfg, 'evm_rpc_for_chain', lambda chain: 'rpc')
    monkeypatch.setattr(v4, 'warm_pool_key_families', lambda chain: calls.append(('families', chain)))
    monkeypatch.setattr(v4, '_pools_for_mint', lambda chain, mint: calls.append(('pools', mint)) or list(range(6)))
    monkeypatch.setattr(v4, '_pool_key', lambda chain, pid, rpc: calls.append(('key', pid)) or {'key': pid})
    assert P.resolve('5042', 'mint') == 4
    assert calls == [('families', '5042'), ('pools', 'mint')] + [('key', n) for n in range(4)]
    monkeypatch.setattr(cfg, 'evm_rpc_for_chain', lambda chain: None)
    calls.clear()
    assert P.resolve('5042', 'mint') == 0
    assert calls == []


def test_ethereum_key_jobs_require_an_explicit_separate_reader(monkeypatch):
    monkeypatch.delenv('MEMECOIN_EVM_KEY_PREWARM_RPC_URL_1', raising=False)
    assert not P.enqueue('1', '0xaaa', now=0)
    monkeypatch.setenv('MEMECOIN_EVM_KEY_PREWARM_RPC_URL_1', 'https://keys.example')
    assert P.enqueue('1', '0xaaa', now=0)


@pytest.mark.parametrize('identity', ['0x1', '0x38', None])
def test_ethereum_key_reads_are_identity_bound_and_never_fall_back(monkeypatch, identity):
    import json
    from sniper.execution import evm_v4_quote as v4, evm_wallet as wallet, evm_rpc_pool as pool
    from sniper.services import memecoin_evm_exact_capture as capture
    monkeypatch.setenv('MEMECOIN_EVM_KEY_PREWARM_RPC_URL_1', 'https://keys.example')
    monkeypatch.setattr(capture, 'taken_budgets', lambda env: {'custody.example'})
    monkeypatch.setattr(v4, 'warm_pool_key_families', lambda c: None)
    monkeypatch.setattr(v4, '_pools_for_mint', lambda c, m: ['pool'])
    seen = []
    def http(method, url, **kw):
        seen.append((url, json.loads(kw['body'])['method']))
        return 200, {'result': identity}, {}
    monkeypatch.setattr(wallet, '_http', http)
    def key(chain, pid, rpc, http=None):
        assert rpc == 'https://keys.example'
        with pytest.raises(pool.RequestNotSent):
            http('POST', 'https://custody.example', body=json.dumps({'method': 'eth_getLogs'}))
        return {'key': True}
    monkeypatch.setattr(v4, '_pool_key', key)
    if identity == '0x1':
        assert P.resolve('1', 'mint') == 1
    else:
        with pytest.raises(ValueError):
            P.resolve('1', 'mint')
    assert seen == [('https://keys.example', 'eth_chainId')]


def test_ethereum_key_reader_cannot_share_the_funded_provider_budget(monkeypatch):
    from sniper.services import memecoin_evm_exact_capture as capture
    monkeypatch.setenv('MEMECOIN_EVM_KEY_PREWARM_RPC_URL_1', 'https://custody.example/key2')
    monkeypatch.setattr(capture, 'taken_budgets', lambda env: {'custody.example'})
    with pytest.raises(ValueError):
        P.resolve('1', 'mint')


def test_ethereum_reader_has_one_bounded_budget_for_all_pool_reads(monkeypatch):
    import json
    from sniper.execution import evm_wallet as wallet, evm_rpc_pool as pool
    from sniper.services import memecoin_evm_exact_capture as capture
    monkeypatch.setenv('MEMECOIN_EVM_KEY_PREWARM_RPC_URL_1', 'https://keys.example')
    monkeypatch.setattr(capture, 'taken_budgets', lambda env: set())
    calls = []
    def http(method, url, **kw):
        calls.append(kw)
        return 200, {'result': '0x1'}, {}
    monkeypatch.setattr(wallet, '_http', http)
    rpc, read = P._ethereum_reader()
    for _ in range(15):
        read('POST', rpc, body=json.dumps({'method': 'eth_getLogs'}))
    with pytest.raises(pool.RequestNotSent):
        read('POST', rpc, body=json.dumps({'method': 'eth_getLogs'}))
    assert len(calls) == 16
    assert all(0 < c['timeout'] <= 3 for c in calls)
