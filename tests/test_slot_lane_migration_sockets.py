"""Two migration sockets (operator order via Alpha, 2026-10-07): the keyless public socket stalls (it
keeps the subscription and stops delivering), so a second socket on the research Helius key runs
beside it. Each socket keeps its own silence check and status; every frame is taken once; a pooled
stream flat for 10 minutes while the chain shows a newer migration pages once."""
import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_slot_signal as sig

HERE = Path(__file__).parent / "fixtures" / "memecoin"
MIGRATIONS = json.loads((HERE / "pump_migrate_frames.json").read_text())
DOUGHERTY_POOL = "qNadZjDebjUwHS5ZRcLTLtoGk4wAU15kydQLFSJGUUe"
USDC_POOL = "46NeEdphMMNxRtZ8quLuyi8vod5k3LjtZJ1u7oFNVqhc"


def _notification(frame):
    return {"jsonrpc": "2.0", "method": "logsNotification",
            "params": {"subscription": 77, "result": {"context": {"slot": frame["slot"]},
                                                      "value": {"signature": frame["signature"],
                                                                "err": None, "logs": frame["logs"]}}}}


class _WS:
    """Answers the subscription, delivers `frames`, then stays connected and silent."""

    def __init__(self, frames):
        self.out, self.frames = asyncio.Queue(), frames

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, text):
        await self.out.put({"jsonrpc": "2.0", "id": json.loads(text)["id"], "result": 77})
        while self.frames:                       # a shared list: a reconnect delivers nothing again
            await self.out.put(self.frames.pop(0))

    async def recv(self):
        if self.out.empty():
            await asyncio.sleep(10)
        return json.dumps(await self.out.get())


def _run(sockets_frames, *, until, silence_s=0.05):
    stop, pools, status, connects = threading.Event(), [], {}, []
    frames = {url: fr for url, fr in sockets_frames.items()}

    def connect(url):
        connects.append(url)
        return _WS(frames[url])
    fanin = sig.start_migration_listeners(
        pools.append, sockets={"public": ("wss://public", None), "research": ("wss://research", None)},
        status=status, stop=stop, connect=connect, silence_s=silence_s, backoff_s=0.0, watch=False)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not until(fanin, status):
        time.sleep(0.01)
    stop.set()
    time.sleep(0.2)
    return fanin, status, pools, connects


def test_a_stalled_socket_is_covered_by_the_other_and_keeps_its_own_silence_check():
    good = [_notification(MIGRATIONS["wsol"]), _notification(MIGRATIONS["usdc"])]
    fanin, status, pools, connects = _run(
        {"wss://public": [], "wss://research": good},
        until=lambda f, s: f.stats["unique_frames"] == 2 and s.get("reconnects", 0) >= 1)
    assert pools == [DOUGHERTY_POOL, USDC_POOL]
    assert status["frames"] == 0 and status["reconnects"] >= 1 and "silent" in status["last_error"]
    assert status["research"]["frames"] == 2 and status["research"]["pools"] == 2
    assert fanin.stats["first_by"] == {"public": 0, "research": 2}       # seeded before any frame
    assert status["pooled"]["sockets"] == ["public", "research"]


def test_the_same_migration_on_both_sockets_is_taken_once():
    both = [_notification(MIGRATIONS["wsol"]), _notification(MIGRATIONS["already_migrated"])]
    fanin, status, pools, _ = _run(
        {"wss://public": list(both), "wss://research": list(both)},
        until=lambda f, s: f.stats["unique_frames"] + f.stats["duplicates"] == 4)
    assert pools == [DOUGHERTY_POOL]                                    # one pool, one hand-over
    assert fanin.stats["unique_frames"] == 2 and fanin.stats["duplicates"] == 2 and fanin.stats["pools"] == 1
    assert status["frames"] == 2 and status["research"]["frames"] == 2   # each socket counts its own


def test_a_socket_without_a_usable_url_is_recorded_and_the_other_still_runs():
    stop, status = threading.Event(), {}
    sig.start_migration_listeners(lambda p: None, sockets={"public": ("wss://public", None),
                                                          "research": (None, "no_research_key")},
                                  status=status, stop=stop, connect=lambda url: _WS([]), watch=False)
    stop.set()
    assert status["research"] == {"disabled": "no_research_key"} and status["pooled"]["sockets"] == ["public"]


def test_the_funded_key_never_becomes_a_migration_socket(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=FUNDED")
    stop, status = threading.Event(), {}
    sig.start_migration_listeners(lambda p: None, sockets={
        "public": ("wss://public", None), "research": ("wss://mainnet.helius-rpc.com/?api-key=FUNDED", None)},
        status=status, stop=stop, connect=lambda url: _WS([]), watch=False)
    stop.set()
    assert "disabled" in status["research"] and status["pooled"]["sockets"] == ["public"]


@pytest.mark.parametrize("env,expect", [
    ({}, (None, "no_research_key")),
    ({"MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=R",
      "MEMECOIN_SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=R"}, (None, "research_key_is_funded")),
    ({"MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=R",
      "ONCHAIN_WATCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=R"},
     (None, "research_key_is_the_signal_key")),
    ({"MEMECOIN_RESEARCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=R",
      "ONCHAIN_WATCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=S",
      "MEMECOIN_SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=F"},
     ("wss://mainnet.helius-rpc.com/?api-key=R", None)),
])
def test_the_research_socket_is_never_the_funded_or_the_signal_key(env, expect):
    assert sig.research_migration_ws(env) == expect


# --- the deafness alarm --------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 5000.0

    def monotonic(self):
        return self.t


def _watch(chain):
    clock = _Clock()
    wall = SimpleNamespace(t=1_791_400_000.0)
    fanin = sig.MigrationFanIn(lambda p: None, clock=clock)
    reads, pages = [], []

    def newest():
        reads.append(1)
        out = chain()
        if isinstance(out, Exception):
            raise out
        return out
    w = sig.MigrationDeafWatch(fanin, chain_newest_fn=newest, alert_fn=pages.append, clock=clock,
                               wall=lambda: wall.t, flat_s=600, check_s=60)
    return w, fanin, clock, wall, reads, pages


def _advance(clock, wall, s):
    clock.t += s
    wall.t += s


def test_ten_flat_minutes_with_a_newer_chain_migration_page_once_and_recover(caplog):
    chain = {"v": None}
    w, fanin, clock, wall, reads, pages = _watch(lambda: chain["v"])
    fanin.frame("public", "OLD", None)
    _advance(clock, wall, 300)
    assert w.check() is False and reads == []                    # flat 5 min: the chain is not asked
    _advance(clock, wall, 301)
    chain["v"] = ("NEWSIG", wall.t - 30)                          # on chain 30 s ago, no socket heard it
    with caplog.at_level("WARNING"):
        assert w.check() is True
    assert len(pages) == 1 and "DEAF" in pages[0] and w.stats["deaf"] and w.stats["episodes"] == 1
    assert any("DEAF" in r.getMessage() for r in caplog.records)
    _advance(clock, wall, 120)
    assert w.check() is True and len(pages) == 1 and len(reads) == 1   # one page per episode
    fanin.frame("research", "NEWSIG2", None)
    assert w.check() is False and w.stats["deaf"] is False


@pytest.mark.parametrize("case", ["seen", "older", "none", "error"])
def test_a_quiet_chain_or_an_unreadable_one_is_not_deafness(case):
    w, fanin, clock, wall, reads, pages = _watch(lambda: None)
    fanin.frame("public", "LAST", None)
    _advance(clock, wall, 700)
    # "seen": the chain's newest IS our last frame, its blockTime 10 s later than our receipt clock
    # (host clock behind the chain): only the seen guard tells it from a missed migration.
    w.chain_newest_fn = {"seen": lambda: ("LAST", wall.t - 690),
                         "older": lambda: ("OTHER", wall.t - 701),
                         "none": lambda: None,
                         "error": lambda: (_ for _ in ()).throw(RuntimeError("429"))}[case]
    assert w.check() is False and pages == []
    assert w.stats["chain_errors"] == (1 if case == "error" else 0)


def test_the_chain_is_read_at_most_once_a_minute_while_flat():
    w, fanin, clock, wall, reads, pages = _watch(lambda: None)
    _advance(clock, wall, 700)
    for _ in range(5):
        w.check()
        _advance(clock, wall, 10)
    assert len(reads) == 1
    _advance(clock, wall, 20)
    w.check()
    assert len(reads) == 2


def test_the_heartbeat_names_both_sockets_and_the_pooled_alarm(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    now = time.monotonic()
    run.write_heartbeat({}, True, path=str(path), slot_lane_status={
        "mode": "live", "signal": {},
        "prewarm": {"connected": True, "frames": 0, "pools": 0, "reconnects": 3, "last_frame_mono": None,
                    "last_error": "silent 120 s",
                    "research": {"connected": True, "frames": 9, "pools": 4, "reconnects": 0,
                                 "last_frame_mono": now - 2.0, "last_error": None, "junk": 1},
                    "pooled": {"sockets": ["public", "research"],
                               "fanin": {"unique_frames": 9, "duplicates": 0, "pools": 4,
                                         "last_unique_mono": now - 2.0, "first_by": {"research": 9}},
                               "watch": {"deaf": False, "deaf_since": None, "episodes": 0, "chain_reads": 0,
                                         "chain_errors": 0}}},
        "pool_cache": {}})
    got = json.loads(path.read_text())["slot_lane"]["prewarm"]
    assert got["frames"] == 0 and got["research"]["frames"] == 9 and "junk" not in got["research"]
    pooled = got["pooled"]
    assert pooled["unique_frames"] == 9 and pooled["first_by"] == {"research": 9} and pooled["deaf"] is False
    assert 1.5 < pooled["last_unique_age_s"] < 10
