"""BSC V3 delivery and attempt-local quote overlap; no funded/network calls."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_post_trade as post


def _shield():
    receipt = json.loads((Path(__file__).parent / "fixtures/memecoin/bsc_shield_source_receipt.json").read_text())
    mint = "0x3b4cbdb0a05d08638d6c11a79d0455fb9ffa65ff"
    owner = "0xd3375d0fe9e0e0a0cd3a5b87b833b61f167d04f8"  # actual owner; tx.from is the solver
    return receipt, mint, owner, post.source_post_trade("56", receipt, mint, owner=owner)


def test_shield_v3_receipt_reaches_the_live_lane_without_a_v2_price():
    receipt, mint, owner, decoded = _shield()
    assert decoded["venue"] == "v3"
    decoded["ms"] = 0.17  # The real watcher attaches decode timing to its receipt facts.
    sent = []
    signal = bsc.make_watcher_signal("live", "test", send_fn=lambda a, p: sent.append(p) or True)
    row = {"buy": {"mint": mint, "source_size_usd": 3272, "source_price": 0.000136},
           "wallet": owner, "tx_hash": receipt["transactionHash"],
           "log": {"blockNumber": receipt["blockNumber"]}, "now": "2026-10-03T19:00:00+00:00"}
    out = signal(row, receipt, decoded, decimals=18, bnb_usd=1000, bnb_usd_age_s=0.1)
    assert out["reason"] == "would_send" and out["sent"], out
    payload, = sent
    assert payload["route_kind"] == "v3_shared"
    assert payload["pool"] == decoded["pool"]
    assert payload["post_ratio"] == decoded["ratio"]
    assert "r_coin" not in payload and "r_quote" not in payload
    assert bsc.check_sender(payload, 123, __import__("os").getuid(), watcher_pid=123) is None


def test_shadow_mode_does_not_mistake_v3_for_its_v2_builder():
    receipt, mint, owner, decoded = _shield()
    signal = bsc.make_watcher_signal("runner_shadow", "test", send_fn=lambda *a: pytest.fail("send"))
    out = signal({"buy": {"mint": mint}, "wallet": owner}, receipt, decoded,
                 decimals=18, bnb_usd=1000, bnb_usd_age_s=0.1)
    assert out["reason"] == "v3_shadow_unsupported" and not out["sent"]


@pytest.mark.parametrize("field,value,clause", [("tx_hash", "0x" + "11" * 32, "tx_mismatch"),
                                              ("block", "0x1", "block_mismatch"),
                                              ("ratio", 99, "decode_mismatch"),
                                              ("pool", "0x" + "22" * 20, "decode_mismatch")])
def test_v3_hint_cannot_bind_a_receipt_to_a_different_source(field, value, clause):
    receipt, mint, owner, decoded = _shield()
    row = {"buy": {"mint": mint, "source_size_usd": 3272, "source_price": 0.000136},
           "wallet": owner, "tx_hash": receipt["transactionHash"],
           "log": {"blockNumber": receipt["blockNumber"]}}
    if field == "block":
        row["log"]["blockNumber"] = value
    elif field == "tx_hash":
        row[field] = value
    else:
        decoded[field] = value
    signal = bsc.make_watcher_signal("live", "test", send_fn=lambda *a: pytest.fail("wrong identity sent"))
    out = signal(row, receipt, decoded, decimals=18, bnb_usd=1, bnb_usd_age_s=0)
    assert out["reason"] == "v3_source_unbound:" + clause and out["decide_ms"] > 0   # F2: the clause, a real time


@pytest.mark.parametrize("field,value", [("post_ratio", float("nan")), ("post_ratio", 0),
                                        ("paid_usd", -1), ("source_price", True),
                                        ("decimals", -1), ("seq", 0), ("tx_hash", "tx")])
def test_v3_sender_rejects_unknown_nonfinite_or_invalid_units(field, value):
    receipt, mint, owner, decoded = _shield()
    payload = {"chain": "56", "route_kind": "v3_shared", "mint": mint,
               "wallet": owner, "pool": decoded["pool"], "quote": "0x" + "11" * 20,
               "tx_hash": receipt["transactionHash"], "seq": int(receipt["blockNumber"], 16),
               "post_ratio": decoded["ratio"], "paid_usd": 100, "source_price": 0.1,
               "decimals": 18, "sent_epoch": time.time()}
    payload[field] = value
    assert bsc.check_sender(payload, 123, __import__("os").getuid(), watcher_pid=123) == "malformed"


def test_attempt_quote_finishes_the_same_read_without_starting_another_ladder():
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    release = threading.Event()
    started = threading.Event()
    def quote(mint, clip):
        started.set()
        release.wait(2)
        return {"quote": {"buyAmount": "123"}, "usd": clip}
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: (25, None), quote_fn=quote)
    assert started.wait(1)
    timer = threading.Timer(0.05, release.set)
    timer.start()
    before = time.monotonic()
    assert early.take("coin", 25)["quote"]["buyAmount"] == "123"
    assert 0.03 <= time.monotonic() - before < 0.5
    release.set()
    early.worker.join(1)
    assert early.take("coin", 25) is None, "an attempt must not leak into a later entry"


@pytest.mark.parametrize("boundary", ["sizing", "ladder"])
def test_fallback_cancels_unstarted_early_ladder_without_waiting(boundary):
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    blocked, release = threading.Event(), threading.Event()
    provider_ladders = []
    def pause():
        blocked.set()
        release.wait(1)
    def sizing():
        if boundary == "sizing":
            pause()
        return 25, None
    def quote(mint, clip, *, on_started):
        if boundary == "ladder":
            pause()
        if on_started(time.monotonic(), 1, {"mint": mint, "usd": clip}) is False:
            return None
        provider_ladders.append("early")
        return {"quote": {"buyAmount": "123"}}
    quote.early_deadline = True
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=sizing, quote_fn=quote)
    assert blocked.wait(1)
    start = time.monotonic()
    assert early.take("coin", 25) is None
    assert time.monotonic() - start < 0.1, "entry must not await sizing or provider launch"
    provider_ladders.append("normal")
    release.set()
    early.worker.join(1)
    assert provider_ladders == ["normal"]


@pytest.mark.parametrize("mint,clip,age", [("other", 25, 0), ("coin", 20, 0), ("coin", 25, 4)])
def test_wrong_mint_clip_or_stale_quotes_are_discarded(mint, clip, age):
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    now = [10.0]
    early = EarlyEvmQuote(mint="coin", trace=None, clock=lambda: now[0],
                         sizing_fn=lambda: (25, None), quote_fn=lambda *a: {"quote": {"buyAmount": "123"}})
    early.worker.join(1)
    now[0] += age
    assert early.take(mint, clip) is None


def test_completed_quote_is_attempt_local_and_used_once():
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    trace = SimpleNamespace(mark_substage=lambda *a, **kw: None)
    early = EarlyEvmQuote(mint="coin", trace=trace, sizing_fn=lambda: (25, None),
                         quote_fn=lambda *a: {"quote": {"buyAmount": "123"}})
    early.worker.join(1)
    assert early.take("coin", 25)["quote"]["buyAmount"] == "123"
    assert early.take("coin", 25) is None


def test_stats_publication_does_not_grant_token_safety():
    from sniper.services.memecoin_rh_fast_lane import SafetyCache
    cache = SafetyCache(lambda *a, **k: (False, "unsafe"), ttl_s=30, usd=25)
    cache.put_market_stats("coin", {"volume_m5": 123}, now=10)
    assert cache.stats("coin", now=11)[0]["volume_m5"] == 123
    assert cache.reason("coin", now=11) == "no_safety_verdict"


@pytest.mark.parametrize("custom_factory", [True, False])
def test_v3_delivery_uses_the_funded_executor_and_cached_stats(monkeypatch, custom_factory):
    from sniper.services.memecoin_rh_fast_lane import SafetyCache
    from test_memecoin_bsc_source_initiation import MINT, TX, WALLET, expected_proof
    cache = SafetyCache(lambda *a, **k: pytest.fail("V2 safety"), ttl_s=30, usd=25)
    cache.put_market_stats(MINT, {"volume_m5": 123}, now=time.time())
    called = []
    def original(*a, **kw):
        return {"status": "filled", "tx_hash": "0x" + "77" * 32, "position_id": 77}
    ctx = {"execute_fn": original, "token_safety_fn": object()}
    def factory(*args, stats_fn=None, **kw):
        assert "blind_mcap_fn" not in kw, "V3 must retain the ordinary fallback resolver"
        contexts = args[1] if stats_fn is not None else args[0]
        stats_fn = stats_fn or args[1]
        assert contexts["56"]["execute_fn"] is original
        assert contexts["56"]["token_safety_fn"] is ctx["token_safety_fn"]
        assert stats_fn(MINT, network_id="56")["volume_m5"] == 123
        def hook(conn, ev):
            called.append(ev)
            return original()
        hook.attempt = lambda *a: pytest.fail("full V3 hook must preserve retry enqueue")
        return hook
    live = object.__new__(bsc.BscLive)
    live.ctx, live.contexts, live.conn, live.cache = ctx, {"56": ctx}, None, cache
    live.host_clock, live.v3_hook_factory = time.time, factory if custom_factory else None
    live.stopped = False                 # LiveLane.__init__ sets it; object.__new__ skips __init__
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(runner, "make_real_hook", factory)
    live.stats_fn = lambda *a: pytest.fail("warm stats fetched again")
    p = {"route_kind": "v3_shared", "mint": MINT, "tx_hash": TX, "pool": "pool",
         "quote": "quote", "sent_epoch": time.time(), "post_ratio": 1.1,
         "paid_usd": 1000, "source_price": 0.01, "arrival_utc": "2026-10-03T19:00:00+00:00",
         "wallet": WALLET, "source_user_id": "u1", "seq": 16, "source_initiation": expected_proof()}
    result = live.handle(p, recv_epoch=time.time())
    assert called[0].delivery_lane == "bsc_v3_fast_lane"
    assert called[0].evm_chase_reference.post()["venue"] == "v3"
    assert result["reason"].startswith("hook:")
    assert result["tx_hash"] == "0x" + "77" * 32 and result["position_id"] == 77


def test_v3_keeps_the_normal_source_identity_and_retry_contract():
    from sniper.services.memecoin_onchain_lane import retry_payload, event_from_retry_payload
    payload = {"route_kind": "v3_shared", "mint": "0x" + "11" * 20,
               "tx_hash": "0x" + "22" * 32, "pool": "0x" + "33" * 20, "post_ratio": 1.1,
               "paid_usd": 1000, "source_price": 0.01, "source_user_id": "u",
               "arrival_utc": "2026-10-03T19:00:00+00:00"}
    ev = bsc.BscLaneEvent(payload, market_cap=None)
    assert ev.fomo_event_id == "onchain:56:%s:%s" % (payload["tx_hash"], payload["mint"])
    assert not ev.no_revival
    restored = event_from_retry_payload(retry_payload(ev))
    assert restored.fomo_event_id == ev.fomo_event_id
    assert restored.delivery_lane == "bsc_v3_fast_lane"


@pytest.mark.parametrize("chain", ["56", "8453", "1", "5042"])
def test_the_funded_hook_starts_quote_before_market_data_finishes(monkeypatch, tmp_path, chain):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    from test_memecoin_entry_identity import _arm, _event, FakeClock, NOW
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM", True)
    monkeypatch.setattr(runner, "_early_evm_quote_clip", lambda *a: (25, None))
    seen, times = {}, {}
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    def quote(*a):
        times["quote"] = time.monotonic()
        return {"quote": {"buyAmount": "123"}}
    def stats(*a, **kw):
        time.sleep(0.2)
        times["stats_end"] = time.monotonic()
        return {}
    def entry(conn, ev, **kw):
        seen["ev"] = ev
        return {"status": "rejected", "reason": "fixture"}
    monkeypatch.setattr(runner, "real_entry", entry)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **kw: None)
    ctx = {"usdc_fn": lambda: 300, "native_fn": lambda: 1,
           "prepare_entry_quote_fn": quote}
    trace = SimpleNamespace(attempt_id="bsc-early", mark=lambda *a, **kw: None,
                            mark_substage=lambda *a, **kw: None, finish=lambda *a, **kw: None)
    runner.real_entry_hook(conn, _event(network_id=chain), {chain: ctx}, now_fn=lambda: NOW,
                          trade_size=25, max_age_s=120, stats_fn=stats, trace=trace,
                          trace_clock=FakeClock())
    early = getattr(seen["ev"], "entry_evm_early_quote", None)
    assert early is not None, "Base must overlap its quote with the market read too"
    early.worker.join(1)
    assert early.trace is trace and times["quote"] < times["stats_end"]


@pytest.mark.parametrize("armed_chain,expected", [("8453", 25.0), ("56", None)])
def test_base_early_quote_uses_base_authority_and_sizing(monkeypatch, tmp_path, armed_chain, expected):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner, memecoin_arming as arming
    from sniper.services import memecoin_evm_risk as risk, memecoin_wallet_caps as caps
    from sniper.settings import memecoin as cfg
    from test_memecoin_entry_identity import _approve, _event
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    _approve(conn, chains=(armed_chain,))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    monkeypatch.setattr(arming, "real_entries_allowed", lambda *a: True)
    monkeypatch.setattr(arming, "in_quiet_hours", lambda: False)
    monkeypatch.setattr(runner, "_trial_for_entry", lambda *a: None)
    checked = []
    def cap(uid, chain, **kw):
        assert chain == "8453"
        return 25
    def context(c, **kw):
        assert kw["network_id"] == "8453"
        assert kw["usdc_balance"] == 300
        assert kw["native_balance"] == 0.02
        assert kw["size_cap_usd"] == 25
        checked.append(kw)
        return SimpleNamespace(trade_size_usd=25)
    monkeypatch.setattr(caps, "entry_cap_usd", cap)
    monkeypatch.setattr(risk, "build_evm_risk_context", context)
    monkeypatch.setattr(risk, "evm_entry_allowed", lambda *a, **kw: SimpleNamespace(allowed=True))
    pre = SimpleNamespace(balances=lambda: (300, 0.02), stale=lambda: False)
    trace = SimpleNamespace(mark_substage=lambda *a, **kw: None)
    prepared = []
    def quote(mint, clip):
        prepared.append(clip)
        return {"quote": {"sellAmount": "1", "buyAmount": "123"}}
    ev = _event(network_id="8453")
    early = runner._start_early_evm_quote(conn, ev, {"8453": {"prepare_entry_quote_fn": quote}},
                                           trace, pre, "8453")
    assert early is not None
    early.worker.join(1)
    assert early.error == (None if expected is not None else "not_approved")
    assert prepared == ([expected] if expected is not None else [])
    assert bool(checked) == (expected is not None)
    conn.close()


def test_funded_executor_reuses_exact_prepared_quote_without_another_ladder(monkeypatch):
    from eth_account import Account
    from sniper.execution import evm_real_seams as seams, evm_swap_executor as executor
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(seams.m, "pending_row", lambda *a: {"strategy": "fixture", "source_user_id": "u"})
    calls = []
    def venue(sell, buy, amount, **kw):
        calls.append((sell, buy, amount))
        return {"sellAmount": str(amount), "buyAmount": "123", "transaction": {"to": "router"}}
    captured = []
    def execute(*a, quote_fn, **kw):
        captured.append(quote_fn("coin", 25))
        return {"status": "aborted", "reason": "fixture"}
    monkeypatch.setattr(executor, "execute_entry_evm", execute)
    ex = seams.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc",
                                      venue_quote_fn=venue, price_fn=lambda: 1000,
                                      usdc_fn=lambda: 300,
                                      http=lambda *a, **kw: (200, {"result": "0x1"}, None))
    ex["warm_quote_price_fn"]()
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: (25, None),
                         quote_fn=ex["prepare_entry_quote_fn"])
    early.worker.join(2)
    assert early.error is None, early.error
    ex["execute_fn"](None, 1, "coin", 25, "now", early_quote=early)
    assert len(calls) == 1, calls
    assert captured[0]["_sniper_early_quote"] is True


def test_production_context_wires_prepared_quote(monkeypatch, tmp_path):
    from test_memecoin_evm_context_keys import _production_context
    assert callable(_production_context(monkeypatch, tmp_path, "56")["prepare_entry_quote_fn"])


@pytest.mark.parametrize("sell_amount", [None, "0", "broken", "1"])
def test_nonempty_prepared_quote_requires_its_own_exact_sell_amount(monkeypatch, sell_amount):
    from eth_account import Account
    from sniper.execution import evm_real_seams as seams, evm_swap_executor as executor
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(seams.m, "pending_row", lambda *a: {"strategy": "fixture", "source_user_id": "u"})
    calls, captured = [], []
    def venue(sell, buy, amount, **kw):
        calls.append(amount)
        q = {"buyAmount": "123", "transaction": {"to": "router"}}
        if len(calls) > 1:
            q["sellAmount"] = str(amount)
        elif sell_amount is not None:
            q["sellAmount"] = sell_amount
        return q
    def execute(*a, quote_fn, **kw):
        captured.append(quote_fn("coin", 25))
        return {"status": "aborted", "reason": "fixture"}
    monkeypatch.setattr(executor, "execute_entry_evm", execute)
    ex = seams.build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc",
                                      venue_quote_fn=venue, price_fn=lambda: 1000,
                                      usdc_fn=lambda: 300,
                                      http=lambda *a, **kw: (200, {"result": "0x1"}, None))
    ex["warm_quote_price_fn"]()
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: (25, None),
                         quote_fn=ex["prepare_entry_quote_fn"])
    early.worker.join(2)
    ex["execute_fn"](None, 1, "coin", 25, "now", early_quote=early)
    assert len(calls) == 2 and not captured[0].get("_sniper_early_quote")


def test_same_source_competition_uses_existing_decision_scope_once():
    from sniper.services.memecoin_entry_knobs import entry_decision
    with entry_decision(origin="onchain:56:tx:coin") as first:
        assert first is True
        with entry_decision(origin="onchain:56:tx:coin") as twin:
            assert twin is False
        with entry_decision(origin="onchain:56:other:coin") as other:
            assert other is True
    with entry_decision(origin="onchain:56:tx:coin") as retry:
        assert retry is True


def test_origin_claim_releases_after_exception_and_never_releases_its_twin():
    from sniper.services.memecoin_entry_knobs import entry_decision
    with pytest.raises(RuntimeError):
        with entry_decision(origin="failure"):
            with entry_decision(origin="failure") as twin:
                assert twin is False
            with entry_decision(origin="failure") as still_twin:
                assert still_twin is False
            raise RuntimeError("fixture")
    with entry_decision(origin="failure") as retry:
        assert retry is True


def test_completed_delivery_marker_is_bounded_expires_and_keeps_retries(monkeypatch):
    from sniper.services import memecoin_entry_knobs as knobs
    now = [10.0]
    monkeypatch.setattr(knobs.time, "monotonic", lambda: now[0])
    for i in range(4100):
        with knobs.entry_decision(origin=("fixture-complete", i), delivery=True) as owner:
            assert owner
    assert len(knobs._DELIVERED_ORIGINS) <= 4096
    key = ("fixture-complete", 4099)
    with knobs.entry_decision(origin=key, delivery=True) as delayed:
        assert delayed is False
    with knobs.entry_decision(origin=key) as retry:
        assert retry is True
    with knobs.entry_decision(origin=("fixture-complete", "another-wallet"), delivery=True) as other:
        assert other is True
    now[0] += 121
    with knobs.entry_decision(origin=key, delivery=True) as expired:
        assert expired is True
    with pytest.raises(RuntimeError):
        with knobs.entry_decision(origin="fixture-delivery-error", delivery=True):
            raise RuntimeError("fixture")
    with knobs.entry_decision(origin="fixture-delivery-error", delivery=True) as after_error:
        assert after_error is True


def test_delayed_duplicate_does_not_quote_again_but_explicit_retry_does(monkeypatch, tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    from test_memecoin_entry_identity import _arm, _approve, _evm_context, _event, NOW
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"})
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    _approve(conn, chains=("56",))
    provider, recorded, queued = [], [], []
    monkeypatch.setattr(runner, "_record_real_decision", lambda c, e, r, **kw: recorded.append(r))
    monkeypatch.setattr(runner, "_maybe_enqueue_retry", lambda c, e, r, now: queued.append(r))
    def decision(*a, **kw):
        provider.append(1)
        return {"status": "aborted", "quote_miss_code": "provider_timeout"}
    monkeypatch.setattr(runner, "real_entry_hook", decision)
    hook = runner.make_real_hook(None, {"56": _evm_context({})},
                                now_fn=lambda: NOW, trade_size=25, max_age_s=120)
    def event():
        from test_memecoin_bsc_source_initiation import source_event, expected_proof
        return source_event(expected_proof())
    assert hook(conn, event())["status"] == "aborted"
    hook(conn, event())
    assert len(provider) == len(recorded) == len(queued) == 1
    assert hook.attempt(conn, event())["status"] == "aborted"
    assert len(provider) == len(recorded) == 2 and len(queued) == 1
    conn.close()


def test_busy_source_owner_does_not_dequeue_a_legitimate_retry(monkeypatch):
    from sniper.services import memecoin_real_runner as runner, memecoin_onchain_lane as lane
    from sniper.settings import memecoin as cfg
    from test_memecoin_entry_identity import _event, NOW
    ev = _event(network_id="56")
    ev.signal_source = "onchain_watch"
    ev.fomo_event_id = "onchain:56:retry-busy-fixture:coin"
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 90)
    monkeypatch.setattr(runner.m, "reap_entry_retries", lambda *a, **kw: [])
    monkeypatch.setattr(runner.m, "due_entry_retries", lambda *a, **kw: [
        {"fomo_event_id": ev.fomo_event_id, "event_json": "fixture"}])
    monkeypatch.setattr(runner, "_retry_event_from_json", lambda *a: ev)
    monkeypatch.setattr(lane, "retry_skip_reason", lambda *a, **kw: None)
    monkeypatch.setattr(runner.m, "dequeue_entry_retry", lambda *a: pytest.fail("busy source dropped retry"))
    monkeypatch.setattr(runner, "_notify_entry_projection", lambda **kw: None)
    # ba2545dbc: the retry lane reads held reservations first; this fixture has no ledger.
    monkeypatch.setattr(runner.m, "pending_open_coin_ids", lambda *a, **kw: set())
    res = runner.run_entry_retry_lane(None, lambda *a: {"status": "duplicate_delivery"},
                                      NOW, include_dips=False)
    assert res["retried"] == 0


def test_expired_original_ladder_returns_typed_miss_and_late_completion_cannot_change_it():
    from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
    started, release = threading.Event(), threading.Event()
    def quote(mint, clip, *, on_started):
        on_started(time.monotonic(), 0.05, {"mint": mint, "usd": clip, "atomic": 123})
        started.set()
        release.wait(1)
        return {"quote": {"buyAmount": "late"}, "mint": mint, "usd": clip}
    quote.early_deadline = True
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: (25, None), quote_fn=quote)
    assert started.wait(1)
    result = early.take("coin", 25)
    assert result["quote"] is None and result["rungs"][0]["blind_reason"] == "ladder_deadline"
    release.set()
    early.worker.join(1)
    assert result["quote"] is None and early.take("coin", 25) is None


def test_dual_bsc_hooks_do_provider_work_once_and_only_owner_records_and_enqueues(monkeypatch, tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from test_memecoin_entry_identity import _arm, _approve, _evm_context, _event, NOW
    from sniper.settings import memecoin as cfg
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"})
    entered, release = threading.Event(), threading.Event()
    provider, recorded, queued = [], [], []
    monkeypatch.setattr(runner, "_record_real_decision", lambda c, e, r, **kw: recorded.append(r))
    monkeypatch.setattr(runner, "_maybe_enqueue_retry", lambda c, e, r, now: queued.append(r))
    def decision(*a, **kw):
        provider.append(1)
        entered.set()
        release.wait(2)
        return {"status": "aborted", "quote_miss_code": "provider_timeout"}
    monkeypatch.setattr(runner, "real_entry_hook", decision)
    path = str(tmp_path / "ledger.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    _approve(conn, chains=("56",))
    conn.close()
    results = []
    def run():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        from test_memecoin_bsc_source_initiation import source_event, expected_proof
        tx_hash = "0x" + "43" * 32
        ev = source_event(expected_proof(tx_hash=tx_hash))
        ev.fomo_event_id = f"onchain:56:{tx_hash}:{ev.mint}"
        hook = runner.make_real_hook(None, {"56": _evm_context({})},
                                    now_fn=lambda: NOW, trade_size=25, max_age_s=120)
        try:
            results.append(hook(conn, ev))
        finally:
            conn.close()
    first = threading.Thread(target=run)
    first.start()
    assert entered.wait(2)
    run()
    release.set()
    first.join(2)
    assert provider == [1] and len(recorded) == len(queued) == 1
    assert {r["status"] for r in results} == {"aborted", "duplicate_delivery"}
