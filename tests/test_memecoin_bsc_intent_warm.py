"""C1 intent warm (2026-10-16, Alpha; MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS).

The 56 watcher tells the fast lane at a source log's arrival, before its ~70 ms receipt decode, so
the coin's market stats GET (p50 168 ms, the long pole of the lane's pre-decision wait), facts and
safety read start early. Pins (Alpha's rule 5): best-effort, bounded (one coin at a time, a rate cap,
its own pool), never awaited by the decision when absent, nothing cached off the lane thread, no
new lock on the lane thread; and the signal claims the intent's reads instead of reading again."""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.services import memecoin_bsc_fast_lane as bsc  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from test_memecoin_bsc_fast_lane_live import SUPPLY, _miss, _payload, _signal, live56  # noqa: E402,F401

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def knob(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS", "56")


def _intent(lane, mint, *, pid=None, uid=None, pair=None):
    got = []
    signal = bsc.make_watcher_signal(
        "live", "\0unused",
        send_fn=lambda a, p: got.append(lane["dispatch"].on_datagram(
            p, os.getpid() if pid is None else pid, os.getuid() if uid is None else uid,
            time.time())) or True)
    assert signal.intent(mint, "0x" + "ab" * 32, pair=pair)
    return got[0]


def _counting(lane, *, stats_s=0.0, facts_s=0.0):
    calls = {"stats": 0, "facts": 0, "safety": 0}

    def stats(m):
        calls["stats"] += 1
        time.sleep(stats_s)
        return lane["dex"].stats(m, network_id="56")

    def facts(_m):
        calls["facts"] += 1
        time.sleep(facts_s)
        return 18, SUPPLY

    def safety(_m, usd=None):
        calls["safety"] += 1
        return True, None
    live = lane["live"]
    live.stats_fn, live.inline_facts_fn, live.inline_safety_fn = stats, facts, safety
    return calls


def _settle(live):
    for leg in ("stats", "facts", "safety"):
        future = (live._intent or (None, {}))[1].get(leg)
        if future is not None:
            future.result(timeout=2)


# --- the knob ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [("56", {"56"}), ("4663,56", {"4663", "56"}), ("", set()),
                                       ("56,x", set())])
def test_the_knob_is_a_call_time_chain_list_and_malformed_is_off(monkeypatch, raw, want):
    monkeypatch.setenv("MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS", raw)
    assert cfg.memecoin_evm_lane_intent_warm_chains() == want


# --- the watcher's datagram --------------------------------------------------------------------------

def test_the_watcher_sends_one_intent_only_when_live_and_listed(monkeypatch):
    sent = []
    signal = bsc.make_watcher_signal("live", "\0x", send_fn=lambda a, p: sent.append(p) or True)
    assert signal.intent("0xABC", "0xDEF") is True
    assert sent[0]["message_kind"] == "intent_v1" and sent[0]["mint"] == "0xabc"
    assert sent[0]["tx_hash"] == "0xdef" and sent[0]["chain"] == "56" and sent[0]["sent_epoch"] > 0
    monkeypatch.setenv("MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS", "4663")
    assert signal.intent("0xabc", "0xdef") is False and len(sent) == 1
    shadow = bsc.make_watcher_signal("runner_shadow", "\0x", send_fn=lambda a, p: sent.append(p))
    monkeypatch.setenv("MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS", "56")
    assert shadow.intent("0xabc", "0xdef") is False and len(sent) == 1


def test_a_failing_send_never_raises_into_the_watcher():
    def boom(_a, _p):
        raise OSError("socket gone")
    assert bsc.make_watcher_signal("live", "\0x", send_fn=boom).intent("0xabc", "0xdef") is False


def test_the_watcher_tells_the_lane_before_the_receipt_decode_and_never_on_backfill():
    src = (REPO / "scripts" / "research" / "onchain_evm_wallet_watch.py").read_text()
    body = src.split("    def _on_transfer(", 1)[1]
    call, decode = body.index("intent(log.get(\"address\"), tx_hash,"), body.index("row = push_detection(")
    assert "pair=_topic_address(topics[1])" in body[call:decode]      # the Transfer's sender
    assert call < decode
    assert "if intent is not None and not backfill and not preconf:" in body[:call]


# --- the lane's dispatch -----------------------------------------------------------------------------

@pytest.mark.parametrize("how, drop", [
    ({"uid": os.getuid() + 1}, "foreign_uid"),
    ({"pid": os.getpid() + 1}, "intent_not_the_watcher"),
])
def test_an_intent_is_authenticated_like_a_signal(live56, how, drop):
    lane = live56[0]
    assert _intent(lane, _payload()["mint"], **how) is None
    assert lane["dispatch"].drops[drop] == 1 and lane["live"]._intent is None


def test_an_intent_with_the_knob_off_or_a_bad_mint_starts_nothing(live56, monkeypatch):
    lane = live56[0]
    dispatch = lane["dispatch"]
    payload = {"message_kind": "intent_v1", "mint": "0xnothex", "tx_hash": "0x1", "sent_epoch": 1.0}
    assert dispatch.on_datagram(payload, os.getpid(), os.getuid(), time.time()) is None
    assert dispatch.drops["intent_malformed"] == 1
    monkeypatch.setenv("MEMECOIN_EVM_LANE_INTENT_WARM_CHAINS", "")
    payload["mint"] = _payload()["mint"]
    assert dispatch.on_datagram(payload, os.getpid(), os.getuid(), time.time()) is None
    assert dispatch.drops["intent_off"] == 1 and lane["live"]._intent is None


# --- the signal claims the intent's reads ------------------------------------------------------------

def test_the_signal_claims_the_intents_reads_instead_of_reading_again(live56):
    lane, _ledger, _net, sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    live.quotes[mint] = str(_payload()["quote"]).lower()   # a candidate the tape already noted
    calls = _counting(lane)
    assert _intent(lane, mint) == "started"
    _settle(live)
    rec = _signal(lane)
    assert sender.raws, rec
    assert calls == {"stats": 1, "facts": 1, "safety": 1}
    assert rec["execution_evidence"]["intent"]["legs"] == ["facts", "safety", "stats"]
    assert rec["parallel_safety"] == "intent_used" and live._intent is None
    assert live.intent_counts["claimed"] == 1
    assert not [k for k in rec if k.startswith("_")]           # no futures ride on the row


def test_an_unknown_quote_starts_no_intent_safety_read(live56):
    """e54dddf6 (2026-10-16): a first-seen coin's intent read ran under no quote; the signal noted
    0x4902c5eb, so Gamma B1's rule refused it and the signal read its own, the intent's read only queued
    on the inline host beside the facts. With no quote noted, the intent starts no safety read now."""
    lane, _ledger, _net, sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    live.quotes.pop(mint, None)
    calls = _counting(lane)
    bound = []
    live.inline_safety_fn = lambda m, usd=None: (bound.append(live.quotes.get(m)), calls.__setitem__(
        "safety", calls["safety"] + 1))[-1] or (True, None)
    assert live.warm_intent(mint, pair=_payload()["pool"]) == "started"
    assert "safety" not in live._intent[1]
    _settle(live)
    rec = _signal(lane)
    assert sender.raws, rec
    assert bound == [str(_payload()["quote"]).lower()], "only the signal's own read, under its quote"
    assert calls["stats"] == 1 and calls["facts"] == 1          # quote-free legs still adopted
    assert live.intent_counts["safety_quote_unknown"] == 1
    assert live.intent_counts["safety_quote_mismatch"] == 0 and rec["parallel_safety"] == "used"


def _noted(live, mint, pool):
    live.cache.note_candidates([{"mint": mint, "quote": str(_payload()["quote"]).lower(), "pool": pool}],
                               now=time.time())


def test_a_pair_bound_intent_safety_read_is_adopted_on_a_matching_signal(live56):
    lane, _ledger, _net, sender, _prime = live56
    live, mint, pool = lane["live"], _payload()["mint"], str(_payload()["pool"]).lower()
    _miss(live, mint)
    _noted(live, mint, pool)                                   # the tape noted the coin's pool and quote
    calls = _counting(lane)
    assert _intent(lane, mint, pair=pool) == "started"
    assert live._intent[1]["safety_binding"] == str(_payload()["quote"]).lower()
    _settle(live)
    rec = _signal(lane)
    assert sender.raws, rec
    assert calls["safety"] == 1, "the intent's read, adopted: no second read"
    assert rec["parallel_safety"] == "intent_used" and live.intent_counts["safety_quote_mismatch"] == 0


def test_a_pair_that_is_not_the_noted_pool_starts_no_intent_safety_read(live56):
    """The token came out of another contract (a router, an aggregator, another pool): the signal may
    bind another quote, so no read is started that could not be adopted."""
    lane, _ledger, _net, _sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _noted(live, mint, str(_payload()["pool"]).lower())
    _counting(lane)
    assert _intent(lane, mint, pair="0x" + "12" * 20) == "started"
    assert "safety" not in live._intent[1] and live.intent_counts["safety_quote_unknown"] == 1


def test_an_intent_without_a_pair_keeps_the_quote_only_binding(live56):
    lane, _ledger, _net, _sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    live.quotes[mint] = str(_payload()["quote"]).lower()
    _counting(lane)
    assert _intent(lane, mint) == "started"
    assert live._intent[1]["safety_binding"] == str(_payload()["quote"]).lower()


def test_a_signal_never_claims_another_coins_intent(live56):
    lane, _ledger, _net, sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    asked = []
    _counting(lane)
    stats = live.stats_fn
    live.stats_fn = lambda m: asked.append(m) or stats(m)
    other = "0x" + "12" * 20
    assert live.warm_intent(other) == "started"
    _settle(live)
    rec = _signal(lane)
    assert sender.raws, rec
    assert "intent" not in rec["execution_evidence"] and live._intent[0] == other
    assert asked == [other, mint]                              # its own read, for its own coin


def test_the_decision_waits_only_for_what_the_intent_has_not_finished(live56):
    """The point of C1: the stats GET started at arrival, so the signal waits the remainder."""
    lane = live56[0]
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _counting(lane, stats_s=0.30)
    assert _intent(lane, mint) == "started"
    time.sleep(0.15)
    rec = _signal(lane)
    evidence = rec["execution_evidence"]
    assert evidence["intent"]["stats_read_ms"] >= 280.0
    assert evidence["inline_stats_ms"] < 250.0                 # the signal's own wait, not the read


def test_without_an_intent_the_signal_reads_as_today(live56):
    lane, _ledger, _net, sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    calls = _counting(lane)
    rec = _signal(lane)
    assert sender.raws and calls == {"stats": 1, "facts": 1, "safety": 1}
    assert "intent" not in rec["execution_evidence"]


def test_an_intents_failed_read_refuses_exactly_as_the_signals_own_would(live56):
    lane = live56[0]
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _counting(lane)

    def broken(_m):
        raise RuntimeError("facts host down")
    live.inline_facts_fn = broken
    assert _intent(lane, mint) == "started"
    _settle_quiet(live)
    rec = _signal(lane)
    assert rec["reason"] == "inline_facts_error:RuntimeError", rec


def _settle_quiet(live):
    for leg in ("stats", "facts", "safety"):
        future = (live._intent or (None, {}))[1].get(leg)
        if future is not None:
            try:
                future.result(timeout=2)
            except Exception:
                pass


# --- bounded, best-effort ----------------------------------------------------------------------------

def test_one_coin_at_a_time_and_a_duplicate_is_counted(live56):
    lane = live56[0]
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _counting(lane)
    other = "0x" + "12" * 20
    assert live.warm_intent(mint) == "started"
    assert live.warm_intent(mint) == "duplicate"
    assert live.warm_intent(other) == "busy"
    live._intent[1]["at"] -= live.INTENT_TTL_S + 1             # nobody claimed it: a no-buy waste
    assert live.warm_intent(other) == "started"
    assert live.intent_counts["expired_unused"] == 1


def test_the_rate_cap_holds(live56, monkeypatch):
    lane = live56[0]
    live = lane["live"]
    _counting(lane)
    monkeypatch.setattr(live, "INTENT_PER_MINUTE", 2)
    for i in range(3):
        if live._intent is not None:
            live._intent[1]["at"] -= live.INTENT_TTL_S + 1
        got = live.warm_intent("0x%040x" % (i + 1))
    assert got == "rate_capped" and live.intent_counts["started"] == 2


def test_nothing_is_read_for_a_coin_the_cache_already_holds(live56):
    lane, _ledger, _net, _sender, prime = live56
    calls = _counting(lane)
    prime()
    assert lane["live"].warm_intent(_payload()["mint"]) == "cached"
    assert calls == {"stats": 0, "facts": 0, "safety": 0}


def test_the_warm_returns_at_once_while_its_reads_block(live56):
    """No lock and no wait on the lane thread: the reads run on the intent pool."""
    lane = live56[0]
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    release = threading.Event()
    live.stats_fn = lambda m: release.wait(5) and {}
    live.inline_facts_fn = lambda m: release.wait(5) and (18, SUPPLY)
    live.inline_safety_fn = lambda m, usd=None: release.wait(5) and (True, None)
    t0 = time.perf_counter()
    assert live.warm_intent(mint) == "started"
    assert (time.perf_counter() - t0) < 0.05
    release.set()
    _settle(live)


def test_nothing_from_an_intent_is_cached_until_a_signal_claims_it(live56):
    lane = live56[0]
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _counting(lane)
    assert live.warm_intent(mint) == "started"
    _settle(live)
    assert live.cache.facts(mint) is None and live.cache.stats(mint, now=time.time()) is None
    assert live.cache.verdict(mint) is None


def test_the_heartbeat_carries_the_intent_counts(live56):
    lane = live56[0]
    live = lane["live"]
    _counting(lane)
    _miss(live, _payload()["mint"])
    live.warm_intent(_payload()["mint"])
    out = bsc.lane_counters(live)
    assert out["intent_started"] == 1
    json.dumps(out)


def test_an_intent_facts_read_past_the_signals_deadline_names_the_timeout(live56, monkeypatch):
    """Gamma I2: the adopted facts future's own timeout reads like the signal's own read error."""
    lane = live56[0]
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _counting(lane)
    release = threading.Event()
    live.inline_facts_fn = lambda m: release.wait(5) and (18, SUPPLY)
    monkeypatch.setattr(live, "inline_deadline_s", 0.3)
    assert live.warm_intent(mint) == "started"
    rec = _signal(lane)
    release.set()
    assert rec["reason"] == "inline_facts_error:TimeoutError", rec


def test_a_dead_inline_host_starts_no_intent(live56):
    """Gamma I3."""
    lane = live56[0]
    live = lane["live"]
    _counting(lane)

    class Dead:
        counts = {"transport_errors": 0}

        def dead(self):
            return True
    live.inline_host = Dead()
    assert live.warm_intent(_payload()["mint"]) == "host_dead" and live._intent is None
