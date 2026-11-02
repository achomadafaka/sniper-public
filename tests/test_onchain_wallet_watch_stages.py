"""The stage stamps must measure the watcher, not disturb it.

Raw Helius frames arrive 0.45 s BEFORE J7, yet this watcher's detection row lands 1.61 s after
block time against J7's 1.25 s, so about 0.7 s is inside our process. These pin the instrument
that will say which stage owns it -- and the instrument has to survive being wrong about the
answer, so the retry sleeps and the 429s are counted SEPARATELY from round-trip time.

No network. No funded ledger.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(tmp_path, **env):
    """Import the watcher with a scratch REPO so its JSONL lands in tmp_path."""
    for k, v in env.items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location(
        "oww_%s" % abs(hash(tuple(sorted(env.items())))),
        ROOT / "scripts" / "research" / "onchain_wallet_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STAGE_LOG = tmp_path / "stages.jsonl"
    # A release worktree has no .env: never reach the real key selection.
    mod.KEY, mod.HTTP_URL, mod.WS_URL = "fake", "https://x.invalid/?api-key=fake", "wss://x.invalid/"
    return mod


# --- the RPC stats, which are the whole point -----------------------------------------------

def test_a_null_retry_is_counted_as_attempts_and_sleep_not_as_latency(tmp_path, monkeypatch):
    """THE PROPERTY. `getTransaction` returns null until the node catches up, and the code sleeps
    0.4 s between tries. A 1.2 s "RPC stage" that is two sleeps is a different problem from one
    slow round trip, and only the split can tell them apart."""
    m = _load(tmp_path)
    calls = {"n": 0}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            calls["n"] += 1
            return json.dumps({"result": None if calls["n"] < 3 else {"ok": 1}}).encode()

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _Resp())
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)       # do not really wait
    stats = {"attempts": 0, "sleep_ms": 0.0, "http_429": 0}
    out = m._http_rpc("getTransaction", ["sig"], stats)
    assert out == {"ok": 1}
    assert stats["attempts"] == 3, stats
    assert stats["sleep_ms"] == pytest.approx(800.0), "two 0.4 s waits, recorded as sleep"


def test_a_429_gets_its_own_counter(tmp_path, monkeypatch):
    """A throttled hour measures the throttle. It must be visible as its own column rather than
    inflating the round trip."""
    m = _load(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(m.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(m.time, "sleep", lambda _s: None)
    stats = {"attempts": 0, "sleep_ms": 0.0, "http_429": 0}
    assert m._http_rpc("getTransaction", ["sig"], stats) is None
    assert stats["http_429"] == 3, stats


def test_stats_are_per_call_so_concurrent_detections_cannot_mix(tmp_path, monkeypatch):
    """`stats` is a parameter and not a module global precisely because detections run as
    concurrent asyncio tasks."""
    m = _load(tmp_path)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"result":{"ok":1}}'

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _Resp())
    a, b = {"attempts": 0}, {"attempts": 0}
    m._http_rpc("getTransaction", ["s1"], a)
    m._http_rpc("getTransaction", ["s2"], b)
    assert a["attempts"] == 1 and b["attempts"] == 1


# --- the writer ------------------------------------------------------------------------------

def test_the_writer_appends_one_json_object_per_detection(tmp_path):
    m = _load(tmp_path)
    m._stage_write({"sig": "a", "rpc_ms": 1.0})
    m._stage_write({"sig": "b", "rpc_ms": 2.0})
    lines = [json.loads(x) for x in open(m.STAGE_LOG)]
    assert [x["sig"] for x in lines] == ["a", "b"]


def test_the_writer_never_raises_into_the_detection_path(tmp_path):
    """A measurement that can break a funded feed is not worth taking."""
    m = _load(tmp_path)
    m.STAGE_LOG = tmp_path / "no_such_dir" / "stages.jsonl"
    m._stage_write({"sig": "a"})            # must not raise


def test_the_knob_off_writes_nothing(tmp_path):
    m = _load(tmp_path, ONCHAIN_WATCH_STAGE_STAMPS="0")
    try:
        m._stage_write({"sig": "a"})
        assert not m.STAGE_LOG.exists()
    finally:
        os.environ["ONCHAIN_WATCH_STAGE_STAMPS"] = "1"


def test_the_commitment_arm_is_readable_and_defaults_to_confirmed(tmp_path):
    """`processed` is a real experiment, not a free win: the code's own comment records that
    confirmed+retry parsed 5/6 live buys where the default parsed 0/25. The default must not
    change silently, and each row records which arm produced it."""
    m = _load(tmp_path)
    assert m.RPC_COMMITMENT == "confirmed"
    os.environ["ONCHAIN_WATCH_RPC_COMMITMENT"] = "processed"
    try:
        m2 = _load(tmp_path, ONCHAIN_WATCH_RPC_COMMITMENT="processed")
        assert m2.RPC_COMMITMENT == "processed"
    finally:
        os.environ.pop("ONCHAIN_WATCH_RPC_COMMITMENT", None)


def test_ms_returns_none_when_the_start_stamp_is_missing(tmp_path):
    """A stage we did not stamp must read as unknown, never as zero."""
    m = _load(tmp_path)
    assert m._ms(None, 1.0) is None
    assert m._ms(1.0, None) is None
    assert m._ms(1.0, 1.5) == 500.0


def test_the_transaction_fetch_accepts_version_1(tmp_path, monkeypatch):
    """Bot routers (FLASHX, term9) send VERSION-1 transactions. A getTransaction capped at
    version 0 is refused with -32015, so those buys were never classified (2026-09-26)."""
    import asyncio
    m = _load(tmp_path)
    seen = []

    def _rpc(method, params, stats):
        seen.append((method, params))
        return None                          # no tx: process_signature returns early

    monkeypatch.setattr(m, "_http_rpc", _rpc)
    asyncio.run(m.process_signature("sig", "wallet", {}, None))
    assert seen and seen[0][0] == "getTransaction"
    assert seen[0][1][1]["maxSupportedTransactionVersion"] >= 1
