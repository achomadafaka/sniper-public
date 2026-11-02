"""Inline safety host ROTATION (operator "2 with rotation", 2026-10-29): the single Chainstack host spent its monthly quota (HTTP 403 / -32005) and every 4663 fast entry was blocked.
Ordered hosts, each with its own pacer; on a quota answer or a 429 pause the NEXT check goes to the next healthy host with no retry and nothing added on the happy path; a spent host is probed once per
15 min and rejoins on a real block number; all spent fails closed; alerts on each rotation and on all-spent; never a URL in a log or an alert."""
import threading

import pytest

from sniper.services import memecoin_rh_fast_lane as lane

QUOTA = (403, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "You've reached your monthly quota of Request Units (RUs)."}}, {})
OK = (200, {"jsonrpc": "2.0", "id": 1, "result": "0x1"}, {})
R429 = (429, {}, {})
URLS = ["https://one.example/SECRETKEY1/", "https://two.example/SECRETKEY2/", "https://three.example/SECRETKEY3/"]


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def build(n=2, clock=None, answers=None, chains=None, admit=True):
    """n hosts over a scripted per-URL transport; each host's safety_fn does ONE http read so we can see where a check went.
    `chains` maps URL -> the chain id its eth_chainId answers (default 0x1237 = 4663); the identity probe has its own log (set.probes)."""
    clock = clock or Clock()
    answers = answers if answers is not None else {u: [] for u in URLS[:n]}
    chains = {} if chains is None else chains
    sent, alerts, logs = [], [], []
    hosts = []
    probes = []
    for i, url in enumerate(URLS[:n], 1):
        def base(method, target, headers=None, body=None, timeout=10, _u=url):
            sent.append(_u)
            q = answers[_u]
            return q.pop(0) if q else OK
        paced = lane.PacedHttp(min_gap_s=0.0, pause_s=120.0, clock=clock)
        paced.quota_probe = lambda _u=url, _b=base: _b("POST", _u)[:2]

        def identity(_u=url):
            probes.append(_u)
            return 200, {"jsonrpc": "2.0", "id": 1, "result": chains.get(_u, "0x1237")}
        http = lane.lane_inline_http(url, paced, base=base)
        hosts.append(lane.InlineHost("host %d of %d" % (i, n), paced, safety_fn=lambda mint, usd=None, timings=None, _h=http, _u=url: (_h("POST", _u)[0] == 200, "ok"),
                                     facts_fn=lambda mint, _h=http, _u=url: _h("POST", _u), identity_probe=identity))
    s = lane.InlineHostSet(hosts, log=logs.append, alert_fn=alerts.append, clock=clock)
    s.probes = probes
    if admit:
        s.admit_pass()                                                  # the background admission, run once and synchronously here
    return s, hosts, sent, alerts, logs, clock, answers


def settle():
    for t in threading.enumerate():
        if t.name == "rh-fast-inline-rotation-alert":
            t.join(2)


def test_the_happy_path_uses_the_first_host_adds_no_call_and_alerts_nothing():
    s, hosts, sent, alerts, logs, clock, _ = build()
    assert s.pick() is hosts[0]
    assert s.safety("0xm")[0] is True and sent == [URLS[0]]
    settle()
    assert alerts == [] and not any("rotat" in x or "unavailable" in x for x in logs) and not s.dead() and not s.paused() and not s.quota_spent()


def test_a_quota_answer_moves_the_NEXT_check_to_the_next_host_without_retrying_the_current_one():
    s, hosts, sent, alerts, logs, clock, ans = build(answers={URLS[0]: [QUOTA], URLS[1]: []})
    assert s.safety("0xm")[0] is False                        # the check that met the quota answer: refused as today, not retried
    assert sent == [URLS[0]]
    assert s.safety("0xm")[0] is True and sent == [URLS[0], URLS[1]]
    settle()
    assert len(alerts) == 1 and "host 1 of 2" in alerts[0] and "quota spent" in alerts[0] and "host 2 of 2" in alerts[0]
    assert all("SECRETKEY" not in x for x in alerts + logs) and s.rotations["rotated"] == 1


def test_a_429_pause_rotates_and_the_earlier_host_takes_over_again_when_the_pause_ends():
    s, hosts, sent, alerts, logs, clock, ans = build(answers={URLS[0]: [R429], URLS[1]: []})
    s.safety("0xm")
    assert hosts[0].paced.paused()
    assert s.safety("0xm")[0] is True and sent[-1] == URLS[1]
    clock.t += 121
    assert s.safety("0xm")[0] is True and sent[-1] == URLS[0]                # list order is priority: host 1 is healthy again
    settle()
    assert s.rotations["rotated"] == 2 and len(alerts) == 2


def test_a_dead_host_is_skipped():
    s, hosts, sent, alerts, logs, clock, _ = build()
    for _ in range(3):
        hosts[0].paced.transport(False)
    assert hosts[0].paced.dead() and s.pick() is hosts[1] and not s.dead()


def test_a_spent_host_is_probed_once_per_interval_and_rejoins_on_a_real_block_number_only():
    s, hosts, sent, alerts, logs, clock, ans = build(answers={URLS[0]: [QUOTA], URLS[1]: []})
    s.safety("0xm")
    assert hosts[0].paced.quota_spent()
    n = len(sent)
    s.quota_recover()                                                          # not due yet
    assert len(sent) == n
    clock.t += lane.PacedHttp.QUOTA_PROBE_S + 1
    ans[URLS[0]][:] = [QUOTA]
    s.quota_recover()                                                          # one probe, still spent
    assert sent[n:] == [URLS[0]] and hosts[0].paced.quota_spent()
    clock.t += lane.PacedHttp.QUOTA_PROBE_S + 1
    ans[URLS[0]][:] = [(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}, {})]
    s.quota_recover()
    assert hosts[0].paced.quota_spent()                                        # an error body is not a block number
    clock.t += lane.PacedHttp.QUOTA_PROBE_S + 1
    ans[URLS[0]][:] = [OK]
    s.quota_recover()
    assert not hosts[0].paced.quota_spent() and s.pick() is hosts[1] and hosts[0].admission == "pending"       # identity again first
    s.admit_pass()
    assert s.pick() is hosts[0]


def test_all_hosts_spent_fails_closed_alerts_once_and_recovery_alerts_the_return():
    s, hosts, sent, alerts, logs, clock, ans = build(answers={URLS[0]: [QUOTA], URLS[1]: [QUOTA]})
    s.safety("0xm")
    s.safety("0xm")
    with pytest.raises(lane.InlineHostDead):
        s.safety("0xm")
    with pytest.raises(lane.InlineHostDead):
        s.facts("0xm")
    assert sent == [URLS[0], URLS[1]]                                          # nothing sent once all are spent
    assert s.quota_spent() and s.quota_blocks() and s.paused() and not s.dead()
    settle()
    assert s.rotations["all_unavailable"] == 1 and sum("ALL 2 unavailable" in a for a in alerts) == 1
    clock.t += lane.PacedHttp.QUOTA_PROBE_S + 1
    ans[URLS[0]][:] = [QUOTA]                                                  # host 1 still spent
    ans[URLS[1]][:] = [OK]
    s.quota_recover()
    assert s.pick() is None                                                    # healed but not yet re-admitted: still closed
    s.admit_pass()
    assert s.pick() is hosts[1]
    settle()
    assert s.rotations["recovered"] == 1 and any("is back" in a for a in alerts)


def test_the_warmer_runs_only_on_the_active_host():
    s, hosts, sent, alerts, logs, clock, ans = build()
    hosts[0].warm_safety_fn = lambda mint, usd=None: (sent.append("warm1") or (True, "ok"))
    hosts[1].warm_safety_fn = lambda mint, usd=None: (sent.append("warm2") or (True, "ok"))
    hosts[0].warm_facts_fn = lambda mint: sent.append("f1")
    hosts[1].warm_facts_fn = lambda mint: sent.append("f2")
    s.warm_safety("0xm")
    assert sent == ["warm1"]
    hosts[0].paced.pause()
    s.warm_safety("0xm")
    s.warm_facts("0xm")
    assert sent == ["warm1", "warm2", "f2"]
    hosts[1].paced.pause()
    with pytest.raises(lane.ResearchPaused):
        s.warm_safety("0xm")


def test_counts_sum_over_the_hosts_and_the_set_reads_like_one_pacer():
    s, hosts, sent, alerts, logs, clock, ans = build()
    hosts[0].paced.counts["calls"] = 3
    hosts[1].paced.counts["calls"] = 4
    hosts[1].paced.counts["http_429"] = 2
    assert s.counts["calls"] == 7 and s.counts["http_429"] == 2 and s.counts.get("nope", 9) == 9
    hosts[0].paced.pause()
    hosts[1].paced.pause()
    assert s.paused() and not s.dead()
    for h in hosts:
        h.paced._dead_until = clock.t + 600
    assert s.dead()


def test_concurrent_picks_record_one_rotation_per_transition():
    s, hosts, sent, alerts, logs, clock, ans = build(n=3)
    hosts[0].paced._quota = True
    barrier = threading.Barrier(16)
    got = []

    def run():
        barrier.wait()
        for _ in range(200):
            got.append(s.pick())
    threads = [threading.Thread(target=run) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    settle()
    assert all(h is hosts[1] for h in got) and s.rotations["rotated"] == 1 and len(alerts) == 1


def test_pick_does_no_io_and_takes_no_lock_on_the_happy_path(monkeypatch):
    s, hosts, sent, alerts, logs, clock, _ = build()

    class Boom:
        def __enter__(self):
            raise AssertionError("pick took the lock on the happy path")

        def __exit__(self, *a):
            return False
    s._lock = Boom()
    for _ in range(1000):
        assert s.pick() is hosts[0]
    assert sent == []


# ---- the URL list: each entry passes safety_host_refusal on its own
def test_the_list_wins_over_the_single_knob_and_each_url_is_checked_alone(monkeypatch):
    env = {"MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS": "https://a.example/k1, https://b.example/k2 https://a.example/k1/",
           "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL": "https://a.example/k1",
           "MEMECOIN_EVM_RPC_URL_4663": "https://custody.example/c", "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663": "https://research.example/r"}
    logs = []
    urls, own = lane.resolve_inline_hosts(env, logs.append)
    assert urls == ["https://a.example/k1", "https://b.example/k2"] and own == "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS"
    assert any("duplicate" in x for x in logs)                                 # the same URL twice is dropped, the single knob's copy is not a conflict


def test_a_refused_entry_is_dropped_by_position_the_rest_kept_and_no_url_is_logged():
    env = {"MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS": "https://custody.example/c,http://plain.example/p,https://research.example/r,https://good.example/g",
           "MEMECOIN_EVM_RPC_URL_4663": "https://custody.example/c", "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663": "https://research.example/r"}
    logs = []
    urls, own = lane.resolve_inline_hosts(env, logs.append)
    assert urls == ["https://good.example/g"]
    assert len(logs) == 3 and any("entry 1 refused" in x for x in logs) and any("not an https URL" in x for x in logs)
    assert all("custody.example" not in x and "research.example" not in x and "plain.example" not in x for x in logs)


def test_an_unset_list_falls_back_to_the_single_knob_and_a_list_of_one_is_the_rollback():
    env = {"MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL": "https://only.example/k"}
    assert lane.resolve_inline_hosts(env, lambda *_: None)[0] == ["https://only.example/k"]
    assert lane.resolve_inline_hosts({"MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS": "https://only.example/k"}, lambda *_: None)[0] == ["https://only.example/k"]
    assert lane.resolve_inline_hosts({}, lambda *_: None)[0] == []


def test_a_host_is_named_by_provider_domain_only_never_by_key_path_or_subdomain():
    assert lane.host_provider("https://robinhood-mainnet.g.alchemy.com/v2/SECRETKEY") == "alchemy.com"
    assert lane.host_provider("https://nd-123-456.p2pify.com/SECRETKEY/") == "p2pify.com"
    assert lane.host_provider("https://x") == "?" and lane.host_provider("not a url") == "?" and lane.host_provider(None) == "?"


def test_the_built_lane_labels_each_host_by_provider_and_keeps_the_key_out_of_every_label(monkeypatch, tmp_path):
    from test_memecoin_rh_fast_entry import _setup
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS",
                       "https://robinhood-mainnet.g.alchemy.com/v2/ALCHKEY,https://nd-1-2-3.p2pify.com/CHAINKEY")
    logs = []
    built = _setup(monkeypatch, tmp_path, "live", log=logs.append)[0]
    try:
        hosts = built["inline_paced"].hosts
        assert [h.label for h in hosts] == ["host 1 of 2 (alchemy.com)", "host 2 of 2 (p2pify.com)"]
        assert all("KEY" not in h.label for h in hosts) and built["live"].inline_safety_fn is not None and built["inline_url"].endswith("ALCHKEY")
        assert built["inline_paced"].counts["calls"] == 0                       # construction sends nothing
    finally:
        built["live"]._safety_pool.shutdown(wait=True)


def test_the_heartbeat_carries_the_active_host_index_states_and_rotation_counters(tmp_path):
    import collections
    import json
    import sqlite3
    s, hosts, sent, alerts, logs, clock, ans = build(answers={URLS[0]: [QUOTA], URLS[1]: []})
    s.safety("0xm")
    s.safety("0xm")                                               # rotated to host 2
    settle()

    class D:
        counts = collections.Counter()

    class C:
        counts = collections.Counter()

        def size(self):
            return 0
    db = str(tmp_path / "l.db")
    lane.write_heartbeat(db, D(), C(), lane.PacedHttp(min_gap_s=0.0), inline=s)
    got = json.loads(sqlite3.connect(db).execute("select inline_hosts from rh_fast_lane_heartbeat").fetchone()[0])
    assert got["n"] == 2 and got["active"] == 2 and got["rotations"] == {"rotated": 1}
    assert got["hosts"][0]["state"] == "quota spent" and got["hosts"][1]["state"] == "healthy" and "SECRETKEY" not in json.dumps(got)
    lane.write_heartbeat(db, D(), C(), lane.PacedHttp(min_gap_s=0.0), inline=lane.PacedHttp(min_gap_s=0.0))
    assert sqlite3.connect(db).execute("select inline_hosts from rh_fast_lane_heartbeat order by rowid desc limit 1").fetchone() == (None,)       # a plain pacer: unknown, not a 0
    hosts[1].paced.pause()
    hosts[0].paced._quota = True
    lane.write_heartbeat(db, D(), C(), lane.PacedHttp(min_gap_s=0.0), inline=s)
    assert json.loads(sqlite3.connect(db).execute("select inline_hosts from rh_fast_lane_heartbeat order by rowid desc limit 1").fetchone()[0])["active"] is None


# ---- identity admission (Delta HOLD on fe1a293ac, Alpha's ruling): eth_chainId OFF the fill path, before a host may serve
def test_a_host_serves_only_after_the_background_pass_proved_its_chain_and_pick_never_probes():
    s, hosts, sent, alerts, logs, clock, _ = build(admit=False)
    assert s.pick() is None and s.paused() and s.probes == []                    # pending: fails closed, and pick made no probe call
    with pytest.raises(lane.InlineHostDead):
        s.safety("0xm")
    assert sent == [] and s.probes == []
    s.admit_pass()
    assert sorted(s.probes) == sorted(URLS[:2]) and s.pick() is hosts[0] and all(h.admission == "admitted" for h in hosts)
    n = len(s.probes)
    for _ in range(100):
        s.safety("0xm")
    assert len(s.probes) == n                                                    # the fill path adds no chainId call
    settle()
    assert alerts == []                                                          # startup admission is not a rotation


def test_a_wrong_chain_host_is_refused_alerted_by_provider_and_never_receives_a_token_read():
    wrong = {URLS[0]: "0x1"}
    s, hosts, sent, alerts, logs, clock, _ = build(chains=wrong)
    assert hosts[0].admission == "refused" and hosts[1].admission == "admitted"
    assert s.pick() is hosts[1]
    for _ in range(5):
        s.safety("0xm")
        s.facts("0xm")
    assert URLS[0] not in sent and set(sent) == {URLS[1]}
    settle()
    assert any("REFUSED" in a and "host 1 of 2" in a and "0x1" in a and "0x1237" in a for a in alerts)
    assert all("SECRETKEY" not in x for x in alerts + logs)
    clock.t += 10_000
    s.admit_pass()
    assert hosts[0].admission == "refused" and s.probes.count(URLS[0]) == 1      # refused for good: never probed again


def test_all_wrong_chain_fails_closed_with_one_all_refused_alert():
    s, hosts, sent, alerts, logs, clock, _ = build(chains={URLS[0]: "0x1", URLS[1]: "0x38"})
    with pytest.raises(lane.InlineHostDead):
        s.safety("0xm")
    assert sent == []
    settle()
    assert sum("ALL 2 REFUSED" in a for a in alerts) == 1


def test_an_unreadable_identity_answer_leaves_the_host_pending_and_it_is_retried_next_pass():
    s, hosts, sent, alerts, logs, clock, _ = build(admit=False)
    results = iter([(200, {"error": {"code": -32000}}), (500, {}), (200, {"result": "garbage"})])
    hosts[0].identity_probe = lambda: next(results)
    hosts[1].identity_probe = lambda: (_ for _ in ()).throw(OSError("down"))
    s.admit_pass()
    assert [h.admission for h in hosts] == ["pending", "pending"] and s.pick() is None
    s.admit_pass()
    s.admit_pass()
    assert hosts[0].admission == "pending"
    hosts[1].identity_probe = lambda: (200, {"result": "0x1237"})
    s.admit_pass()
    assert s.pick() is hosts[1]
    settle()
    assert alerts == [] or all("REFUSED" not in a for a in alerts)


def test_a_quota_rejoin_must_be_admitted_again_before_it_serves():
    s, hosts, sent, alerts, logs, clock, ans = build(answers={URLS[0]: [QUOTA], URLS[1]: []})
    s.safety("0xm")
    assert hosts[0].paced.quota_spent() and s.pick() is hosts[1]
    clock.t += lane.PacedHttp.QUOTA_PROBE_S + 1
    ans[URLS[0]][:] = [OK]
    s.quota_recover()                                                           # the real block number clears the latch
    assert not hosts[0].paced.quota_spent() and hosts[0].admission == "pending"
    assert s.pick() is hosts[1]                                                 # not yet: identity first
    s.admit_pass()
    assert hosts[0].admission == "admitted" and s.pick() is hosts[0]


def test_an_admitted_host_is_rechecked_after_ten_minutes_and_a_changed_chain_refuses_it():
    chains = {}
    s, hosts, sent, alerts, logs, clock, _ = build(chains=chains)
    n = len(s.probes)
    clock.t += 300
    s.admit_pass()
    assert len(s.probes) == n                                                   # inside the recheck interval: no probe
    clock.t += 400
    chains[URLS[0]] = "0x1"
    s.admit_pass()
    assert hosts[0].admission == "refused" and s.pick() is hosts[1]


def test_a_paused_dead_or_spent_host_is_not_probed():
    s, hosts, sent, alerts, logs, clock, _ = build(admit=False)
    hosts[0].paced.pause()
    hosts[1].paced._quota = True
    s.admit_pass()
    assert s.probes == []


def test_the_heartbeat_names_the_identity_state_without_a_url():
    s, hosts, sent, alerts, logs, clock, _ = build(chains={URLS[0]: "0x1"})
    got = s.beat()
    assert got["active"] == 2 and got["hosts"][0]["state"] == "wrong chain (refused)" and got["rotations"].get("refused") == 1


# ---- the real per-host safety closures of a built lane (Delta's and Zeta's witnesses, at the layer the lifecycle puts the proof)
def _call(fn, *a):
    """The closure's result, or the exception it raised (a verdict, an unsafe reason or InlineHostDead: never a send to an unadmitted host)."""
    try:
        return fn(*a)
    except Exception as exc:
        return exc


def _lane_with_two_hosts(monkeypatch, tmp_path, chains):
    import json
    from sniper.execution import evm_wallet
    from test_memecoin_rh_fast_entry import _setup
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URLS", "https://one.example/KEY1,https://two.example/KEY2")
    built = _setup(monkeypatch, tmp_path, "live")[0]
    for h in built["inline_paced"].hosts:
        h.admission = "pending"                                   # undo the test helper's pre-admission: this test drives the real pass
    sent = []

    def fake(method, url, headers=None, body=None, timeout=10):
        rpc = json.loads(body)["method"]
        sent.append((url, rpc))
        return 200, {"jsonrpc": "2.0", "id": 1, "result": chains.get(url, "0x1237") if rpc == "eth_chainId" else "0x"}, {}
    monkeypatch.setattr(evm_wallet, "_http", fake)
    return built, sent


def test_an_unadmitted_host_receives_no_token_read_through_its_real_safety_closure(monkeypatch, tmp_path):
    built, sent = _lane_with_two_hosts(monkeypatch, tmp_path, {})
    try:
        one = built["inline_paced"].hosts[0]
        verdict = _call(one.safety_fn, "0x" + "22" * 20)            # pending: the closure itself sends nothing and never says "safe"
        assert not (isinstance(verdict, tuple) and verdict[0] is True)
        _call(one.facts_fn, "0x" + "22" * 20)
        assert sent == []
    finally:
        built["live"]._safety_pool.shutdown(wait=True)


def test_a_wrong_chain_host_never_gets_eth_getcode_or_eth_call_and_the_right_one_is_admitted_first(monkeypatch, tmp_path):
    built, sent = _lane_with_two_hosts(monkeypatch, tmp_path, {"https://one.example/KEY1": "0x1"})
    try:
        s = built["inline_paced"]
        s.admit_pass()                                            # the background pass: eth_chainId only
        assert {m for _u, m in sent} == {"eth_chainId"} and [h.admission for h in s.hosts] == ["refused", "admitted"]
        sent.clear()
        verdict = _call(s.hosts[0].safety_fn, "0x" + "22" * 20)     # even called directly, the refused host's closure sends nothing and never says "safe"
        assert not (isinstance(verdict, tuple) and verdict[0] is True) and sent == []
        _call(s.hosts[1].safety_fn, "0x" + "22" * 20)               # the admitted host reads (the closure's own result is irrelevant here)
        assert sent and all(u == "https://two.example/KEY2" for u, _m in sent) and all(m != "eth_chainId" for _u, m in sent)
    finally:
        built["live"]._safety_pool.shutdown(wait=True)


def test_start_lane_runs_the_admission_loop_off_the_fill_path():
    import inspect
    src = inspect.getsource(lane.start_lane)
    assert "inline_admission_loop" in src and "rh-fast-inline-admission" in src
