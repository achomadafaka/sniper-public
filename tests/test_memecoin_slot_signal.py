"""Slot-lane signal decode on REAL processed frames (tests/fixtures/memecoin/slot_signal_frames.json).

Expected numbers were read off the frames with a full decode against the official pump IDLs, not
with this module, so a wrong offset, a reversed owner check or a swapped reserve fails here.
"""
import asyncio
import base64
import copy
import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.services import memecoin_slot_signal as s

FRAMES = json.loads((Path(__file__).parent / "fixtures/memecoin/slot_signal_frames.json").read_text())
CURVE_BUYER = "2SQarCLrLGA73Hwqfo5fegUbzff88uFMnEq8Y5jP65Yf"
CURVE_SELLER = "D4pycXUXjs7FEWES39RMwfrs2E5LsbxGm5asCX6Tthqu"
AMM_SECOND_BUYER = "Hj8e8KNBbmvdUUVbTmnJTEEQ6mEi5PRf2BnjRDjQCLTF"  # 2nd of 4 BuyEvents in one tx


def _decode(kind, owner, uid="uid-1"):
    return s.decode_owner_buy(FRAMES[kind], owner, bytes(Pubkey.from_string(owner)), uid, "h",
                              recv_monotonic=1.0, recv_utc="2026-09-26T20:26:05Z")


def test_curve_buy_decodes_the_owners_trade():
    ev = _decode("curve_buy", CURVE_BUYER)
    assert (ev.venue, ev.source_user_id, ev.owner, ev.slot) == ("pump_curve", "uid-1", CURVE_BUYER, 450781549)
    assert ev.signature.startswith("2gtM4kij2d4f")
    assert ev.mint == "C1P7oNjsUyQ8XJpYJ21waw6tc1dJtFxNgTuH37bGpump"
    assert (ev.sol_amount, ev.token_amount) == (98765431, 984478963817)
    assert (ev.virtual_sol_reserves, ev.virtual_token_reserves) == (56877060898, 565957516599287)
    assert (ev.real_sol_reserves, ev.real_token_reserves) == (26877060898, 286057516599287)
    # the tx's FIRST token invoke is classic Tokenkeg (an ATA create); the pump CPI uses Token-2022
    assert ev.token_program == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    assert ev.trade_event_log.startswith("Program data: vdt/007mYe6ji")
    assert (ev.pool, ev.handle, ev.recv_monotonic, ev.recv_utc) == (None, "h", 1.0, "2026-09-26T20:26:05Z")


CREATOR_MIGRATION = json.loads((Path(__file__).parent /
    'fixtures/memecoin/pump_creator_migration_74106.json').read_text())


def _creator_migration_frame():
    source = CREATOR_MIGRATION['source']
    return {'params': {'result': {'context': {'slot': source['slot']}, 'value': {
        'signature': source['signature'], 'err': None, 'logs': list(source['logs'])}}}}


def _migration_buy(frame):
    owner = CREATOR_MIGRATION['owner']
    return s.decode_owner_buy(frame, owner, bytes(Pubkey.from_string(owner)), 'u',
                              recv_monotonic=0., recv_utc='')


def _create_event_bytes(frame):
    from sniper.research.memecoin_opportunity.decoders import BorshReader
    logs = frame['params']['result']['value']['logs']
    index = next(i for i, line in enumerate(logs) if line.startswith('Program data: G3Kp'))
    raw = bytearray(base64.b64decode(logs[index].split(': ', 1)[1]))
    reader = BorshReader(raw)
    reader.take(8)
    for _ in range(3):
        reader.string()
    offsets = {'mint': reader.offset, 'curve': reader.offset + 32}
    reader.take(4 * 32 + 5 * 8)
    offsets['token_program'] = reader.offset
    reader.take(32 + 1)
    offsets['cashback'] = reader.offset
    return logs, index, raw, offsets


def test_launch_buy_proves_cashback_false_before_creator_migration():
    buy = _migration_buy(_creator_migration_frame())
    assert buy.mint == CREATOR_MIGRATION['mint']
    assert buy.signature == CREATOR_MIGRATION['source']['signature']
    assert getattr(buy, 'curve_cashback_enabled', 'missing') is False


def test_launch_buy_preserves_explicit_cashback_true():
    frame = _creator_migration_frame()
    logs, index, raw, offsets = _create_event_bytes(frame)
    raw[offsets['cashback']] = 1
    logs[index] = 'Program data: ' + base64.b64encode(raw).decode()
    assert getattr(_migration_buy(frame), 'curve_cashback_enabled', 'missing') is True


@pytest.mark.parametrize('change', ['absent', 'wrong_mint', 'wrong_curve', 'wrong_token_program',
                                   'wrong_program', 'malformed', 'conflicting', 'after_buy'])
def test_unbound_creation_keeps_cashback_unknown(change):
    frame = _creator_migration_frame()
    logs, index, raw, offsets = _create_event_bytes(frame)
    if change == 'absent':
        del logs[index]
    elif change in ('wrong_mint', 'wrong_curve', 'wrong_token_program'):
        field = change.removeprefix('wrong_')
        raw[offsets[field]:offsets[field] + 32] = bytes(Pubkey.from_string(s.TOKEN_PROGRAM))
        logs[index] = 'Program data: ' + base64.b64encode(raw).decode()
    elif change == 'wrong_program':
        logs[index:index + 1] = ['Program %s invoke [2]' % s.PUMP_AMM_PROGRAM,
                                logs[index], 'Program %s success' % s.PUMP_AMM_PROGRAM]
    elif change == 'malformed':
        raw[offsets['cashback']] = 2
        logs[index] = 'Program data: ' + base64.b64encode(raw).decode()
    elif change == 'conflicting':
        raw[offsets['cashback']] = 1
        logs.insert(index, 'Program data: ' + base64.b64encode(raw).decode())
    else:
        create = logs.pop(index)
        logs.extend(['Program %s invoke [1]' % s.PUMP_PROGRAM, create,
                     'Program %s success' % s.PUMP_PROGRAM])
    buy = _migration_buy(frame)
    assert buy is not None
    assert getattr(buy, 'curve_cashback_enabled', 'missing') is None


def test_old_curve_buy_has_no_cashback_creation_proof():
    assert getattr(_decode('curve_buy', CURVE_BUYER), 'curve_cashback_enabled', 'missing') is None


def test_amm_buy_picks_the_owners_event_out_of_four():
    ev = _decode("amm_buy", AMM_SECOND_BUYER)
    assert (ev.venue, ev.slot, ev.mint, ev.token_program) == ("pump_amm", 450781550, None, None)
    assert ev.pool == "Au5iJcWEh8oYiE5LsgtSArMnkC92bwtz9N4uKAiny3Gj"
    assert (ev.sol_amount, ev.token_amount) == (587192212, 5833003151531)
    # PRE-trade: the first event's reserves minus its own trade.
    assert (ev.real_token_reserves, ev.real_sol_reserves) == (163404206079321, 13346054378)
    assert ev.virtual_sol_reserves is None and ev.virtual_token_reserves is None


def test_only_the_subscribed_owners_buy_counts():
    assert _decode("curve_buy", CURVE_SELLER) is None          # someone else's buy
    assert _decode("curve_sell", CURVE_SELLER) is None         # the owner's SELL
    assert _decode("amm_buy", CURVE_BUYER) is None             # owner absent from the tx


def test_two_own_buys_in_one_transaction_are_refused_out_loud():
    frame = copy.deepcopy(FRAMES["curve_buy"])
    logs = frame["params"]["result"]["value"]["logs"]
    i = next(n for n, line in enumerate(logs) if line.startswith("Program data: vdt/"))
    logs.insert(i, logs[i])  # policy check on our own rule, not a decode claim
    ev = s.decode_owner_buy(frame, CURVE_BUYER, bytes(Pubkey.from_string(CURVE_BUYER)), "u",
                            recv_monotonic=0.0, recv_utc="")
    assert ev.refusal == "multi_own_buy:2xpump_curve"
    assert ev.mint == "C1P7oNjsUyQ8XJpYJ21waw6tc1dJtFxNgTuH37bGpump"


ROUTE = json.loads((Path(__file__).parent / "fixtures/memecoin/slot_double_buy_823NjegkV3.json")
                   .read_text())


def test_a_route_buy_with_two_own_events_is_refused_and_named_never_dropped():
    """notdecu, 2026-09-27 from 03:49Z: 22 buys of 823Nje... each carried TWO own events, a PumpSwap
    buy of the curve's quote token then a curve buy quoted in it (FLASHX route). The decoder returned
    None for more than one own event, and the lane logged no decision line from 03:06:43Z on."""
    frame = {"params": {"result": {"context": {"slot": ROUTE["slot"]}, "value": {
        "signature": ROUTE["signature"], "err": None, "logs": ROUTE["logs"]}}}}
    owner = ROUTE["owner"]
    ev = s.decode_owner_buy(frame, owner, bytes(Pubkey.from_string(owner)), "pump:" + owner,
                            "notdecu", recv_monotonic=1.0, recv_utc="2026-09-27T03:50:00Z")
    assert ev.refusal == "multi_own_buy:pump_amm+pump_curve"
    assert ev.mint.startswith("823Nje") and ev.signature == ROUTE["signature"]
    assert ev.slot == ROUTE["slot"] and ev.handle == "notdecu"


def test_failed_transaction_is_not_a_signal():
    frame = copy.deepcopy(FRAMES["curve_buy"])
    frame["params"]["result"]["value"]["err"] = {"InstructionError": [3, {"Custom": 6002}]}
    assert s.decode_owner_buy(frame, CURVE_BUYER, bytes(Pubkey.from_string(CURVE_BUYER)), "u",
                              recv_monotonic=0.0, recv_utc="") is None


def test_ws_url_conversion_and_funded_key_refusal(monkeypatch):
    assert s.ws_url_from_http("https://mainnet.helius-rpc.com/?api-key=abc123") == \
        "wss://mainnet.helius-rpc.com/?api-key=abc123"
    assert s.ws_url_from_http("http://127.0.0.1:8899") == "ws://127.0.0.1:8899"
    assert s.ws_url_from_http("wss://x") == s.ws_url_from_http(None) == s.ws_url_from_http("https://") == ""
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_POOL_URLS", "https://a/?api-key=zz,https://b/?api-key=abc123")
    for bad in ("wss://mainnet.helius-rpc.com/?api-key=abc123", ""):
        try:
            s.start_signal_thread(lambda b: None, owners_fn=dict, ws_url=bad, status={})
        except ValueError:
            continue
        raise AssertionError("started on %r" % bad)


class _FakeWS:
    """Acks each logsSubscribe with a subscription id, then replays one captured frame per owner."""

    def __init__(self, stop):
        self.sent, self.out, self._stop = [], asyncio.Queue(), stop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["method"] == "slotSubscribe":
            await self.out.put({"jsonrpc": "2.0", "id": msg["id"], "result": 7})
            for slot in (450781550, 450781549):  # a lower fork slot arrives second
                await self.out.put({"jsonrpc": "2.0", "method": "slotNotification", "params": {
                    "subscription": 7, "result": {"slot": slot, "parent": slot - 1, "root": slot - 32}}})
        if msg["method"] == "logsSubscribe":
            sid = 1000 + msg["id"]
            await self.out.put({"jsonrpc": "2.0", "id": msg["id"], "result": sid})
            frame = copy.deepcopy(FRAMES["curve_buy"])
            frame["params"]["subscription"] = sid
            await self.out.put(frame)

    async def recv(self):
        if self.out.empty():
            self._stop.set()
            await asyncio.sleep(0.05)
            raise asyncio.TimeoutError
        return json.dumps(await self.out.get())


def test_socket_loop_routes_frames_by_subscription_owner(monkeypatch):
    import threading
    monkeypatch.setattr(s, "ROSTER_REFRESH_S", 0.0)   # re-read the roster on every loop turn
    got, status, stop, reads = [], {}, threading.Event(), []
    ws = _FakeWS(stop)

    def owners():
        reads.append(1)
        if len(reads) > 1:     # every later read is garbage: the live roster must survive it
            return ("not", "a dict")
        # seller FIRST: routing by anything but the frame's own subscription id reads the seller
        return {CURVE_SELLER: ("uid-seller", "seller"), CURVE_BUYER: ("uid-buyer", "buyer"),
                "not-base58!": ("uid-bad", "bad")}
    snapshots = []
    def receive(buy):
        snapshots.append(dict(status))
        got.append(buy)
    thread = s.start_signal_thread(receive, status=status, ws_url="wss://x", stop=stop,
                                   connect=lambda url: ws, owners_fn=owners)
    thread.join(timeout=10)
    assert not thread.is_alive()
    subscribed = sorted(m["params"][0]["mentions"][0] for m in ws.sent if m["method"] == "logsSubscribe")
    assert subscribed == sorted([CURVE_BUYER, CURVE_SELLER])      # malformed owner skipped
    # the same buy frame arrives on both subscriptions; only the buyer's subscription emits it
    assert [(e.source_user_id, e.handle, e.owner) for e in got] == [("uid-buyer", "buyer", CURVE_BUYER)]
    assert status["subscriptions"] == 2 and status["events"] == 1 and status["decode_errors"] == 0
    assert status["last_frame_mono"] is not None and status["connected"] is False  # stopped
    assert status["head_slot"] == 450781550 and status["head_slot_mono"] is not None
    assert len(reads) > 1 and status["reconnects"] == 0 and status["last_error"].startswith("roster:")


    assert snapshots[0]["requested_owners"] == tuple(sorted([CURVE_BUYER, CURVE_SELLER]))
    order = [m["params"][0]["mentions"][0] for m in ws.sent if m["method"] == "logsSubscribe"]
    expected = tuple(sorted(order[:order.index(CURVE_BUYER) + 1]))
    assert snapshots[0]["acknowledged_owners"] == expected
    assert status["acknowledged_owners"] == ()

def test_each_buy_carries_the_head_slot_known_at_its_receipt(monkeypatch):
    """The fake socket delivers slots 450781550 then 450781549 (a fork, ignored) before the buy at
    450781549, so the head the lane knew at receipt is 450781550: head minus S = +1."""
    import threading
    monkeypatch.setattr(s, "ROSTER_REFRESH_S", 0.0)
    got, status, stop = [], {}, threading.Event()
    ws = _FakeWS(stop)
    thread = s.start_signal_thread(got.append, status=status, ws_url="wss://x", stop=stop,
                                   connect=lambda url: ws,
                                   owners_fn=lambda: {CURVE_BUYER: ("uid-buyer", "buyer")})
    thread.join(timeout=10)
    (ev,) = got
    assert ev.head_at_recv == 450781550 and ev.head_at_recv - ev.slot == 1
    assert ev.head_at_recv_mono is not None and ev.head_at_recv_mono <= ev.recv_monotonic


def test_a_decode_without_a_head_reading_records_none_not_zero():
    ev = _decode("curve_buy", CURVE_BUYER)
    assert ev.head_at_recv is None and ev.head_at_recv_mono is None


def test_the_token_program_comes_from_the_owner_events_own_pump_invocation():
    """An earlier pump.fun invocation in the same transaction (another event, a classic-token coin)
    must not lend its token program to the owner's buy, a Token-2022 coin. Found by Codex's
    2026-09-26 re-review; the frame is the real curve buy with one pump invocation prepended."""
    from sniper.execution import pump_curve_buy as pcb
    frame = copy.deepcopy(FRAMES["curve_buy"])
    logs = frame["params"]["result"]["value"]["logs"]
    logs[:0] = ["Program %s invoke [1]" % pcb.PUMP_PROGRAM, "Program %s invoke [2]" % pcb.TOKEN_PROGRAM,
                "Program %s success" % pcb.TOKEN_PROGRAM, "Program %s success" % pcb.PUMP_PROGRAM]
    ev = s.decode_owner_buy(frame, CURVE_BUYER, bytes(Pubkey.from_string(CURVE_BUYER)), "u",
                            recv_monotonic=0, recv_utc="")
    assert ev.token_program == pcb.TOKEN_2022_PROGRAM


def test_two_token_programs_inside_the_events_invocation_are_unknown_not_a_guess():
    from sniper.execution import pump_curve_buy as pcb
    frame = copy.deepcopy(FRAMES["curve_buy"])
    logs = frame["params"]["result"]["value"]["logs"]
    event_index = next(i for i, line in enumerate(logs) if line.startswith("Program data:"))
    logs[event_index:event_index] = ["Program %s invoke [3]" % pcb.TOKEN_PROGRAM,
                                     "Program %s success" % pcb.TOKEN_PROGRAM]
    ev = s.decode_owner_buy(frame, CURVE_BUYER, bytes(Pubkey.from_string(CURVE_BUYER)), "u",
                            recv_monotonic=0, recv_utc="")
    assert ev.token_program is None


def test_a_mixed_owner_buy_and_sell_dispatches_the_buy_before_the_sell(monkeypatch):
    """A rotation must emit both legs; sell decoding must not delay buy dispatch."""
    import base64
    import threading
    mixed = copy.deepcopy(FRAMES['curve_buy'])
    sell_logs = []
    for line in FRAMES['curve_sell']['params']['result']['value']['logs']:
        if line.startswith('Program data: '):
            raw = base64.b64decode(line.split(': ', 1)[1])
            raw = raw.replace(s._owner_bytes(CURVE_SELLER), s._owner_bytes(CURVE_BUYER))
            line = 'Program data: ' + base64.b64encode(raw).decode()
        sell_logs.append(line)
    mixed['params']['result']['value']['logs'] += sell_logs
    monkeypatch.setitem(FRAMES, 'curve_buy', mixed)
    stop, status, delivered = threading.Event(), {}, []
    ws = _FakeWS(stop)
    decode_sell = s.decode_owner_sells
    def after_buy(*a, **kw):
        assert delivered and delivered[0][0] == 'buy'
        return decode_sell(*a, **kw)
    monkeypatch.setattr(s, 'decode_owner_sells', after_buy)
    thread = s.start_signal_thread(lambda ev: delivered.append(('buy', ev)),
        on_sell=lambda rows: delivered.extend(('sell', ev) for ev in rows),
        owners_fn=lambda: {CURVE_BUYER: ('u', 'h')}, status=status,
        ws_url='wss://offline', stop=stop, connect=lambda url: ws)
    thread.join(5)
    assert not thread.is_alive()
    assert [kind for kind, _ in delivered] == ['buy', 'sell']
    assert delivered[0][1].signature == delivered[1][1].signature
    assert delivered[1][1].mint != delivered[0][1].mint


def test_socket_receipt_uses_corrected_clock_and_keeps_initial_monotonic(monkeypatch):
    import threading
    import sniper.util.clock as clock
    marker = '2026-10-09T10:11:12+00:00'
    monkeypatch.setattr(clock, 'real_utc_now_iso', lambda: marker)
    got, status, stop = [], {}, threading.Event()
    ws = _FakeWS(stop)
    thread = s.start_signal_thread(got.append, status=status, ws_url='wss://x', stop=stop,
        connect=lambda url: ws, owners_fn=lambda: {CURVE_BUYER: ('uid', 'h')})
    thread.join(timeout=3)
    assert got[0].recv_utc == marker
    assert got[0].recv_monotonic <= status['last_frame_mono']


def test_proof_telemetry_is_optional_and_does_not_change_signal_identity():
    from dataclasses import replace
    buy = _decode('curve_buy', CURVE_BUYER)
    assert replace(buy, proof_ready_monotonic=2, proof_provenance='confirmed_shyft_grpc') == buy


def test_cancelled_socket_clears_acknowledged_owners():
    import threading
    import pytest
    status = {'reconnects': 0}
    class CancelledWS(_FakeWS):
        async def recv(self):
            raise asyncio.CancelledError
    ws = CancelledWS(threading.Event())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(s._run(lambda buy: None, lambda: {CURVE_BUYER: ('u', 'h')},
                           'unused', status, threading.Event(), lambda url: ws))
    assert status['connected'] is False
    assert status['acknowledged_owners'] == ()


def test_single_feed_journal_is_after_callback_and_isolates_failed_publisher(monkeypatch):
    import threading
    from test_memecoin_feed_arrivals import _journal
    for broken in (False, True):
        journal, stop, status, got, order = _journal(), threading.Event(), {}, [], []
        record = journal.record_source
        def publish(**fields):
            assert got
            order.append('publication')
            if broken: raise RuntimeError('measurement')
            record(**fields)
        monkeypatch.setattr(journal, 'record_source', publish)
        ws = _FakeWS(stop)
        def receive(buy): got.append(buy); order.append('callback')
        thread = s.start_signal_thread(receive, owners_fn=lambda: {CURVE_BUYER: ('u', 'h')},
            ws_url='wss://x', status=status, stop=stop, connect=lambda url: ws, measurement=journal)
        thread.join(5)
        assert len(got) == 1 and order == ['callback', 'publication']
        assert got[0].receipt_evidence['producer_run_id'] == 'source-A'
        if not broken:
            assert journal.snapshot()['rows'][0]['recv_monotonic'] == got[0].recv_monotonic


def test_pair_origin_provenance_and_expiry_gap(monkeypatch):
    from test_memecoin_feed_arrivals import _journal
    journal = _journal()
    pairer = s.RoutedPairer(wait_s=.5, measurement=journal)
    pairer.watch('owner', 'uid', 'h')
    for kind, _ in s.ROUTED_PROGRAMS: pairer.set_baseline('owner', kind, 1, {})
    pairer.on_token('owner', 'token', 2, 'coin', 'mint', 3, 6, recv_monotonic=0, recv_utc='first')
    pairer.on_token('owner', 'token', 2, 'usd', s.USDC_MINT, 0, 6, recv_monotonic=.1, recv_utc='second')
    assert pairer.expire(1) == 1
    gap, = journal.snapshot()['rows']
    assert gap['reason'] == 'pair_expired' and gap['mono_start'] == 0
    assert gap['lost_count'] is None


def test_unknown_native_amm_receipt_never_infers_mint():
    from test_memecoin_feed_arrivals import _journal
    journal = _journal()
    buy = _decode('amm_buy', AMM_SECOND_BUYER)
    s._publish_receipt(journal, buy, 'helius', 3)
    row, = journal.snapshot()['rows']
    assert row['mint'] is None and row['pool'] == buy.pool
    assert row['decode_state'] == 'pool_unresolved' and row['connection_generation'] == 3


def test_publication_isolated_from_callback_exception_and_forbidden_producer_work(monkeypatch):
    import threading
    import sqlite3
    from sniper.services import memecoin_slot_skips as skips
    from test_memecoin_feed_arrivals import _journal
    journal = _journal()
    stop, status, dispatched = threading.Event(), {}, []
    ws = _FakeWS(stop)
    def forbidden(*a, **k): raise AssertionError('producer did off-path work')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(skips, 'note', forbidden)
    # The existing exception logger is expected once; measurement itself may not log.
    logged = []
    monkeypatch.setattr(s.logger, 'exception', lambda *a, **k: logged.append(a[0]))
    def receive(buy):
        dispatched.append(buy)
        raise RuntimeError('entry callback')
    thread = s.start_signal_thread(receive, owners_fn=lambda: {CURVE_BUYER: ('u', 'h')},
        ws_url='wss://x', status=status, stop=stop, connect=lambda url: ws, measurement=journal)
    thread.join(5)
    assert len(dispatched) == len(journal.snapshot()['rows']) == 1
    assert logged == ['slot signal on_buy failed for %s']


def test_source_decode_error_and_connection_loss_are_unknown_gaps(monkeypatch):
    import threading
    from test_memecoin_feed_arrivals import _journal
    journal, stop, status = _journal(20), threading.Event(), {}
    class BrokenWS(_FakeWS):
        async def recv(self):
            if self.out.empty():
                stop.set()
                raise RuntimeError('provider down')
            return await super().recv()
    ws = BrokenWS(stop)
    monkeypatch.setattr(s, 'decode_owner_buy', lambda *a, **k: (_ for _ in ()).throw(ValueError('decode')))
    async def no_sleep(_): pass
    monkeypatch.setattr(s.asyncio, 'sleep', no_sleep)
    thread = s.start_signal_thread(lambda b: (_ for _ in ()).throw(AssertionError('dispatch')),
        owners_fn=lambda: {CURVE_BUYER: ('u', 'h')}, ws_url='wss://x', status=status,
        stop=stop, connect=lambda url: ws, measurement=journal)
    thread.join(5)
    rows = journal.snapshot()['rows']
    assert {r['reason'] for r in rows if r['kind'] == 'gap'} == {'decode_error', 'connection_lost'}
    assert all(r['lost_count'] is None for r in rows if r['kind'] == 'gap')


def test_background_roster_freezes_provenance_for_original_receipt(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    from test_memecoin_feed_arrivals import _journal
    from types import MappingProxyType
    roster, status = {}, {}
    monkeypatch.setattr(lane, 'owners_from_roster', lambda *a: ({'owner': ('uid', 'h')}, frozenset({'uid'})))
    assert lane._refresh_roster(roster, status, None, 'unused')
    frozen = roster['receipt_provenance']
    journal = _journal()
    journal.provenance_fn = lambda: roster['receipt_provenance']
    evidence = journal.identity(0, 1)
    monkeypatch.setattr(lane, 'owners_from_roster', lambda *a: ({'other': ('new', 'n')}, frozenset({'new'})))
    assert lane._refresh_roster(roster, status, None, 'unused')
    assert evidence['roster_generation'] == frozen['roster_generation']
    assert evidence['roster_generation'] != roster['receipt_provenance']['roster_generation']
    assert 'uid' in evidence['roster_json'] and 'new' not in evidence['roster_json']
    assert isinstance(frozen, MappingProxyType)


def test_source_enrollment_survives_cold_admission_failure_and_roster_change(monkeypatch):
    from dataclasses import replace
    from sniper.services import memecoin_slot_lane as lane
    from test_memecoin_feed_arrivals import _journal
    roster, status, journal = {}, {}, _journal()
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_SOLANA_SLOT_LANE_MODE', 'live')
    monkeypatch.setattr(lane, 'owners_from_roster',
        lambda *a: ({CURVE_BUYER: ('uid-1', 'h')}, frozenset({'uid-1'})))
    assert lane._refresh_roster(roster, status, None, 'unused')
    journal.provenance_fn = lambda: roster['receipt_provenance']
    evidence = journal.identity(1, 2)
    buy = replace(_decode('curve_buy', CURVE_BUYER), receipt_evidence=evidence)
    frozen_cohort = evidence['frozen_cohort_id']
    # Our readiness/refusal and subsequent disarm cannot change source enrollment.
    roster['owners'] = {}
    roster['receipt_provenance'] = {}
    s._publish_receipt(journal, buy, 'shyft:grpc.ams.shyft.to', 2, state='cold_admission_refused')
    row, = journal.snapshot()['rows']
    assert row['frozen_cohort_id'] == frozen_cohort
    assert row['source_authorized'] is True and row['source_venue_authorized'] is True
    assert row['source_stratum'] == 'native_local_proof'
    assert row['source_buy_state'] == 'successful'
    assert row['decoded_monotonic'] >= row['recv_monotonic']


def test_source_stratum_uses_source_completion_and_routed_proof_without_cache_outcome():
    from dataclasses import replace
    from test_memecoin_feed_arrivals import _journal
    journal = _journal()
    buy = _decode('curve_buy', CURVE_BUYER)
    for changed in (replace(buy, real_token_reserves=0),
                    replace(buy, refusal='multi_own_buy:pump_amm+pump_curve')):
        s._publish_receipt(journal, changed, 'shyft:grpc.ams.shyft.to', 2)
    first, second = journal.snapshot()['rows']
    assert first['source_stratum'] == 'graduation' and first['source_buy_state'] == 'successful'
    assert second['source_stratum'] == 'routed_confirmed_proof'
    assert second['source_buy_state'] == 'proof_pending'


def test_pair_keeps_first_half_frozen_provenance_after_roster_refresh():
    from test_memecoin_feed_arrivals import _journal
    journal = _journal(20)
    current = {'roster_generation': 'first', 'config_generation': 'config-1'}
    journal.provenance_fn = lambda: current
    pairer = s.RoutedPairer(measurement=journal, connection_generation=4)
    pairer.watch('owner', 'uid', 'h')
    for kind, _ in s.ROUTED_PROGRAMS: pairer.set_baseline('owner', kind, 1, {'usd': 100})
    pairer.on_token('owner', 'token', 2, 'coin', 'mint', 3, 6, recv_monotonic=0, recv_utc='first')
    current = {'roster_generation': 'later', 'config_generation': 'config-2'}
    pairer.on_token('owner', 'token', 2, 'usd', s.USDC_MINT, 50, 6, recv_monotonic=.1, recv_utc='second')
    buy, = pairer.on_logs('owner', 2, 'sig', lane=False, recv_monotonic=.2, recv_utc='last')
    s._emit_routed([buy], lambda _: None, measurement=journal, generation=4)
    row, = journal.snapshot()['rows']
    assert row['recv_monotonic'] == 0 and row['roster_generation'] == 'first'
    assert row['config_generation'] == 'config-1' and row['connection_generation'] == 4


def test_measurement_wiring_keeps_fake_starter_signature_compatible(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    from test_memecoin_feed_arrivals import _journal
    class Thread:
        def __init__(self, **kw): pass
        def start(self): pass
    monkeypatch.setattr('threading.Thread', Thread)
    called = []
    def starter(on_buy, *, owners_fn, status): called.append(owners_fn)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key='fixture')
    journal = _journal()
    status = lane.start_slot_lane(rc, builder=object(), signal_start_fn=starter,
        sol_usd_fn=lambda: None, log=lambda _: None, roster_path='unused',
        conn_factory=object, readonly_conn_factory=object, measurement=journal)
    assert len(called) == 1 and status['_receipt_journals'] == (journal,)


def test_reconnect_retains_connection_generation_and_duplicate_original_receipts(monkeypatch):
    import threading
    from test_memecoin_feed_arrivals import _journal
    journal, stop, status, got, opened = _journal(20), threading.Event(), {}, [], []
    class ReconnectingWS(_FakeWS):
        async def recv(self):
            if self.out.empty():
                if len(opened) > 1: stop.set()
                raise RuntimeError('disconnect')
            return await super().recv()
    def connect(url):
        ws = ReconnectingWS(stop)
        opened.append(ws)
        return ws
    async def no_sleep(_): pass
    monkeypatch.setattr(s.asyncio, 'sleep', no_sleep)
    thread = s.start_signal_thread(got.append, owners_fn=lambda: {CURVE_BUYER: ('u', 'h')},
        ws_url='wss://x', status=status, stop=stop, connect=connect, measurement=journal)
    thread.join(5)
    assert not thread.is_alive() and len(got) == 1
    rows = [r for r in journal.snapshot()['rows'] if r['kind'] == 'source']
    assert [r['connection_generation'] for r in rows] == [1, 2]
    assert [r['decode_state'] for r in rows] == ['decoded_buy', 'duplicate_buy']


def test_failed_receipt_publication_retains_typed_unknown_coverage(monkeypatch):
    from test_memecoin_feed_arrivals import _journal
    journal = _journal()
    monkeypatch.setattr(journal, 'record_source', lambda **kw: (_ for _ in ()).throw(ValueError('schema')))
    s._publish_receipt(journal, _decode('curve_buy', CURVE_BUYER), 'helius', 2)
    row, = journal.snapshot()['rows']
    assert row['kind'] == 'gap' and row['reason'] == 'receipt_publication_failed'
    assert row['mono_start'] == row['mono_end'] == 1 and row['lost_count'] == 1


def test_unknown_helius_decode_keeps_receipt_provenance_when_roster_changes_during_decode(monkeypatch):
    import threading
    from test_memecoin_feed_arrivals import _journal
    journal, stop, status = _journal(), threading.Event(), {}
    current = {'roster_generation': 'at-receipt', 'config_generation': 'at-receipt'}
    journal.provenance_fn = lambda: current
    def undecoded(*args, **kwargs):
        current.update(roster_generation='after-decode', config_generation='after-decode')
        return None
    monkeypatch.setattr(s, 'decode_owner_buy', undecoded)
    ws = _FakeWS(stop)
    thread = s.start_signal_thread(lambda _: (_ for _ in ()).throw(AssertionError('buy')),
        owners_fn=lambda: {CURVE_BUYER: ('u', 'h')}, ws_url='wss://x', status=status,
        stop=stop, connect=lambda _: ws, measurement=journal)
    thread.join(5)
    row, = journal.snapshot()['rows']
    assert row['decode_state'] == 'empty_decode_unknown'
    assert row['roster_generation'] == row['config_generation'] == 'at-receipt'


def test_logs_first_pair_reuses_explicit_receipt_provenance_and_original_clock():
    from test_memecoin_feed_arrivals import _journal
    journal = _journal()
    current = {'roster_generation': 'first', 'config_generation': 'first'}
    journal.provenance_fn = lambda: current
    first = journal.identity(0, 7)
    current.update(roster_generation='later', config_generation='later')
    pairer = s.RoutedPairer(measurement=journal, connection_generation=7)
    pairer.watch('owner', 'uid', 'h')
    for kind, _ in s.ROUTED_PROGRAMS: pairer.set_baseline('owner', kind, 1, {'usd': 100})
    pairer.on_logs('owner', 2, 'signature', lane=False, recv_monotonic=0,
                   recv_utc='first', receipt_evidence=first)
    pairer.on_token('owner', 'token', 2, 'coin', 'mint', 3, 6,
                    recv_monotonic=.1, recv_utc='coin')
    buy, = pairer.on_token('owner', 'token', 2, 'usd', s.USDC_MINT, 50, 6,
                           recv_monotonic=.2, recv_utc='usd')
    assert buy.recv_monotonic == 0 and buy.paired_monotonic == .2
    assert buy.receipt_evidence is first
    assert buy.receipt_evidence['roster_generation'] == buy.receipt_evidence['config_generation'] == 'first'


def test_real_helius_logs_first_pair_receives_predecode_provenance(monkeypatch):
    import threading
    from test_memecoin_feed_arrivals import _journal
    journal, stop, status, observed = _journal(), threading.Event(), {}, []
    current = {'roster_generation': 'first', 'config_generation': 'first'}
    journal.provenance_fn = lambda: current
    class SpyPairer(s.RoutedPairer):
        def watching(self, owner): return True
        def on_logs(self, *args, **kwargs):
            observed.append(kwargs)
            return []
    monkeypatch.setattr(s, 'RoutedPairer', SpyPairer)
    def undecoded(*args, **kwargs):
        current.update(roster_generation='later', config_generation='later')
        return None
    monkeypatch.setattr(s, 'decode_owner_buy', undecoded)
    ws = _FakeWS(stop)
    thread = s.start_signal_thread(lambda _: None, on_routed=lambda _: None,
        owners_fn=lambda: {CURVE_BUYER: ('u', 'h')}, ws_url='wss://x', status=status,
        stop=stop, connect=lambda _: ws, measurement=journal)
    thread.join(5)
    receipt = observed[0]['receipt_evidence']
    assert receipt['roster_generation'] == receipt['config_generation'] == 'first'
