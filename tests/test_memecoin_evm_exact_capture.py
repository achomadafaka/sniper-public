"""Exact-route EVM refusal capture.

Spec docs/superpowers/specs/2026-09-28-memecoin-evm-exact-refusal-capture-design.md. The frames are
REAL: tests/fixtures/memecoin/evm_exact_capture_frames.json, recorded 2026-09-28 22:33Z by the lane
itself, run without .env so its quoter config is this test's, on the research hosts it reads
(robinhood.drpc.org, bsc-dataseed1.ninicoin.io, base.drpc.org): the V4Quoter, QuoterV2, Router02
and flap Portal answers for one refused mint per chain and one sell of the winning bag. A call the
recording does not hold fails the test, so a calldata drift cannot pass."""
import contextlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin_captures as store
from sniper.services import memecoin_evm_exact_capture as ex
from sniper.services import memecoin_refusal_capture as cap

REPO = Path(__file__).resolve().parents[1]
FIX = json.load(open(Path(__file__).parent / "fixtures" / "memecoin" / "evm_exact_capture_frames.json"))
CHAINS = sorted(FIX["chains"])
REFUSED_AT = "2026-09-28T22:25:00+00:00"
REFUSED_EPOCH = 1790634300.0


@pytest.fixture(autouse=True)
def recorded_connector_fee(monkeypatch):
    """September 28's RPC frames used the then-default 1% WBNB/USDT bridge.

    Preserve those exact calls; current defaults are tested separately by connector-fee tests.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setitem(cfg.MEMECOIN_EVM_V3_CONNECTORS, "56",
                        (("0x55d398326f99059ff775485246999027b3197955", 10000),))


def _key(method, params):
    if method == "eth_call":
        return method, params[0]["to"].lower(), params[0]["data"].lower()
    return (method,)


class Replay:
    """The lane's HTTP seam, answering from the recording. `override(key) -> reply or None`."""

    def __init__(self, chain, override=None, price=None):
        self.fix = FIX["chains"][chain]
        self.frames = {_key(f["method"], f["params"]): (f["status"], f["reply"]) for f in self.fix["frames"]}
        self.override, self.price = override, price
        self.posts, self.gets, self.unrecorded = [], [], []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        if method == "GET":
            self.gets.append(url)
            if self.price is not None:
                return self.price()
            return 200, {"price": str(self.fix["native_usd"])}, {}
        req = json.loads(body)
        key = _key(req["method"], req["params"])
        self.posts.append((url, key))
        if self.override is not None:
            hit = self.override(key)
            if hit is not None:
                return hit
        if key not in self.frames:
            self.unrecorded.append(key)
            raise AssertionError("unrecorded call %s" % (key[:2],))
        status, reply = self.frames[key]
        return status, reply, {}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, float(s))


def _lane(chain, http, clock=None, pending=False):
    fx = FIX["chains"][chain]
    clock = clock or Clock()
    urls = {chain: "https://" + fx["host"]}
    return ex.ExactLane(
        {} if pending else urls, pending=urls if pending else None, rps=4, yield_s=120, clip_usd=20.0, http=http,
        keys_fn=lambda c, m: [tuple(k) for k in fx["v4_keys"]], clock=clock, sleep=clock.sleep,
        wall=lambda: ("2026-09-28T22:25:30+00:00", REFUSED_EPOCH + 30))


def _capture(chain, captures=None, **kw):
    captures = {} if captures is None else captures
    return cap.start(captures, mint=FIX["chains"][chain]["mint"], origin_event_id=kw.pop("origin", "o-" + chain),
                     gate="mcap_over_ceiling", refused_at=REFUSED_AT, network_id=chain, horizon_s=810.0,
                     cadence_s=25.0, max_concurrent=kw.pop("max_concurrent", 6), now=0.0,
                     now_epoch=REFUSED_EPOCH + 1, exact=True, **kw)


def _word(raw):
    return int(raw[2:66], 16)


def _solana_row_keys():
    c = cap.Capture(capture_id="solana:m:o", mint="m", origin_event_id="o", gate="mcap_over_ceiling",
                    refused_at=REFUSED_AT, refused_mono=0.0, horizon_s=810.0, cadence_s=25.0, expected_obs=32)
    row = cap.quote_once(c, http=lambda *a, **k: (200, {}, {}), rpc_url="http://unused", clip_usd=20.0,
                         now_iso=REFUSED_AT, now_mono=0.0, quote_fn=lambda *a, **k: None)
    return set(row)


@pytest.mark.parametrize("chain", CHAINS)
def test_a_buy_then_sells_write_rows_in_the_solana_shape(chain):
    fx = FIX["chains"][chain]
    http = Replay(chain)
    lane = _lane(chain, http)
    c = _capture(chain)
    assert c.capture_id.endswith("#exact") and c.basis == "exact_route"
    buy = lane.observe(c)
    assert http.unrecorded == []
    assert buy is not None and set(buy) == _solana_row_keys() | {"quote_asset_usd"}
    dec = _word(next(f["reply"]["result"] for f in fx["frames"]
                     if f["method"] == "eth_call" and f["params"][0]["data"] == "0x313ce567"))
    out = int(fx["best"]["out"])
    assert (buy["quote_side"], buy["quality"], buy["quote_price_basis"]) == ("buy", "ok", "exact_route")
    assert buy["provider"] == {"v4": "evm_v4_quoter", "v2": "evm_v2_router"}[fx["best"]["kind"]]
    assert buy["quote_output_atomic"] == str(out) and buy["token_decimals"] == dec
    assert buy["quote_input_usd"] == 20.0 and buy["quote_asset_usd"] == fx["native_usd"]
    assert buy["quote_price"] == pytest.approx(20.0 / (out / 10 ** dec))
    assert buy["capture_id"] == c.capture_id and buy["age_s"] == pytest.approx(30.0)
    if chain == "56":                                   # the flap Portal named the pair token
        assert buy["quote_route_target"].startswith("v2:") and buy["quote_route_target"].count(">") == 2
    sells = [lane.observe(c), lane.observe(c)]
    assert http.unrecorded == []
    for s in sells:
        assert (s["quote_side"], s["quality"], s["token_qty_atomic"]) == ("sell", "ok", str(out))
        got = int(s["quote_output_atomic"])
        assert s["quote_price"] == pytest.approx(got / 1e18 * fx["native_usd"] / (out / 10 ** dec))
    rt = sells[0]["quote_price"] / buy["quote_price"] - 1.0
    assert -0.5 < rt < 0.0, "a round trip on a real pool costs something and not everything: %r" % rt
    assert {url for url, _k in http.posts} == {"https://" + fx["host"]}
    assert http.gets and all(u.startswith("https://api.binance.com/") for u in http.gets)


def test_the_store_keeps_the_quote_asset_rate(tmp_path):
    conn = store.connect(tmp_path / "caps.db")
    store.record_observation(conn, {"capture_id": "4663:m:o#exact", "network_id": "4663", "mint": "m",
                                    "refused_at": REFUSED_AT, "observed_at": REFUSED_AT, "age_s": 1.0,
                                    "quote_asset_usd": 4000.0, "quote_output_atomic": "1"})
    assert conn.execute("SELECT quote_asset_usd FROM memecoin_refusal_captures").fetchone()[0] == 4000.0


def _chain_id_http(reply_hex):
    def http(method, url, headers=None, body=None, timeout=None):
        assert json.loads(body)["method"] == "eth_chainId"
        return 200, {"jsonrpc": "2.0", "id": 1, "result": reply_hex}, {}
    return http


def test_the_guard_refuses_a_host_any_other_role_uses():
    env = {"MEMECOIN_CAPTURE_EVM_READ_URL_8453": "https://mainnet.base.org",
           "MEMECOIN_CAPTURE_EVM_READ_URL_4663": "https://robinhood-mainnet.g.alchemy.com/v2/research",
           "MEMECOIN_EVM_RPC_POOL_URLS_4663": "https://robinhood-mainnet.g.alchemy.com/v2/funded",
           "MEMECOIN_CAPTURE_EVM_READ_URL_56": "https://bsc-dataseed1.ninicoin.io"}
    ok = ex.usable_read_urls(env, ("8453", "4663", "56", "5042"), http=_chain_id_http("0x38"))
    assert ok == {"56": "https://bsc-dataseed1.ninicoin.io"}, "8453 is a free-submission host, 4663 funded, 5042 unset"
    env["MEMECOIN_ONCHAIN_EVM_RPC_URL_56"] = "https://bsc-dataseed1.ninicoin.io"
    assert ex.usable_read_urls(env, ("56",), http=_chain_id_http("0x38")) == {}, "a watcher host is a money role"
    del env["MEMECOIN_ONCHAIN_EVM_RPC_URL_56"]
    assert ex.usable_read_urls(env, ("56",), http=_chain_id_http("0x2105")) == {}, "wrong chain id"
    # A host that only the free-submission registry names (the 4663 sequencer is in no read pool).
    env["MEMECOIN_CAPTURE_EVM_READ_URL_4663"] = "https://sequencer.mainnet.chain.robinhood.com"
    assert ex.usable_read_urls(env, ("4663",), http=_chain_id_http("0x1237")) == {}
    env["MEMECOIN_CAPTURE_EVM_READ_URL_4663"] = "https://robinhood.drpc.org"
    assert ex.usable_read_urls(env, ("4663",), http=_chain_id_http("0x1237")) == {
        "4663": "https://robinhood.drpc.org"}


@pytest.mark.parametrize("reply", [(429, {}, {}),
                                   (200, {"jsonrpc": "2.0", "id": 1,
                                          "error": {"code": -32005, "message": "limit exceeded"}}, {})])
def test_a_limit_holds_the_host_and_writes_no_row(reply):
    clock = Clock()
    http = Replay("4663", override=lambda key: reply if key[0] == "eth_call" else None)
    lane = _lane("4663", http, clock)
    c = _capture("4663")
    assert lane.observe(c) is None
    calls = len(http.posts)
    assert calls == 1, "the first limit stops the scan"
    clock.t += 100
    assert lane.observe(c) is None and len(http.posts) == calls, "held: no call at all"
    clock.t += 30
    http.override = None
    assert lane.observe(c)["quality"] == "ok"


def test_a_revert_is_an_answer_and_a_miss_is_cached():
    clock = Clock()
    revert = (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}}, {})
    http = Replay("8453", override=lambda key: revert)
    lane = _lane("8453", http, clock)
    c = _capture("8453")
    row = lane.observe(c)
    assert row["quality"] == "no_route" and row["quote_side"] == "buy" and row["quote_price"] is None
    calls = len(http.posts)
    clock.t += 60
    assert lane.observe(c)["quality"] == "no_route" and len(http.posts) == calls, "a miss is cached"
    clock.t += 61
    lane.observe(c)
    assert len(http.posts) > calls, "and rescanned after it expires"
    assert lane.stats()["base.drpc.org"]["held"] == 0


def test_the_bucket_never_exceeds_eight_calls_in_any_second():
    clock = Clock()
    stamps = []
    replay = Replay("4663")

    def http(*a, **k):
        if a[0] == "POST":
            stamps.append(clock.t)
        return replay(*a, **k)
    lane = _lane("4663", http, clock)
    assert lane.observe(_capture("4663"))["quality"] == "ok"
    assert len(stamps) >= 12
    assert max(sum(1 for s in stamps if t <= s < t + 1.0) for t in stamps) <= 8
    assert stamps[-1] - stamps[0] >= (len(stamps) - 8) / 4.0 - 1e-9, "paced at 4/s after the burst"


def test_an_idle_host_still_bursts_at_most_one_second_of_rate():
    clock = Clock()
    host = ex._Host("https://robinhood.drpc.org", 4, 120, clock, clock.sleep)
    clock.t += 30.0                                  # idle: the bucket must not bank 30 s of tokens
    stamps = []
    for _ in range(20):
        host.take()
        stamps.append(clock.t)
    assert max(sum(1 for s in stamps if t <= s < t + 1.0) for t in stamps) <= 8
    assert sum(1 for s in stamps if s == stamps[0]) == 4


def test_an_unknown_native_price_writes_price_unknown_and_holds_the_price_read():
    clock = Clock()
    def limited():
        import urllib.error
        raise urllib.error.HTTPError("https://api.binance.com", 429, "Too Many Requests", {}, None)
    http = Replay("8453", price=limited)
    lane = _lane("8453", http, clock)
    c = _capture("8453")
    row = lane.observe(c)
    assert (row["quality"], row["quote_price"], row["quote_asset_usd"]) == ("price_unknown", None, None)
    assert http.posts == [], "an unsized buy asks no quoter"
    gets = len(http.gets)
    clock.t += 60
    lane.observe(c)
    assert len(http.gets) == gets, "a 429 holds the price read"


def test_exact_capacity_is_its_own_lane():
    captures = {}
    for i in range(6):
        cap.start(captures, mint="0x%040x" % i, origin_event_id="d%d" % i, gate="mcap_over_ceiling",
                  refused_at=REFUSED_AT, network_id="4663", horizon_s=810.0, cadence_s=25.0,
                  max_concurrent=6, now=0.0, now_epoch=REFUSED_EPOCH + 1)
    refused = []
    for i in range(7):
        cap.start(captures, mint="0x%040x" % i, origin_event_id="d%d" % i, gate="mcap_over_ceiling",
                  refused_at=REFUSED_AT, network_id="4663", horizon_s=810.0, cadence_s=25.0,
                  max_concurrent=6, now=0.0, now_epoch=REFUSED_EPOCH + 1, exact=True,
                  on_refuse=lambda key, outcome, *a: refused.append((key, outcome)))
    assert sum(1 for k in captures if k.endswith("#exact")) == 6
    assert refused == [("4663:0x%040x:d6#exact" % 6, "not_captured_capacity")]


def test_pump_hands_exact_captures_to_the_lane_only():
    captures = {}
    c = _capture("4663", captures)
    seen, rows = [], []

    class Lane:
        def observe(self, capture):
            seen.append(capture.capture_id)
            return {"capture_id": capture.capture_id}
    n = cap.pump(captures, now_mono=0.0, now_iso=REFUSED_AT, http=None, rpc_url=None, clip_usd=20.0,
                 record_fn=rows.append, quote_fn=lambda *a, **k: pytest.fail("Jupiter asked"),
                 display_fn=lambda keys: pytest.fail("DexScreener asked"), exact_lane=Lane())
    assert (n, seen, rows) == (1, [c.capture_id], [{"capture_id": c.capture_id}])
    assert cap.pump(captures, now_mono=25.0, now_iso=REFUSED_AT, http=None, rpc_url=None, clip_usd=20.0,
                    record_fn=rows.append) == 0, "no lane: no row and no error"


def test_the_knob_defaults_to_no_chain(monkeypatch):
    from sniper.settings import memecoin as cfg
    assert cfg.memecoin_capture_evm_exact_chains({}) == frozenset()
    assert cfg.memecoin_capture_evm_exact_chains(
        {"MEMECOIN_CAPTURE_EVM_EXACT_NETWORKS": "4663, bsc,base,solana"}) == frozenset({"4663", "56", "8453"})


def _caps_db(path):
    conn = store.connect(path)
    for cid, net, basis, side, out in (("solana:s:o1", "solana", "exact_route", "buy", "5"),
                                       ("solana:s:o1", "solana", "exact_route", "sell", "19000000"),
                                       ("4663:m:o2#exact", "4663", "exact_route", "buy", "5"),
                                       ("4663:m:o2#exact", "4663", "exact_route", "sell", "10" + "0" * 16),
                                       ("4663:m:o2", "4663", "display", None, None)):
        store.record_observation(conn, {
            "capture_id": cid, "origin_event_id": cid.split(":")[2].split("#")[0], "network_id": net,
            "mint": "m", "gate": "chase_first_seen", "refused_at": "2026-09-28T22:00:00+00:00",
            "observed_at": "2026-09-28T22:00:05+00:00", "age_s": 5.0 if side != "sell" else 30.0,
            "horizon_s": 810.0, "quote_side": side, "quote_price": 1.0, "quote_input_usd": 20.0,
            "quote_output_atomic": out, "quote_price_basis": basis, "quality": "ok"})
    return conn


def test_the_readers_ignore_exact_evm_rows(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import first_seen_refusal_outcomes as fs
    import gate_instrument_audit as gia
    conn = _caps_db(tmp_path / "caps.db")
    assert fs.captures(conn, "o2") == [(5.0, 1.0)], "display only; the exact series is another capture"
    monkeypatch.setattr(gia, "CAPS", "file:%s?mode=ro" % (tmp_path / "caps.db"))
    loaded = gia.load_captures("2026-09-28T00:00:00+00:00")
    assert [c["capture"] for c in loaded] == ["solana:s:o1"], "sell atoms are USDC only on Solana"


def test_the_v4_calldata_is_the_funded_quoters(monkeypatch):
    from sniper.execution import evm_v4_quote as v4
    key = FIX["chains"]["4663"]["v4_keys"][0][1]
    sent = []
    monkeypatch.setattr(v4, "_rpc", lambda url, method, params, http=None: sent.append(params[0]["data"]) or {})
    import sniper.execution.evm_read_rpc as rr
    monkeypatch.setattr(rr, "_freshest_read_member", lambda url: url)
    v4._quote_single("4663", key, True, 5 * 10 ** 15, "http://unused")
    assert ex.v4_quote_data(key, True, 5 * 10 ** 15) == sent[0]


def test_the_funded_process_never_imports_the_lane():
    code = ("import sys; import sniper.execution.evm_swap_executor, sniper.services.memecoin_real_runner; "
            "print('sniper.services.memecoin_evm_exact_capture' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True,
                         timeout=120, env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())})
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False"


def test_the_guard_compares_registrable_domains():
    """Beta C1: publicnode and Alchemy limit per IP or account across their chain hostnames."""
    env = {"MEMECOIN_EVM_WS_URL_4663": "wss://robinhood-rpc.publicnode.com",
           "MEMECOIN_EVM_RPC_POOL_URLS_4663": "https://robinhood-mainnet.g.alchemy.com/v2/funded",
           "MEMECOIN_CAPTURE_EVM_READ_URL_1": "https://eth-rpc.publicnode.com",
           "MEMECOIN_CAPTURE_EVM_READ_URL_8453": "https://base-mainnet.g.alchemy.com/v2/research",
           "MEMECOIN_CAPTURE_EVM_READ_URL_56": "https://bsc-dataseed1.ninicoin.io"}
    own = {"eth-rpc.publicnode.com": "0x1", "base-mainnet.g.alchemy.com": "0x2105",
           "bsc-dataseed1.ninicoin.io": "0x38"}

    def http(method, url, headers=None, body=None, timeout=None):     # each host answers its chain
        return 200, {"jsonrpc": "2.0", "id": 1, "result": own[url.split("/")[2]]}, {}
    assert ex.usable_read_urls(env, ("1", "8453", "56"), http=http) == {
        "56": "https://bsc-dataseed1.ninicoin.io"}, "refused on the shared domain, not the chain id"
    del env["MEMECOIN_EVM_WS_URL_4663"], env["MEMECOIN_EVM_RPC_POOL_URLS_4663"]
    # Alchemy frees up once no other role names alchemy.com; publicnode stays refused, because the
    # config's 4663 read pool falls back to robinhood-rpc.publicnode.com when no pool is set.
    assert sorted(ex.usable_read_urls(env, ("1", "8453", "56"), http=http)) == ["56", "8453"]
    assert (ex._domain("robinhood-mainnet.g.alchemy.com"), ex._domain("127.0.0.1"),
            ex._domain("localhost")) == ("alchemy.com", "127.0.0.1", "localhost")


@pytest.mark.parametrize("reply,held", [
    ((200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "rate limit exceeded"}}, {}), True),
    ((200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "Too Many Requests"}}, {}), True),
    ((403, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "forbidden"}}, {}), True),
    ((200, {"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted: max wallet limit"}}, {}), False),
])
def test_a_limit_in_words_holds_and_a_revert_that_says_limit_does_not(reply, held):
    """Beta C5 / Alpha: any 429/403, and any error text naming a limit, holds the host. A revert
    is the pool answering, even when its reason says "limit"."""
    lane = _lane("8453", Replay("8453", override=lambda key: reply))
    state = {}
    assert lane._call("8453", state)("0x" + "11" * 20, "0x00") is None
    stats = lane.stats()["base.drpc.org"]
    assert (stats["held"], stats["reverted"]) == ((1, 0) if held else (0, 1))


class _FakeLane:
    clip_usd = 20.0
    pending, refused = {}, frozenset()

    def __init__(self):
        self.seen = []

    def retry_pending(self):
        pass

    def observe(self, c):
        self.seen.append(c.capture_id)
        return {"capture_id": c.capture_id, "network_id": c.network_id, "mint": c.mint,
                "refused_at": c.refused_at, "observed_at": REFUSED_AT, "age_s": 1.0,
                "quote_price_basis": "exact_route", "quality": "ok"}

    def stats(self):
        return {}


def _refusal(i=0):
    from sniper.util.clock import real_utc_now_iso
    return {"mint": "0x%040x" % (i + 1), "origin_event_id": "w%d" % i, "gate": "mcap_over_ceiling",
            "refused_at": real_utc_now_iso(), "network_id": "4663"}


def test_the_worker_owns_its_captures_and_writes_its_rows(tmp_path):
    conn = store.connect(tmp_path / "caps.db")
    lane = _FakeLane()
    handed = {}
    w = ex.ExactWorker(lane, ledger_path=tmp_path / "unused.db", store_path=tmp_path / "caps.db",
                       horizon_s=810.0, cadence_s=25.0, max_concurrent=6, poll_s=0.01,
                       handed_off_fn=lambda ledger, c: handed.get(c.mint))
    w.submit(_refusal(0))
    w.submit(_refusal(1))
    assert w.run_once(conn, None) == 2
    assert sorted(lane.seen) == sorted(k for k in w.captures) and all(k.endswith("#exact") for k in w.captures)
    handed["0x%040x" % 1] = 77
    w.run_once(conn, None)
    rows = conn.execute("SELECT outcome, quote_id FROM memecoin_refusal_captures WHERE outcome IS NOT NULL").fetchall()
    assert [tuple(r) for r in rows] == [("handed_off_to_position", "77")] and len(w.captures) == 1
    assert w.stats()["pass_ms"] is not None and w.stats()["errors"] == 0


def test_the_worker_runs_on_its_own_thread(tmp_path):
    import time as _t
    store.connect(tmp_path / "caps.db").close()
    sqlite3.connect(tmp_path / "ledger.db").close()
    lane = _FakeLane()
    w = ex.ExactWorker(lane, ledger_path=tmp_path / "ledger.db", store_path=tmp_path / "caps.db",
                       horizon_s=810.0, cadence_s=25.0, max_concurrent=6, poll_s=0.01,
                       handed_off_fn=lambda ledger, c: None)
    w.start()
    try:
        w.submit(_refusal(0))
        deadline = _t.monotonic() + 5
        while not lane.seen and _t.monotonic() < deadline:
            _t.sleep(0.01)
        assert lane.seen and w.thread.name == "evm-exact-capture" and w.thread.daemon
    finally:
        w.stop()
        w.thread.join(timeout=5)
    assert not w.thread.is_alive()


def test_the_capture_loop_hands_exact_refusals_to_the_worker_only():
    """Alpha C6: the Solana grid never waits for an exact scan. The loop's own pump gets no lane
    and the loop never starts an exact capture itself."""
    code = [ln.split("#", 1)[0] for ln in (REPO / "scripts" / "memecoin_refusal_capture_loop.py").read_text().splitlines()]
    body = "\n".join(code)
    assert "exact_worker.submit(r)" in body
    assert "exact=True" not in body and "exact_lane=" not in body


def test_the_impact_shadow_read_uses_solana_sells_only(tmp_path):
    sys.path.insert(0, str(REPO / "scripts" / "research"))
    import impact_shadow_read as isr
    conn = store.connect(tmp_path / "caps.db")
    store.record_impact_shadow(conn, {"attempt_id": "a9", "origin_event_id": "o9", "network_id": "solana",
                                      "mint": "m", "refused_at": REFUSED_AT, "observed_at": REFUSED_AT,
                                      "clip_usd": 20.0})
    for cid, net, age, out in (("solana:m:o9", "solana", 25.0, "19000000"),
                               ("4663:m:o9#exact", "4663", 31.0, "5" + "0" * 16)):
        store.record_observation(conn, {"capture_id": cid, "origin_event_id": "o9", "network_id": net,
                                        "mint": "m", "refused_at": REFUSED_AT, "observed_at": REFUSED_AT,
                                        "age_s": age, "quote_side": "sell", "quote_output_atomic": out})
    ledger = sqlite3.connect(":memory:")
    ledger.row_factory = sqlite3.Row
    ledger.executescript("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, source_handle TEXT, trace_json TEXT);"
                         "CREATE TABLE memecoin_events (fomo_event_id TEXT, their_size_usd REAL);"
                         "CREATE TABLE onchain_wallet_detections (signature TEXT, source_size_usd REAL);")
    out = isr.rows(ledger, conn)
    assert out[0]["out_30s"] == pytest.approx(-5.0), "the Solana USDC sell, not WETH atoms read as USDC"


# --- the dip retry's EVM watch prices through this lane (2026-09-30, spec EVM extension) ---

@pytest.mark.parametrize("chain", CHAINS)
def test_the_dip_watch_price_is_the_capture_buy_price(chain):
    """buy_price_usd is observe()'s buy quote price for the same clip, from the same calls."""
    fx = FIX["chains"][chain]
    buy = _lane(chain, Replay(chain)).observe(_capture(chain))
    http = Replay(chain)
    got = _lane(chain, http).buy_price_usd(chain, fx["mint"].upper().replace("0X", "0x"), 20.0)
    assert http.unrecorded == [] and buy["quality"] == "ok"
    assert got == pytest.approx(buy["quote_price"])


@pytest.mark.parametrize("case", ["limit", "unknown_chain", "no_native_price"])
def test_the_dip_watch_price_is_unknown_when_the_lane_cannot_read(case):
    clock = Clock()
    if case == "limit":
        http = Replay("4663", override=lambda key: (429, {}, {}) if key[0] == "eth_call" else None)
    elif case == "no_native_price":
        http = Replay("4663", price=lambda: (500, {}, {}))
    else:
        http = Replay("4663")
    lane = _lane("4663", http, clock)
    chain = "56" if case == "unknown_chain" else "4663"
    assert lane.buy_price_usd(chain, FIX["chains"]["4663"]["mint"], 20.0) is None
    if case == "limit":
        calls = len(http.posts)
        assert lane.buy_price_usd("4663", FIX["chains"]["4663"]["mint"], 20.0) is None
        assert len(http.posts) == calls, "held: no call at all"


def test_a_route_hint_limits_the_scan_to_that_venue():
    """The dip watch passes the refused route's venue; a V4 hint never asks the V3 or V2 quoters."""
    from sniper.execution.evm_v4_quote import V4
    fx = FIX["chains"]["4663"]
    http = Replay("4663")
    lane = _lane("4663", http)
    lane.buy_price_usd("4663", fx["mint"], 20.0, kinds=("v4",))
    quoters = {key[1] for _url, key in http.posts if key[0] == "eth_call"}
    assert quoters <= {V4["4663"]["quoter"].lower(), fx["mint"].lower()}


def test_a_v3_hint_never_asks_the_v4_quoter():
    from sniper.execution.evm_v4_quote import V4
    fx = FIX["chains"]["4663"]
    http = Replay("4663")
    _lane("4663", http).buy_price_usd("4663", fx["mint"], 20.0, kinds=("v3",))
    asked = {key[1] for _url, key in http.posts if key[0] == "eth_call"}
    assert asked and V4["4663"]["quoter"].lower() not in asked and http.unrecorded == []


# --- store-only V4 multihop in the exact lane (2026-09-30, Alpha; Gamma: 15 of 41 exact rows on
#     4663 were no_route, 71 of 77 price with the store-only multihop search) ---

MH_MINT = "0x" + "c1" * 20
MH_BRIDGE = "0x" + "b2" * 20
MH_LEGS = [{"currency": MH_BRIDGE,
            "key": {"currency0": "0x" + "00" * 20, "currency1": MH_BRIDGE, "fee": 500,
                    "tick_spacing": 10, "hooks": "0x" + "00" * 20}},
           {"currency": MH_MINT,
            "key": {"currency0": MH_BRIDGE, "currency1": MH_MINT, "fee": 10000,
                    "tick_spacing": 200, "hooks": "0x" + "00" * 20}}]


def _pool_id(key):
    from eth_utils import keccak
    from sniper.execution.evm_v4_quote import _encode_pool_key
    return "0x" + keccak(bytes.fromhex(_encode_pool_key(key))).hex()


MH_POOLS = tuple(_pool_id(leg["key"]) for leg in MH_LEGS)       # the label names each leg's pool


class Bare:
    """A research host where no single-hop venue prices the coin: every eth_call reverts."""

    def __init__(self, status=200):
        self.posts, self.status = [], status

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        if method == "GET":
            return 200, {"price": "2500"}, {}
        req = json.loads(body)
        self.posts.append((url, req["method"]))
        if self.status != 200:
            return self.status, None, {}
        if req["params"] and isinstance(req["params"][0], dict) and (
                req["params"][0].get("data") == "0x313ce567"):
            return 200, {"result": "0x" + format(18, "064x")}, {}
        return 200, {"error": {"code": 3, "message": "execution reverted"}}, {}


def _bare_lane(monkeypatch, http=None, chains=("4663",)):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS", tuple(chains))
    clock = Clock()
    http = http or Bare()
    lane = ex.ExactLane({"4663": "https://research.invalid"}, rps=4, yield_s=120, clip_usd=20.0,
                        http=http, keys_fn=lambda c, m: [], clock=clock, sleep=clock.sleep,
                        wall=lambda: ("2026-09-30T13:00:30+00:00", REFUSED_EPOCH + 30))
    return lane, http


def _mh_capture():
    return cap.start({}, mint=MH_MINT, origin_event_id="o-mh", gate="execution_premium",
                     refused_at=REFUSED_AT, network_id="4663", horizon_s=810.0, cadence_s=25.0,
                     max_concurrent=6, now=0.0, now_epoch=REFUSED_EPOCH + 1, exact=True)


def _fake_search(monkeypatch, route="route", seen=None, legs=None, during=None):
    from sniper.execution import evm_v4_multihop as mh
    seen = [] if seen is None else seen
    legs = MH_LEGS if legs is None else legs

    def search(chain, cash, mint, amount, **kw):
        seen.append({"chain": chain, "cash": cash, "mint": mint, "amount": amount, **kw})
        if during is not None:
            during(kw)
        if route is None:
            return None
        return {"amount_out": 5 * 10 ** 18, "legs_path": legs, "bridge_pool_id": "0xbridge",
                "coin_pool_id": "0xcoin", "search_requests": 9, "discovery": "store"}
    monkeypatch.setattr(mh, "multihop_quote", search)
    return seen


def test_a_coin_no_single_hop_venue_prices_is_bought_through_the_store_only_multihop(monkeypatch):
    from sniper.execution import evm_v4_multihop as mh
    lane, http = _bare_lane(monkeypatch)
    seen = _fake_search(monkeypatch)
    c = _mh_capture()
    buy = lane.observe(c)
    [call] = seen
    assert call["store_only"] is True and call["limits"] == ex.MULTIHOP_LIMITS
    assert call["rpc_url"] == "https://research.invalid" and call["cash"] == mh._multihop_cash("4663")
    assert call["amount"] == int(20.0 / 2500 * 10 ** 18) and call["clip_usd"] == pytest.approx(20.0)
    assert (buy["quality"], buy["provider"], buy["quote_side"]) == ("ok", "evm_v4_multihop_quoter", "buy")
    assert buy["quote_route_target"] == "v4_multihop:%s>%s" % MH_POOLS
    assert buy["quote_output_atomic"] == str(5 * 10 ** 18) and buy["quote_price"] == pytest.approx(4.0)
    assert c.route["kind"] == "v4_multihop" and c.tokens_atomic == 5 * 10 ** 18


def test_the_sell_quotes_the_fixed_path_in_reverse_on_the_research_host(monkeypatch):
    from sniper.execution import evm_v4_multihop as mh
    lane, http = _bare_lane(monkeypatch)
    _fake_search(monkeypatch)
    c = _mh_capture()
    lane.observe(c)
    quoted = []
    monkeypatch.setattr(mh, "_quote_path", lambda chain, currency_in, path, amount, rpc_url, **kw: (
        quoted.append((currency_in, [(cur, key["fee"]) for cur, key in path], amount, rpc_url))
        or 7 * 10 ** 15))
    sell = lane.observe(c)
    assert quoted == [(MH_MINT, [(MH_BRIDGE, 10000), (mh._multihop_cash("4663"), 500)],
                       5 * 10 ** 18, "https://research.invalid")]
    assert (sell["quote_side"], sell["quality"]) == ("sell", "ok")
    assert sell["quote_output_atomic"] == str(7 * 10 ** 15)
    assert sell["quote_price"] == pytest.approx(7e-3 * 2500 / 5.0)


def test_a_single_hop_route_is_never_followed_by_a_multihop_search(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS", ("4663",))
    seen = _fake_search(monkeypatch)
    http = Replay("4663")
    buy = _lane("4663", http).observe(_capture("4663"))
    assert buy["quality"] == "ok" and seen == []


@pytest.mark.parametrize("case", ["chain_not_named", "no_route", "url_in_a_pool"])
def test_when_the_multihop_search_does_not_run_or_finds_nothing_the_row_is_no_route(
        monkeypatch, case):
    from sniper.execution import evm_rpc_pool
    lane, http = _bare_lane(monkeypatch, chains=() if case == "chain_not_named" else ("4663",))
    seen = _fake_search(monkeypatch, route=None if case == "no_route" else "route")
    if case == "url_in_a_pool":
        # a read URL that expands into a pool could reach a funded member: never search through it
        monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda url: [url, "https://funded.invalid"])
    buy = lane.observe(_mh_capture())
    assert buy["quality"] == "no_route"
    assert len(seen) == (1 if case == "no_route" else 0)


def test_the_lanes_rpc_seam_reaches_only_its_own_read_host(monkeypatch):
    from sniper.execution.evm_rpc_pool import RequestNotSent
    lane, http = _bare_lane(monkeypatch)
    state = {}
    seam = lane._rpc_http("4663", state)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    with pytest.raises(RequestNotSent):
        seam("POST", "https://funded-pool.invalid", body=body)
    with pytest.raises(RequestNotSent):
        seam("GET", "https://api.dexscreener.com/latest/dex/tokens/x")
    assert http.posts == [] and not state.get("failed")
    status, reply, _h = seam("POST", "https://research.invalid", body=body)
    assert status == 200 and http.posts == [("https://research.invalid", "eth_blockNumber")]
    assert lane.stats()["research.invalid"]["calls"] == 1


def test_a_limit_inside_the_multihop_search_holds_the_host_and_writes_nothing(monkeypatch):
    from sniper.execution import evm_v4_multihop as mh
    lane, http = _bare_lane(monkeypatch)

    def search(chain, cash, mint, amount, *, http, rpc_url, **kw):
        try:
            http("POST", rpc_url, body=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                   "method": "eth_blockNumber", "params": []}))
        except Exception:
            return None
        return None
    monkeypatch.setattr(mh, "multihop_quote", search)
    http.status = 200
    c = _mh_capture()
    real = http.__call__

    def limited(method, url, headers=None, body=None, timeout=None):
        if method == "POST" and json.loads(body)["method"] == "eth_blockNumber":
            return 429, None, {}
        return Bare.__call__(http, method, url, headers=headers, body=body, timeout=timeout)
    lane.http = limited
    assert lane.observe(c) is None                                  # a gap, never a no_route row
    assert lane.hosts["4663"].held() and lane.stats()["research.invalid"]["held"] == 1
    assert c.route is None


def test_the_multihop_knob_defaults_to_4663_only():
    import os
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS"}
    out = subprocess.run(
        [sys.executable, "-c", "from sniper.settings import memecoin as c; "
         "print(c.MEMECOIN_CAPTURE_EVM_MULTIHOP_CHAINS)"],
        env=env, capture_output=True, text=True, timeout=60, check=True, cwd=str(REPO))
    assert out.stdout.strip() == "('4663',)"


def test_the_exact_lane_has_four_venues_and_no_curve():
    """Stated for the report: a launchpad-curve coin (0xd1ced051, 2026-09-30) has no route in this
    lane, because the lane has no curve venue. Building one is a separate decision."""
    assert set(ex.PROVIDER) == {"v4", "v3", "v2", "v4_multihop"}


# --- Gamma LOW on 8e4919f48: three surviving mutants, the 3-leg label, the docstring ---

def test_the_seam_takes_from_the_lanes_bucket_so_a_search_cannot_outrun_the_rate(monkeypatch):
    """`host.take()` is the only bound on a 16-30 request search. At 4 requests/s and a bucket of 4,
    12 requests need 2 s of waiting; without the bucket they would need none."""
    lane, http = _bare_lane(monkeypatch)
    seam = lane._rpc_http("4663", {})
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    start = lane.clock()
    for _ in range(12):
        seam("POST", "https://research.invalid", body=body)
    waited = lane.clock() - start
    assert waited == pytest.approx((12 - 4) / 4.0, abs=0.01), waited
    assert len(http.posts) == 12


def test_the_seam_refuses_every_method_but_post_even_to_its_own_host(monkeypatch):
    from sniper.execution.evm_rpc_pool import RequestNotSent
    lane, http = _bare_lane(monkeypatch)
    seam = lane._rpc_http("4663", {})
    for method in ("GET", "PUT", "DELETE"):
        with pytest.raises(RequestNotSent):
            seam(method, "https://research.invalid")
    assert http.posts == [] and lane.stats()["research.invalid"]["calls"] == 0


def test_a_route_found_while_the_read_failed_is_not_a_route(monkeypatch):
    """The search can return a route it built before a later read of the same observation failed.
    `_multihop` itself drops it (the caller's own check would too, which is why only a direct call
    can tell the two apart). A failed observation is a gap: nothing is used and nothing cached."""
    lane, http = _bare_lane(monkeypatch)

    def fail_a_read(kw):
        with pytest.raises(Exception):
            lane.hosts["4663"].hold("status=429 code=15")
            kw["http"]("POST", "https://research.invalid", body=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}))
    _fake_search(monkeypatch, during=fail_a_read)             # ... and the search still answers
    state = {}
    assert lane._multihop("4663", MH_MINT, 10 ** 16, state) is None
    assert state.get("failed") == "held"
    lane2, _http2 = _bare_lane(monkeypatch)
    _fake_search(monkeypatch)                                  # control: no failure, a route
    assert lane2._multihop("4663", MH_MINT, 10 ** 16, {})[1] == "v4_multihop"
    c = _mh_capture()
    _fake_search(monkeypatch, during=fail_a_read)
    assert lane.observe(c) is None and c.route is None
    assert ("4663", MH_MINT) not in lane._routes


def test_a_three_leg_path_is_labelled_with_all_three_pools(monkeypatch):
    middle = "0x" + "d4" * 20
    legs = [{"currency": middle, "key": {"currency0": "0x" + "00" * 20, "currency1": middle,
                                          "fee": 500, "tick_spacing": 10, "hooks": "0x" + "00" * 20}},
            {"currency": MH_BRIDGE, "key": {"currency0": MH_BRIDGE, "currency1": middle, "fee": 3000,
                                            "tick_spacing": 60, "hooks": "0x" + "00" * 20}},
            MH_LEGS[1]]
    lane, http = _bare_lane(monkeypatch)
    _fake_search(monkeypatch, legs=legs)
    buy = lane.observe(_mh_capture())
    pools = [_pool_id(leg["key"]) for leg in legs]
    assert len(set(pools)) == 3
    assert buy["quote_route_target"] == "v4_multihop:" + ">".join(pools)


def test_the_module_docstring_names_its_real_importers():
    doc = ex.__doc__
    assert "memecoin_refusal_capture_loop.py" in doc and "EvmDipWatch" not in doc


# --- a read host that did not answer its identity check is PENDING, not refused ------------------
#
# Alpha, 2026-09-30. At its 15:01:07Z relaunch the capture loop refused chain 8453 for the life of
# the process because base-mainnet.public.blastapi.io answered 429 to eth_chainId ("answered chain
# None (status 429), not 8453"); the same on 2026-09-29 17:57. A host that did not answer has not
# said which chain it is.
#
# Rule 5 (CLAUDE.md):
# 1. Money path: none. The lane lives in the capture loop's process; the funded process never
#    imports it (test_the_funded_process_never_imports_the_lane).
# 2. Fill-path speed: none. The retry runs on the lane's own thread (evm-exact-capture).
# 3. Concurrency: `pending` and `refused` live on the lane and change on its thread only; the
#    heartbeat reads a copy.
# 4. Absent vs zero vs unknown: the right chain id admits; ANOTHER chain id refuses for good; no
#    chain id (limit, timeout, error reply, null) is unknown: pending, never admitted, never refused.
# 5. Dependencies: the check is retried 30 s, 60 s, 120 s after the last failure, then every 300 s.
#    NOT handled: a host that never answers is asked every 5 minutes for the life of the process
#    (288 calls a day), and a pending chain's captures hold slots of the shared exact budget.
# 6. Units: a hex chain id against the decimal chain key; seconds on the lane's monotonic clock.
# 7. Sample: a pending chain's captures are started and miss observations (gaps), which the
#    completeness label already counts; they never read another chain and never write no_route.
# 8. Rollback: revert the commit. No schema, no setting.

_UNKNOWN = [(429, None, {}), (503, None, {}), OSError("timed out"),
            (200, {"error": {"code": -32005, "message": "limit exceeded"}}, {}),
            (200, {"result": None}, {}), (200, "<html>busy</html>", {})]


class _Identity:
    """eth_chainId answered from a script; every other call goes to `inner` (the recording)."""

    def __init__(self, inner, answers):
        self.inner, self.answers, self.asked = inner, list(answers), []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        if method == "POST" and json.loads(body)["method"] == "eth_chainId":
            self.asked.append(url)
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return self.inner(method, url, headers=headers, body=body, timeout=timeout)


def _right(chain):
    return 200, {"jsonrpc": "2.0", "id": 1, "result": hex(int(chain))}, {}


_BSC = {"MEMECOIN_CAPTURE_EVM_READ_URL_56": "https://bsc-dataseed1.ninicoin.io"}


@pytest.mark.parametrize("answer", _UNKNOWN, ids=["429", "503", "timeout", "rpc-limit", "null", "html"])
def test_a_host_that_did_not_answer_its_identity_check_is_pending_not_refused(answer):
    assert ex.read_urls(_BSC, ("56",), http=_Identity(None, [answer])) == (
        {}, {"56": "https://bsc-dataseed1.ninicoin.io"})
    # The one-shot research scripts keep the strict answer: only a proven host.
    assert ex.usable_read_urls(_BSC, ("56",), http=_Identity(None, [answer])) == {}


def test_a_wrong_chain_id_an_unset_host_and_a_shared_host_are_refused_and_never_pending():
    assert ex.read_urls(_BSC, ("56",), http=_Identity(None, [_right("8453")])) == ({}, {})
    assert ex.read_urls(_BSC, ("56",), http=_Identity(None, [_right("56")])) == (
        {"56": "https://bsc-dataseed1.ninicoin.io"}, {})
    shared = dict(_BSC, MEMECOIN_ONCHAIN_EVM_RPC_URL_56="https://bsc-dataseed1.ninicoin.io")
    http = _Identity(None, [])
    assert ex.read_urls(shared, ("56", "5042"), http=http) == ({}, {}) and http.asked == []


def _pending_lane(chain, answers):
    replay, clock = Replay(chain), Clock()
    http = _Identity(replay, answers)
    return _lane(chain, http, clock, pending=True), http, replay, clock


@pytest.mark.parametrize("chain", CHAINS)
def test_a_pending_chain_makes_no_call_and_its_observation_is_a_gap(chain):
    lane, http, replay, clock = _pending_lane(chain, [])
    assert lane.observe(_capture(chain)) is None
    assert lane.buy_price_usd(chain, FIX["chains"][chain]["mint"], 20.0) is None
    assert replay.posts == [] and replay.gets == [] and http.asked == []


@pytest.mark.parametrize("chain", CHAINS)
def test_a_429_then_the_right_chain_id_admits_the_chain(chain):
    lane, http, replay, clock = _pending_lane(chain, [(429, None, {}), _right(chain)])
    url = "https://" + FIX["chains"][chain]["host"]
    lane.retry_pending()
    assert http.asked == [], "the startup check just failed; the first retry waits 30 s"
    clock.t += 30.0
    lane.retry_pending()
    assert http.asked == [url] and list(lane.pending) == [chain] and lane.observe(_capture(chain)) is None
    clock.t += 59.0
    lane.retry_pending()
    assert http.asked == [url]
    clock.t += 1.0
    lane.retry_pending()
    assert http.asked == [url, url] and not lane.pending and not lane.refused
    row = lane.observe(_capture(chain))
    assert row is not None and row["quality"] == "ok" and replay.posts
    assert {u for u, _k in replay.posts} == {url}
    clock.t += 1000.0
    lane.retry_pending()
    assert http.asked == [url, url], "an admitted chain is not asked again"


def test_the_identity_check_backs_off_30_60_120_then_every_300_s():
    chain = CHAINS[0]
    lane, http, replay, clock = _pending_lane(chain, [(429, None, {})] * 7)
    start, asked_at = clock.t, []
    for _ in range(1200):
        clock.t += 1.0
        before = len(http.asked)
        lane.retry_pending()
        if len(http.asked) > before:
            asked_at.append(clock.t - start)
    assert asked_at == [30.0, 90.0, 210.0, 510.0, 810.0, 1110.0]
    assert list(lane.pending) == [chain] and replay.posts == []


def test_a_retry_that_answers_another_chain_id_refuses_the_chain_for_good(tmp_path, caplog):
    chain = CHAINS[0]
    lane, http, replay, clock = _pending_lane(chain, [_right("1")])
    clock.t += 30.0
    with caplog.at_level("ERROR"):
        lane.retry_pending()
    assert not lane.pending and set(lane.refused) == {chain}
    assert "REFUSED" in caplog.text and FIX["chains"][chain]["host"] in caplog.text
    clock.t += 5000.0
    lane.retry_pending()
    assert len(http.asked) == 1 and lane.observe(_capture(chain)) is None and replay.posts == []
    # and the worker starts no capture for it
    conn = store.connect(tmp_path / "caps.db")
    w = ex.ExactWorker(lane, ledger_path=tmp_path / "unused.db", store_path=tmp_path / "caps.db",
                       horizon_s=810.0, cadence_s=25.0, max_concurrent=6, poll_s=0.01,
                       handed_off_fn=lambda ledger, c: None, clock=clock)
    w.submit(dict(_refusal(0), network_id=chain, mint=FIX["chains"][chain]["mint"]))
    assert w.run_once(conn, None) == 0 and w.captures == {}
    assert w.stats()["refused"] == [chain] and w.stats()["pending"] == []
    assert conn.execute("SELECT COUNT(*) FROM memecoin_refusal_captures").fetchone()[0] == 0


def test_the_worker_retries_a_pending_chain_and_its_heartbeat_names_it(tmp_path):
    chain = CHAINS[0]
    lane, http, replay, clock = _pending_lane(chain, [(429, None, {}), _right(chain)])
    conn = store.connect(tmp_path / "caps.db")
    w = ex.ExactWorker(lane, ledger_path=tmp_path / "unused.db", store_path=tmp_path / "caps.db",
                       horizon_s=810.0, cadence_s=25.0, max_concurrent=6, poll_s=0.01,
                       handed_off_fn=lambda ledger, c: None, clock=clock)
    w.submit(dict(_refusal(0), network_id=chain, mint=FIX["chains"][chain]["mint"]))
    assert w.run_once(conn, None) == 0 and len(w.captures) == 1, "captured, and the observation is a gap"
    assert w.stats()["pending"] == [chain] and http.asked == [] and replay.posts == []
    clock.t += 30.0
    assert w.run_once(conn, None) == 0 and w.stats()["pending"] == [chain] and len(http.asked) == 1
    clock.t += 60.0
    assert w.run_once(conn, None) == 1 and w.stats()["pending"] == [] and len(http.asked) == 2
    rows = conn.execute("SELECT quality, network_id FROM memecoin_refusal_captures").fetchall()
    assert [tuple(r) for r in rows] == [("ok", chain)], "no row on a wrong chain, no no_route row"


def test_the_identity_retry_takes_the_hosts_rate_bucket():
    """Gamma LOW 3 on 328e08bc4: the retry is a call to the host like any other, so it waits for a
    token when the bucket is empty."""
    chain = CHAINS[0]
    lane, http, replay, clock = _pending_lane(chain, [_right(chain)])
    clock.t += 30.0
    host = lane.hosts[chain]
    host.tokens, host.stamp = 0.0, clock.t
    lane.retry_pending()
    assert len(http.asked) == 1 and clock.t == pytest.approx(1030.25), "one token at 4/s is 0.25 s"
    assert host.tokens < 1.0


def _env_cfg(monkeypatch, chains):
    from sniper.settings import memecoin as cfg
    env = {"MEMECOIN_CAPTURE_EVM_EXACT_NETWORKS": ",".join(chains),
           "MEMECOIN_CAPTURE_EVM_READ_URL_56": "https://bsc-dataseed1.ninicoin.io",
           "MEMECOIN_CAPTURE_EVM_READ_URL_4663": "https://robinhood.drpc.org",
           "MEMECOIN_CAPTURE_EVM_READ_URL_8453": "https://mainnet.base.org"}     # a free-submission host
    return env, cfg


def _build(tmp_path, env, cfg, http, clock):
    return ex.build_worker(env, cfg, ledger_path=tmp_path / "unused.db", store_path=tmp_path / "caps.db",
                           horizon_s=810.0, cadence_s=25.0, handed_off_fn=lambda ledger, c: None,
                           http=http, clock=clock, sleep=clock.sleep)


def test_a_relaunch_with_every_host_rate_limited_still_starts_the_worker(tmp_path, monkeypatch, caplog):
    """Gamma LOW 1 and 2 on 328e08bc4, the defect of 2026-09-30 15:01:07Z in its worst case: no host
    answers the identity check. The worker exists, holds every chain as pending, and its retries
    find a host for each (a lane handed the usable hosts only raised KeyError on every retry)."""
    env, cfg = _env_cfg(monkeypatch, ("56", "4663"))
    clock = Clock()
    http = _Identity(None, [(429, None, {}), (429, None, {}), _right("4663"), _right("56")])
    with caplog.at_level("INFO"):
        worker, networks = _build(tmp_path, env, cfg, http, clock)
    assert worker is not None and networks == ("4663", "56")
    assert "chains=off pending=4663,56 refused=none" in caplog.text
    assert sorted(worker.lane.hosts) == sorted(worker.lane.pending) == ["4663", "56"]
    assert worker.stats()["pending"] == ["4663", "56"]
    clock.t += 30.0
    conn = store.connect(tmp_path / "caps.db")
    assert worker.run_once(conn, None) == 0 and worker.stats()["pending"] == []
    assert http.asked[2:] == ["https://robinhood.drpc.org", "https://bsc-dataseed1.ninicoin.io"]


def test_the_worker_holds_the_usable_and_the_pending_chains_and_not_the_refused(tmp_path, monkeypatch, caplog):
    env, cfg = _env_cfg(monkeypatch, ("56", "4663", "8453", "5042"))
    clock = Clock()
    http = _Identity(None, [(429, None, {}), _right("56")])          # asked in chain order: 4663, then 56
    with caplog.at_level("INFO"):
        worker, networks = _build(tmp_path, env, cfg, http, clock)
    assert networks == ("4663", "56") and sorted(worker.lane.hosts) == ["4663", "56"]
    assert list(worker.lane.pending) == ["4663"] and "chains=56 pending=4663 refused=5042,8453" in caplog.text
    assert worker.max_concurrent == cfg.MEMECOIN_CAPTURE_EVM_EXACT_MAX_CONCURRENT
    assert worker.lane.clip_usd == float(cfg.MEMECOIN_CAPTURE_CLIP_USD)


def test_no_worker_when_every_chain_is_refused_for_good(tmp_path, monkeypatch):
    env, cfg = _env_cfg(monkeypatch, ("8453", "5042"))
    assert _build(tmp_path, env, cfg, _Identity(None, []), Clock()) == (None, ())
    env, cfg = _env_cfg(monkeypatch, ("56",))
    assert _build(tmp_path, env, cfg, _Identity(None, [_right("1")]), Clock()) == (None, ())


def test_the_capture_loop_builds_the_exact_worker_in_one_call():
    """The loop cannot merge the usable and the pending hosts wrong: it never sees them."""
    code = [ln.split("#", 1)[0] for ln in (REPO / "scripts" / "memecoin_refusal_capture_loop.py").read_text().splitlines()]
    body = "\n".join(code)
    assert "exact_worker, exact_networks = exact_capture.build_worker(" in body
    assert all(name not in body for name in ("read_urls", "ExactLane(", "ExactWorker(", "exact_pending"))


def test_a_keyed_drpc_url_is_a_budget_apart_from_the_keyless_drpc_hosts():
    """Gamma probe 2026-10-02: dRPC limits a keyed account (`dkey`) apart from its keyless per-IP
    hosts. dRPC alone splits; every key reads as one budget; a blank key or a key in the path reads
    as keyless. Each of those refuses more, never less."""
    keyed = "https://lb.drpc.org/ogrpc?network=robinhood&dkey=SECRET"
    env = {"MEMECOIN_CAPTURE_EVM_READ_URL_4663": "https://robinhood.drpc.org",
           "MEMECOIN_RESEARCH_DRPC_RPC_URL_4663": keyed}
    assert ex.usable_read_urls(env, ("4663",), http=_chain_id_http("0x1237")) == {
        "4663": "https://robinhood.drpc.org"}, "keyless capture beside the keyed research account"
    env["MEMECOIN_RESEARCH_OTHER"] = "https://bsc.drpc.org"
    assert ex.usable_read_urls(env, ("4663",), http=_chain_id_http("0x1237")) == {}, \
        "a second keyless dRPC host shares the per-IP budget"
    del env["MEMECOIN_RESEARCH_OTHER"]
    env["MEMECOIN_CAPTURE_EVM_READ_URL_4663"] = keyed.replace("robinhood", "base")
    assert ex.usable_read_urls(env, ("4663",), http=_chain_id_http("0x1237")) == {}, \
        "two keyed dRPC URLs share the account budget"
    assert ex.budget(keyed) == "drpc.org (keyed)" and "SECRET" not in ex.budget(keyed)
    assert ex.budget("https://lb.drpc.org/ogrpc?network=base&dkey=") == "drpc.org"
    assert ex.budget("https://lb.drpc.org/base/SECRET") == "drpc.org"
    assert ex.budget("https://x.quiknode.pro/k/?dkey=1") == "quiknode.pro"
    assert ex.budget("https://robinhood-mainnet.g.alchemy.com/v2/k") == "alchemy.com"


def test_the_hop_seed_is_an_env_host_now_and_no_code_set_host_is_taken():
    """Alpha 2026-10-02: the BSC tape seeds on MEMECOIN_RESEARCH_DRPC_RPC_URL_56 (1rpc answered
    -32001 on every call), an env var the derived guards see like any other; nothing in code adds a
    host, so 1rpc is no longer taken."""
    from sniper.services import memecoin_bsc_v2_live as tape
    assert tape.HOP_SEED_URL_VAR == "MEMECOIN_RESEARCH_DRPC_RPC_URL_56"
    assert ex.budget("https://1rpc.io/bnb") not in ex.taken_budgets({})
