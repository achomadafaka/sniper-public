"""4663 research-key rotation across numbered spare slots (operator 2026-10-02 via Alpha)."""
import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from sniper.execution import research_rotation_4663 as rr

BASE = "https://base.robinhood-mainnet.quiknode.pro/k1/"
SPARE = "https://spare.robinhood-mainnet.quiknode.pro/k2/"
INLINE = "https://inline.robinhood-mainnet.quiknode.pro/k3/"
QUOTA = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32003, "message": "daily request limit reached"}}
RPS = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message":
                                            "You've exceeded the RPS limit available on the current plan"}}
OK = {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
NOON = datetime(2026, 10, 2, 12, tzinfo=timezone.utc).timestamp()


def _rot(env, clock=lambda: NOON):
    lines = []
    return rr.Rotation(env, clock=clock, log=lines.append), lines


@pytest.mark.parametrize("reply, quota", [
    (QUOTA, True),
    ({"error": {"code": -32000, "message": "monthly credit limit exceeded"}}, True),
    (RPS, False),                                          # names the plan, but it is a per-second limit
    ({"error": {"code": 429, "message": "Too Many Requests"}}, False),
    ({"error": {"code": -32003, "message": "rate limit"}}, False),
    # QuickNode's -32007 is its per-second limit: RPS by code even when the text reads like a quota
    ({"error": {"code": -32007, "message": "request limit reached"}}, False),
    ({"error": {"code": -32007, "message": "daily request limit reached"}}, False),
    ({"error": {"code": -32008, "message": "per-minute request limit reached"}}, False),   # Gamma LOW
    (None, False), ({}, False), (OK, False)])
def test_only_a_spent_quota_answer_rotates_never_an_rps_limit(reply, quota):
    assert rr.is_quota_answer(reply) is quota


def test_without_spares_a_quota_answer_is_returned_as_today_and_nothing_retries():
    rot, _ = _rot({rr.BASE: BASE})
    sent = []
    out = rot.send(BASE, lambda t: sent.append(t) or (200, QUOTA, {}))
    assert out[1] is QUOTA and sent == [BASE]
    assert rot.active() == (1, BASE) and rot.stats()[1] == [
        {"slot": 1, "calls": 1, "quota": 1, "spent": True, "transport": 0, "unusable": False}]


def test_a_quota_answer_moves_to_the_next_slot_and_retries_the_read_once():
    rot, lines = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE})
    sent = []
    out = rot.send(BASE, lambda t: sent.append(t) or ((200, QUOTA, {}) if t == BASE else (200, OK, {})))
    assert out[1] is OK and sent == [BASE, SPARE]
    assert rot.send(BASE, lambda t: sent.append(t) or (200, OK, {}))[1] is OK and sent[-1] == SPARE
    assert any("slot 1 spent its quota: slot 2" in ln for ln in lines) and not any("k2" in ln for ln in lines)


def test_an_rps_limit_keeps_the_slot_and_returns_the_429_to_the_callers_own_pause():
    rot, _ = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE})
    sent = []
    assert rot.send(BASE, lambda t: sent.append(t) or (429, RPS, {}))[0] == 429 and sent == [BASE]
    assert rot.active() == (1, BASE)


def test_returned_transport_errors_mark_a_slot_down_without_spending_its_quota():
    rot, _ = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE})
    for _ in range(3):
        rot.send(BASE, lambda target: (None, {"error": {"transport": "URLError"}}))
    active, slots = rot.stats()
    assert active == 2
    assert slots[0]["transport"] == 3 and slots[0]["unusable"] and not slots[0]["spent"]
    assert rot.send(BASE, lambda target: (200, OK))[1] is OK


@pytest.mark.parametrize("status, reply", [(500, {"error": {"transport": "provider"}}),
                                          (200, {"error": {"code": -32000}}),
                                          (None, {"error": {"http": 429}})])
def test_answered_errors_do_not_mark_a_research_slot_transport_dead(status, reply):
    rot, _ = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE})
    for _ in range(3):
        rot.send(BASE, lambda target: (status, reply))
    assert rot.stats()[1][0]["transport"] == 0
    assert not rot.stats()[1][0]["unusable"]


def test_consumers_without_drpc_optin_keep_original_quicknode_read_order():
    drpc = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    env = {rr.BASE: BASE, rr.BASE + "_2": SPARE, "MEMECOIN_RESEARCH_DRPC_RPC_URL_4663": drpc}
    rot = rr.Rotation(env, log=lambda _: None)
    sent = []
    assert rot.send(BASE, lambda target: sent.append(target) or (
        (200, QUOTA) if target == BASE else (200, OK)))[1] == OK
    assert sent == [BASE, SPARE]


def test_drpc_fallback_refuses_a_drpc_url_shared_with_custody():
    drpc = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    env = {rr.BASE: BASE, "MEMECOIN_RESEARCH_DRPC_RPC_URL_4663": drpc,
           "MEMECOIN_EVM_RPC_URL_4663": drpc}
    lines = []
    rot = rr.Rotation(env, drpc_fallback=True, log=lines.append)
    assert rot.slots == [(1, BASE)] and lines
    assert all("SECRET" not in line for line in lines)


@pytest.mark.parametrize("chain", ["0x38", None, "broken"])
def test_unverified_fallback_never_reads_wallet_or_token_data(monkeypatch, chain):
    from sniper.services import memecoin_research_pacer as rp
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: None)
    drpc = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    rot = rr.Rotation({rr.BASE: BASE, "MEMECOIN_RESEARCH_DRPC_RPC_URL_4663": drpc},
                      drpc_fallback=True, log=lambda _: None)
    rot._spent.add(1)
    with pytest.raises(OSError):
        rot.send(BASE, lambda target: pytest.fail("read before chain identity"),
                 identity=lambda target: (200, {"result": chain}))


def test_the_utc_day_roll_brings_the_base_back():
    now = [NOON]
    rot, _ = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE}, clock=lambda: now[0])
    rot.send(BASE, lambda t: (200, QUOTA, {}) if t == BASE else (200, OK, {}))
    assert rot.active()[0] == 2
    now[0] = NOON + 11.9 * 3600                            # 23:54Z: still today
    assert rot.active()[0] == 2
    now[0] = NOON + 12.1 * 3600                            # 00:06Z: a new UTC day
    assert rot.active() == (1, BASE)


def test_every_slot_spent_returns_the_last_answer_on_the_base():
    rot, lines = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE})
    sent = []
    assert rot.send(BASE, lambda t: sent.append(t) or (200, QUOTA, {}))[1] is QUOTA
    assert sent == [BASE, SPARE] and rot.active() == (1, BASE)
    assert any("no slot left today" in ln for ln in lines)


def test_a_quota_answer_from_a_slot_another_thread_left_still_retries_on_the_active_one():
    third = "https://third.robinhood-mainnet.quiknode.pro/k4/"
    rot, _ = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE, rr.BASE + "_3": third})
    rot._note(1, True)                                     # another consumer thread spent the base
    rot._spent.add(2)
    assert rot._note(1, True) is True and rot.active()[1] == third


def test_rotation_never_crosses_into_inline_custody_a_funded_pool_or_a_duplicate(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": ["https://pool.test/k9"]})
    good = "https://good.robinhood-mainnet.quiknode.pro/k8/"
    env = {rr.BASE: BASE, "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL": INLINE,
           "MEMECOIN_EVM_RPC_URL_4663": "https://custody-4663.test/",
           rr.BASE + "_2": INLINE,                         # the inline key
           rr.BASE + "_3": "https://custody-4663.test/",   # the funded custody RPC
           rr.BASE + "_4": "https://pool.test/k9/",        # a funded read-pool member
           rr.BASE + "_5": BASE.rstrip("/"),               # the base again
           rr.BASE + "_6": "http://plain.test/k",          # not https
           rr.BASE + "_7": "",                             # absent, not a slot
           rr.BASE + "_11": good}
    rot, lines = _rot(env)
    assert [n for n, _u in rot.slots] == [1, 11]
    assert sum("refused" in ln for ln in lines) == 5 and not any(x in " ".join(lines) for x in ("k3", "k9", "k1"))
    sent = []
    for _ in range(3):
        rot.send(BASE, lambda t: sent.append(t) or (200, QUOTA, {}))
    assert set(sent) == {BASE, good}
    # a URL that is not a research slot is passed through untouched and never counted
    assert rot.send(INLINE, lambda t: (200, QUOTA, t))[2] == INLINE
    assert [s["calls"] for s in rot.stats()[1]] == [3, 1]       # all spent: the base only, as today


def test_a_raise_is_counted_on_its_slot_and_passed_on():
    rot, _ = _rot({rr.BASE: BASE})

    def boom(target):
        raise OSError("transport")
    with pytest.raises(OSError):
        rot.send(BASE, boom)
    assert rot.stats()[1][0]["calls"] == 1


# ---- the three consumers ----

def _urlopen_by_url(monkeypatch, answers, seen):
    def fake(req, timeout=None):
        seen.append(req.full_url)
        status, body = answers[req.full_url]
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps(body).encode()))

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        r = R(json.dumps(body).encode())
        r.status = 200
        return r
    monkeypatch.setattr("urllib.request.urlopen", fake)


def test_the_feed_float_poll_rotates_on_a_quota_429_and_keeps_an_rps_429_for_its_backoff(monkeypatch):
    from collections import Counter
    from sniper.services import memecoin_rh_fast_decide as fd
    seen = []
    _urlopen_by_url(monkeypatch, {BASE: (429, QUOTA), SPARE: (200, OK)}, seen)
    rot, _ = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE})
    call = fd._research_rpc(BASE, Counter(), spacing_s=0.0, rotation=rot)
    assert call("eth_gasPrice", []) == ("0x1", None) and seen == [BASE, SPARE]
    _urlopen_by_url(monkeypatch, {SPARE: (429, RPS)}, seen)
    result, error = call("eth_gasPrice", [])
    assert result is None and error["http"] == 429 and rot.active()[0] == 2   # poll_snapshot's rate_limit


def test_the_exit_follow_up_4663_lane_reads_through_the_rotation(monkeypatch):
    from sniper.execution import evm_wallet
    from sniper.services import memecoin_exit_followup as fu
    monkeypatch.setenv(rr.BASE, BASE)
    monkeypatch.setenv(rr.BASE + "_2", SPARE)
    seen = []
    monkeypatch.setattr(evm_wallet, "_http", lambda m, url, headers=None, body=None, timeout=10: (
        seen.append(url) or ((200, QUOTA, {}) if url == BASE else (200, OK, {}))))
    lane = fu.build_lane("4663", BASE, 2.5)
    state = {}
    status, reply, _h = lane._rpc_http("4663", state)("POST", BASE, body="{}")
    assert reply == OK and seen == [BASE, SPARE] and not state.get("failed")
    other = fu.build_lane("4663", "https://override.test/x", 2.5)      # MEMECOIN_EXIT_TAPE_READ_URL_4663
    other._rpc_http("4663", {})("POST", "https://override.test/x", body="{}")
    assert seen[-1] == "https://override.test/x"


def test_the_feed_heartbeat_gains_the_slot_columns_on_an_older_db(tmp_path):
    import sqlite3
    from collections import Counter
    from types import SimpleNamespace
    from sniper.services import memecoin_rh_fast_decide as fd
    path = str(tmp_path / "feed.db")
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE rh_fast_heartbeat (at TEXT NOT NULL, rpc_calls INTEGER, rpc_errors "
                      "INTEGER, rpc_429 INTEGER, tape_rows INTEGER, tape_lag_s REAL, pools INTEGER, "
                      "decisions INTEGER, threads_alive INTEGER);")
    old.execute("INSERT INTO rh_fast_heartbeat VALUES ('t0',1,0,0,0,0.0,0,0,3)")
    old.commit()
    old.close()
    rot, _ = _rot({rr.BASE: BASE, rr.BASE + "_2": SPARE})
    rot.send(BASE, lambda t: (200, QUOTA, {}) if t == BASE else (200, OK, {}))
    tape = SimpleNamespace(ingested=5, _meta={})
    fd._beat(path, Counter(rpc_calls=2), 1.5, tape, lambda: "t1", 3, rot)
    fd._beat(path, Counter(), 1.5, tape, lambda: "t2", 3, None)
    rows = sqlite3.connect(path).execute(
        "SELECT at, research_slot, research_slots FROM rh_fast_heartbeat ORDER BY rowid").fetchall()
    assert rows[0] == ("t0", None, None) and rows[2] == ("t2", None, None)
    assert rows[1][1] == 2 and json.loads(rows[1][2]) == [
        {"slot": 1, "calls": 1, "quota": 1, "spent": True, "transport": 0, "unusable": False},
        {"slot": 2, "calls": 1, "quota": 0, "spent": False, "transport": 0, "unusable": False}]
