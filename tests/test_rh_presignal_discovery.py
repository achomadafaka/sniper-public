"""Offline real V4 discovery/proof and RH background scheduling regressions."""
import json
import os
from types import SimpleNamespace

import pytest

from sniper.execution import evm_v4_launchpad as LP
from sniper.execution import evm_v4_quote as V
from sniper.services import memecoin_rh_fast_lane as lane

CHAIN = "4663"
MINT = "0x0057d5824de503e1f1bc55748edf2a77a9522dd7"
POOL = "0xddbdaafabec9d4b0b00ea3e6839727e84f433be383e7f743935e0104683e476f"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
NATIVE = "0x" + "00" * 20
ENTRY = {"address": "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e",
         "info_selector": "3cf28b5a", "hook_selector": "6651812c", "coin_word": 0,
         "counter_word": 4, "fee_word": 6, "tick_spacing_word": 7}
KEY = {"currency0": NATIVE, "currency1": MINT, "fee": 0, "tick_spacing": 200,
       "hooks": HOOK}
URL = "https://rh-research.invalid/key"
CLIP = 19728301827235316


class Clock:
    def __init__(self):
        self.t = 100.0

    def mono(self):
        return self.t

    def wall(self):
        return 1000.0 + self.t

    def ns(self):
        return int(self.t * 10**9)

    def sleep(self, seconds):
        assert seconds >= 0
        self.t += seconds


def word(value):
    return (value.removeprefix("0x") if isinstance(value, str) else format(value, "x")).rjust(64, "0")


def receipt(clock, *, ns=None, boot="boot"):
    return {"version": 1, "raw_utc": "2026-10-11T19:00:00+00:00",
            "corrected_utc": "2026-10-11T19:00:00+00:00", "correction_s": 0.0,
            "monotonic_ns": clock.ns() if ns is None else ns, "boot_id": boot,
            "producer_id": "offline", "sequence": 1}


@pytest.fixture
def rig(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.execution import evm_rpc_pool
    clock = Clock()
    monkeypatch.setattr(V.time, "time", clock.wall)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS", {CHAIN: (ENTRY,)})
    monkeypatch.setattr(cfg, "dexscreener_off", lambda *a: False)
    monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda url: [url])
    monkeypatch.setattr(evm_rpc_pool, "chain_of", lambda url: None)
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a: [])
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a: None)
    monkeypatch.setattr(V, "_own_depths", lambda chain, pools: {p: None for p in pools})
    stored = []
    monkeypatch.setattr(V, "_store_pool_key", lambda c, p, k: stored.append((c, p, dict(k))))
    for name in ("_pool_cache", "_key_cache", "_ranking_cache", "_reserve_cache"):
        monkeypatch.setattr(V, name, {})
    monkeypatch.setattr(LP, "_hook_cache", {(CHAIN, ENTRY["address"]): HOOK})
    calls = []
    state = {"failure": None, "generation": 1, "busy": False, "native": None,
             "late": False, "stopped": False, "urgent": False}

    def http(method, url, headers=None, body=None, timeout=10):
        clock.sleep(0.025)
        if method == "GET":
            calls.append("gt" if "geckoterminal" in url else "ds")
            if state["failure"] == "metadata":
                return 200, {"data": "partial"}, {}
            return 200, {"data": [], "pairs": []}, {}
        req = json.loads(body)
        assert req["method"] == "eth_call"
        data = req["params"][0]["data"]
        if data.startswith("0x3cf28b5a"):
            calls.append("info")
            words = [word(0)] * 15
            words[0], words[4], words[6], words[7] = word(MINT), word(NATIVE), word(0), word(200)
            if state["failure"] == "wrong_mint":
                words[0] = word("0x" + "ab" * 20)
            if state["failure"] == "partial":
                words = words[:3]
            result = "0x" + "".join(words)
        else:
            assert data.startswith("0x" + V._SEL_QUOTE_SINGLE)
            words = [data[10 + i:10 + i + 64] for i in range(0, len(data) - 10, 64)]
            buy, amount = bool(int(words[6], 16)), int(words[7], 16)
            calls.append(("buy" if buy else "sell", amount))
            if state["failure"] == "provider":
                return 503, {}, {}
            if state["failure"] == "pending" or state["failure"] == "buy_only" and not buy:
                return 200, {"jsonrpc": "2.0", "id": 1,
                             "error": {"code": 3, "message": "execution reverted"}}, {}
            result = "0x" + word(amount * 10**8 if buy else amount // 10**8 * 99 // 100)
            if state["late"]:
                clock.sleep(61)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": result}, {}

    paced = lane.PacedHttp(http, min_gap_s=2.0, clock=clock.mono, sleep=clock.sleep)
    cache = SimpleNamespace(ttl_s=60.0, next_due=lambda now: state["native"],
                            urgent_due=lambda now: state["urgent"])
    def preparation():
        return lane.DiscoveryPreparation(paced=paced, url=URL, cache=cache,
            generation_fn=lambda: state["generation"],
            available_fn=lambda: not state["busy"] and not state["stopped"],
            clock=clock.wall, monotonic_ns=clock.ns, boot_id="boot")
    def quote():
        weth = CHAIN_QUOTE[CHAIN]["token"]
        bought = V.v4_quote_amount_out(CHAIN, weth, MINT, CLIP, rpc_url=URL, http=http,
                                     gt_http=lambda url: {"data": []})
        assert bought["pool_id"] == POOL
        sold = V.v4_quote_amount_out(CHAIN, MINT, weth, bought["amount_out"], rpc_url=URL,
                                    http=http, gt_http=lambda url: {"data": []})
        assert sold["pool_id"] == POOL
        return bought, sold
    # Cold/default DS is another boundary; never permit a real request in this fixture.
    real_ds = V._ds_pools_ranked
    monkeypatch.setattr(V, "_ds_pools_ranked", lambda *a, **k: ([], False))
    return SimpleNamespace(clock=clock, calls=calls, state=state, stored=stored, prep=preparation,
                           quote=quote, http=http, paced=paced, cache=cache, real_ds=real_ds)


def test_actual_cold_quote_reproduces_three_proof_reads_then_actual_clip(rig):
    bought, sold = rig.quote()
    assert rig.calls == ["info", ("buy", 10**15), ("sell", 10**23),
                         ("buy", CLIP), ("sell", CLIP * 10**8)]
    assert rig.stored == [(CHAIN, POOL, KEY)]
    assert sold["amount_out"] == CLIP * 99 // 100


def test_background_full_proof_removes_only_derivation_reads_at_consumption(rig):
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT) is True
    assert prep.counts["completed"] == 1
    assert rig.calls == ["gt", "info", ("buy", 10**15), ("sell", 10**23)]
    rig.calls.clear()
    rig.quote()
    assert rig.calls == [("buy", CLIP), ("sell", CLIP * 10**8)]
    assert V._ranking_cache[(CHAIN, MINT)]["cut"] == [POOL]
    assert V._pool_cache[(CHAIN, MINT)][0] == 1700.0  # original discovery start + existing600
    assert rig.paced.min_gap_s == 2.0


def test_completion_status_exposes_only_admitted_proof_and_cache_validity(rig):
    prep = rig.prep()
    assert prep.status().get("last_completion") is None
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT)
    record = prep.status()["last_completion"]
    assert record["scope"] == "discovery_only"
    assert record["chain_id"] == CHAIN and record["mint"] == MINT
    assert record["boot_id"] == "boot"
    assert record["proof_pool_ids"] == [POOL]
    assert record["completion_monotonic_ns"] == rig.clock.ns()
    assert record["cache_valid"] is True
    assert URL not in json.dumps(record)
    V._pool_cache.clear()
    assert prep.status()["last_completion"]["cache_valid"] is False


@pytest.mark.parametrize("failure", ["wrong_mint", "partial", "provider", "pending", "buy_only", "metadata"])
def test_failed_or_incomplete_proof_never_becomes_prepared(rig, failure):
    rig.state["failure"] = failure
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT) is False
    assert prep.counts["completed"] == 0
    assert prep.readiness_at_ingress(MINT, receipt(rig.clock),
                                      generation=1)["state"] == "cold"
    assert rig.stored == []


@pytest.mark.parametrize("reason", ["busy", "generation", "stop", "native", "late"])
def test_background_lifecycle_never_publishes_cancelled_durable_fallback(rig, monkeypatch, reason):
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a: [POOL])
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a: KEY)
    if reason == "busy": rig.state["busy"] = True
    if reason == "generation": rig.state["generation"] = lane.GENERATION_INFLIGHT
    if reason == "stop": rig.state["stopped"] = True
    if reason == "native": rig.state["native"] = MINT
    if reason == "late": rig.state["late"] = True
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT) is False
    assert prep.counts["completed"] == 0
    assert (CHAIN, MINT) not in V._pool_cache
    assert (CHAIN, MINT) not in V._ranking_cache
    assert (CHAIN, MINT) not in V._reserve_cache


def test_causal_readiness_requires_original_clock_before_completion_and_identity(rig, monkeypatch):
    from sniper.settings import memecoin as cfg
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    ingress = receipt(rig.clock)
    assert prep.run_one(MINT)
    assert prep.readiness_at_ingress(MINT, ingress, generation=1)["state"] == "cold"
    ingress["monotonic_ns"] = rig.clock.ns()
    assert prep.readiness_at_ingress(MINT, ingress, generation=1)["state"] == "warm"
    assert prep.readiness_at_ingress(MINT, None, generation=1)["state"] == "unknown"
    assert prep.readiness_at_ingress(MINT, dict(ingress, boot_id="previous"), generation=1)["state"] == "unknown"
    # A wallet execution generation does not invalidate an immutable confirmed PoolKey.
    assert prep.readiness_at_ingress(MINT, ingress, generation=2)["state"] == "warm"
    assert prep.readiness_at_ingress(MINT, ingress)["state"] == "warm"
    assert prep.readiness_at_ingress(MINT, ingress, generation=1, chain="56")["state"] == "cold"
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS", {CHAIN: (dict(ENTRY, fee_word=5),)})
    assert prep.readiness_at_ingress(MINT, ingress, generation=1)["state"] == "cold"


def test_expiry_preserves_original_lifetime_and_reverts_to_cold_discovery(rig):
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT)
    rig.clock.sleep(600)
    assert prep.readiness_at_ingress(MINT, receipt(rig.clock),
                                      generation=1)["state"] == "cold"
    rig.calls.clear()
    rig.quote()
    assert rig.calls[:3] == ["info", ("buy", 10**15), ("sell", 10**23)]


@pytest.mark.parametrize('capacity', ['pause', 'reserved', 'lock'])
def test_rotated_speculative_read_skips_shared_account_wait_without_sending(rig, monkeypatch, tmp_path, capacity):
    import fcntl
    from sniper.execution import research_rotation_4663 as rr
    from sniper.services import memecoin_research_pacer as rp
    drpc = 'https://lb.drpc.org/ogrpc?network=robinhood&dkey=OFFLINE'
    rotation = rr.Rotation({rr.BASE: URL, 'MEMECOIN_RESEARCH_DRPC_RPC_URL_4663': drpc},
                           clock=rig.clock.wall, drpc_fallback=True, log=lambda _: None)
    rotation._spent.add(1)
    account = rp.SharedPacer(tmp_path / 'account.json', gap_s=rp.DRPC_GAP_S,
                            clock=rig.clock.wall, sleep=rig.clock.sleep)
    monkeypatch.setattr(rp.SharedPacer, 'named', classmethod(lambda cls, *a, **k: account))
    if capacity == 'pause':
        account.pause(300)
    elif capacity == 'reserved':
        account._locked(lambda s: (None, dict(s, last=rig.clock.wall() + 300)))
    handle = None
    if capacity == 'lock':
        handle = open(str(account.path) + '.lock', 'w')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Ordinary acquire would wait/hang here. Keep this regression bounded and expose its use.
    monkeypatch.setattr(account, 'acquire', lambda: pytest.fail('speculative read waited for account capacity'))
    rig.paced.base = rotation.http_seam(lambda: rig.http)
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    try:
        assert not prep.run_one(MINT)
    finally:
        if handle is not None:
            handle.close()
    assert prep.counts['completed'] == 0
    assert all(isinstance(call, str) and call in ('gt', 'ds') for call in rig.calls)
    assert all(slot['calls'] == slot['transport'] == slot['quota'] == 0 for slot in rotation.stats()[1])
    assert rotation.provider_errors() == 0
    assert rig.clock.mono() - 100 < 5
    assert (CHAIN, MINT) not in V._pool_cache


@pytest.mark.parametrize('change', ['deadline', 'busy', 'generation', 'stop'])
def test_rotated_account_grant_rechecks_job_before_identity_http(rig, monkeypatch, change):
    from sniper.execution import research_rotation_4663 as rr
    from sniper.services import memecoin_research_pacer as rp
    drpc = 'https://lb.drpc.org/ogrpc?network=robinhood&dkey=OFFLINE'
    rotation = rr.Rotation({rr.BASE: URL, 'MEMECOIN_RESEARCH_DRPC_RPC_URL_4663': drpc},
                           clock=rig.clock.wall, drpc_fallback=True, log=lambda _: None)
    rotation._spent.add(1)
    def grant(self):
        if change == 'deadline':
            rig.clock.sleep(61)
        elif change == 'generation':
            rig.state['generation'] += 1
        elif change == 'stop':
            rig.state['stopped'] = True
        else:
            rig.state['busy'] = True
        return True
    monkeypatch.setattr(rp.SharedPacer, 'try_acquire', grant)
    rig.paced.base = rotation.http_seam(lambda: rig.http)
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)
    assert all(isinstance(call, str) and call in ('gt', 'ds') for call in rig.calls)
    assert all(slot['calls'] == 0 for slot in rotation.stats()[1])
    assert rotation.provider_errors() == 0
    if change == 'deadline':
        assert prep.counts['deadline'] == 1


@pytest.mark.parametrize('error', [PermissionError('account file denied'), OSError('account file full')])
def test_speculative_account_file_failure_is_not_a_provider_transport_failure(rig, monkeypatch, error):
    from sniper.execution import research_rotation_4663 as rr
    from sniper.services import memecoin_research_pacer as rp
    drpc = 'https://lb.drpc.org/ogrpc?network=robinhood&dkey=OFFLINE'
    rotation = rr.Rotation({rr.BASE: URL, 'MEMECOIN_RESEARCH_DRPC_RPC_URL_4663': drpc},
                           clock=rig.clock.wall, drpc_fallback=True, log=lambda _: None)
    rotation._spent.add(1)
    def denied(self):
        raise error
    monkeypatch.setattr(rp.SharedPacer, 'try_acquire', denied)
    rig.paced.base = rotation.http_seam(lambda: rig.http)
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)
    assert rig.calls == ['gt']
    assert all(slot['calls'] == slot['transport'] == slot['quota'] == 0
               for slot in rotation.stats()[1])
    assert rotation.provider_errors() == 0
    assert (CHAIN, MINT) not in V._pool_cache


def test_rotated_speculative_discovery_keeps_verified_identity_and_finishes_full_proof(rig, monkeypatch, tmp_path):
    from sniper.execution import research_rotation_4663 as rr
    from sniper.services import memecoin_research_pacer as rp
    drpc = 'https://lb.drpc.org/ogrpc?network=robinhood&dkey=OFFLINE'
    rotation = rr.Rotation({rr.BASE: URL, 'MEMECOIN_RESEARCH_DRPC_RPC_URL_4663': drpc},
                           clock=rig.clock.wall, drpc_fallback=True, log=lambda _: None)
    rotation._spent.add(1)
    account = rp.SharedPacer(tmp_path / 'account.json', gap_s=rp.DRPC_GAP_S,
                            clock=rig.clock.wall, sleep=rig.clock.sleep)
    monkeypatch.setattr(rp.SharedPacer, 'named', classmethod(lambda cls, *a, **k: account))
    monkeypatch.setattr(account, 'acquire', lambda: pytest.fail('speculative account read waited'))
    def http(method, url, headers=None, body=None, timeout=10):
        if method == 'POST' and json.loads(body)['method'] == 'eth_chainId':
            rig.calls.append('identity')
            rig.clock.sleep(0.025)
            return 200, {'jsonrpc': '2.0', 'id': 1, 'result': '0x1237'}, {}
        return rig.http(method, url, headers=headers, body=body, timeout=timeout)
    rig.paced.base = rotation.http_seam(lambda: http)
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)  # identity consumed the shared account's immediate grant
    assert rig.calls == ['gt', 'identity']
    assert rotation._verified == {2}
    rig.clock.sleep(5)
    assert prep.run_one(MINT)
    assert rig.calls == ['gt', 'identity', 'gt', 'info', ('buy', 10**15), ('sell', 10**23)]
    assert prep.counts['completed'] == 1
    assert rig.stored == [(CHAIN, POOL, KEY)]
    assert rotation.provider_errors() == 0
    assert account.gap_s == rp.DRPC_GAP_S


def test_rotated_quota_retry_rechecks_job_before_sending_to_next_slot(rig):
    from sniper.execution import research_rotation_4663 as rr
    spare = 'https://second.robinhood-mainnet.quiknode.pro/OFFLINE'
    rotation = rr.Rotation({rr.BASE: URL, rr.BASE + '_2': spare},
                           clock=rig.clock.wall, log=lambda _: None)
    sent = []
    def http(method, url, headers=None, body=None, timeout=10):
        if method == 'POST':
            sent.append(url)
            rig.state['generation'] += 1
            return 200, {'jsonrpc': '2.0', 'id': 1,
                         'error': {'code': -32003, 'message': 'daily request limit reached'}}, {}
        return rig.http(method, url, headers=headers, body=body, timeout=timeout)
    rig.paced.base = rotation.http_seam(lambda: http)
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)
    assert sent == [URL]
    slots = rotation.stats()[1]
    assert slots[0]['calls'] == slots[0]['quota'] == 1
    assert slots[1]['calls'] == slots[1]['transport'] == slots[1]['quota'] == 0
    assert rotation.provider_errors() == 1  # the real quota answer, without a fabricated second failure
    assert (CHAIN, MINT) not in V._pool_cache


def test_shared_candidates_are_tape_observed_bounded_and_do_not_create_native_entries(monkeypatch):
    from sniper.services import memecoin_rh_fast_decide as decide
    coins = ["0x" + format(i, "040x") for i in range(1, 50)]
    tape = SimpleNamespace(busiest=lambda now: coins)
    monkeypatch.setattr(decide, "lane_route", lambda tape, mint, now: {"kind": "v4_native"} if mint == coins[0] else None)
    assert decide.make_discovery_mints(tape, 100, signal_mints={coins[2], "0x" + "ff" * 20},
                                       armed_mints={coins[3]}, k=5) == coins[2:4] + coins[:2] + [coins[4]]
    assert len(decide.make_discovery_mints(tape, 100, signal_mints=set(), armed_mints=set(), k=100)) == 32


def test_known_native_pool_cannot_exclude_authenticated_pons_shared_demand():
    from test_memecoin_pons_counter_route import _tenet_receipt
    from test_memecoin_rh_source_binding import CTX
    from sniper.services import memecoin_rh_fast_decide as decide
    from sniper.services.memecoin_evm_post_trade import source_post_trade
    frame = _tenet_receipt()
    proof = frame['source_fill']
    mint, block = proof['mint'], proof['block_number']
    proof['source_post'] = source_post_trade('4663', frame['receipt'], mint, owner=proof['wallet'],
        pool_key_fn=lambda chain, pool: frame['bridge_keys'].get(pool))
    meta = dict(protocol='v4', mint=mint, token0=NATIVE, token1=mint,
                fee=0, tick_spacing=200, hooks=HOOK)
    tape = decide.TapeState(lambda pool: meta)
    tape.ingest([dict(pool=POOL, observed=CTX['now'], block_no=block)])
    tape.learn_source_fills([proof])
    sig = dict(wallet=proof['wallet'], source_user_id=proof['source_user_id'], mint=mint,
               tx_hash=proof['tx_hash'], seq=block, payment_token=proof['payment_token'],
               paid=int(proof['payment_atomic']))
    out = decide.decide(sig, tape=tape, **dict(CTX, decimals={proof['payment_token']: 6}))
    assert out['reason'] == 'would_send' and out['route_kind'] == 'pons_shared'
    native = decide.make_candidates(tape, CTX['now'], signal_mints={mint}, armed_mints=set(), k=1)
    assert len(native) == 1 and native[0]['kind'] == 'v4_native'
    assert decide.make_discovery_mints(tape, CTX['now'], signal_mints={mint},
                                       armed_mints=set(), k=1) == [mint]
    assert decide.make_candidates(tape, CTX['now'], signal_mints={mint}, armed_mints=set(), k=1) == native


def test_discovery_envelope_rejects_oversize_and_bad_identity():
    base = {"kind": "candidates", "entries": [], "discovery_mints": [MINT]}
    assert lane.check_sender(base, 42, os.getuid(), detector_pid=42) is None
    for mints in ([MINT] * 33, ["0xa"], [None], "bad"):
        assert lane.check_sender(dict(base, discovery_mints=mints), 42, os.getuid(), detector_pid=42) == "malformed"


def test_all_metadata_and_proof_reads_use_the_owned_research_pacer(rig, monkeypatch):
    monkeypatch.setattr(V, "_ds_pools_ranked", rig.real_ds)
    from sniper.execution import solana_quote
    monkeypatch.setattr(solana_quote, "dexscreener_cooldown_active", lambda **k: False)
    monkeypatch.setattr(solana_quote, "_ds_start_cooldown", lambda **k: pytest.fail("funded DS breaker changed"))
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT)
    assert rig.calls == ["gt", "ds", "info", ("buy", 10**15), ("sell", 10**23)]
    assert rig.paced.counts["calls"] == 5
    assert 8 <= rig.clock.t - 100 < 9
    assert prep.status()["queue_size"] == 0
    assert prep.status()["completion_age_s"] == 0


@pytest.mark.parametrize("stage", ["metadata", "info", "buy", "sell"])
@pytest.mark.parametrize("failure", ["timeout", "malformed", "wrong_id", "generation", "stop"])
def test_every_reply_boundary_cancels_without_fallback_publication(rig, monkeypatch, stage, failure):
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a: [POOL])
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a: KEY)
    base = rig.http
    def boundary(method, url, **kwargs):
        reply = base(method, url, **kwargs)
        current = rig.calls[-1]
        current = current[0] if isinstance(current, tuple) else current
        if current == ("gt" if stage == "metadata" else stage):
            if failure == "timeout": raise TimeoutError("offline timeout")
            if failure == "malformed": return 200, [], {}
            if failure == "wrong_id": return 200, {"jsonrpc": "2.0", "id": 999, "result": "0x01"}, {}
            if failure == "generation": rig.state["generation"] += 1
            if failure == "stop": rig.state["stopped"] = True
        return reply
    rig.paced.base = boundary
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)
    assert prep.counts["completed"] == 0
    assert (CHAIN, MINT) not in V._pool_cache
    assert (CHAIN, MINT) not in V._ranking_cache
    assert (CHAIN, MINT) not in V._reserve_cache
    assert rig.stored == []


def test_wrong_derived_hash_cannot_enter_durable_or_memory_cache(rig, monkeypatch):
    monkeypatch.setattr(LP, "pool_id_for_key", lambda key: "0x" + "ab" * 32)
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)
    assert rig.stored == []
    assert (CHAIN, MINT) not in V._pool_cache


def test_deadline_before_dispatch_does_not_penalize_any_provider(rig):
    prep = rig.prep()
    job = lane._DiscoveryReads(prep, 1, lane.discovery_descriptor())
    rig.clock.sleep(60)
    from sniper.execution.evm_rpc_pool import RequestNotSent
    with pytest.raises(RequestNotSent):
        job("POST", URL, body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": []}))
    assert rig.paced.counts == {"calls": 0, "http_429": 0, "transport_errors": 0, "dead_marked": 0, "quota_403": 0}


def test_other_rpc_host_cannot_be_touched_by_background_discovery(rig):
    prep = rig.prep()
    job = lane._DiscoveryReads(prep, 1, lane.discovery_descriptor())
    from sniper.execution.evm_rpc_pool import RequestNotSent
    with pytest.raises(RequestNotSent):
        job("POST", "https://funded.invalid/other", body="{}")
    assert rig.calls == []


def test_single_job_and_native_priority_do_not_wait_or_use_extra_capacity(rig):
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    rig.state["native"] = "0x" + "11" * 20
    assert prep.next_due(rig.clock.wall()) is None
    assert not prep.run_one(MINT)
    rig.state["native"] = None
    assert prep.next_due(rig.clock.wall() + 11) == MINT
    prep._job_lock.acquire()
    try:
        assert not prep.run_one(MINT)
    finally:
        prep._job_lock.release()
    assert rig.calls == []


def test_native_route_has_unknown_discovery_warmth(rig):
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT)
    assert prep.readiness_at_ingress(MINT, receipt(rig.clock),
        generation=1, route="v4_native")["state"] == "unknown"


def test_heartbeat_distinguishes_preparation_off_from_zero(tmp_path):
    import sqlite3
    dispatch = SimpleNamespace(counts={"candidates_msgs": 0, "signals": 0}, preparation=None)
    cache = SimpleNamespace(size=lambda: 0, counts={"verdicts_ok": 0, "verdicts_unsafe": 0,
                                                  "verdict_errors": 0, "cpu_ms": 0})
    research = lane.PacedHttp(lambda *a, **k: (200, {}, {}))
    path = str(tmp_path / "lane.db")
    lane.write_heartbeat(path, dispatch, cache, research)
    query = "SELECT discovery_attempted,discovery_completed,discovery_unavailable,discovery_deadline,discovery_queue_size,discovery_completion_age_s FROM rh_fast_lane_heartbeat ORDER BY rowid DESC LIMIT 1"
    with sqlite3.connect(path) as db:
        assert db.execute(query).fetchone() == (None,) * 6
    dispatch.preparation = SimpleNamespace(status=lambda: dict(attempted=2, completed=1,
        unavailable=1, deadline=0, queue_size=3, completion_age_s=2.5))
    lane.write_heartbeat(path, dispatch, cache, research)
    with sqlite3.connect(path) as db:
        assert db.execute(query).fetchone() == (2, 1, 1, 0, 3, 2.5)


def test_bounded_fairness_completes_shared_proof_under_continuous_native_load(rig):
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    rig.state["native"] = "0x" + "11" * 20
    assert prep.next_work(rig.clock.wall()) == ("native", rig.state["native"])
    prep.native_completed()
    assert prep.next_work(rig.clock.wall()) == ("native", rig.state["native"])
    prep.native_completed()
    assert prep.next_work(rig.clock.wall()) == ("discovery", MINT)
    assert prep.run_one(MINT, allow_native_due=True)
    assert prep.next_work(rig.clock.wall()) == ("native", rig.state["native"])
    assert prep.counts["completed"] == 1
    assert rig.paced.counts["calls"] == 4


@pytest.mark.parametrize("pressure", ["urgent", "busy"])
def test_fairness_lease_cancels_for_new_urgent_native_or_live_pressure(rig, pressure):
    base = rig.http
    def pressured(method, url, **kwargs):
        reply = base(method, url, **kwargs)
        if rig.calls[-1] == "info":
            rig.state[pressure] = True
        return reply
    rig.paced.base = pressured
    rig.state["native"] = "0x" + "11" * 20
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    prep.native_completed()
    prep.native_completed()
    assert prep.next_work(rig.clock.wall()) == ("discovery", MINT)
    assert not prep.run_one(MINT, allow_native_due=True)
    assert prep.counts["completed"] == 0
    assert rig.calls == ["gt", "info"]
    assert (CHAIN, MINT) not in V._pool_cache


def test_malformed_original_clock_is_unknown_even_if_ns_and_boot_match(rig):
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT)
    for clock in ({"boot_id": "boot", "monotonic_ns": rig.clock.ns()},
                  dict(receipt(rig.clock), history_unknown=True),
                  dict(receipt(rig.clock), sequence=None)):
        assert prep.readiness_at_ingress(MINT, clock, generation=1)["state"] == "unknown"


def test_evicted_candidate_cannot_publish_late_readiness(rig):
    base = rig.http
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    def evicted(method, url, **kwargs):
        reply = base(method, url, **kwargs)
        if rig.calls[-1] == "info":
            prep.note_candidates(["0x" + format(i, "040x") for i in range(1, 33)],
                                 now=rig.clock.wall() + 1)
        return reply
    rig.paced.base = evicted
    assert not prep.run_one(MINT)
    assert len(prep._records) <= 32
    assert (CHAIN, MINT) not in V._pool_cache


@pytest.mark.parametrize("provider", ["gt", "ds"])
def test_metadata_429_never_pauses_native_rpc_safety_account(rig, monkeypatch, provider):
    from sniper.execution import geckoterminal_quote as gt, solana_quote as ds
    monkeypatch.setattr(gt, "_cooldown_until", [0.0])
    monkeypatch.setattr(gt, "_funded_cooldown_until", [0.0])
    monkeypatch.setattr(ds, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(ds, "_ds_funded_cooldown_until", [0.0])
    prep = rig.prep()
    job = lane._DiscoveryReads(prep, 1, lane.discovery_descriptor())
    rig.paced.base = lambda *a, **k: (429, {}, {})
    from sniper.execution.evm_rpc_pool import RequestNotSent
    url = "https://api.geckoterminal.com/api/v2/networks/robinhood/tokens/" + MINT + "/pools" if provider == "gt" else "https://api.dexscreener.com/latest/dex/tokens/" + MINT
    with pytest.raises(RequestNotSent):
        job.get_json(url)
    assert rig.paced._paused_until == 0.0
    assert not rig.paced.paused()
    assert rig.paced.counts["http_429"] == 0
    assert gt._funded_cooldown_until == [0.0]
    assert ds._ds_funded_cooldown_until == [0.0]
    assert (gt._cooldown_until[0] > 0) == (provider == "gt")
    assert (ds._ds_cooldown_until[0] > 0) == (provider == "ds")


def test_disabled_gt_is_not_requested_but_other_exact_proof_remains_possible(rig, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_GT_ENABLED", False)
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert prep.run_one(MINT)
    assert "gt" not in rig.calls


@pytest.mark.parametrize("wrong", ["result_and_error", "bool_id"])
def test_ambiguous_or_wrong_id_rpc_response_never_supplies_a_pool_proof(rig, wrong):
    base = rig.http
    def ambiguous(method, url, **kwargs):
        status, response, headers = base(method, url, **kwargs)
        if method == "POST":
            response = dict(response)
            if wrong == "result_and_error":
                response["error"] = {"code": 3, "message": "execution reverted"}
            else:
                response["id"] = True
        return status, response, headers
    rig.paced.base = ambiguous
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)
    assert rig.stored == []
    assert (CHAIN, MINT) not in V._pool_cache


@pytest.mark.parametrize("error", [{}, 0, False, [], None,
                                  {"code": 3.0, "message": "execution reverted"},
                                  {"code": 3}, {"code": 3, "message": ""},
                                  {"code": 3, "message": 0}, {"code": 3, "message": " "}])
def test_malformed_error_cannot_publish_partial_launchpad_discovery(rig, monkeypatch, error):
    from sniper.settings import memecoin as cfg
    other = dict(ENTRY, address="0x" + "de" * 20)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_LAUNCHPADS", {CHAIN: (ENTRY, other)})
    LP._hook_cache[(CHAIN, other["address"])] = HOOK
    base = rig.http
    def partial(method, url, **kwargs):
        reply = base(method, url, **kwargs)
        request = json.loads(kwargs["body"]) if method == "POST" else None
        if request and request["params"][0]["to"] == ENTRY["address"]:
            return 200, {"jsonrpc": "2.0", "id": 1, "error": error}, {}
        return reply
    rig.paced.base = partial
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    assert not prep.run_one(MINT)
    assert prep.counts["completed"] == 0
    assert (CHAIN, MINT) not in V._pool_cache
    assert rig.stored == []


def test_explicit_contract_revert_remains_a_token_result(rig):
    prep = rig.prep()
    job = lane._DiscoveryReads(prep, 1, lane.discovery_descriptor())
    answer = {"jsonrpc": "2.0", "id": 1,
              "error": {"code": 3, "message": "execution reverted"}}
    rig.paced.base = lambda *a, **k: (200, answer, {})
    assert job("POST", URL, body=json.dumps({"jsonrpc": "2.0", "id": 1,
               "method": "eth_call", "params": []}))[1] == answer
    job.raise_if_cancelled()
    assert job.failure is None


def test_evicted_job_cannot_leave_orphan_retry_state(rig):
    prep = rig.prep()
    prep.note_candidates([MINT], now=rig.clock.wall())
    base = rig.http
    def evict(method, url, **kwargs):
        reply = base(method, url, **kwargs)
        if rig.calls[-1] == "info":
            prep.note_candidates(["0x" + format(i, "040x") for i in range(1, 33)],
                                 now=rig.clock.wall() + 1)
        return reply
    rig.paced.base = evict
    assert not prep.run_one(MINT)
    assert MINT not in prep._retry_at
    assert set(prep._retry_at).issubset(prep._candidates)
