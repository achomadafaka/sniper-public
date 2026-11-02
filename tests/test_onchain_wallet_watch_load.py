"""The confirmed on-chain watcher must not throttle itself blind.

2026-09-26: 39% of its getTransaction calls (4,676 of 11,874 in 26.7 min) came back HTTP 429
('Too Many Requests', Retry-After: 1) on its own Helius key, and each 429 was retried 0.3 s later,
tripling the load. One watched bot wallet (notdecu) sent ~2.5 successful frames a second, 145 of
163 of them invoking a single unidentified program and NO token program, so no token could have
moved. A real 2.93 SOL buy by that wallet (dKKFcS8r...) was lost to the throttle, and with it the
on-chain fallback for slot-lane send 4036.

These pin: (1) a frame that invokes no token program is not fetched, and a truncated or empty log
is fetched anyway; (2) a 429 waits for Retry-After on a cooldown every caller shares, and a
quota 429 is not retried; (3) concurrent fetches are bounded; (4) a WS 429 never moves the
watcher onto the spent research key or a funded pool key; (5) stage rows carry the address, a
wall-clock stamp and the 429 kind, so the before/after can be read.

Frames are REAL (tests/fixtures/memecoin/onchain_watch_frames.json and the truncated PumpSwap
frame in slot_signal_frames.json). No network. No funded ledger.
"""
import asyncio
import importlib.util
import io
import json
import shutil
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)

ROOT = Path(__file__).resolve().parents[1]
FRAMES = json.loads((ROOT / "tests/fixtures/memecoin/onchain_watch_frames.json").read_text())
SLOT_FRAMES = json.loads((ROOT / "tests/fixtures/memecoin/slot_signal_frames.json").read_text())
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def _load(tmp_path, src=ROOT / "scripts/research/onchain_wallet_watch.py", name="oww_load"):
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STAGE_LOG = tmp_path / "stages.jsonl"
    mod.HEARTBEAT = tmp_path / "hb.json"
    # A release worktree has no .env: a test must never reach the real key selection.
    mod.KEY, mod.HTTP_URL, mod.WS_URL = "fake", "https://x.invalid/?api-key=fake", "wss://x.invalid/"
    return mod


def _http_429(body=b"Too Many Requests", retry_after="1"):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://x.invalid/", 429, "Too Many Requests", headers,
                                  io.BytesIO(body))


class _Ok:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"result":{"ok":1}}'


# --- (1) the prefilter -----------------------------------------------------------------------

def test_a_real_token_2022_buy_frame_is_fetched(tmp_path):
    """untaxxable's pump.fun BuyExactSolIn moves the coin through Token-2022 ONLY. A filter that
    looked for the classic token program alone would drop this buy."""
    m = _load(tmp_path)
    logs = FRAMES["token2022_buy"]["logs"]
    joined = "\n".join(logs)
    assert TOKEN_PROGRAMS[1] in joined and TOKEN_PROGRAMS[0] not in joined   # the fixture's point
    assert m.frame_may_be_buy(logs) is True


def test_a_real_frame_with_no_token_program_is_not_fetched(tmp_path):
    m = _load(tmp_path)
    assert m.frame_may_be_buy(FRAMES["no_token_program"]["logs"]) is False
    assert m.frame_may_be_buy(FRAMES["fee_distribution"]["logs"]) is False


def test_a_truncated_log_is_fetched_even_when_its_visible_part_shows_no_token_program(tmp_path):
    """Solana cuts logs at ~10 KB and ends them with 'Log truncated'. The real PumpSwap frame is
    cut this way. Its visible part DOES contain token invokes, so the whole frame cannot reach the
    branch: the value that makes it reachable is the frame's own prefix BEFORE its first token
    invoke, followed by its own real truncation line."""
    m = _load(tmp_path)
    logs = SLOT_FRAMES["amm_buy"]["params"]["result"]["value"]["logs"]
    assert logs[-1] == "Log truncated"
    first_token = next(i for i, line in enumerate(logs)
                       if any(p + " invoke" in line for p in TOKEN_PROGRAMS))
    prefix = logs[:first_token]
    assert m.frame_may_be_buy(logs) is True
    assert m.frame_may_be_buy(prefix + [logs[-1]]) is True      # truncated: unknown, fetch it
    assert m.frame_may_be_buy(prefix) is False                  # same prefix, not truncated


@pytest.mark.parametrize("logs", [None, []])
def test_missing_logs_are_unknown_and_fetched(tmp_path, logs):
    m = _load(tmp_path)
    assert m.frame_may_be_buy(logs) is True


def test_the_socket_loop_skips_the_no_token_frame_and_fetches_the_buy(tmp_path, monkeypatch):
    """The predicate is only worth something if the loop calls it before getTransaction."""
    m = _load(tmp_path)
    m.DB = tmp_path / "w.db"
    addr = FRAMES["token2022_buy"]["address"]

    def note(key):
        v = FRAMES[key]
        return {"jsonrpc": "2.0", "method": "logsNotification",
                "params": {"subscription": 77, "result": {"context": {"slot": v["slot"]},
                           "value": {"signature": v["signature"], "err": v["err"], "logs": v["logs"]}}}}

    queue = [{"jsonrpc": "2.0", "id": 1, "result": 77}, note("no_token_program"),
             note("token2022_buy")]

    class _WS:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send(self, _msg): pass
        async def recv(self):
            if queue:
                return json.dumps(queue.pop(0))
            await asyncio.sleep(0.02)
            raise asyncio.TimeoutError

    fetched = []

    async def _process(sig, *a, **k):
        fetched.append(sig)

    monkeypatch.setattr(m.websockets, "connect", lambda *a, **k: _WS())
    monkeypatch.setattr(m, "process_signature", _process)
    asyncio.run(m._run(0.3, [addr], {}))
    assert fetched == [FRAMES["token2022_buy"]["signature"]]
    beat = json.loads(m.HEARTBEAT.read_text())
    assert beat["frames"] == 2 and beat["dropped_no_token_program"] == 1, beat


# --- (2) 429 handling ------------------------------------------------------------------------

def test_a_rate_429_waits_retry_after_on_the_shared_cooldown(tmp_path, monkeypatch):
    m = _load(tmp_path)
    answers = [_http_429(retry_after="1"), _Ok()]

    def _urlopen(*a, **k):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    slept = []
    monkeypatch.setattr(m.urllib.request, "urlopen", _urlopen)
    patch_local(monkeypatch, "sleep", lambda s: slept.append(s))
    stats = {"attempts": 0, "sleep_ms": 0.0, "http_429": 0}
    t0 = time.monotonic()
    assert m._http_rpc("getTransaction", ["sig"], stats) == {"ok": 1}
    assert stats["http_429"] == 1 and stats["http_429_kind"] == "rate", stats
    assert slept and max(slept) >= 0.9, slept           # waited ~Retry-After, not 0.3 s
    assert m._cooldown_until() >= t0 + 0.9              # and every other caller sees it


def test_the_cooldown_delays_the_next_callers_first_attempt(tmp_path, monkeypatch):
    m = _load(tmp_path)
    slept = []
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *a, **k: _Ok())
    patch_local(monkeypatch, "sleep", lambda s: slept.append(s))
    m._note_rate_limit(0.5)
    m._note_rate_limit(0.1)                             # a shorter answer never shortens it
    assert m._http_rpc("getTransaction", ["sig"], {}) == {"ok": 1}
    assert slept and 0.35 <= slept[0] <= 0.5, slept


def test_a_quota_429_is_not_retried(tmp_path, monkeypatch):
    """'max usage reached' is the monthly quota: a retry never helps and only burns the key."""
    m = _load(tmp_path)
    calls = []

    def _urlopen(*a, **k):
        calls.append(1)
        raise _http_429(body=b'{"error":"max usage reached"}', retry_after=None)

    monkeypatch.setattr(m.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    stats = {}
    assert m._http_rpc("getTransaction", ["sig"], stats) is None
    assert len(calls) == 1 and stats["http_429_kind"] == "quota", stats


@pytest.mark.parametrize("value", [None, "", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "-4"])
def test_an_unreadable_retry_after_waits_the_default(tmp_path, value):
    m = _load(tmp_path)
    assert m._retry_after_s(value) == 1.0


def test_a_huge_retry_after_is_capped(tmp_path):
    m = _load(tmp_path)
    assert m._retry_after_s("600") == 5.0


# --- (3) bounded concurrency -----------------------------------------------------------------

def test_concurrent_fetches_are_bounded(tmp_path, monkeypatch):
    """Each frame used to start its own getTransaction on the executor, unbounded, so a burst of
    frames was a burst of requests. Ten threads must never have more than three in flight."""
    m = _load(tmp_path)
    live, peak, lock = [0], [0], threading.Lock()
    real_sleep = time.sleep

    def _urlopen(*a, **k):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        real_sleep(0.05)
        with lock:
            live[0] -= 1
        return _Ok()

    monkeypatch.setattr(m.urllib.request, "urlopen", _urlopen)
    threads = [threading.Thread(target=m._http_rpc, args=("getTransaction", ["s"], {}))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert 1 <= peak[0] <= 3, peak


# --- (4) key selection -----------------------------------------------------------------------

DEDICATED = "https://mainnet.helius-rpc.com/?api-key=dedicated0000000000000000000001"
SPENT = "https://mainnet.helius-rpc.com/?api-key=spentsingular00000000000000002"
HEALTHY = "https://mainnet.helius-rpc.com/?api-key=healthyfirst0000000000000000003"
SECOND = "https://mainnet.helius-rpc.com/?api-key=secondresearch000000000000004"
POOL = "https://mainnet.helius-rpc.com/?api-key=fundedpool00000000000000000005"
CUSTODY = "https://mainnet.helius-rpc.com/?api-key=custody000000000000000000000006"


def _load_with_env(tmp_path, lines, name):
    """A copy of the watcher whose REPO is tmp_path, so it reads THIS .env at import."""
    dst = tmp_path / "scripts" / "research" / "onchain_wallet_watch.py"
    dst.parent.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/research/onchain_wallet_watch.py", dst)
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")
    mod = _load(tmp_path, src=dst, name=name)
    # The .env is read from the STATE root since 2026-09-28 (release pinning); the suite's own
    # SNIPER_STATE_ROOT points elsewhere, so this copy's state root is set to its tmp repo.
    mod.STATE_ROOT = tmp_path
    mod.KEY = mod.HTTP_URL = mod.WS_URL = None        # these tests exercise the real selection
    return mod


def _funded_lines():
    return [f"MEMECOIN_RESEARCH_HELIUS_RPC_URL={SPENT}",
            f"MEMECOIN_RESEARCH_HELIUS_RPC_URLS={HEALTHY},{SECOND}",
            f"MEMECOIN_SOLANA_RPC_POOL_URLS=https://api.mainnet-beta.solana.com,{POOL}",
            f"MEMECOIN_SOLANA_RPC_URL={CUSTODY}"]


def test_a_ws_429_streak_never_selects_a_spent_or_funded_url(tmp_path):
    m = _load_with_env(tmp_path, [f"ONCHAIN_WATCH_HELIUS_RPC_URL={DEDICATED}"] + _funded_lines(),
                       "oww_keys_a")
    m._select_key()
    for _ in range(6):
        m._rotate_key("server rejected WebSocket connection: HTTP 429")
        assert m.HTTP_URL == DEDICATED
        assert "dedicated0000000000000000000001" in m.WS_URL


def test_without_a_dedicated_key_the_watcher_uses_the_first_healthy_research_key(tmp_path):
    m = _load_with_env(tmp_path, _funded_lines(), "oww_keys_b")
    m._select_key()
    assert m.HTTP_URL == HEALTHY
    for _ in range(4):
        m._rotate_key("429")
        assert m.HTTP_URL == HEALTHY


def test_a_watcher_key_that_is_also_a_funded_key_refuses_to_start(tmp_path):
    m = _load_with_env(tmp_path, [f"ONCHAIN_WATCH_HELIUS_RPC_URL={POOL}"] + _funded_lines(),
                       "oww_keys_c")
    with pytest.raises(SystemExit):
        m._select_key()


def test_only_the_spent_singular_key_refuses_at_start_not_at_import(tmp_path):
    """Importing never needs a key (tests and report() import it without one); run() refuses."""
    m = _load_with_env(tmp_path, [f"MEMECOIN_RESEARCH_HELIUS_RPC_URL={SPENT}",
                                  f"MEMECOIN_SOLANA_RPC_POOL_URLS={POOL}"], "oww_keys_d")
    assert m.HTTP_URL is None
    with pytest.raises(SystemExit):
        m._select_key()


# --- (5) the stage rows the before/after is read from ----------------------------------------

def test_a_stage_row_carries_the_address_a_wall_clock_stamp_and_the_429_kind(tmp_path, monkeypatch):
    m = _load(tmp_path)

    def _rpc(method, params, stats):
        stats.update(attempts=3, http_429=3, http_429_kind="rate")
        return None

    monkeypatch.setattr(m, "_http_rpc", _rpc)
    before = time.time()
    asyncio.run(m.process_signature("sig", "Owner1111", {}, None))
    row = json.loads(m.STAGE_LOG.read_text().splitlines()[-1])
    assert row["outcome"] == "no_tx" and row["addr"] == "Owner1111"
    assert row["http_429_kind"] == "rate" and before <= row["ts"] <= time.time() + 1
