"""The ingest topic list must cover every topic the decoder can handle (2026-08-30).

`memecoin_tick_stream` filtered eth_getLogs by a HARDCODED list of eight V2/V3 topics. V4_SWAP was
not among them, so the 4663 tape had ZERO V4 coverage: all 11,864,229 rows carried a 42-char pool
id and not one carried a 66-char V4 pool id, ever. Our funded positions are on V4 — OMEGA, the
+$10.54 winner, has 11 V4 pools and 0 V3 — so the tape had never seen the pool we actually traded.

The decoder was never the problem. `decode_evm_log` calls `decode_v4_log` FIRST, the PoolManager
is configured and matches the one we quote against, and real logs decode into valid rows. Only the
request was short.

THIS IS THE THIRD HARDCODED LIST IN ONE THREAD to fail the same way — after a dex-NAME filter that
discarded real pools, and a fee screen standing in for tradeability. A GUARD CANNOT CATCH WHAT ITS
OWN LIST OMITS.

So this test does NOT restate the topic list — restating it reproduces the bug one layer up. It
DERIVES the decoder's vocabulary from the topic constants the decoder modules declare, and asserts
the request covers all of them. Add a topic constant and a decode branch without adding it to
INGEST_TOPICS, and this goes red.
"""
import re

import sniper.services.memecoin_tick_stream as TS
import sniper.services.memecoin_evm_v4_events as V4E

_TOPIC = re.compile(r"^0x[0-9a-f]{64}$")


def _declared_topics(module):
    """Every module-level constant that IS a topic hash — derived, never restated."""
    out = {}
    for name in dir(module):
        if name.startswith("_"):
            continue
        value = getattr(module, name)
        if isinstance(value, str) and _TOPIC.match(value.lower()):
            out[name] = value.lower()
    return out


# V4_MODIFY_LIQUIDITY is decodable but DELIBERATELY not ingested: nothing reads it, and it costs
# ~11% of the V4 volume increase. Deferral is recorded here so the derivation stays honest rather
# than the exclusion being silent.
DEFERRED = {"V4_MODIFY_LIQUIDITY"}

# REQUESTED, BUT NOT THROUGH INGEST_TOPICS. `INGEST_TOPICS` is the topic list of the MANAGER-filtered
# scan, and the Pons v2 launcher is a different contract, so putting its topic there would fetch
# nothing. It is subscribed and backfilled on its own address filter instead, which the assertion
# below checks by source — the exclusion has to carry its own proof or it is just a silenced guard.
ELSEWHERE = {"PONS_V2_LAUNCHED"}

# DECODED FOR THE VOLUME TAPE ONLY (2026-09-28, Beta T1). The Solidly/Aerodrome Swap decodes to
# `v2_swap` for the 8453 volume tape, which subscribes it under MEMECOIN_ONCHAIN_V2_SWAP_CHAINS. The
# held-pool scan does NOT request it: an Aerodrome pool would read as `v2` with swaps and no Sync
# (its Sync(uint256,uint256) is a different topic), which changes exit and V6 inputs. Adding it here
# needs that Sync topic and a reader test; the proof below keeps the exclusion honest.
VOLUME_TAPE_ONLY = {"SOLIDLY_SWAP"}


def test_every_topic_the_decoder_declares_is_requested():
    declared = {}
    declared.update(_declared_topics(TS))
    declared.update({k: v for k, v in _declared_topics(V4E).items() if k not in DEFERRED})
    declared = {k: v for k, v in declared.items() if k not in ELSEWHERE | VOLUME_TAPE_ONLY}
    assert declared, "found no topic constants — the derivation is broken, not the code"
    requested = {t.lower() for t in TS.INGEST_TOPICS}
    missing = {n: v for n, v in declared.items() if v not in requested}
    assert not missing, (
        "the ingest topic list omits topics the decoder can handle, so those logs are never "
        "FETCHED and their absence looks like quiet: %s" % sorted(missing)
    )


def test_every_topic_excluded_from_the_scan_is_requested_somewhere_else():
    """The exclusion above is only honest while the other request actually exists."""
    import inspect

    from sniper.services import memecoin_volume_writer as vw
    live = inspect.getsource(vw.run) + inspect.getsource(vw.subscriptions)   # run sends subscriptions()
    backfill = inspect.getsource(vw.backfill_window)
    for name in ELSEWHERE:
        assert name in live, "%s is excluded from the scan and not subscribed either" % name
        assert name in backfill, "%s is subscribed live but never backfilled" % name


def test_v4_swap_specifically_is_requested():
    """Named explicitly because this is the omission that cost 4663 its entire V4 tape."""
    assert V4E.V4_SWAP.lower() in {t.lower() for t in TS.INGEST_TOPICS}


def test_the_request_uses_the_canonical_list_not_a_literal():
    """One source of truth. A second literal in the params is how the two drift apart again."""
    import inspect
    src = inspect.getsource(TS)
    i = src.find("params = {")
    assert i >= 0, "could not locate the getLogs params construction"
    body = src[i:i + 260]
    assert "INGEST_TOPICS" in body, (
        "the getLogs params build their own topic literal instead of using INGEST_TOPICS: %r" % body)


def test_decoder_handles_a_v4_swap_shaped_log():
    """The capability existed; only the request was short. Pins that it still does."""
    log = {
        "address": V4E.V4_POOL_MANAGERS["4663"],
        "topics": [V4E.V4_SWAP, "0x" + "ab" * 32, "0x" + "00" * 12 + "11" * 20],
        "data": "0x" + "".join([
            format((1 << 256) - 1000, "064x"), format(2000, "064x"), format(3, "064x"),
            format(4, "064x"), format(5, "064x"), format(6, "064x")]),
        "blockNumber": "0x1", "logIndex": "0x0",
        "transactionHash": "0xdead", "blockHash": "0xbeef",
    }
    row = TS.decode_evm_log("4663", log, "2026-08-30T18:00:00+00:00")
    assert row is not None, "decoder no longer handles a V4 swap"
    assert len(row["pool"]) == 66, "V4 rows must carry the 66-char pool id"


def test_the_volume_tape_only_topics_stay_out_of_the_held_pool_scan(monkeypatch):
    """Knob empty or set, the held-pool scan's topic list is today's: exits and V6 never see an
    Aerodrome pool through this change. The volume tape requests the topic under its knob."""
    from sniper.services import memecoin_volume_writer as vw
    for name in VOLUME_TAPE_ONLY:
        topic = getattr(TS, name).lower()
        assert topic not in {t.lower() for t in TS.INGEST_TOPICS}
        monkeypatch.setenv("MEMECOIN_ONCHAIN_V2_SWAP_CHAINS", "8453")
        assert any(topic in [t.lower() for t in p[1]["topics"][0]] for _rid, p in vw.subscriptions("8453")
                   if len(p) > 1 and isinstance(p[1], dict) and p[1].get("topics"))
