"""New PCS pairs supply bounded read targets; only the existing canonical tape authorizes a hop."""
import json
import pytest

from eth_utils import keccak
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_bsc_v2_live as tape

QUOTE = "0xeee9eee593cb8f7946260b4066cba7907f40acfa"
COIN = "0x41df9249b0c4e34eab6e9591e02d5d7a31547777"
FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"


def pair_created():
    return {"address": FACTORY, "removed": False,
            "topics": ["0x" + keccak(b"PairCreated(address,address,address,uint256)").hex(),
                       "0x" + COIN[2:].rjust(64, "0"), "0x" + QUOTE[2:].rjust(64, "0")],
            "data": "0x" + bsc.our_pair(COIN, QUOTE)[2:].rjust(64, "0") + format(1, "064x")}


def test_new_coin_pair_warms_its_unlisted_quote_and_persists_only_targets(tmp_path):
    targets = tape.created_hop_targets(pair_created())
    hop = bsc.our_pair(bsc.WBNB, QUOTE)
    assert targets[hop] == QUOTE
    c = tape.connect(tmp_path / "tape.db")
    tape.remember_hop_targets(c, targets, 1.0)
    assert tape.discovered_hop_pairs(c)[hop] == QUOTE
    seeder = tape.HopSeeder({})
    seeder.window(10.0, 100)
    seeder.update_hops(targets)
    assert hop in seeder.pending
    # A target supplies no reserve facts, including after restart.
    assert bsc.tape_hop_state(c, hop, 100, now=10.0) == "no_head"
    tape.record_head(c, 100, "0x" + "a" * 64, 10.0, 10.0)
    tape.record_seed(c, hop, 100, "0x" + "a" * 64, 10**20, 10**20, 10.0)
    assert bsc.tape_hop_state(c, hop, 100, now=10.0) == (10**20, 10**20, 100)
    tape.reset_coverage(c)
    assert bsc.tape_hop_state(c, hop, 100, now=10.0) == "no_head"
    assert tape.discovered_hop_pairs(c)[hop] == QUOTE
    c.close()


def test_foreign_removed_malformed_or_wrong_create2_pair_is_never_a_target():
    for key, value in [("address", QUOTE), ("removed", True), ("data", "0x"),
                       ("data", "0x" + "0" * 128)]:
        assert tape.created_hop_targets(dict(pair_created(), **{key: value})) == {}


def test_discovery_is_bounded_and_bootstraps_quote_from_existing_mixed_routes(tmp_path):
    c = tape.connect(tmp_path / "tape.db")
    for i in range(tape.HOP_TARGET_CAP + 3):
        quote = "0x" + format(i + 1, "040x")
        tape.remember_hop_targets(c, {bsc.our_pair(bsc.WBNB, quote): quote}, float(i))
    assert len(tape.discovered_hop_pairs(c)) == tape.HOP_TARGET_CAP
    routes = [{"protocol": "pcs_mixed_v3_v2", "base_token": QUOTE},
              {"protocol": "pcs_mixed_v3_v2", "base_token": "invalid"}]
    assert tape.route_hop_targets([json.dumps(r) for r in routes]) == {
        bsc.our_pair(bsc.WBNB, QUOTE): QUOTE}
    c.close()



# F1c: real stored 56 routes (memecoin_positions 66432 uniswap_v2 via DOGE, 70947 direct, 71432 mixed).
ROUTE_66432 = ('{"path":["0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c","0xba2ae424d960c26247dd6c32edc70b295c744c43",'
               '"0x7b307ad0360499099291db190c5c0b111c147777"],"protocol":"uniswap_v2","round_trip_measured_by":"v2",'
               '"round_trip_pct":-6.018852361472582,"router":"0x10ed43c718714eb63d5aa57b78b54704e256024e","tokens":['
               '"0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c","0xba2ae424d960c26247dd6c32edc70b295c744c43",'
               '"0x7b307ad0360499099291db190c5c0b111c147777"]}')
ROUTE_70947 = ('{"path":["0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c","0x36420ae5ec60cd00451a7bc6a06af4d702157777"],'
               '"protocol":"uniswap_v2","round_trip_measured_by":"v2","round_trip_pct":-5.224840511783613,'
               '"router":"0x10ed43c718714eb63d5aa57b78b54704e256024e","tokens":["0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",'
               '"0x36420ae5ec60cd00451a7bc6a06af4d702157777"]}')
ROUTE_71432 = ('{"base_token":"0xba2ae424d960c26247dd6c32edc70b295c744c43","exit_proof":"exit_simulated",'
               '"protocol":"pcs_mixed_v3_v2","router":"0x13f4ea83d0bd40e75c8222255bc855a974568dd4","v3_fees":[2500],'
               '"v3_tokens":["0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c","0xba2ae424d960c26247dd6c32edc70b295c744c43"]}')
DOGE = "0xba2ae424d960c26247dd6c32edc70b295c744c43"


def test_v2_path_middle_of_a_real_stored_route_is_pinned():
    """SWOGE 71432 missed its WBNB/DOGE hop (hop_state_stale:untracked): DOGE was a funded hop
    only through 66432's uniswap_v2 path, which route_hop_targets never read."""
    assert tape.route_hop_targets([ROUTE_66432]) == {bsc.our_pair(bsc.WBNB, DOGE): DOGE}
    assert tape.route_hop_targets([ROUTE_70947]) == {}
    assert tape.route_hop_targets([ROUTE_66432, ROUTE_70947, ROUTE_71432, ""]) == {
        bsc.our_pair(bsc.WBNB, DOGE): DOGE}


def v2_route(path, router=bsc.PCS_V2_ROUTER):
    return json.dumps({"protocol": "uniswap_v2", "router": router, "path": path})


def test_v2_path_pin_is_the_coin_side_quote_on_the_pcs_router_only():
    w, coin = bsc.WBNB, COIN
    # The lane prices WBNB -> quote -> coin: the coin's quote is path[-2]; earlier middles are not hops.
    assert tape.route_hop_targets([v2_route([w, DOGE, QUOTE, coin])]) == {bsc.our_pair(w, QUOTE): QUOTE}
    for path in ([w, w, coin], [w, "0x" + "0" * 40, coin], [w, "nope", coin], [w, 7, coin], [w, coin],
                 [coin], [], None, "0xabc"):
        assert tape.route_hop_targets([v2_route(path)]) == {}, path
    # Another V2 factory's route: the lane only ever reads the PCS pair, so it pins nothing.
    assert tape.route_hop_targets([v2_route([w, DOGE, coin], router="0x" + "1" * 40)]) == {}
    assert tape.route_hop_targets([v2_route([w, DOGE.upper().replace("0X", "0x"), coin],
                                            router=bsc.PCS_V2_ROUTER.upper().replace("0X", "0x"))]) == {
        bsc.our_pair(w, DOGE): DOGE}


@pytest.mark.parametrize("discovery_ack", ["accepted", "rejected", "missing"])
def test_live_observer_receives_creation_subscription_and_seeds_before_next_buy(tmp_path, monkeypatch, discovery_ack):
    import time
    import pytest
    import websockets.sync.client
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "live")
    monkeypatch.setenv(tape.HOP_SEED_URL_VAR, "https://fixture.invalid")
    path = tmp_path / "live.db"
    monkeypatch.setattr(tape, "db_path", lambda: path)
    monkeypatch.setattr(tape, "tape_hop_pairs", lambda: {bsc.our_pair(bsc.WBNB, COIN): COIN})
    monkeypatch.setattr(tape, "_open_pools", lambda: ({}, {}))
    monkeypatch.setattr("sniper.execution.evm_wallet.quote_usd_price", lambda _: 700.0)
    monkeypatch.setattr(tape, "HOP_SEED_GAP_S", 0)
    monkeypatch.setattr(tape, "_rpc", lambda method, *args, **kwargs: "0x38")
    monkeypatch.setattr(tape, "_paced_seed_rpc", lambda *args: "0x" + format(7, "064x") + format(9, "064x"))
    frames = [{"id": 1, "result": "0x38"}] + [
        {"id": i, "result": "s%d" % i} for i in range(2, 7)]
    if discovery_ack != "accepted":
        frames = [f for f in frames if f.get("id") != 6]
        if discovery_ack == "rejected":
            frames.insert(1, {"id": 6, "error": {"code": -32601}})
    for height in (100, 101):
        frames.append({"params": {"subscription": "s2", "result": {
            "number": hex(height), "hash": "0x%064x" % height,
            "parentHash": "0x%064x" % (height - 1), "timestamp": hex(int(time.time()))}}})
        if height == 100 and discovery_ack == "accepted":
            frames.append({"params": {"subscription": "s6", "result": pair_created()}})
        frames.extend([None] * 25)
    offset = [0]
    monkeypatch.setattr(tape, "real_utc_now", lambda: time.time() + offset[0])
    class WS:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def send(self, message): pass
        def ping(self): pass
        def recv(self, **kwargs):
            if not frames: raise KeyboardInterrupt
            frame = frames.pop(0)
            if frame is None:
                time.sleep(.01)
                raise TimeoutError
            item = (frame.get("params") or {}).get("result") or {}
            if item.get("number") == hex(101) and discovery_ack == "missing":
                offset[0] = 11
                item["timestamp"] = hex(int(time.time() + 11))
            return json.dumps(frame)
    connects = [0]
    def connect(*args, **kwargs):
        connects[0] += 1
        if connects[0] > 1:
            raise KeyboardInterrupt
        return WS()
    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    with pytest.raises(KeyboardInterrupt):
        tape.run(ws_url="wss://fixture.invalid", rpc_url="https://fixture.invalid")
    c = tape.connect(path, readonly=True)
    hop = bsc.our_pair(bsc.WBNB, QUOTE)
    if discovery_ack == "accepted":
        assert tape.discovered_hop_pairs(c)[hop] == QUOTE
        assert bsc.tape_hop_state(c, hop, 101, now=time.time()) is not None
    else:
        assert c.execute("SELECT height FROM feed WHERE id=1").fetchone()[0] == 101
    c.close()


def test_absent_hops_rotate_behind_unattempted_targets():
    seeder = tape.HopSeeder({'a', 'b', 'c', 'd', 'good'})
    seeder.window(1, 100)
    first = seeder.batch()
    assert 'good' not in first
    seeder.done([], False, 1, attempted=first)
    assert 'good' in seeder.batch()
    seeder.update_hops({'good'})
    assert set(seeder.tried) <= seeder.hops
    seeder.window(2, 101)
    assert seeder.tried == {}


# ---- F1b (operator standing order via Alpha 2026-10-07, dfeaef22): funded-route quotes are pinned ----
# The 128 newest targets turned over in ~11 min (every PairCreated adds 1-2), so the WBNB hops of
# quotes our own funded entries used (this QUOTE: 41df9249 on 10-05) were evicted within minutes of a
# restart and the lane skipped them as hop_state_stale while the slower path filled.
import sqlite3

FUNDED = bsc.our_pair(bsc.WBNB, QUOTE)


def _created(i):
    a, b = "0x%040x" % (0x1000 + i), "0x%040x" % (0x900000 + i)        # a non-WBNB pair, a < b
    return {"address": FACTORY, "removed": False,
            "topics": ["0x" + keccak(b"PairCreated(address,address,address,uint256)").hex(),
                       "0x" + a[2:].rjust(64, "0"), "0x" + b[2:].rjust(64, "0")],
            "data": "0x" + bsc.our_pair(a, b)[2:].rjust(64, "0") + format(1, "064x")}


def test_a_funded_route_pair_survives_200_new_pair_creations(tmp_path):
    c = tape.connect(tmp_path / "tape.db")
    tape.remember_hop_pins(c, {FUNDED: QUOTE}, 1.0)
    for i in range(200):
        targets = tape.created_hop_targets(_created(i))
        assert len(targets) == 2
        tape.remember_hop_targets(c, targets, 2.0 + i)
    hops = tape.discovered_hop_pairs(c)
    assert hops[FUNDED] == QUOTE and len(hops) == tape.HOP_TARGET_CAP + 1
    tape.record_head(c, 100, "0x" + "a" * 64, 10.0, 10.0)
    assert bsc.tape_hop_state(c, FUNDED, 100, now=10.0) == "no_sync", "pinned with no Sync: stale, never zero"


def _ledger(tmp_path, rows):
    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, network_id TEXT, "
               "execution_mode TEXT, evm_route_json TEXT)")
    db.executemany("INSERT INTO memecoin_positions (network_id, execution_mode, evm_route_json) "
                   "VALUES (?,?,?)", rows)
    db.commit()
    db.close()
    return path


def test_the_route_read_pins_only_real_mixed_route_quotes_on_56(tmp_path, monkeypatch):
    from sniper.persistence import db_core
    other = "0x" + "9b" * 20
    path = _ledger(tmp_path, [
        ("56", "real", json.dumps({"protocol": "pcs_mixed_v3_v2", "base_token": QUOTE})),
        ("56", "paper", json.dumps({"protocol": "pcs_mixed_v3_v2", "base_token": other})),
        ("8453", "real", json.dumps({"protocol": "pcs_mixed_v3_v2", "base_token": other})),
        ("56", "real", json.dumps({"protocol": "pancake_v3"})), ("56", "real", "not json")])
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda *a, **k: sqlite3.connect(path))
    assert tape.read_route_pins() == {FUNDED: QUOTE}


def test_a_failed_route_read_keeps_the_pinned_set(tmp_path, monkeypatch, capsys):
    from sniper.persistence import db_core
    c = tape.connect(tmp_path / "tape.db")
    tape.remember_hop_pins(c, {FUNDED: QUOTE}, 1.0)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db_core, "get_readonly_connection", locked)
    assert tape.read_route_pins() is None
    assert "BSC hop pins unreadable: OperationalError" in capsys.readouterr().out
    assert tape.apply_route_pins(c, None, 2.0) is False
    assert tape.discovered_hop_pairs(c)[FUNDED] == QUOTE


def test_the_tape_re_reads_the_pins_on_its_timer_and_seeds_a_new_one(tmp_path, monkeypatch):
    from test_memecoin_bsc_v2_live_hops import KEYED, _drive_tape, _reserves
    monkeypatch.setenv(tape.HOP_SEED_URL_VAR, KEYED)
    monkeypatch.setattr(tape, "HOP_PIN_REFRESH_S", 0.0)
    answers = [{}, {FUNDED: QUOTE}]          # the start read, the first timer read; then None (failed)
    monkeypatch.setattr(tape, "read_route_pins", lambda: answers.pop(0) if answers else None)
    seeds = []

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[0]["to"])
        return _reserves()
    rows = _drive_tape(monkeypatch, tmp_path, [100, 101, 102, 103, 104, 105], rpc)
    assert FUNDED in seeds and FUNDED in rows
    c = tape.connect(tmp_path / "tape.db")
    assert tape.discovered_hop_pairs(c)[FUNDED] == QUOTE


# ---- F3 (standing order, dfeaef22): a coverage reset re-seeds the configured and pinned hops first ----
# Every reconnect wipes every Sync (201 timestamped resets 10-02..10-07). Re-seeding ~130 targets at
# four a job took ~150 s, and a funded quote's hop could land anywhere in that order.

def test_a_new_window_seeds_the_configured_and_pinned_pairs_first():
    cold = {"0x%040x" % i: "q" for i in range(1, 129)}            # sort ahead of both
    first = {"0x" + "fe" * 20, "0x" + "ff" * 20}
    s = tape.HopSeeder({**cold, **{p: "q" for p in first}}, first=first)
    s.window(1000.0, 100)
    assert first <= s.batch()


def test_a_failing_first_pair_does_not_starve_the_rest():
    first = {"0x" + "ff" * 20}
    s = tape.HopSeeder({"0x%040x" % i: "q" for i in range(1, 9)} | {p: "q" for p in first}, first=first)
    s.window(1000.0, 100)
    one = s.batch()
    s.done([p for p in one if p not in first], False, 1.0, attempted=one)   # the pinned read failed
    assert not first & s.batch(), "untried pairs go before a retry, pinned or not"


def test_the_run_loop_seeds_the_pins_and_the_configured_hop_in_its_first_job(tmp_path, monkeypatch):
    from test_memecoin_bsc_v2_live_hops import A, KEYED, _drive_tape, _reserves
    monkeypatch.setenv(tape.HOP_SEED_URL_VAR, KEYED)
    monkeypatch.setattr(tape, "read_route_pins", lambda: None)     # the ledger is unreadable: keep pins
    c = tape.connect(tmp_path / "tape.db")
    tape.remember_hop_pins(c, {FUNDED: QUOTE}, 1.0)
    cold = {bsc.our_pair(bsc.WBNB, "0x%040x" % (0x10 + i)): "0x%040x" % (0x10 + i) for i in range(20)}
    tape.remember_hop_targets(c, cold, 2.0)
    c.close()
    seeds = []

    def rpc(method, params, url=tape.RPC, timeout=3):
        if method == "eth_chainId":
            return "0x38"
        seeds.append(params[0]["to"])
        return _reserves()
    _drive_tape(monkeypatch, tmp_path, [100, 101], rpc)
    assert {A, FUNDED} <= set(seeds[:4]), seeds[:4]
