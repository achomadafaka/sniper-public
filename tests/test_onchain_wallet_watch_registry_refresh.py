"""The confirmed watcher subscribes the registry's verified addresses of ARMED Solana profiles and
refreshes them in process (registry step 3, docs/superpowers/specs/2026-09-26-solana-address-
registry-design.md).

Before: `load_watchlist()` read memecoin_wallet_addresses once per start. On 2026-09-27 00:35Z that
was 24 addresses of which 9 were armed, while 6 armed wallets were not watched at all; Tekkerrss'
$489 buy at 23:12:00Z on 09-26 was one of them. Now the set comes from memecoin_address_registry,
is re-read every REFRESH_S off the frame path, and is diffed against the live subscriptions.

No network, no funded ledger: fixtures in tmp, a fake socket, fake keys (a release tree has no .env).
"""
import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "scripts/research/onchain_wallet_watch.py"
FRAMES = json.loads((ROOT / "tests/fixtures/memecoin/onchain_watch_frames.json").read_text())
NOW = "2026-09-27T00:40:00+00:00"
A, B, C, D = ("A" * 43 + "1", "B" * 43 + "2", "C" * 43 + "3", "D" * 43 + "4")


def _load(tmp_path, src=SRC, name="oww_registry"):
    spec = importlib.util.spec_from_file_location(name, src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STAGE_LOG = tmp_path / "stages.jsonl"
    mod.HEARTBEAT = tmp_path / "hb.json"
    mod.KEY, mod.HTTP_URL, mod.WS_URL = "fake", "https://x.invalid/?api-key=fake", "wss://x.invalid/"
    return mod


def _ledger(path, *, registry=True):
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    if not registry:
        c.execute("DROP TABLE memecoin_address_registry_events")
        c.execute("DROP TABLE memecoin_address_registry")
    c.execute("CREATE TABLE IF NOT EXISTS memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, "
              "handle TEXT, network_id TEXT, address TEXT, verify_status TEXT)")
    for uid, handle, approved, chains in (("u-a", "alice", 1, "solana"), ("u-b", "bob", 1, "solana"),
                                          ("u-c", "carol", 0, None), ("u-d", "dave", 1, "56")):
        c.execute("INSERT INTO memecoin_wallets (source_user_id, handle, real_approved, "
                  "real_chains, paused, excluded) VALUES (?,?,?,?,0,0)", (uid, handle, approved, chains))
    if registry:
        for uid, addr, status in (("u-a", A, "verified"), ("u-b", B, "verified"),
                                  ("u-b", C, "candidate"), ("u-c", C, "verified"),
                                  ("u-d", D, "verified")):
            m.record_address_evidence(c, source_user_id=uid, network_id="solana", address=addr,
                                      status=status, evidence_kind="test", now_iso=NOW, actor="t")
    c.execute("INSERT INTO memecoin_wallet_addresses VALUES ('u-c','carol','solana',?,'live_confirmed')",
              (C,))
    c.commit()
    c.close()
    return path


# --- which addresses ---------------------------------------------------------------------------

def test_only_verified_addresses_of_profiles_armed_on_solana_are_watched(tmp_path):
    w = _load(tmp_path)
    w.DB = _ledger(tmp_path / "l.db")
    watch, over_cap = w.desired_watchlist()
    assert set(watch) == {A, B}, "candidate, unarmed and armed-elsewhere addresses are not watched"
    assert watch[A] == {"uid": "u-a", "handle": "alice", "status": "registry_verified"}
    assert over_cap == 0


def test_a_ledger_without_the_registry_uses_the_legacy_loader(tmp_path):
    w = _load(tmp_path)
    w.DB = _ledger(tmp_path / "l.db", registry=False)
    watch, _ = w.desired_watchlist()
    assert set(watch) == {C}


def test_a_failed_read_returns_none_so_the_caller_keeps_its_set(tmp_path):
    w = _load(tmp_path)
    w.DB = tmp_path / "missing" / "nowhere.db"
    assert w.desired_watchlist() is None


def test_an_empty_armed_set_keeps_the_current_set(tmp_path):
    w = _load(tmp_path)
    w.DB = _ledger(tmp_path / "l.db")
    c = sqlite3.connect(str(w.DB))
    c.execute("UPDATE memecoin_wallets SET real_approved = 0")
    c.commit()
    c.close()
    assert w.desired_watchlist() is None


@pytest.mark.parametrize("raw,kept,over", [("1", [A], 1), ("100", [A, B], 0), ("junk", [A, B], 0),
                                           ("0", [A, B], 0)])
def test_the_hard_cap_keeps_address_order_and_counts_the_rest(tmp_path, monkeypatch, raw, kept, over):
    w = _load(tmp_path)
    w.DB = _ledger(tmp_path / "l.db")
    monkeypatch.setenv("MEMECOIN_ONCHAIN_WATCH_MAX_SUBS", raw)
    watch, over_cap = w.desired_watchlist()
    assert list(watch) == kept and over_cap == over


# --- the subscription bookkeeping ---------------------------------------------------------------

INFO = {"uid": "u", "handle": "h", "status": "registry_verified"}


def _ack_all(subs, requests, first_sub=100):
    for i, req in enumerate(requests):
        result = first_sub + i if req["method"] == "logsSubscribe" else True
        assert subs.on_reply({"jsonrpc": "2.0", "id": req["id"], "result": result})


def test_a_refresh_subscribes_the_added_and_unsubscribes_the_removed(tmp_path):
    subs = _load(tmp_path).Subscriptions()
    first = subs.plan({A: INFO, B: INFO})
    assert [(r["method"], r["params"][0]) for r in first] == [
        ("logsSubscribe", {"mentions": [A]}), ("logsSubscribe", {"mentions": [B]})]
    _ack_all(subs, first)
    assert subs.plan({A: INFO, B: INFO}) == [], "an unchanged set sends nothing"
    second = subs.plan({B: INFO, C: INFO})
    assert sorted((r["method"], json.dumps(r["params"])) for r in second) == sorted([
        ("logsSubscribe", json.dumps([{"mentions": [C]}, {"commitment": "confirmed"}])),
        ("logsUnsubscribe", json.dumps([100]))])


def test_a_frame_racing_a_removal_still_resolves_until_the_unsubscribe_is_acked(tmp_path):
    subs = _load(tmp_path).Subscriptions()
    _ack_all(subs, subs.plan({A: {"uid": "u-a", "handle": "alice", "status": "x"}}))
    unsub = subs.plan({})
    assert subs.lookup(100) == (A, {"uid": "u-a", "handle": "alice", "status": "x"})
    _ack_all(subs, unsub)
    assert subs.lookup(100) is None


def test_a_failed_subscribe_is_counted_and_asked_again(tmp_path):
    subs = _load(tmp_path).Subscriptions()
    req = subs.plan({A: INFO})
    assert subs.plan({A: INFO}) == [], "an in-flight subscribe is not sent twice"
    assert subs.on_reply({"jsonrpc": "2.0", "id": req[0]["id"],
                          "error": {"code": -32602, "message": "Invalid param"}})
    assert subs.failed == 1 and subs.lookup(100) is None
    assert [r["method"] for r in subs.plan({A: INFO})] == ["logsSubscribe"]


def test_a_failed_unsubscribe_keeps_the_subscription_and_asks_again(tmp_path):
    subs = _load(tmp_path).Subscriptions()
    _ack_all(subs, subs.plan({A: INFO}))
    req = subs.plan({})
    assert subs.on_reply({"jsonrpc": "2.0", "id": req[0]["id"], "result": False})
    assert subs.lookup(100) == (A, INFO)
    assert [r["method"] for r in subs.plan({})] == ["logsUnsubscribe"]


def test_a_reply_that_is_not_ours_is_not_swallowed(tmp_path):
    subs = _load(tmp_path).Subscriptions()
    assert subs.on_reply({"jsonrpc": "2.0", "method": "logsNotification", "params": {}}) is False
    assert subs.on_reply({"jsonrpc": "2.0", "id": 999, "result": 5}) is False


# --- the real loop against a fake socket --------------------------------------------------------

class _FakeSocket:
    """Acks every request. An unsubscribe's ack is preceded by a frame for that subscription and
    followed by another, so the loop meets a frame racing a removal on both sides of the ack."""

    def __init__(self):
        self.sent, self.queue, self.sub_of = [], [], {}
        self._next_sub, self._sig = 100, 0

    def _frame(self, sub):
        self._sig += 1
        return {"jsonrpc": "2.0", "method": "logsNotification", "params": {
            "subscription": sub, "result": {"value": {
                "signature": "sig%d" % self._sig, "err": None,
                "logs": FRAMES["token2022_buy"]["logs"]}}}}

    async def send(self, text):
        req = json.loads(text)
        self.sent.append(req)
        if req["method"] == "logsSubscribe":
            sub, self._next_sub = self._next_sub, self._next_sub + 1
            self.sub_of[req["params"][0]["mentions"][0]] = sub
            self.queue += [{"jsonrpc": "2.0", "id": req["id"], "result": sub}, self._frame(sub)]
        else:
            sub = req["params"][0]
            self.queue += [self._frame(sub), {"jsonrpc": "2.0", "id": req["id"], "result": True},
                           self._frame(sub)]

    async def recv(self):
        if self.queue:
            return json.dumps(self.queue.pop(0))
        await asyncio.sleep(0.005)
        return json.dumps({"jsonrpc": "2.0", "method": "idle"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_the_loop_follows_the_registry_and_a_failed_refresh_keeps_the_set(tmp_path, monkeypatch):
    w = _load(tmp_path)
    w.DB = tmp_path / "detections.db"
    w.REFRESH_S = 0.0
    sock = _FakeSocket()
    monkeypatch.setattr(w.websockets, "connect", lambda *a, **k: sock)
    infos = {a: {"uid": "u-" + a[0], "handle": a[0].lower(), "status": "registry_verified"}
             for a in (A, B, C)}
    answers = [({A: infos[A], B: infos[B]}, 0), ({B: infos[B], C: infos[C]}, 0), None]
    calls = []

    def desired():
        calls.append(1)
        return answers.pop(0) if answers else None

    monkeypatch.setattr(w, "desired_watchlist", desired)
    seen = []

    async def record(sig, addr, info, wdb, frame_mono=None, *, sol_usd=None):
        seen.append((sig, addr, info["handle"]))

    monkeypatch.setattr(w, "process_signature", record)
    asyncio.run(w._run(0.4, None, {}))

    methods = [(r["method"], (r["params"][0]["mentions"][0] if r["method"] == "logsSubscribe"
                              else r["params"][0])) for r in sock.sent]
    assert methods == [("logsSubscribe", A), ("logsSubscribe", B), ("logsSubscribe", C),
                       ("logsUnsubscribe", sock.sub_of[A])], methods
    assert len(calls) >= 4, "the refresh kept running after it returned None"
    by_addr = {}
    for _sig, addr, handle in seen:
        by_addr.setdefault(addr, []).append(handle)
    # A: the frame after its subscribe and the one racing its unsubscribe; NOT the one after the ack.
    assert by_addr[A] == ["a", "a"]
    assert by_addr[B] == ["b"] and by_addr[C] == ["c"]
    beat = json.loads(w.HEARTBEAT.read_text())
    assert list(beat)[:2] == ["ts", "subs"], "the watchdog's sed reads the first plain subs"
    assert beat["subs"] == 2 and beat["subs_desired"] == 2 and beat["subs_failed"] == 0


# --- the release handover's readiness line (operator order 2026-10-07) ---------------------------

def _run_ready(tmp_path, monkeypatch, sock):
    import os
    w = _load(tmp_path)
    w.DB = tmp_path / "detections.db"
    w.READY = tmp_path / "ready"
    w.REFRESH_S = 3600.0
    monkeypatch.setattr(w.websockets, "connect", lambda *a, **k: sock)
    infos = {a: {"uid": "u-" + a[0], "handle": a[0].lower(), "status": "registry_verified"}
             for a in (A, B)}
    monkeypatch.setattr(w, "desired_watchlist", lambda: (dict(infos), 0))

    async def record(*a, **k):
        return None

    monkeypatch.setattr(w, "process_signature", record)
    asyncio.run(w._run(0.3, None, {}))
    return w.READY, os.getpid()


def test_the_watcher_says_ready_once_every_owner_is_acked(tmp_path, monkeypatch):
    """memecoin_watchdog.sh stops the older-release watcher only after this line names the new pid."""
    ready, pid = _run_ready(tmp_path, monkeypatch, _FakeSocket())
    assert ready.read_text().split()[0] == str(pid)


class _OneRefusal(_FakeSocket):
    async def send(self, text):
        req = json.loads(text)
        if req["method"] == "logsSubscribe" and req["params"][0]["mentions"][0] == B:
            self.sent.append(req)
            self.queue.append({"jsonrpc": "2.0", "id": req["id"],
                               "error": {"code": -32603, "message": "refused"}})
            return
        await super().send(text)


def test_an_owner_not_acked_leaves_the_watcher_partial(tmp_path, monkeypatch):
    """Not ready (the old watcher keeps its socket before the cap), but it acked something: at the cap
    the watchdog then stops the OLD one, since a refusal every try would never end (Gamma LOW)."""
    ready, pid = _run_ready(tmp_path, monkeypatch, _OneRefusal())
    assert ready.read_text().split() == [str(pid), "partial", "1/2"]
