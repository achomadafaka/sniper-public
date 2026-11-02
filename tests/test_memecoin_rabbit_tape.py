"""The rabbit pre-execution pool tape (memecoin_rabbit_tape, task 5a7e1967, Alpha GO 2026-10-17).

Invariants pinned here, each with a check that fails if it returns:
- PRE-NESS: a frame in slot S never reads state written in S (or later): the state is the end of
  S's own parent P, known from SLOT_CREATED_BANK(S, P);
- a stream gap (reconnect, error) invalidates every entry: nothing read before it is served;
- an account the stream never wrote since the gap is a miss, never "unchanged";
- a write on a sibling fork (not an ancestor of P) is never served;
- the tape trailing the lane head by more than 2 slots is a miss;
- inside one slot the highest write_version wins; a stream write beats a seed of the same slot."""
import base64

import pytest

from sniper.services import memecoin_rabbit_tape as T

POOL, VAULT, CURVE = "PoolAddr111", "VaultAddr111", "CurveAddr111"


def _tape():
    tape = T.PoolTape()
    tape.reset("connect")
    return tape


def _created(tape, *chain):
    """CREATED_BANK for each (slot, parent)."""
    for slot, parent in chain:
        tape.on_slot(slot, parent, T.SLOT_CREATED_BANK)


def _data(account):
    return base64.b64decode(account["data"][0])


def test_a_frame_in_s_reads_the_end_of_its_parent_never_s_itself():
    tape = _tape()
    tape.on_account(POOL, "owner", 10, b"at-98", 98, 1)
    tape.on_account(POOL, "owner", 10, b"at-99", 99, 5)
    tape.on_account(POOL, "owner", 10, b"at-100", 100, 9)       # the source's own slot S
    _created(tape, (99, 98), (100, 99))
    accounts, parent = tape.lookup([POOL], 100)
    assert parent == 99 and _data(accounts[POOL]) == b"at-99"


def test_inside_a_slot_the_highest_write_version_wins():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"late", 99, 7)
    tape.on_account(POOL, "o", 1, b"early", 99, 3)               # delivered out of order
    _created(tape, (100, 99))
    assert _data(tape.lookup([POOL], 100)[0][POOL]) == b"late"


def test_a_quiet_account_serves_its_last_write_through_a_continuous_ancestry():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"old", 90, 1)
    _created(tape, (91, 90), (92, 91), (93, 92), (94, 93), (95, 94))
    accounts, parent = tape.lookup([POOL], 95)
    assert parent == 94 and _data(accounts[POOL]) == b"old"


def test_an_unseen_account_is_a_miss_not_unchanged():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 99, 1)
    _created(tape, (100, 99))
    assert tape.lookup([POOL, VAULT], 100) == (None, "unseen")


def test_a_gap_invalidates_everything_seen_before_it():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 99, 1)
    _created(tape, (100, 99))
    assert tape.lookup([POOL], 100)[1] == 99
    tape.reset("stream_error")
    assert tape.lookup([POOL], 100) == (None, "no_coverage")
    tape.reset("connect")                                        # the reconnect
    _created(tape, (100, 99))
    assert tape.lookup([POOL], 100) == (None, "unseen")


def test_without_created_bank_of_s_the_parent_is_unknown_and_it_is_a_miss():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 98, 1)
    _created(tape, (99, 98))
    assert tape.lookup([POOL], 100) == (None, "parent_unknown")


def test_a_write_on_a_sibling_fork_is_never_served():
    tape = _tape()
    # 98 <- 99 (forked off) and 98 <- 100: S=100's parent is 98, slot 99 is a sibling.
    tape.on_account(POOL, "o", 1, b"at-98", 98, 1)
    tape.on_account(POOL, "o", 1, b"at-99-fork", 99, 2)
    _created(tape, (98, 97), (99, 98), (100, 98))
    accounts, parent = tape.lookup([POOL], 100)
    assert parent == 98 and _data(accounts[POOL]) == b"at-98"


def test_an_ancestry_that_breaks_before_the_write_is_a_miss():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 97, 1)
    _created(tape, (100, 99))                                    # 99's parent never seen
    assert tape.lookup([POOL], 100) == (None, "ancestry_unknown")


def test_a_tape_more_than_two_slots_behind_the_lane_head_is_a_miss():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 99, 1)
    _created(tape, (100, 99))
    assert tape.lookup([POOL], 100, lane_head=102)[1] == 99
    assert tape.lookup([POOL], 100, lane_head=103) == (None, "tape_lag")


def test_before_any_coverage_it_is_a_miss():
    tape = T.PoolTape()
    assert tape.lookup([POOL], 100) == (None, "no_coverage")


def test_a_stream_write_in_the_seeds_slot_beats_the_seed_and_a_stale_generation_seed_is_dropped():
    tape = _tape()
    gen = tape.generation
    tape.on_account(VAULT, "o", 1, b"stream", 99, 4)
    _created(tape, (100, 99))
    tape.seed(VAULT, {"owner": "o", "lamports": 1, "data": [base64.b64encode(b"seed").decode(), "base64"]},
              99, gen)
    assert _data(tape.lookup([VAULT], 100)[0][VAULT]) == b"stream"
    tape.seed(CURVE, {"owner": "o", "lamports": 1, "data": [base64.b64encode(b"seed").decode(), "base64"]},
              99, gen - 1)                                       # read before the last reset
    assert tape.lookup([CURVE], 100) == (None, "unseen")
    tape.seed(CURVE, {"owner": "o", "lamports": 1, "data": [base64.b64encode(b"seed").decode(), "base64"]},
              99, gen)
    assert _data(tape.lookup([CURVE], 100)[0][CURVE]) == b"seed"


def test_a_seed_never_overrides_a_newer_stream_write():
    tape = _tape()
    gen = tape.generation
    tape.on_account(VAULT, "o", 1, b"stream-100", 100, 1)
    _created(tape, (100, 99), (101, 100))
    tape.seed(VAULT, {"owner": "o", "lamports": 1, "data": [base64.b64encode(b"seed-99").decode(), "base64"]},
              99, gen)
    assert _data(tape.lookup([VAULT], 101)[0][VAULT]) == b"stream-100"


def test_a_deleted_account_reads_as_absent():
    tape = _tape()
    tape.on_account(VAULT, "o", 0, b"", 99, 1, deleted=True)
    _created(tape, (100, 99))
    accounts, parent = tape.lookup([VAULT], 100)
    assert parent == 99 and accounts[VAULT] is None


def test_the_ring_and_the_key_count_are_bounded(monkeypatch):
    monkeypatch.setattr(T.PoolTape, "MAX_KEYS", 3)
    tape = _tape()
    for slot in range(10, 20):
        tape.on_account(POOL, "o", 1, b"x%d" % slot, slot, 1)
    assert len(tape._acc[POOL]) == T.PoolTape.RING
    for i in range(6):
        tape.on_account("K%d" % i, "o", 1, b"x", 30 + i, 1)
    assert len(tape._acc) <= 3 and tape.stats["evicted"] > 0


def test_the_fill_path_lookup_takes_no_lock():
    """Readers are lock-free (immutable tuples swapped whole); a held writer lock never blocks one."""
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 99, 1)
    _created(tape, (100, 99))
    with tape._write_lock:
        assert tape.lookup([POOL], 100)[1] == 99


# --- the handlers on the tape -------------------------------------------------------------------

def _load(tape, accounts, slot):
    for key, account in accounts.items():
        if account is None:
            continue
        tape.on_account(key, account["owner"], account.get("lamports", 1),
                        base64.b64decode(account["data"][0]), slot, 1)


@pytest.mark.parametrize("name", ["ResellCalendar_5UJpHGLm_v0", "jpegkite_3cPBmtyC_v1"])
def test_the_amm_tape_state_gives_the_same_fill_as_the_rpc_read_of_the_parent_slot(name):
    from test_memecoin_rabbit_dflow_amm import FIX, candidate, handler, state_for
    tx = FIX["pure"][name]
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    S = tx["slot"]
    rpc_state = state_for(tx, plan, read_slot=S - 1)
    tape = _tape()
    _load(tape, {k: v for k, v in rpc_state["accounts"].items() if k not in plan["holders"].values()}, S - 1)
    _created(tape, (S - 1, S - 2), (S, S - 1))
    state, why = h.tape_read(plan, tape, S, None)
    assert why is None and state["slot"] == S - 1
    assert set(state["accounts"]) == set(h.read_keys(plan))
    assert h.verdict(plan, state, cand) == h.verdict(plan, rpc_state, cand)
    assert h.verdict(plan, state, cand)[1] is None


def test_an_amm_tape_without_the_mint_or_a_vault_is_a_miss():
    from test_memecoin_rabbit_dflow_amm import FIX, candidate, handler, state_for
    tx = FIX["pure"]["jpegkite_3cPBmtyC_v1"]
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    S = tx["slot"]
    accounts = dict(state_for(tx, plan, read_slot=S - 1)["accounts"])
    tape = _tape()
    _load(tape, {k: v for k, v in accounts.items() if k != plan["mint"]}, S - 1)
    _created(tape, (S, S - 1))
    assert h.tape_read(plan, tape, S, None) == (None, "unseen")


def _curve_world(monkeypatch, tape_on=True):
    from sniper.execution.solana_native_entry_quote import accounts_for
    from sniper.settings import memecoin as cfg
    from test_memecoin_rabbit_signal_stage2 import FIRE_MINT, FIRE_SLOT, _buyer, fire_rpc
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_TAPE", tape_on)
    calls = []
    rpc = fire_rpc(calls=calls, slot=FIRE_SLOT - 1)
    buyer, stats, fired = _buyer(rpc)
    tape = _tape()
    buyer.tape = tape
    keys = accounts_for(FIRE_MINT)
    return buyer, stats, fired, tape, rpc, calls, keys, FIRE_SLOT


def _fire_once(buyer):
    import asyncio
    from test_memecoin_rabbit_frame_log import Clock, _log
    from test_memecoin_rabbit_signal_stage2 import _curve_candidate, _offer, fire_frame
    clock = Clock()
    frames, lines = _log(clock=clock)
    buyer.frames = frames
    cand = _curve_candidate()
    frames.frame(cand["signature"], cand["owner"], cand["slot"], clock.t)
    asyncio.run(_offer(buyer, cand, fire_frame()))
    return frames._recs[(cand["signature"], cand["owner"])]


def test_a_curve_fire_from_the_tape_makes_no_rpc_read_and_prices_like_the_parent_read(monkeypatch):
    buyer, stats, fired, tape, rpc, calls, keys, S = _curve_world(monkeypatch)
    _load(tape, {keys["curve"]: rpc.accounts["curve"], keys["curve_fee_config"]: rpc.accounts["config"]}, S - 1)
    _created(tape, (S - 1, S - 2), (S, S - 1))
    rec = _fire_once(buyer)
    assert calls == [] and len(fired) == 1 and rec["read"]["state_source"] == "tape"
    assert fired[0].rabbit["read_slot"] == S - 1
    from_tape = fired[0]
    buyer2, _stats, fired2, _tape2, _rpc, calls2, _keys, _S = _curve_world(monkeypatch, tape_on=False)
    rec2 = _fire_once(buyer2)
    assert len(calls2) == 1 and rec2["read"]["state_source"] == "rpc"
    assert (from_tape.token_amount, from_tape.sol_amount) == (fired2[0].token_amount, fired2[0].sol_amount)


def test_a_curve_tape_miss_reads_rpc_names_the_reason_and_seeds_the_tape(monkeypatch):
    buyer, stats, fired, tape, rpc, calls, keys, S = _curve_world(monkeypatch)
    _created(tape, (S - 1, S - 2), (S, S - 1))
    rec = _fire_once(buyer)
    assert len(calls) == 1 and len(fired) == 1 and rec["read"]["state_source"] == "tape_miss:unseen"
    assert stats["by_route"]["direct"]["tape_miss:unseen"] == 1
    assert keys["curve_fee_config"] in tape._acc and keys["curve"] in tape._acc       # seeded after the fire


def _warm_common(tape, accounts, slot, *, change=None, transform=None):
    import asyncio
    import threading
    calls = []
    stop = threading.Event()

    def snapshot(method, params, timeout=None):
        calls.append((method, params, timeout))
        if change is not None:
            change(tape)
        result = {"context": {"slot": slot}, "value": [dict(accounts[key], executable=False,
                  lamports=accounts[key].get("lamports", 1)) for key in T.fee_config_addresses()]}
        return transform(result) if transform else result

    async def run():
        task = asyncio.create_task(T.warm_fee_configs(tape, snapshot, stop))
        try:
            for _ in range(100):
                await asyncio.sleep(.01)
                if task.done():
                    await task
                stats = tape.status().get("snapshots", {})
                if sum(stats.get(key, 0) for key in ("successes", "failures", "discarded")):
                    break
        finally:
            stop.set()
            await task
    asyncio.run(run())
    return calls


def test_common_fee_snapshot_removes_the_curve_entry_read(monkeypatch):
    buyer, stats, fired, tape, rpc, calls, keys, S = _curve_world(monkeypatch)
    _load(tape, {keys["curve"]: rpc.accounts["curve"]}, S - 1)
    _created(tape, (S - 2, S - 3), (S - 1, S - 2), (S, S - 1))
    assert tape.lookup([keys["curve"], keys["curve_fee_config"]], S) == (None, "unseen")
    accounts = {key: rpc.accounts["config"] for key in T.fee_config_addresses()}
    warm_calls = _warm_common(tape, accounts, S - 1)
    rec = _fire_once(buyer)
    assert calls == [] and len(fired) == 1 and rec["read"]["state_source"] == "tape"
    assert fired[0].rabbit["read_slot"] == S - 1
    assert warm_calls == [("getMultipleAccounts", [list(T.fee_config_addresses()),
        {"encoding": "base64", "commitment": "processed", "minContextSlot": S - 1}], 1.0)]
    buyer2, _, fired2, _, _, calls2, _, _ = _curve_world(monkeypatch, tape_on=False)
    _fire_once(buyer2)
    assert len(calls2) == 1
    assert (fired[0].token_amount, fired[0].sol_amount) == (fired2[0].token_amount, fired2[0].sol_amount)


def test_common_fee_snapshot_restores_the_amm_parent_fill_without_an_entry_read():
    from test_memecoin_rabbit_dflow_amm import FIX, candidate, handler, state_for
    tx = FIX["pure"]["jpegkite_3cPBmtyC_v1"]
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    S = tx["slot"]
    parent_state = state_for(tx, plan, read_slot=S - 1)
    tape = _tape()
    _load(tape, {k: v for k, v in parent_state["accounts"].items()
                if k not in plan["holders"].values() and k != plan["fee_config"]}, S - 1)
    _created(tape, (S - 2, S - 3), (S - 1, S - 2), (S, S - 1))
    assert h.tape_read(plan, tape, S, None) == (None, "unseen")
    accounts = {key: parent_state["accounts"][plan["fee_config"]] for key in T.fee_config_addresses()}
    _warm_common(tape, accounts, S - 1)
    h.read_fn = lambda *a: pytest.fail("the parent tape hit must not read RPC")
    state, why = h.tape_read(plan, tape, S, None)
    assert why is None and state["slot"] == S - 1
    assert h.verdict(plan, state, cand) == h.verdict(plan, parent_state, cand)
    assert h.verdict(plan, state, cand)[1] is None


@pytest.mark.parametrize("case", ["old_generation", "disconnect", "stop", "bootstrap", "future", "short",
    "missing_context", "boolean_slot", "wrong_owner", "executable", "bad_lamports", "bad_base64",
    "wrong_encoding", "bad_layout", "timeout"])
def test_an_unproven_common_snapshot_cannot_make_parent_state_usable(monkeypatch, case):
    import copy
    buyer, _, _, tape, rpc, _, keys, S = _curve_world(monkeypatch)
    _load(tape, {keys["curve"]: rpc.accounts["curve"]}, S - 1)
    _created(tape, (S - 2, S - 3), (S - 1, S - 2), (S, S - 1))
    accounts = {key: copy.deepcopy(rpc.accounts["config"]) for key in T.fee_config_addresses()}
    slot = S - 2 if case == "bootstrap" else S + 1 if case == "future" else S - 1
    def change(store):
        if case == "old_generation":
            store.reset("connect")
            _created(store, (S, S - 1))
        elif case == "disconnect":
            store.disconnect("test")
        elif case == "timeout":
            raise TimeoutError("sensitive provider detail must not be exported")
    def transform(result):
        if case == "short":
            result["value"].pop()
        elif case == "missing_context":
            del result["context"]
        elif case == "boolean_slot":
            result["context"]["slot"] = True
        elif case == "wrong_owner":
            result["value"][0]["owner"] = "wrong"
        elif case == "executable":
            result["value"][0]["executable"] = True
        elif case == "bad_lamports":
            result["value"][0]["lamports"] = "1"
        elif case == "bad_base64":
            result["value"][0]["data"][0] = "%%%"
        elif case == "wrong_encoding":
            result["value"][0]["data"][1] = "base58"
        elif case == "bad_layout":
            result["value"][0]["data"][0] = base64.b64encode(b"short").decode()
        return result
    if case == "stop":
        import asyncio
        import threading
        stop = threading.Event()
        def snapshot(*args, **kwargs):
            stop.set()
            return {"context": {"slot": S - 1}, "value": list(accounts.values())}
        asyncio.run(T.warm_fee_configs(tape, snapshot, stop))
    else:
        _warm_common(tape, accounts, slot, change=change, transform=transform)
    assert keys["curve_fee_config"] not in tape._acc
    assert "sensitive provider detail" not in str(tape.status())


def test_common_snapshot_preserves_same_slot_stream_precedence(monkeypatch):
    _, _, _, tape, rpc, _, keys, S = _curve_world(monkeypatch)
    key = keys["curve_fee_config"]
    tape.on_account(key, "owner", 1, b"stream wins", S - 1, 7)
    _created(tape, (S - 2, S - 3), (S - 1, S - 2), (S, S - 1))
    accounts = {key: rpc.accounts["config"] for key in T.fee_config_addresses()}
    _warm_common(tape, accounts, S - 1)
    assert _data(tape.lookup([key], S)[0][key]) == b"stream wins"


def test_common_snapshot_calls_are_paced_and_never_overlap(monkeypatch):
    import asyncio
    import threading
    _, _, _, tape, rpc, _, _, S = _curve_world(monkeypatch)
    _created(tape, (S - 2, S - 3), (S - 1, S - 2), (S, S - 1))
    now, active, calls = [0.0], [0], []
    stop = threading.Event()
    tape.clock = lambda: now[0]
    def snapshot(method, params, timeout=None):
        active[0] += 1
        assert active[0] == 1
        calls.append(now[0])
        active[0] -= 1
        return {"context": {"slot": S - 1}, "value": [dict(rpc.accounts["config"], executable=False)] * 2}
    async def run():
        task = asyncio.create_task(T.warm_fee_configs(tape, snapshot, stop))
        try:
            while tape.status().get("snapshots", {}).get("successes", 0) < 1:
                await asyncio.sleep(.01)
            now[0] = 29.99
            await asyncio.sleep(.3)
            assert calls == [0.0]
            now[0] = 30.01
            while tape.status()["snapshots"]["successes"] < 2:
                await asyncio.sleep(.01)
        finally:
            stop.set()
            await task
    asyncio.run(run())
    assert calls == [0.0, 30.01]


def test_common_snapshot_refresh_keeps_a_quiet_fee_config_inside_the_ancestry_window(monkeypatch):
    buyer, _, fired, tape, rpc, calls, keys, S = _curve_world(monkeypatch)
    monkeypatch.setattr(T, "ANCESTRY_MAX", 4)
    _load(tape, {keys["curve"]: rpc.accounts["curve"]}, S - 1)
    tape.seed(keys["curve_fee_config"], rpc.accounts["config"], S - 8, tape.generation)
    for slot in range(S - 8, S + 1):
        _created(tape, (slot, slot - 1))
    assert tape.lookup([keys["curve"], keys["curve_fee_config"]], S) == (None, "ancestry_unknown")
    _warm_common(tape, {key: rpc.accounts["config"] for key in T.fee_config_addresses()}, S - 1)
    rec = _fire_once(buyer)
    assert calls == [] and len(fired) == 1 and rec["read"]["state_source"] == "tape"


def test_a_snapshot_client_setup_error_does_not_stop_the_tape(monkeypatch):
    import threading
    seen = []
    stop, tape = threading.Event(), _tape()
    async def run(*args, **kwargs):
        seen.append("stream started")
        stop.set()
    def broken():
        raise RuntimeError("credential-like detail must not be exported")
    monkeypatch.setattr(T, "run_tape", run)
    thread = T.start_tape_thread("https://grpc.invalid", "test", tape, lambda: (), {}, stop,
                                 snapshot_factory=broken)
    thread.join(1.0)
    assert not thread.is_alive() and seen == ["stream started"]
    assert tape.status()["snapshots"]["last_error"] == "init:RuntimeError"
    assert "credential-like detail" not in str(tape.status())


def test_with_the_knob_off_the_tape_is_never_read(monkeypatch):
    buyer, stats, fired, tape, rpc, calls, keys, S = _curve_world(monkeypatch, tape_on=False)
    _load(tape, {keys["curve"]: rpc.accounts["curve"], keys["curve_fee_config"]: rpc.accounts["config"]}, S - 1)
    _created(tape, (S - 1, S - 2), (S, S - 1))
    rec = _fire_once(buyer)
    assert len(calls) == 1 and rec["read"]["state_source"] == "rpc" and tape.stats["hits"] == 0


def test_a_curve_frame_whose_leg_token_program_does_not_bind_the_holder_never_reads_the_tape():
    from sniper.services import memecoin_rabbit_signal as R
    from test_memecoin_rabbit_signal_stage2 import _curve_candidate
    cand = _curve_candidate()
    leg = next(i for i, (p, a, d) in enumerate(cand["instructions"]) if len(a) > 8)
    program, accounts, data = cand["instructions"][leg]
    wrong = list(accounts)
    wrong[8] = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" if accounts[8].startswith("Tokenkeg") else \
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    ixs = list(cand["instructions"])
    ixs[leg] = (program, tuple(wrong), data)
    from sniper.services.memecoin_shyft_prepare import preparation_target
    mint = preparation_target(cand)["mint"]
    assert R._leg_token_program(dict(cand, instructions=tuple(ixs)), mint, cand["owner"], accounts[5]) is None
    assert R._leg_token_program(cand, mint, cand["owner"], accounts[5]) == accounts[8]


# --- the stream -----------------------------------------------------------------------------------

class _FakeCall:
    def __init__(self, requests, script, sent):
        self.requests, self.script, self.sent = requests, list(script), sent
        import asyncio
        self.task = asyncio.ensure_future(self._drain())

    async def _drain(self):
        async for req in self.requests:
            self.sent.append(req)

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio
        await asyncio.sleep(0.01)
        if not self.script:
            await asyncio.sleep(3600)              # an open, quiet stream
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def cancel(self):
        self.task.cancel()


class _FakeChannel:
    def __init__(self, scripts, sent):
        self.scripts, self.sent = scripts, sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream_stream(self, *a, **k):
        script = self.scripts.pop(0) if self.scripts else []
        return lambda requests, metadata=None: _FakeCall(requests, script, self.sent)


def _slot_update(slot, parent, status=T.SLOT_CREATED_BANK):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    u = pb.SubscribeUpdate()
    u.slot.slot, u.slot.parent, u.slot.status = slot, parent, status
    return u


def _account_update(key_bytes, slot, wv, data, label="amm"):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    u = pb.SubscribeUpdate()
    u.filters.append(label)
    u.account.slot = slot
    a = u.account.account
    a.pubkey, a.owner, a.lamports, a.data, a.write_version = key_bytes, bytes(32), 5, data, wv
    return u


def _run_stream(scripts, watch_fn, seconds, check=None):
    import asyncio
    import threading
    tape, status, stop, sent = T.PoolTape(), {}, threading.Event(), []

    async def main():
        task = asyncio.ensure_future(T.run_tape("https://grpc.ams.shyft.to", "t", tape, watch_fn, status, stop,
                                                channel_fn=lambda: _FakeChannel(scripts, sent)))
        await asyncio.sleep(seconds)
        if check is not None:
            check(tape)
        stop.set()
        await asyncio.sleep(0.3)
        task.cancel()
    asyncio.run(main())
    return tape, status, sent


def test_tape_transport_default_preserves_dns_and_explicit_empty_ignores_environment(monkeypatch):
    monkeypatch.delenv("MEMECOIN_SHYFT_TAPE_CONNECT_IP", raising=False)
    expected = dict(host="grpc.fra.shyft.to", dial_target="grpc.fra.shyft.to:443",
                    tls_authority="grpc.fra.shyft.to", connect_ip="")
    assert T.tape_transport("https://grpc.fra.shyft.to") == expected
    monkeypatch.setenv("MEMECOIN_SHYFT_TAPE_CONNECT_IP", "not-an-ip")
    assert T.tape_transport("https://grpc.fra.shyft.to", "") == expected


@pytest.mark.parametrize("url", ["https://grpc.ams.shyft.to", "https://grpc.ams.shyft.to:443/"])
def test_tape_transport_pin_keeps_the_canonical_tls_identity(monkeypatch, url):
    monkeypatch.setenv("MEMECOIN_SHYFT_TAPE_CONNECT_IP", "84.32.186.182")
    assert T.tape_transport(url) == dict(host="grpc.ams.shyft.to", dial_target="ipv4:84.32.186.182:443",
                                         tls_authority="grpc.ams.shyft.to", connect_ip="84.32.186.182")
    assert T.tape_transport(url, "127.0.0.1")["dial_target"] == "ipv4:127.0.0.1:443"


@pytest.mark.parametrize("pin", ["hostname.invalid", "84.32.186.182:443", "84.032.186.182", "::1",
                                  "84.32.186.999", "84.32.186.182,127.0.0.1", True, 84])
def test_tape_transport_rejects_nonliteral_ipv4_pins(pin):
    with pytest.raises(ValueError, match="tape"):
        T.tape_transport("https://grpc.ams.shyft.to", pin)


@pytest.mark.parametrize("url", [
    "http://grpc.ams.shyft.to", "https://grpc.fra.shyft.to", "https://beta-grpc.ams.shyft.to",
    "https://grpc.ams.shyft.to:444", "https://user@grpc.ams.shyft.to",
    "https://user:pass@grpc.ams.shyft.to", "https://grpc.ams.shyft.to/path",
    "https://grpc.ams.shyft.to/?query=1", "https://grpc.ams.shyft.to/#fragment",
    "https://grpc.ams.shyft.to.evil.invalid", "https://84.32.186.182", "https://grpc.ams.shyft.to:bad",
])
def test_tape_transport_rejects_noncanonical_pinned_urls(url):
    with pytest.raises(ValueError, match="tape"):
        T.tape_transport(url, "84.32.186.182")


@pytest.mark.parametrize("pin", ["", "84.32.186.182"])
def test_tape_transport_channel_keeps_auth_and_freezes_routing_across_reconnects(monkeypatch, pin):
    import asyncio
    import threading
    import grpc
    monkeypatch.setenv("MEMECOIN_SHYFT_TAPE_CONNECT_IP", pin)
    tape, status, stop, sent = T.PoolTape(), {}, threading.Event(), []
    targets, metadata_seen = [], []
    credentials = object()
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: credentials)
    scripts = [[_slot_update(100, 99), RuntimeError("stream_gap")], []]

    class Channel(_FakeChannel):
        def stream_stream(self, method, **kwargs):
            assert method == "/geyser.Geyser/Subscribe"
            subscribe = super().stream_stream(method, **kwargs)
            def call(requests, metadata=None):
                metadata_seen.append(metadata)
                return subscribe(requests, metadata=metadata)
            return call

    def channel(target, creds, *, options):
        targets.append((target, creds, dict(options)))
        monkeypatch.setenv("MEMECOIN_SHYFT_TAPE_CONNECT_IP", "changed-after-setup.invalid")
        return Channel(scripts, sent)
    monkeypatch.setattr(grpc.aio, "secure_channel", channel)

    async def run():
        task = asyncio.create_task(T.run_tape("https://grpc.ams.shyft.to", "test-token", tape,
                                             lambda: (), status, stop))
        try:
            async with asyncio.timeout(3):
                while len(metadata_seen) < 2:
                    await asyncio.sleep(.01)
            assert tape.generation >= 3 and not tape._acc
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    target = "ipv4:84.32.186.182:443" if pin else "grpc.ams.shyft.to:443"
    assert len(targets) == 2 and all(t[0] == target and t[1] is credentials for t in targets)
    assert metadata_seen == [(("x-token", "test-token"),)] * 2
    options = targets[0][2]
    assert options["grpc.max_receive_message_length"] == 16 * 1024 * 1024
    assert options["grpc.keepalive_time_ms"] == 10000
    pinned = {"grpc.ssl_target_name_override": "grpc.ams.shyft.to",
              "grpc.default_authority": "grpc.ams.shyft.to", "grpc.enable_http_proxy": 0}
    assert all(options.get(k) == v for k, v in pinned.items()) if pin else not (options.keys() & pinned.keys())
    assert {k: status[k] for k in ("host", "dial_target", "tls_authority", "connect_ip")} == dict(
        host="grpc.ams.shyft.to", dial_target=target, tls_authority="grpc.ams.shyft.to", connect_ip=pin)
    assert status["connected"] is False and status["reconnects"] == 1


@pytest.mark.parametrize("certificate_host", ["grpc.ams.shyft.to", "wrong.invalid"])
def test_tape_transport_tls_checks_canonical_hostname_on_a_loopback_peer(monkeypatch, certificate_host):
    import asyncio
    import threading
    from datetime import datetime, timedelta, timezone
    import grpc
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from sniper.feeds.yellowstone import geyser_pb2 as pb

    now = datetime.now(timezone.utc)
    ca_key, server_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(2))
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local tape test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, certificate_host)]))
            .issuer_name(ca_name).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(certificate_host)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(ca_key, hashes.SHA256()))
    key_pem = server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
    cert_pem, ca_pem = (c.public_bytes(serialization.Encoding.PEM) for c in (cert, ca))
    real_credentials, real_channel = grpc.ssl_channel_credentials, grpc.aio.secure_channel
    monkeypatch.setattr(grpc, "ssl_channel_credentials", lambda: real_credentials(root_certificates=ca_pem))
    monkeypatch.setenv("MEMECOIN_SHYFT_TAPE_CONNECT_IP", "127.0.0.1")
    tape, status, stop, metadata_seen, targets = T.PoolTape(), {}, threading.Event(), [], []

    async def run():
        async def subscribe(requests, context):
            metadata_seen.append(dict(context.invocation_metadata()))
            await anext(requests)
            yield _slot_update(100, 99)
            await asyncio.Event().wait()
        server = grpc.aio.server()
        server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler("geyser.Geyser", {
            "Subscribe": grpc.stream_stream_rpc_method_handler(subscribe,
                request_deserializer=pb.SubscribeRequest.FromString,
                response_serializer=pb.SubscribeUpdate.SerializeToString)}),))
        port = server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials(((key_pem, cert_pem),)))
        assert port > 0
        await server.start()

        def channel(target, creds, *, options):
            targets.append(target)
            assert target == "ipv4:127.0.0.1:443"  # Only remap the local fixture's ephemeral port.
            configured = dict(options)
            assert configured["grpc.ssl_target_name_override"] == "grpc.ams.shyft.to"
            assert configured["grpc.default_authority"] == "grpc.ams.shyft.to"
            assert configured["grpc.enable_http_proxy"] == 0
            return real_channel("ipv4:127.0.0.1:%d" % port, creds, options=options)
        monkeypatch.setattr(grpc.aio, "secure_channel", channel)
        task = asyncio.create_task(T.run_tape("https://grpc.ams.shyft.to", "local-token", tape,
                                             lambda: (), status, stop))
        try:
            async with asyncio.timeout(3):
                while tape.head != 100 and status.get("reconnects", 0) == 0:
                    await asyncio.sleep(.01)
            if certificate_host == "grpc.ams.shyft.to":
                assert tape.head == 100 and metadata_seen[0]["x-token"] == "local-token"
                assert status["reconnects"] == 0
            else:
                assert tape.head is None and metadata_seen == []
                assert status["last_error"] == "AioRpcError" and status["connected"] is False
                assert tape._acc == {} and tape.connected is False
            assert targets == ["ipv4:127.0.0.1:443"]
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await server.stop(0)
    asyncio.run(run())


def test_the_stream_feeds_slots_and_accounts_into_the_tape():
    import base58
    key = bytes(range(32))
    script = [_slot_update(99, 98), _account_update(key, 99, 3, b"pool-99"), _account_update(key, 100, 4, b"pool-100"),
              _slot_update(100, 99)]
    seen = {}
    tape, status, sent = _run_stream([script], lambda: ("FeeCfg",), 0.3,
                                     check=lambda t: seen.update(out=t.lookup([base58.b58encode(key).decode()], 100)))
    accounts, parent = seen["out"]
    assert parent == 99 and _data(next(iter(accounts.values()))) == b"pool-99"
    assert status["by_filter"]["amm"][0] == 2 and status["frames"] >= 4


def test_stream_preserves_optional_provenance_and_does_not_turn_absent_bank_into_zero():
    import base58
    import copy
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    slot = _slot_update(100, 99)
    slot.slot.bank_id = 0
    messages = [slot]
    for i, label in enumerate(("amm", "watch", "watch")):
        u = _account_update(bytes([i]) * 32, 100, i, b"post", label)
        u.account.bank_id = 0
        u.account.account.txn_signature = b"s" * 64
        messages.append(u)
    absent = _account_update(bytes([4]) * 32, 100, 4, b"absent", "curve")
    assert not absent.account.HasField("bank_id")
    assert not absent.account.account.HasField("txn_signature")
    messages.append(absent)
    startup = _account_update(bytes([5]) * 32, 100, 5, b"startup", "curve")
    startup.account.is_startup = True
    messages.append(startup)
    dead = _slot_update(101, 100, pb.SLOT_DEAD)
    dead.slot.bank_id = 1
    messages.append(dead)
    seen = {}
    _run_stream([messages], lambda: (), .25,
                check=lambda t: seen.update(acc=copy.deepcopy(t._acc), banks=dict(t._banks),
                                            tainted=set(t._bank_tainted), status=t.status()))
    stored = seen["acc"][base58.b58encode(bytes(32)).decode()][-1]
    assert len(stored) == 4 and stored[:3] == (100, 0, False)
    assert stored[3]["txn_signature"] == b"s" * 64 and stored[3]["bank_id"] == 0
    assert stored[3]["is_startup"] is False
    missing = seen["acc"][base58.b58encode(bytes([4]) * 32).decode()][-1][3]
    assert missing["txn_signature"] is None and missing["bank_id"] is None
    assert seen["acc"][base58.b58encode(bytes([5]) * 32).decode()][-1][3]["is_startup"] is True
    assert seen["banks"] == {100: 0} and 101 in seen["tainted"]
    p = seen["status"]["provenance"]
    assert p["by_filter"]["amm"]["both"] == 1
    assert p["by_filter"]["watch"]["both"] == 2
    assert p["by_filter"]["curve"]["both"] == 0
    assert p["groups_with_2_accounts"] == p["groups_with_3_accounts"] == 1
    assert "ssss" not in str(p)


@pytest.mark.parametrize("fields, expected", [
    ({"txn_signature": b"s" * 63, "bank_id": 1}, (None, 1, False)),
    ({"txn_signature": "s" * 64, "bank_id": 1}, (None, 1, False)),
    ({"txn_signature": b"s" * 64, "bank_id": True}, (b"s" * 64, None, False)),
    ({"txn_signature": b"s" * 64, "bank_id": -1}, (b"s" * 64, None, False)),
    ({"txn_signature": b"s" * 64, "bank_id": 2**64}, (b"s" * 64, None, False)),
    ({"txn_signature": b"s" * 64, "bank_id": 0, "is_startup": 0}, (b"s" * 64, 0, True)),
])
def test_malformed_provenance_cannot_become_a_valid_transaction_witness(fields, expected):
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 100, 1, **fields)
    account = tape._acc[POOL][-1][3]
    assert (account["txn_signature"], account["bank_id"], account["is_startup"]) == expected
    assert tape.status()["provenance"]["malformed"] == 1


def test_dead_before_created_and_reordered_bank_conflicts_never_revive_the_slot():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    tape = _tape()
    tape.on_slot(100, 99, pb.SLOT_DEAD, bank_id=0)
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=0)
    assert tape._banks[100] == 0 and 100 in tape._bank_tainted
    revision = tape.provenance_revision
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=1)
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=0)  # late old bank cannot repair it
    assert 100 in tape._bank_tainted and tape.provenance_revision > revision
    tape.on_slot(101, 100, T.SLOT_CREATED_BANK, bank_id=2)
    assert 101 not in tape._bank_tainted


def test_bank_invalidation_during_a_lock_free_lookup_discards_the_result():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 99, 1)
    _created(tape, (100, 99))
    class Invalidate(dict):
        def get(self, key, default=None):
            out = super().get(key, default)
            tape.on_slot(100, 99, pb.SLOT_DEAD, bank_id=0)
            return out
    tape._acc = Invalidate(tape._acc)
    assert tape.lookup([POOL], 100) == (None, "no_coverage")


@pytest.mark.parametrize("discovery", ["account", "created"])
def test_unrelated_first_bank_discovery_during_frozen_lookup_keeps_the_memory_hit(discovery):
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"x", 99, 1)
    _created(tape, (100, 99))
    class Discover(dict):
        done = False
        def get(self, key, default=None):
            out = super().get(key, default)
            if not self.done:
                self.done = True
                if discovery == "account":
                    tape.on_account(VAULT, "o", 1, b"unrelated", 101, 2, bank_id=7)
                else:
                    tape.on_slot(101, 100, T.SLOT_CREATED_BANK, bank_id=7)
            return out
    tape._acc = Discover(tape._acc)
    accounts, parent = tape.lookup([POOL], 100)
    assert parent == 99 and _data(accounts[POOL]) == b"x"


@pytest.mark.parametrize("account_first, deleted", [(True, False), (False, False), (True, True), (False, True)])
def test_optional_bank_conflict_does_not_disable_legacy_preparation_in_either_delivery_order(account_first, deleted):
    tape = _tape()
    def account():
        tape.on_account(POOL, "o", 0 if deleted else 1, b"x", 100, 1, deleted=deleted,
                        txn_signature=b"s" * 64, bank_id=0)
    def created():
        tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=1)
    first, second = (account, created) if account_first else (created, account)
    first()
    second()
    assert 100 in tape._bank_tainted
    tape.on_slot(101, 100, T.SLOT_CREATED_BANK, bank_id=2)
    accounts, parent = tape.lookup([POOL], 101)
    assert parent == 100 and 100 not in tape._tainted
    assert accounts[POOL] is None if deleted else _data(accounts[POOL]) == b"x"


def test_distinct_bank_metadata_and_a_deleted_record_remain_guard_only_ambiguity():
    tape = _tape()
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=0)
    tape.on_account(POOL, "o", 1, b"good", 100, 1, bank_id=0)
    tape.on_account(POOL, "o", 0, b"", 100, 2, deleted=True, bank_id=1)
    assert 100 in tape._bank_tainted
    tape.on_slot(101, 100, T.SLOT_CREATED_BANK, bank_id=2)
    assert tape.lookup([POOL], 101) == ({POOL: None}, 100)
    assert 100 not in tape._tainted


def test_absent_optional_bank_on_created_replay_does_not_invent_zero_or_conflict():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"good", 100, 1, bank_id=7)
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=7)
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK)
    assert tape._banks[100] == 7 and 100 not in tape._bank_tainted
    tape.on_account(VAULT, "o", 1, b"unproved-bank", 101, 2, bank_id=7)
    tape.on_slot(101, 100, T.SLOT_CREATED_BANK)
    assert 101 not in tape._banks and tape._account_banks[101] == 7
    assert _data(tape.lookup([POOL], 101)[0][POOL]) == b"good"


@pytest.mark.parametrize("kind", ["account_vs_account", "account_vs_created", "created_vs_account",
                                  "created_vs_created", "parent"])
def test_bank_conflict_diagnostics_identify_the_actual_comparison_without_changing_taint(kind):
    tape = _tape()
    def account(bank, version):
        tape.on_account(POOL, "o", 1, b"x", 100, version, bank_id=bank, label="watch")
    def created(bank, parent=99):
        tape.on_slot(100, parent, T.SLOT_CREATED_BANK, bank_id=bank)
    if kind == "account_vs_account":
        account(0, 1)
        account(1, 2)
    elif kind == "account_vs_created":
        created(0)
        account(1, 2)
    elif kind == "created_vs_account":
        account(0, 1)
        created(1)
    elif kind == "created_vs_created":
        created(0)
        created(1)
    else:
        created(0)
        created(0, 98)
    p = tape.status()["provenance"]
    assert p["bank_conflicts_by_kind"] == {
        "account_vs_account": int(kind == "account_vs_account"),
        "account_vs_created": int(kind == "account_vs_created"),
        "created_vs_account": int(kind == "created_vs_account"),
        "created_vs_created": int(kind == "created_vs_created"),
        "parent": int(kind == "parent"),
    }
    assert p["bank_conflicts"] == 1 and 100 in tape._bank_tainted
    assert (100 in tape._tainted) == (kind == "parent")
    sample, = p["bank_conflict_samples"]
    assert sample["kind"] == kind and sample["slot"] == 100
    assert sample["incoming_bank_id"] == (0 if kind == "parent" else 1)
    assert sample["label"] == ("watch" if kind.startswith("account_") else "slot")
    assert sample["write_version"] == (2 if kind.startswith("account_") else None)
    if kind == "parent":
        assert sample["parent"] == 98 and sample["previous_parent"] == 99


def test_bank_conflict_diagnostics_preserve_one_taint_when_two_comparisons_disagree():
    tape = _tape()
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=0)
    tape.on_account(POOL, "o", 1, b"x", 100, 1, bank_id=0)
    before = tape.provenance_revision
    tape.on_account(POOL, "o", 1, b"y", 100, 2, bank_id=1)
    p = tape.status()["provenance"]
    assert p["bank_conflicts"] == 1 and tape.provenance_revision == before + 1
    assert p["bank_conflicts_by_kind"]["account_vs_account"] == 1
    assert p["bank_conflicts_by_kind"]["account_vs_created"] == 1
    assert len(p["bank_conflict_samples"]) == 2


def test_bank_conflict_diagnostics_are_bounded_private_snapshots_and_reset_on_reconnect():
    import json
    tape = _tape()
    tape.on_account("private-pubkey", "private-owner", 1, b"private-bytes", 100, 0, bank_id=0)
    for version in range(1, 13):
        tape.on_account("private-pubkey", "private-owner", 1, b"private-bytes", 100, version,
                        bank_id=1, txn_signature=b"s" * 64, label="private-label")
    p = tape.status()["provenance"]
    samples = p["bank_conflict_samples"]
    assert len(samples) == 8 and samples[0]["write_version"] == 5 and samples[-1]["write_version"] == 12
    assert p["bank_conflicts_by_kind"]["account_vs_account"] == 12
    assert "private" not in json.dumps(p) and "ssss" not in json.dumps(p)
    for sample in samples:
        assert sample["label"] == "other"
        assert all(type(value) is int or value is None for name, value in sample.items()
                   if name not in ("label", "kind"))
    samples[0]["slot"] = -1
    p["bank_conflicts_by_kind"]["account_vs_account"] = -1
    assert tape.status()["provenance"]["bank_conflict_samples"][0]["slot"] == 100
    assert tape.status()["provenance"]["bank_conflicts_by_kind"]["account_vs_account"] == 12
    tape.reset("connect")
    p = tape.status()["provenance"]
    assert not p["bank_conflict_samples"] and set(p["bank_conflicts_by_kind"].values()) == {0}


@pytest.mark.parametrize("changed", [{"data": b"other"}, {"owner": "different-owner"}, {"lamports": 2},
                                     {"txn_signature": b"t" * 64},
                                     {"bank_id": 1}, {"is_startup": True}])
def test_conflicting_duplicate_write_taints_the_bank_but_exact_replay_does_not(changed):
    tape = _tape()
    fields = dict(owner="o", lamports=1, data=b"x", txn_signature=b"s" * 64, bank_id=0, is_startup=False)
    def write(**updates):
        f = dict(fields, **updates)
        tape.on_account(POOL, f.pop("owner"), f.pop("lamports"), f.pop("data"), 100, 1, **f)
    write()
    write()
    assert 100 not in tape._bank_tainted
    revision = tape.provenance_revision
    write(**changed)
    assert 100 in tape._bank_tainted
    raw_changed = bool(changed.keys() & {"owner", "lamports", "data"})
    assert (100 in tape._tainted) == raw_changed
    assert tape.provenance_revision > revision
    _created(tape, (101, 100))
    if raw_changed:
        assert tape.lookup([POOL], 101) == (None, "late_parent_write")
    else:
        assert _data(tape.lookup([POOL], 101)[0][POOL]) == b"x"


def test_optional_bank_invalidation_during_frozen_lookup_keeps_the_preparation_hit():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"good", 99, 1, bank_id=0)
    tape.on_slot(99, 98, T.SLOT_CREATED_BANK, bank_id=0)
    tape.on_slot(100, 99, T.SLOT_CREATED_BANK, bank_id=1)
    class MetadataConflict(dict):
        done = False
        def get(self, key, default=None):
            out = super().get(key, default)
            if not self.done:
                self.done = True
                tape.on_slot(99, 98, T.SLOT_CREATED_BANK, bank_id=2)
            return out
    tape._acc = MetadataConflict(tape._acc)
    accounts, parent = tape.lookup([POOL], 100)
    assert parent == 99 and _data(accounts[POOL]) == b"good"
    assert 99 in tape._bank_tainted and not tape._tainted


def test_provenance_lifecycle_and_group_counters_are_bounded_and_reset_per_connection(monkeypatch):
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    monkeypatch.setattr(T.PoolTape, "PARENTS_MAX", 3)
    tape = _tape()
    for slot in range(100, 110):
        tape.on_slot(slot, slot - 1, pb.SLOT_DEAD, bank_id=slot)
        tape.on_slot(slot, slot - 1, T.SLOT_CREATED_BANK, bank_id=slot)
        for i in range(3):
            tape.on_account(str(i), "o", 1, b"x", slot, slot * 3 + i,
                            txn_signature=bytes([slot]) * 64, bank_id=slot)
    assert len(tape._banks) <= 3 and len(tape._bank_tainted) <= 3 and len(tape._account_banks) <= 3
    assert len(tape._provenance_groups) <= 3
    before = tape.status()["provenance"]
    assert before["groups_with_3_accounts"] == 10
    generation = tape.generation
    tape.reset("connect")
    assert tape.generation == generation + 1 and tape._banks == {} and tape._bank_tainted == set()
    assert tape.status()["provenance"]["generation"] == tape.generation
    assert tape.status()["provenance"]["groups_with_3_accounts"] == 0


def _provenance_write(tape, key, version, signature=b"s" * 64, *, slot=100, **changes):
    fields = dict(owner="o", lamports=1, data=b"source", bank_id=0,
                  txn_signature=signature, is_startup=False)
    fields.update(changes)
    tape.on_account(key, fields.pop("owner"), fields.pop("lamports"), fields.pop("data"),
                    slot, version, **fields)


def test_retained_source_survives_raw_ring_trimming_with_full_accepted_entries():
    tape = _tape()
    source = {}
    for version, key in enumerate((POOL, VAULT, CURVE), 1):
        _provenance_write(tape, key, version, data=key.encode())
        source[key] = tape._acc[key][-1]
    for group in range(1, 6):
        for version, key in enumerate((POOL, VAULT, CURVE), group * 10):
            _provenance_write(tape, key, version, bytes([group]) * 64, data=b"later")
    assert all(len(tape._acc[key]) == 4 and source[key] not in tape._acc[key] for key in source)
    records = tape._provenance_groups[(100, 0, b"s" * 64)]
    assert all(isinstance(record, tuple) and len(record) == 2 for record in records)
    retained = dict(records)
    assert all(retained[key] is entry for key, entry in source.items())
    assert retained[POOL][1] == 1 and retained[POOL][3]["data"] == POOL.encode()
    p = tape.status()["provenance"]
    assert p["retained_groups"] == 6 and p["retained_entries"] == 18
    assert p["groups_with_2_accounts"] == p["groups_with_3_accounts"] == 6


@pytest.mark.parametrize("changes", [
    {"data": b"changed"}, {"owner": "other"}, {"lamports": 2},
    {"txn_signature": b"t" * 64}, {"txn_signature": b"short"},
    {"bank_id": 1}, {"is_startup": True}, {"deleted": True},
])
def test_retained_source_detects_conflicting_duplicates_after_raw_eviction(changes):
    tape = _tape()
    _provenance_write(tape, POOL, 1)
    for version in range(2, 7):
        _provenance_write(tape, POOL, version, bytes([version]) * 64, data=b"later")
    _provenance_write(tape, POOL, 1)  # Exact replay stays harmless even outside the raw ring.
    assert 100 not in tape._bank_tainted
    assert tape.status()["provenance"]["duplicate_conflicts"] == 0
    _provenance_write(tape, POOL, 1, **changes)
    assert tape.status()["provenance"]["duplicate_conflicts"] == 1
    assert 100 in tape._bank_tainted
    assert (100 in tape._tainted) == bool(changes.keys() & {"data", "owner", "lamports", "deleted"})
    assert (100, 0, b"t" * 64) not in tape._provenance_groups


def test_retained_source_publishes_only_accepted_writes():
    tape = _tape()
    _provenance_write(tape, POOL, 1)
    _provenance_write(tape, POOL, 1, b"t" * 64)
    assert tape.status()["provenance"]["duplicate_conflicts"] == 1
    assert (100, 0, b"t" * 64) not in tape._provenance_groups


def test_retained_source_replaces_only_with_a_greater_accepted_version():
    tape = _tape()
    _provenance_write(tape, POOL, 3)
    records = tape._provenance_groups[(100, 0, b"s" * 64)]
    assert all(isinstance(record, tuple) and len(record) == 2 for record in records)
    _provenance_write(tape, POOL, 2)
    assert tape._provenance_groups[(100, 0, b"s" * 64)] is records
    _provenance_write(tape, POOL, 4)
    newer = tape._provenance_groups[(100, 0, b"s" * 64)]
    assert newer is not records and dict(newer)[POOL][1] == 4
    assert dict(records)[POOL][1] == 3  # Published readers keep their immutable snapshot.
    assert (POOL, 100, 3, False) not in tape._provenance_entries
    assert tape._provenance_entries[(POOL, 100, 4, False)] is dict(newer)[POOL]
    _provenance_write(tape, POOL, 4)
    assert tape._provenance_groups[(100, 0, b"s" * 64)] is newer


def test_retained_source_cannot_be_created_by_a_replay_below_the_raw_ring():
    tape = _tape()
    _provenance_write(tape, POOL, 1)
    _provenance_write(tape, POOL, 2)
    for version in range(3, 8):
        _provenance_write(tape, POOL, version, bytes([version]) * 64)
    assert (POOL, 100, 1, False) not in tape._provenance_entries
    assert all(entry[1] > 2 for entry in tape._acc[POOL])
    _provenance_write(tape, POOL, 1, b"t" * 64, data=b"changed")
    assert all(entry[1] > 2 for entry in tape._acc[POOL])
    assert (100, 0, b"t" * 64) not in tape._provenance_groups
    assert dict(tape._provenance_groups[(100, 0, b"s" * 64)])[POOL][1] == 2


def test_retained_source_keeps_required_accounts_after_the_first_three_and_caps_storage(monkeypatch):
    monkeypatch.setattr(T.PoolTape, "PARENTS_MAX", 3)
    tape = _tape()
    keys = ["unrelated0", "unrelated1", "unrelated2", POOL, VAULT, CURVE]
    keys += ["extra%d" % i for i in range(60)]
    for version, key in enumerate(keys):
        _provenance_write(tape, key, version)
    records = tape._provenance_groups[(100, 0, b"s" * 64)]
    assert len(records) == 64
    assert {POOL, VAULT, CURVE} <= dict(records).keys()
    assert len(tape._provenance_entries) == 64
    p = tape.status()["provenance"]
    assert p["groups_with_2_accounts"] == p["groups_with_3_accounts"] == 1
    assert p["retention_trims"] == 2
    for version in range(1, 4):
        _provenance_write(tape, "new", version, bytes([version]) * 64)
    assert (100, 0, b"s" * 64) not in tape._provenance_groups
    assert len(tape._provenance_groups) == len(tape._provenance_entries) == 3
    assert all(identity[0] == "new" for identity in tape._provenance_entries)
    assert tape.status()["provenance"]["retention_trims"] == 66


def test_retained_source_reset_and_key_eviction_cannot_bridge_a_coverage_gap(monkeypatch):
    monkeypatch.setattr(T.PoolTape, "MAX_KEYS", 2)
    tape = _tape()
    _provenance_write(tape, POOL, 1)
    _provenance_write(tape, VAULT, 2, b"v" * 64, slot=101)
    _provenance_write(tape, "new", 3, b"n" * 64, slot=102)
    assert POOL not in tape._acc
    assert (100, 0, b"s" * 64) not in tape._provenance_groups
    assert (POOL, 100, 1, False) not in tape._provenance_entries
    _provenance_write(tape, POOL, 4)
    assert dict(tape._provenance_groups[(100, 0, b"s" * 64)])[POOL][1] == 4
    tape.reset("connect")
    assert not tape._provenance_groups and not tape._provenance_entries
    p = tape.status()["provenance"]
    assert p["retained_groups"] == p["retained_entries"] == p["retention_trims"] == 0


def test_retained_source_caps_payload_without_leaving_or_recreating_an_older_witness():
    tape = _tape()
    _provenance_write(tape, POOL, 1, data=b"x" * 4096)
    group = (100, 0, b"s" * 64)
    assert dict(tape._provenance_groups[group])[POOL][3]["data"] == b"x" * 4096
    _provenance_write(tape, VAULT, 2)
    _provenance_write(tape, POOL, 3, data=b"x" * 4097)
    assert tape._acc[POOL][-1][3]["data"] == b"x" * 4097
    assert not tape._provenance_groups[group] and not tape._provenance_entries
    _provenance_write(tape, VAULT, 2)  # A replay cannot rebuild incomplete source proof.
    _provenance_write(tape, CURVE, 4)
    assert not tape._provenance_groups[group] and not tape._provenance_entries
    for version in range(5, 10):
        _provenance_write(tape, POOL, version, bytes([version]) * 64)
    assert all(entry[1] != 3 for entry in tape._acc[POOL])
    assert not tape._provenance_groups[group]


def test_a_stream_error_resets_the_tape_and_reconnects():
    key = bytes(range(32))
    script = [_slot_update(99, 98), _account_update(key, 99, 3, b"pool-99"), _slot_update(100, 99),
              RuntimeError("UNAVAILABLE")]
    tape, status, sent = _run_stream([script, []], lambda: (), 1.5)
    assert status["reconnects"] >= 1 and status["last_error"] == "RuntimeError"
    assert tape.stats["resets"] >= 2 and tape._acc == {}                     # nothing survives the gap


def test_ten_cache_learns_in_a_second_produce_one_batched_resend(monkeypatch):
    """Gamma flag on 2490b5f66 (30 re-sends a minute): additions inside one WATCH_RESEND_MIN_S window
    go out as ONE subscribe update carrying all of them."""
    monkeypatch.setattr(T, "WATCH_RESEND_MIN_S", 1.5)
    monkeypatch.setattr(T, "WATCH_POLL_S", 0.1)
    import time as _t
    started = _t.monotonic()

    def watch():                                   # 10 learns over the first second, then stable
        n = min(10, int((_t.monotonic() - started) * 10))
        return ("Base",) + tuple("New%d" % i for i in range(n))
    tape, status, sent = _run_stream([[_slot_update(100 + i, 99 + i) for i in range(400)]], watch, 2.4)
    subs = [r for r in sent if r.accounts]
    assert len(subs) == 2 and status["resends"] == 1      # the first subscribe + one batched resend
    assert set(subs[1].accounts["watch"].account) == {"Base"} | {"New%d" % i for i in range(10)}
    assert status["addresses_added"] == 10


def test_removals_alone_wait_for_the_sweep_and_a_stable_set_never_resends(monkeypatch):
    monkeypatch.setattr(T, "WATCH_RESEND_MIN_S", 0.2)
    monkeypatch.setattr(T, "WATCH_SWEEP_S", 1.5)
    monkeypatch.setattr(T, "WATCH_POLL_S", 0.1)
    import time as _t

    def shrinking():                               # loses an address at 0.3 s, gains none
        started = _t.monotonic()
        return lambda: ("A", "B") if _t.monotonic() - started < 0.3 else ("A",)
    tape, status, sent = _run_stream([[_slot_update(100 + i, 99 + i) for i in range(400)]], shrinking(), 1.2)
    assert len([r for r in sent if r.accounts]) == 1 and status["resends"] == 0
    tape, status, sent = _run_stream([[_slot_update(100 + i, 99 + i) for i in range(400)]], shrinking(), 2.2)
    assert len([r for r in sent if r.accounts]) == 2 and status["resends"] == 1 and status["addresses_added"] == 0
    tape2, status2, sent2 = _run_stream([[_slot_update(100 + i, 99 + i) for i in range(200)]],
                                        lambda: ("Same",), 1.0)
    assert len([r for r in sent2 if r.accounts]) == 1 and status2["resends"] == 0


def test_the_subscription_is_processed_accounts_with_interslot_slots():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    from sniper.execution.pump_native_exit import PUMP_PROGRAM
    req = T.subscription(("X", "Y"))
    assert req.commitment == pb.CommitmentLevel.PROCESSED and not req.transactions
    assert list(req.accounts["curve"].owner) == [PUMP_PROGRAM]
    assert list(req.accounts["amm"].owner) == ["pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"]
    assert list(req.accounts["watch"].account) == ["X", "Y"]
    assert req.slots["head"].interslot_updates is True


def test_the_stream_never_issues_an_rpc_read():
    """Alpha: nothing on the stream thread may read RPC. The stream functions name no read seam."""
    import ast
    from _source_guard import code_of
    source = "\n".join(code_of(f) for f in (T.run_tape, T.start_tape_thread, T.watch_addresses, T.tape_transport))
    for word in ("getMultipleAccounts", "getAccountInfo", "_rpc", "read_fn", "urlopen", "requests."):
        assert word not in source.replace("https://", ""), word
    # A gRPC proxy option is transport configuration; HTTP imports/call names remain forbidden.
    for node in ast.walk(ast.parse(source)):
        for field in ("id", "attr", "module", "name"):
            symbol = getattr(node, field, "") or ""
            assert "http" not in symbol, symbol


def test_watch_addresses_are_the_fee_configs_plus_known_pools_vaults_and_mints():
    class Cache:
        _pools = {"P1": {"pool_base_token_account": "V1", "pool_quote_token_account": "Q1", "base_mint": "M1"}}
    addresses = T.watch_addresses(Cache())
    assert {"V1", "Q1", "M1"} <= set(addresses) and set(T.fee_config_addresses()) <= set(addresses)
    assert set(T.watch_addresses(None)) == set(T.fee_config_addresses())


def test_the_heartbeat_carries_the_tape_stream_its_rates_and_the_store(tmp_path):
    import json
    import time as _time
    from sniper.services.memecoin_runner import write_heartbeat
    tape = _tape()
    _created(tape, (100, 99))
    tape.lookup(["nope"], 100)
    status = {"connected": True, "reconnects": 1, "frames": 300, "bytes": 90_000, "watch": 25_000,
              "resends": 2, "last_error": None, "thread_cpu_s": 1.5, "connected_mono": _time.monotonic() - 10.0,
              "by_filter": {"amm": [200, 60_000], "curve": [100, 30_000]}, "store": tape.status}
    path = tmp_path / "hb"
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status={"signal": {"rabbit_tape": status}})
    out = json.loads(path.read_text())["slot_lane"]["rabbit_tape"]
    assert out["connected"] and out["thread_cpu_s"] == 1.5 and out["watch"] == 25_000
    assert 19 <= out["by_filter"]["amm"]["frames_per_s"] <= 21 and out["by_filter"]["amm"]["kb_per_s"] > 5
    assert out["store"]["misses"] == {"unseen": 1} and out["store"]["generation"] == tape.generation
    assert out["store"]["snapshots"]["attempts"] == 0
    assert out["store"]["snapshots"]["last_success_age_s"] is None
    assert out["store"]["snapshots"]["current_generation"] is False
    assert 11 <= out["resends_per_min"] <= 13 and out["addresses_added_per_min"] is not None


def test_the_heartbeat_json_retains_the_installed_slippage_guards_current_stats(tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    from sniper.services.memecoin_slot_slippage import make_guard
    guard = make_guard(None, head_fn=lambda: (None, None))
    path = tmp_path / "hb"
    status = {"signal": {"slippage_guard": guard.stats}}
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=status)
    assert json.loads(path.read_text())["slot_lane"]["slippage_guard"] == guard.stats
    guard(None, {})  # No tape: an observed unknown, without provider or ledger work.
    write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=status)
    exported = json.loads(path.read_text())["slot_lane"]["slippage_guard"]
    assert exported == guard.stats
    assert exported["checked"] == exported["unknown"] == 1
    assert exported["refused"] == 0 and exported["last_reason"] == "tape_unavailable"


def test_an_amm_tape_read_whose_two_lookups_see_different_parents_is_a_miss():
    """The mint's program picks the base vault, so the AMM read looks up twice; a CREATED_BANK(S)
    re-parenting between them (a fork switch) must not mix two parents' state."""
    from test_memecoin_rabbit_dflow_amm import FIX, candidate, handler, state_for
    tx = FIX["pure"]["jpegkite_3cPBmtyC_v1"]
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    S = tx["slot"]
    tape = _tape()
    accounts = {k: v for k, v in state_for(tx, plan, read_slot=S - 1)["accounts"].items()
                if k not in plan["holders"].values()}
    _load(tape, accounts, S - 2)
    _created(tape, (S - 2, S - 3), (S - 1, S - 2), (S, S - 1))
    real = tape.lookup
    calls = []

    def lookup(keys, frame_slot, lane_head=None):
        calls.append(1)
        if len(calls) == 2:
            tape.on_slot(S, S - 2, T.SLOT_CREATED_BANK)              # S re-parented onto S-2
        return real(keys, frame_slot, lane_head=lane_head)
    tape.lookup = lookup
    assert h.tape_read(plan, tape, S, None) == (None, "parent_moved")


@pytest.mark.parametrize("knob,watch,started", [(True, True, True), (False, True, False), (True, False, False)])
def test_the_signal_loop_starts_one_tape_stream_only_with_the_knob_and_the_lanes_watch_set(monkeypatch, knob, watch, started):
    import asyncio
    import threading
    from sniper.services import memecoin_shyft_prepare as prep
    from sniper.services import memecoin_shyft_signal as full
    from sniper.services import memecoin_slot_signal as signal
    from sniper.services import memecoin_research_rpc as roles
    from sniper.execution import pump_curve_buy as curve
    from sniper.settings import memecoin as cfg
    from test_memecoin_rabbit_signal_stage2 import FIRE_OWNER, backstop, fire_rpc
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_TAPE", knob)
    threads, purposes, client_urls = [], [], []
    snapshot_client = lambda *args, **kwargs: None
    def dependency(purpose):
        purposes.append(purpose)
        return "https://read.invalid"
    def client(url):
        client_urls.append(url)
        return snapshot_client
    monkeypatch.setattr(roles, "funded_dependency_url", dependency)
    monkeypatch.setattr(curve, "keepalive_custody_rpc", client)
    def start(url, token, tape, watch_fn, status, stop, **kwargs):
        threads.append((url, token, tape, watch_fn(), kwargs['snapshot_factory']()))
    monkeypatch.setattr(T, "start_tape_thread", start)
    stop, status = threading.Event(), {}

    async def fake_rabbit(url, token, owners_fn, on_prepare, feed_status, stop, **kwargs):
        await asyncio.sleep(.05)
        stop.set()

    async def fake_full(url, token, owners_fn, on_buy, feed_status, stop, **kwargs):
        while not stop.is_set():
            await asyncio.sleep(.01)
    monkeypatch.setattr(prep, "run_prepare_feed", fake_rabbit)
    monkeypatch.setattr(full, "run_feed", fake_full)
    rabbit = backstop(fire_rpc())
    asyncio.run(signal._run_signals(lambda b: None, lambda: {FIRE_OWNER: ("uid", "h")}, None, status, stop,
        None, None, None, None, ("tok", ("https://grpc.ams.shyft.to",)), infra_provider="shyft",
        prepare_config=("tok", "https://rabbitstream.fra.shyft.to"), on_prepare=lambda c: None,
        rabbit=rabbit, rabbit_tape_watch_fn=(lambda: ("W",)) if watch else None))
    if started:
        assert len(threads) == 1 and threads[0][:2] == ("https://grpc.ams.shyft.to", "tok")
        assert threads[0][3] == ("W",) and callable(status["rabbit_tape"]["store"])
        assert purposes == ["tape"] and client_urls == ["https://read.invalid"]
        assert threads[0][4] is snapshot_client
        assert callable(rabbit.slippage_guard)
        assert status["slippage_guard"] is rabbit.slippage_guard.stats
    else:
        assert threads == [] and "rabbit_tape" not in status
        assert getattr(rabbit, "slippage_guard", None) is None
        assert purposes == client_urls == []


# --- Gamma BLOCK-1 / LOW-2 on f64362f0b ---------------------------------------------------------

def test_a_write_into_a_parent_after_its_child_was_created_taints_it_and_lookups_miss():
    tape = _tape()
    tape.on_account(POOL, "o", 1, b"before", 98, 1)
    _created(tape, (99, 98), (100, 99))
    assert tape.lookup([POOL], 100)[1] == 99                     # the ordinary order serves
    tape.on_account(POOL, "o", 1, b"late", 99, 2)               # 99 already has a child (100)
    assert tape.stats["late_parent_writes"] == 1
    assert tape.lookup([POOL], 100) == (None, "late_parent_write")
    tape.reset("connect")
    tape.on_account(POOL, "o", 1, b"x", 99, 1)
    _created(tape, (100, 99))
    assert tape.lookup([POOL], 100)[1] == 99                     # a reset clears the taint set


def test_with_no_tape_by_route_and_the_rpc_state_are_exactly_as_before(monkeypatch):
    buyer, stats, fired, tape, rpc, calls, keys, S = _curve_world(monkeypatch, tape_on=False)
    buyer.tape = None
    _fire_once(buyer)
    assert not any(k.startswith("state:") or k.startswith("tape_miss") for k in stats["by_route"].get("direct", {}))
    state = buyer.handlers[0].read({"found": (__import__("test_memecoin_rabbit_signal_stage2").FIRE_MINT, None, "direct"),
                                    "holder": None, "owner": None})
    assert "_raw" not in state


def test_an_owner_seen_buying_the_coin_before_refuses_on_a_tape_hit_with_the_rpc_label(monkeypatch):
    """Alpha: admission identical to the RPC path. RPC read held > 0 for a prior holder and refused
    read_after_source_unproven; the tape reads 0 by construction, so our own buy memory decides."""
    from test_memecoin_rabbit_signal_stage2 import FIRE_MINT, FIRE_OWNER
    buyer, stats, fired, tape, rpc, calls, keys, S = _curve_world(monkeypatch)
    _load(tape, {keys["curve"]: rpc.accounts["curve"], keys["curve_fee_config"]: rpc.accounts["config"]}, S - 1)
    _created(tape, (S - 1, S - 2), (S, S - 1))
    buyer.landing.observe_buy(FIRE_OWNER, FIRE_MINT, 1.0, signature="EarlierBuy", slot=S - 50)
    rec = _fire_once(buyer)
    assert fired == [] and calls == [] and rec["verdict"] == "read_after_source_unproven"
    assert stats["refused"]["read_after_source_unproven"] == 1 and rec["read"]["state_source"] == "tape"


def test_an_unseen_owner_fires_from_the_tape():
    from sniper.services import memecoin_rabbit_signal as R
    w = R.LandingWatch(backstop=None, head_fn=lambda: (None, None), status={})
    w.observe_buy("O", "M", 1.0, signature="Sig1", slot=10)
    assert w.has_bought("O", "M", other_than="Sig2") and not w.has_bought("O", "M", other_than="Sig1")
    assert not w.has_bought("O", "Other")
