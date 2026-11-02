"""Slot lane admission: every refusal before the reservation is decided in memory, with no write.

The lane exists to be fast, so a stale input must refuse rather than trigger a network read, and a
refusal before the reservation must leave no ledger row (the J7 lane records its own verdict on the
same buy a second later).
"""
import contextlib
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as lane


import json
from pathlib import Path
from sniper.execution.pump_curve_buy import parse_trade_event

CURVE = json.loads((Path(__file__).parent/'fixtures/pump_curve_buy_frames.json').read_text())['direct_buy_exact_sol_in']
CURVE_EVENT = parse_trade_event(CURVE['trade_event_log'])


def _buy(**kw):
    base = dict(source_user_id="w1", owner=CURVE_EVENT["user"], mint=CURVE_EVENT["mint"], signature=CURVE["signature"],
                trade_event_log=CURVE["trade_event_log"],
                slot=450_000_000, sol_amount=2 * 10 ** 9,
                # 30 SOL / 1.0e9 tokens (6 dp) = 3e-8 SOL per token; at $150/SOL mcap = $4,500
                virtual_sol_reserves=30 * 10 ** 9, virtual_token_reserves=10 ** 9 * 10 ** 6,
                recv_utc="2026-09-26T20:30:00+00:00", recv_monotonic=time.monotonic())
    base.update(kw)
    return SimpleNamespace(**base)


def _snap(now, **age):
    seen = {"sol_usd": now - age.get("sol_usd", 1), "balances": now - age.get("balances", 1),
            "blockhash": now - age.get("blockhash", 1)}
    return lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5,
                                 blockhash="Hash1", last_valid_height=1, observed_mono=seen)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _lane(conn, snap, armed=("w1",), kill=False, lock_key="sol-test"):
    def forbidden(*_a, **_k):
        raise AssertionError("no build/execute before admission")
    return lane.SlotLane(conn=conn, executor={"slot_execute_fn": forbidden}, build_fn=forbidden,
                         snapshot_fn=lambda: snap, armed_ids_fn=lambda: set(armed),
                         kill_tripped_fn=lambda: kill,
                         now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key=lock_key,
                         mode="live")


def _attempt_rows(conn):
    return conn.execute("SELECT COUNT(*) FROM memecoin_entry_attempts").fetchone()[0]


def test_curve_price_reads_post_trade_reserves():
    price = lane.curve_price_usd(_buy(), 150.0)
    assert price == pytest.approx(30 / 1e9 * 150.0)
    assert lane.curve_price_usd(_buy(virtual_token_reserves=0), 150.0) is None


@pytest.mark.parametrize("stale", ["sol_usd", "balances", "blockhash"])
def test_a_stale_input_refuses_without_any_write(conn, stale):
    now = time.monotonic()
    snap = _snap(now, **{stale: lane.MAX_AGE_S[stale] + 5})
    result = _lane(conn, snap).handle(_buy())
    assert result == {"status": "rejected", "reason": "slot_lane_stale_%s" % stale}
    assert _attempt_rows(conn) == 0


@pytest.mark.parametrize("armed,kill,reason", [((), False, "not_approved"),
                                               (("w1",), True, "kill_switch")])
def test_unarmed_wallet_or_kill_switch_refuses_without_any_write(conn, armed, kill, reason):
    result = _lane(conn, _snap(time.monotonic()), armed=armed, kill=kill).handle(_buy())
    assert result["status"] == "rejected" and result["reason"] == reason
    assert _attempt_rows(conn) == 0


def test_mcap_over_the_ceiling_refuses_without_any_write(conn, monkeypatch):
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 1_000.0)
    result = _lane(conn, _snap(time.monotonic())).handle(_buy())
    assert result["status"] == "rejected" and result["reason"].startswith("mcap $4500 over")
    assert _attempt_rows(conn) == 0


def test_a_held_wallet_lock_refuses_without_waiting_or_writing(conn, tmp_path, monkeypatch):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    with exec_lock.wallet_exec_lock("sol-test") as held:
        assert held
        t0 = time.monotonic()
        result = _lane(conn, _snap(time.monotonic())).handle(_buy())
        assert time.monotonic() - t0 < 1.0
    assert result == {"status": "rejected", "reason": "slot_lane_wallet_busy"}
    assert _attempt_rows(conn) == 0


def test_admitted_buy_reserves_with_a_contract_then_builds_and_sends(
        conn, tmp_path, monkeypatch):
    """End to end on the real schema up to the executor seam: an admitted buy writes one attempt
    and one reservation carrying a funded contract, then builds and hands the message to the executor."""
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)   # see _live_lane
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW", False)
    seen = {}

    def build_fn(buy, clip, snap):
        seen.update(clip=clip)
        return "MESSAGE", {"blockhash": "Hash1"}

    def slot_execute_fn(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                        entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        seen.update(pending=dict(pending), message=message, validity=validity,
                    entry_min_slot=entry_min_slot, entry_max_slot=entry_max_slot,
                    entry_head_fn=entry_head_fn)
        return {"status": "filled", "position_id": 1}

    head = lambda: (450_000_100, time.monotonic())
    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": slot_execute_fn},
                       build_fn=build_fn, snapshot_fn=lambda: _snap(time.monotonic()),
                       armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key="sol-test",
                       mode="live", head_slot_fn=head)
    result = ln.handle(_buy())
    assert result["status"] == "filled", result
    assert seen["message"] == "MESSAGE" and seen["validity"]["blockhash"] == "Hash1"
    assert seen["entry_min_slot"] is None and seen["entry_max_slot"] is None
    assert seen["entry_head_fn"] is head
    from sniper.execution.submission_transport import entry_slot_refusal
    assert entry_slot_refusal(None, seen["entry_head_fn"]) is None
    assert seen["validity"]["entry_sol_budget"]["pending_id"] == seen["pending"]["id"]
    assert seen["validity"]["entry_sol_budget"]["available_lamports"] > 0
    assert seen["validity"]["entry_rent_lamports"] == 6_117_840
    pending = seen["pending"]
    assert pending["status"] == "reserved" and pending["network_id"] == "solana"
    assert pending["signal_source"] == "slot_lane"
    assert pending["origin_event_id"] == "slotlane:" + CURVE["signature"]
    assert pending["funded_contract_hash"] and pending["funded_contract_json"]
    assert pending["reserved_notional_usd"] == pytest.approx(seen["clip"])
    row = conn.execute("SELECT terminal_status, signal_source FROM memecoin_entry_attempts").fetchone()
    assert tuple(row) == ("filled", "slot_lane")


@pytest.mark.parametrize("same_target", [True, False])
def test_native_buffer_binds_exact_lamports_before_building(conn, tmp_path, monkeypatch,
                                                            same_target):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    seen = {}

    def build_fn(_buy, clip, _snap, *, funding_lamports=None):
        seen["clip"], seen["lamports"] = clip, funding_lamports
        return "NATIVE_MESSAGE", {"funding_asset": "sol",
                                  "funding_amount_atomic": funding_lamports,
                                  "target_mint": _buy.mint if same_target else "wrong-mint"}

    def execute(_conn, pending, _message, **_kw):
        seen["pending"] = pending
        return {"status": "unknown"}

    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": execute},
                       build_fn=build_fn, snapshot_fn=lambda: lane.PrefetchSnapshot(
                           sol_usd=100.0, usdc_balance=400.0, sol_balance=2.0,
                           blockhash="Hash1", last_valid_height=1,
                           observed_mono={k: time.monotonic() for k in
                                          ("sol_usd", "balances", "blockhash")}),
                       armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-09-27T15:00:00Z", lock_key="sol-test",
                       mode="live", shadow_clip_fn=lambda: 100.0, sol_buffer_cap_usd=175.0)
    assert ln.handle(_buy())["status"] == ("unknown" if same_target else "aborted")
    if not same_target:
        assert "pending" not in seen
        return
    assert seen["pending"]["funding_asset"] == "sol"
    assert int(seen["pending"]["funding_amount_atomic"]) == seen["lamports"]
    assert seen["lamports"] > 0


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


def test_prefetcher_keeps_the_old_stamp_when_a_read_fails_so_the_lane_sees_it_age_out():
    clock = _Clock()
    state = {"usdc": 400.0}

    def usdc():
        if state["usdc"] is None:
            raise RuntimeError("rpc down")
        return state["usdc"]
    pre = lane.LanePrefetcher(usdc_fn=usdc, sol_fn=lambda: 0.5,
                              blockhash_fn=lambda: ("Hash1", 99), sol_usd_fn=lambda: 150.0,
                              clock=clock)
    pre.refresh_once()
    snap = pre.snapshot()
    assert lane.stale_input(snap, clock.t) is None
    assert (snap.usdc_balance, snap.sol_balance, snap.blockhash) == (400.0, 0.5, "Hash1")
    state["usdc"] = None                      # every later balance read fails
    clock.t += lane.MAX_AGE_S["balances"] + 1
    pre.refresh_once()
    snap = pre.snapshot()
    assert snap.usdc_balance == 400.0         # old value kept...
    assert lane.stale_input(snap, clock.t) == "balances"   # ...but it ages out and refuses


def test_a_signal_older_than_the_slot_window_refuses_without_any_write(conn):
    old = _buy(recv_monotonic=time.monotonic() - lane.MAX_SIGNAL_AGE_S - 0.1)
    result = _lane(conn, _snap(time.monotonic())).handle(old)
    assert result["status"] == "rejected" and result["reason"] == "slot_lane_signal_too_old"
    assert _attempt_rows(conn) == 0


def test_owners_from_roster_watches_only_armed_solana_wallets_with_a_resolved_owner(conn, tmp_path):
    # The LEGACY path: a ledger without the address registry keeps the JSONL handle join. The
    # registry path is covered in tests/test_memecoin_slot_lane_registry_owners.py.
    conn.execute("DROP TABLE memecoin_address_registry_events")
    conn.execute("DROP TABLE memecoin_address_registry")
    rows = [("u1", "Alice", "solana"), ("u2", "Bob", "4663"), ("u3", "Carol", "solana")]
    for uid, handle, chains in rows:
        conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle, paused, excluded, "
                     "real_approved, real_chains) VALUES (?,?,0,0,1,?)", (uid, handle, chains))
    conn.commit()
    roster = tmp_path / "owners.jsonl"
    roster.write_text("\n".join([
        '{"handle": "alice", "owner": "OwnerA", "error": null}',
        '{"handle": "bob", "owner": "OwnerB", "error": null}',       # armed on 4663 only
        '{"handle": "carol", "owner": null, "error": "no tx"}',      # unresolved owner
        'not json',
    ]))
    owners, armed = lane.owners_from_roster(conn, str(roster))
    assert owners == {"OwnerA": ("u1", "alice")}
    assert armed == frozenset({"u1", "u3"})


def _live_lane(conn, tmp_path, monkeypatch, *, head=None, mode="live", sent=None):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    # No market observations in the test ledger -> regime unknown -> a scaled clip, which the lane
    # refuses before reserving; a full-scale regime is what production shows on 12 of 12 lane buys.
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    sent = sent if sent is not None else []

    def slot_execute_fn(*a, **k):
        sent.append(1)
        return {"status": "filled"}
    return lane.SlotLane(
        conn=conn, executor={"slot_execute_fn": slot_execute_fn},
        build_fn=lambda buy, clip, snap: ("MESSAGE", {}),
        snapshot_fn=lambda: _snap(time.monotonic()), armed_ids_fn=lambda: {"w1"},
        kill_tripped_fn=lambda: False, now_iso_fn=lambda: "2026-09-26T20:30:00+00:00",
        lock_key="sol-test", mode=mode, head_slot_fn=(lambda: head) if head else None,
        shadow_clip_fn=lambda: 25.0), sent


def test_a_fresh_head_past_s_plus_1_still_sends_under_the_soft_deadline(conn, tmp_path, monkeypatch):
    """Soft deadline (operator, 2026-09-26): S/S+1 is a target, not a cutoff. A head already at S+3
    refuses nothing; the price cap and the risk limits decide."""
    ln, sent = _live_lane(conn, tmp_path, monkeypatch, head=(450_000_003, time.monotonic()))
    result = ln.handle(_buy())
    assert result["status"] == "filled" and sent == [1]
    events = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])[
        "substage_events"]
    post = next(e for e in events if e["name"] == "slot_post_build")
    assert post["head_slot"] == 450_000_003 and "reason" not in post and "max_slot" not in post
    assert not any(e["name"] == "slot_route_preparation" for e in events)


def test_a_stale_head_reading_does_not_refuse(conn, tmp_path, monkeypatch):
    ln, sent = _live_lane(conn, tmp_path, monkeypatch, head=(450_000_009, time.monotonic() - 5))
    assert ln.handle(_buy())["status"] == "filled" and sent == [1]


@pytest.mark.parametrize("mode", ["shadow", "", "LIVEX"])
def test_anything_but_live_is_shadow_and_never_reserves_or_sends(conn, tmp_path, monkeypatch, mode):
    ln, sent = _live_lane(conn, tmp_path, monkeypatch, mode=mode)
    result = ln.handle(_buy())
    assert result["status"] == "shadow" and result["reason"] == "built" and not sent
    assert _attempt_rows(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_a_pumpswap_buy_is_refused_before_any_read_of_its_reserves(conn):
    amm = _buy(venue="pump_amm", mint=None, virtual_sol_reserves=None, virtual_token_reserves=None)
    result = _lane(conn, _snap(time.monotonic())).handle(amm)
    assert result == {"status": "rejected", "reason": "slot_lane_venue_pump_amm"}
    assert _attempt_rows(conn) == 0


# --- PumpSwap (venue pump_amm) ------------------------------------------------------------------
# A live buy and its pool's accounts (tests/fixtures/pumpswap_buy_frames.json, read 2026-09-26).

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from sniper.execution import pumpswap_buy as ps  # noqa: E402

AMM_FX = json.loads((Path(__file__).parent / "fixtures" / "pumpswap_buy_frames.json").read_text())
AMM_LINE = AMM_FX["direct_buy_exact_quote_in"]["buy_event_log"]
AMM_EVENT = ps.parse_buy_event(AMM_LINE)


def _pool_facts(**over):
    facts = ps.parse_pool_account(AMM_FX["pool_account_b64"])
    facts.update(base_token_program=AMM_FX["base_mint_owner"],
                 quote_token_program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                 **ps.mint_supply_and_decimals(AMM_FX["base_mint_account_b64"]))
    facts["pump_migrated"] = ps.is_pump_migrated_pool(facts)
    facts.update(over)
    return facts


def _pools(facts=None):
    cache = ps.PumpSwapPoolCache(rpc=None)
    if facts is not None:
        cache._pools[AMM_EVENT["pool"]] = facts
    return cache


def _amm_buy(**kw):
    base = dict(venue="pump_amm", source_user_id="w1", owner=AMM_EVENT["user"], mint=None,
                pool=AMM_EVENT["pool"], signature=AMM_FX["direct_buy_exact_quote_in"]["signature"], slot=450_784_762,
                sol_amount=AMM_EVENT["quote_amount_in"], trade_event_log=AMM_LINE,
                recv_utc="2026-09-26T20:30:00+00:00", recv_monotonic=time.monotonic())
    base.update(kw)
    return SimpleNamespace(**base)


def _open_floors(monkeypatch):
    """The source-buy floor is not under test here; the live buy is a $7 buy of a ~$450k coin."""
    for name, value in (("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {}),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD", 0.0),
                        ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0.0)):
        monkeypatch.setattr(lane.cfg, name, value)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 1_000_000.0)


def test_amm_price_is_post_trade_with_the_virtual_quote_and_the_real_supply():
    facts = _pool_facts()
    reason, got = lane.amm_market_facts(_amm_buy(), _pools(facts), 150.0)
    assert reason is None and got["mint"] == facts["base_mint"]
    base_post, quote_post = ps.post_trade_reserves(AMM_EVENT)
    price = (quote_post + facts["virtual_quote"]) / 1e9 / (base_post / 1e6) * 150.0
    assert got["price_usd"] == pytest.approx(price)
    # The mint is burned below 1e9 (965,316,184.8 tokens); the mcap uses what exists.
    assert got["mcap_usd"] == pytest.approx(price * 965_316_184.834876)
    # What the trader PAID, pool fees included: the lanes' one source-size basis (2026-09-28).
    assert got["their_usd"] == pytest.approx(ps.buy_event_gross_quote(AMM_EVENT) / 1e9 * 150.0)


def test_amm_buy_on_a_known_pool_reserves_with_a_contract(conn, tmp_path, monkeypatch):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)   # see _live_lane
    _open_floors(monkeypatch)
    facts, seen = _pool_facts(), {}

    class _Amm:
        def build(self, buy, clip, snap):
            seen.update(buy=buy, clip=clip)
            return "AMM_MESSAGE", {"blockhash": "Hash1"}

    def slot_execute_fn(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                        entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        seen.update(pending=dict(pending), message=message)
        return {"status": "filled", "position_id": 1}

    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": slot_execute_fn},
                       build_fn=lane.dispatch_build({"pump_amm": _Amm()}),
                       snapshot_fn=lambda: _snap(time.monotonic()), armed_ids_fn=lambda: {"w1"},
                       kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key="sol-test",
                       mode="live", amm_pools=_pools(facts))
    result = ln.handle(_amm_buy())
    assert result["status"] == "filled", result
    assert seen["message"] == "AMM_MESSAGE"
    pending = seen["pending"]
    assert pending["mint"] == facts["base_mint"] and pending["status"] == "reserved"
    assert pending["origin_event_id"] == "slotlane:" + AMM_FX["direct_buy_exact_quote_in"]["signature"] and pending["signal_source"] == "slot_lane"
    assert pending["funded_contract_hash"] and pending["funded_contract_json"]
    row = conn.execute("SELECT mint, terminal_status FROM memecoin_entry_attempts").fetchone()
    assert tuple(row) == (facts["base_mint"], "filled")


def test_amm_buy_on_an_unknown_pool_refuses_before_any_write_and_is_queued(conn):
    pools = _pools(None)
    ln = _lane(conn, _snap(time.monotonic()))
    ln.amm_pools = pools
    result = ln.handle(_amm_buy())
    assert result == {"status": "rejected", "reason": "pool_unknown"}
    assert _attempt_rows(conn) == 0
    assert AMM_EVENT["pool"] in pools._pending            # the prefetch thread learns it next


@pytest.mark.parametrize("pool_hint", [None, AMM_EVENT["pool"]])
def test_amm_trace_keeps_event_bound_pool_before_submission(conn, tmp_path, monkeypatch, pool_hint):
    _open_floors(monkeypatch)
    ln, _ = _live_lane(conn, tmp_path, monkeypatch)
    ln.amm_pools = _pools(_pool_facts())
    def execute(_conn, _pending, _message, **kwargs):
        trace = json.loads(conn.execute('SELECT trace_json FROM memecoin_entry_attempts').fetchone()[0])
        assert trace['source_evidence']['pool'] == AMM_EVENT['pool']
        assert trace['source_evidence']['mint'] == _pool_facts()['base_mint']
        return {'status': 'unknown', 'sig': 'OurSig'}
    ln.executor['slot_execute_fn'] = execute
    assert ln.handle(_amm_buy(pool=pool_hint))['status'] == 'unknown'


def test_conflicting_amm_pool_hint_cannot_create_measurement_identity(conn, tmp_path, monkeypatch):
    _open_floors(monkeypatch)
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    ln.amm_pools = _pools(_pool_facts())
    assert ln.handle(_amm_buy(pool='wrong-pool'))['reason'] == 'slot_lane_event_pool_mismatch'
    assert not sent and _attempt_rows(conn) == 0


@pytest.mark.parametrize("over, reason", [
    ({"pump_migrated": False}, "slot_lane_not_pump_mint"),
    ({"base_supply": None}, "supply_unknown"),
    ({"quote_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}, "slot_lane_quote_not_sol"),
])
def test_amm_buy_refuses_a_pool_it_cannot_price_before_any_write(conn, over, reason):
    ln = _lane(conn, _snap(time.monotonic()))
    ln.amm_pools = _pools(_pool_facts(**over))
    result = ln.handle(_amm_buy())
    assert result["status"] == "rejected" and result["reason"] == reason
    assert _attempt_rows(conn) == 0


def test_the_non_pump_check_is_structural_not_the_mint_suffix():
    facts = _pool_facts()
    assert facts["pump_migrated"] is True
    # Same pump-looking mint, a creator that is not pump's pool-authority PDA: not a pump pool.
    assert ps.is_pump_migrated_pool({**facts, "creator": facts["base_mint"]}) is False


def test_dispatch_routes_by_venue():
    calls = []

    class _B:
        def __init__(self, name):
            self.name = name

        def build(self, *a):
            calls.append(self.name)
            return "M", {}
    build = lane.dispatch_build({"pump_curve": _B("curve"), "pump_amm": _B("amm")})
    build(_buy(), 1.0, None)
    build(_amm_buy(), 1.0, None)
    assert calls == ["curve", "amm"]
    assert build(_buy(venue="raydium"), 1.0, None) == (None, "no_builder_for_raydium")


def test_an_armed_pump_profile_is_watched_at_the_address_its_id_names(conn, tmp_path):
    addr = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
    for uid, handle, chains in (("pump:" + addr, "notdecu", "solana"),
                                ("pump:not-a-key", "junk", "solana"),
                                ("pump:DNsh1UfJdxmze6T6GV9QK5SoFm7HsM5TRNxVuwVgo8Zj", "hb", "4663")):
        conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle, paused, excluded, "
                     "real_approved, real_chains) VALUES (?,?,0,0,1,?)", (uid, handle, chains))
    conn.commit()
    roster = tmp_path / "owners.jsonl"
    roster.write_text("")
    owners, _armed = lane.owners_from_roster(conn, str(roster))
    assert owners == {addr: ("pump:" + addr, "notdecu")}


def test_a_build_delay_past_the_decision_age_still_sends_and_is_recorded(conn, tmp_path, monkeypatch):
    """Only the decision-time age check refuses. A stall after it (send 4029: 340 ms in
    market_data) no longer aborts; the post-build substage records the age, record-only."""
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    clock = _Clock()
    clock.perf_counter = clock.monotonic
    ln.clock = clock
    ln.snapshot_fn = lambda: _snap(clock.t)

    def slow_build(*args):
        clock.t += lane.MAX_SIGNAL_AGE_S + 0.01
        return 'MESSAGE', {}

    ln.build_fn = slow_build
    result = ln.handle(_buy(recv_monotonic=clock.t))
    assert result['status'] == 'filled' and sent == [1]
    payload = json.loads(conn.execute('SELECT trace_json FROM memecoin_entry_attempts').fetchone()[0])
    post = next(e for e in payload['substage_events'] if e['name'] == 'slot_post_build')
    assert post['signal_age_ms'] > lane.MAX_SIGNAL_AGE_S * 1000 and 'reason' not in post
    assert payload['source_evidence']['their_size_usd'] == 300.0


def test_source_evidence_is_durable_before_executor_and_survives_finish(conn, tmp_path, monkeypatch):
    ln, _ = _live_lane(conn, tmp_path, monkeypatch)
    def execute(conn, pending, message, **kwargs):
        row = dict(conn.execute('SELECT * FROM memecoin_entry_attempts').fetchone())
        evidence = json.loads(row['trace_json'])['source_evidence']
        assert evidence['signature'] == CURVE['signature'] and evidence['their_size_usd'] == 300.0
        assert evidence['source_timestamp_trust'] == 'observed_detection'
        assert evidence['producer_run_id'] == 'original-run'
        assert evidence['boot_id'] == 'original-boot' and evidence['clock_domain'] == 'original-domain'
        assert evidence['recv_monotonic'] == 0 and evidence['roster_generation'] == 'original-roster'
        return {'status': 'unknown', 'sig': 'OurSig'}
    ln.executor['slot_execute_fn'] = execute
    result = ln.handle(_buy(receipt_evidence=dict(producer_run_id='original-run',
        boot_id='original-boot', clock_domain='original-domain', recv_monotonic=0,
        roster_generation='original-roster')))
    row = dict(conn.execute('SELECT * FROM memecoin_entry_attempts').fetchone())
    assert row['tx_sig'] == 'OurSig' and row['pending_id'] == result['pending_id']
    assert json.loads(row['trace_json'])['source_evidence']['their_size_usd'] == 300.0


def test_the_signed_price_limit_lands_in_the_trace(conn, tmp_path, monkeypatch):
    """The builder's pump_price_limit reaches the attempt's trace as substage slot_price_limit."""
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    limit = {"source_price_usd": 4.5e-6, "cap_pct": 30.0, "cost_usdc_atomic": 175_000_000,
             "base_decimals": 6, "spend_lamports": 1_418_853_206,
             "expected_tokens_atomic": 30_857_271_081_473, "output_floor_atomic": 21_600_089_757_031,
             "price_floor_atomic": 25_302_498_233_294, "min_tokens_atomic": 25_302_498_233_294}
    ln.build_fn = lambda buy, clip, snap: ("MESSAGE", {"blockhash": "Hash1",
                                                               "pump_price_limit": limit})
    assert ln.handle(_buy())["status"] == "filled" and sent == [1]
    events = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])[
        "substage_events"]
    row = next(e for e in events if e["name"] == "slot_price_limit")
    assert {k: row[k] for k in limit} == limit


def test_the_attempt_row_write_is_the_first_substage_and_the_receipt_head_follows(
        conn, tmp_path, monkeypatch):
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    now = time.monotonic()
    buy = _buy(recv_monotonic=now, head_at_recv=450_000_001, head_at_recv_mono=now - 0.1)
    assert ln.handle(buy)["status"] == "filled" and sent == [1]
    events = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])[
        "substage_events"]
    assert [e["name"] for e in events[:2]] == ["attempt_row", "slot_receipt"]
    receipt = events[1]
    assert receipt["head_at_recv"] == 450_000_001 and receipt["head_at_recv_minus_s"] == 1
    assert receipt["head_at_recv_age_ms"] == pytest.approx(100.0, abs=1.0)
    assert receipt["source_slot"] == 450_000_000 and receipt["duration_ms"] == 0.0


def test_an_unknown_receipt_head_is_none_not_zero(conn, tmp_path, monkeypatch):
    ln, _sent = _live_lane(conn, tmp_path, monkeypatch)
    ln.handle(_buy())                                    # no head attributes at all
    events = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])[
        "substage_events"]
    receipt = next(e for e in events if e["name"] == "slot_receipt")
    assert receipt["head_at_recv"] is None and receipt["head_at_recv_minus_s"] is None
    assert receipt["head_at_recv_age_ms"] is None


@pytest.mark.parametrize("head, landing, expected", [
    (450_000_001, 450_000_003, (1, 3)),
    (None, 450_000_001, (None, 1)),
    (449_999_999, None, (-1, None)),     # the frame beat the slot notification
    (450_000_001, 0, (1, None)),         # a zero is never a landing slot
])
def test_slot_offsets_and_the_log_line(head, landing, expected):
    buy = _buy(head_at_recv=head, handle="notdecu")
    result = {"status": "filled", "reason": "", "lane_ms": 5.0, "head_minus_s": 0,
              "landing_slot": landing}
    offsets = lane.slot_offsets(buy, result)
    assert (offsets["head_at_recv_minus_s"], offsets["landing_minus_s"]) == expected
    line = lane.result_log_line(buy, {**result, **offsets})
    assert "recv-S=%s land-S=%s" % expected in line and "head-S=0" in line


def test_a_refused_signal_socket_starts_no_worker_and_opens_no_connection(monkeypatch):
    """start_signal_thread raises on a funded key or an empty URL. Before 2026-09-26 the prefetch
    and lane threads were already running by then, and the orphaned prefetch kept re-quoting
    Jupiter on the FUNDED key for the life of the process (Codex re-review witness)."""
    starts, opened = [], []

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.name = name

        def start(self):
            starts.append(self.name)
    monkeypatch.setattr("threading.Thread", Thread)

    def refuse(*a, **k):
        raise ValueError("slot signal needs its own ws url, never the funded custody key")
    rc = dict(usdc_fn=lambda: pytest.fail("provider read"), sol_fn=lambda: None,
              latest_blockhash_fn=lambda: None, slot_lock_key="fixture")
    with pytest.raises(ValueError, match="funded custody key"):
        lane.start_slot_lane(rc, builder=object(), signal_start_fn=refuse, sol_usd_fn=lambda: None,
                             log=lambda _: None, roster_path="unused",
                             conn_factory=lambda: opened.append("rw"),
                             readonly_conn_factory=lambda: opened.append("ro"))
    assert starts == [] and opened == []


def test_a_started_lane_runs_the_signal_first_then_both_workers(monkeypatch):
    starts = []

    class Thread:
        def __init__(self, *, target, name, daemon):
            self.name = name

        def start(self):
            starts.append(self.name)
    monkeypatch.setattr("threading.Thread", Thread)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    status = lane.start_slot_lane(rc, builder=object(),
                                  signal_start_fn=lambda *a, **k: starts.append("slot-signal"),
                                  sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
                                  conn_factory=object, readonly_conn_factory=object)
    # The lane's own threads, in order, the signal first. Other daemons the lane starts (the
    # entry-shadow-read reader, 4992dd69e) may sit anywhere after the signal: an exact list over every
    # thread broke on each new one.
    own = ["slot-signal", "slot-lane-prefetch-sol_usd", "slot-lane-prefetch-balances",
           "slot-lane-prefetch-blockhash", "slot-lane-prefetch", "slot-lane"]
    assert starts[0] == "slot-signal"
    assert [name for name in starts if name in own] == own
    assert status["handled"] == 0


def test_stream_migration_arriving_at_signal_start_is_queued_until_consumer_ready(monkeypatch):
    targets, offered = {}, []
    class Thread:
        def __init__(self, *, target, name, daemon): targets[name] = target
        def start(self): pass
    monkeypatch.setattr('threading.Thread', Thread)
    from sniper.services import memecoin_slot_graduation as grad
    monkeypatch.setattr(grad, 'mode', lambda: 'off')
    pools = SimpleNamespace(stats={}, offer_stream=offered.append, note_migration=lambda *a: None)
    def signal(*_, on_migration, **kw):
        on_migration('early-pool')
        assert offered == []
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key='fixture')
    status = lane.start_slot_lane(rc, builders={'pump_curve': object(),
        'pump_amm': SimpleNamespace(pools=pools)}, signal_start_fn=signal,
        migration_on_signal=True, sol_usd_fn=lambda: None, log=lambda _: None,
        roster_path='unused', conn_factory=object, readonly_conn_factory=object)
    # Stop after the queued pool; no provider or entry code runs on the signal callback.
    class Stop(BaseException): pass
    def offer(pool): offered.append(pool); raise Stop()
    pools.offer_stream = offer
    with pytest.raises(Stop): targets['slot-lane-migrations']()
    assert offered == ['early-pool'] and status['prewarm']['queued'] == 1


def test_extra_prefetch_opens_its_readonly_connection_on_its_own_worker(monkeypatch):
    import threading
    real_thread = threading.Thread
    targets, owner, observed = {}, [], []

    class Thread:
        def __init__(self, *, target, name, daemon):
            targets[name] = target

        def start(self):
            pass

    class StopLoop(Exception):
        pass

    class Clock:
        @staticmethod
        def monotonic():
            return 1000.0

        @staticmethod
        def sleep(_seconds):
            raise StopLoop()

    class Reader:
        def rollback(self):
            assert threading.get_ident() == owner[0]
            observed.append("rollback")

        def close(self):
            assert threading.get_ident() == owner[0]
            observed.append("close")

    def open_reader():
        owner.append(threading.get_ident())
        observed.append("open")
        return Reader()

    def refresh(*_a, **_kw):
        assert observed[-1] == "rollback"  # no read transaction held across provider work
        observed.append("refresh")

    monkeypatch.setattr(threading, "Thread", Thread)
    monkeypatch.setattr(lane, "load_cu_demotions", lambda *_a, **_k: ())
    monkeypatch.setattr(lane, "owners_from_roster", lambda *_a: ({}, frozenset()))
    monkeypatch.setattr(lane, "leg_plan", lambda *_a: (100.0, 1.0))
    monkeypatch.setattr(lane, "refresh_builder_legs", refresh)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_ONCHAIN_LANE_ENABLED", False)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture")
    lane.start_slot_lane(rc, builder=object(), signal_start_fn=lambda *_a, **_k: None,
                         sol_usd_fn=lambda: None, log=lambda _: None, roster_path="unused",
                         conn_factory=object, readonly_conn_factory=open_reader, clock=Clock)
    assert not owner  # startup must not hand a SQLite handle to another thread

    def run_extra():
        try:
            targets["slot-lane-prefetch"]()
        except StopLoop:
            pass

    worker = real_thread(target=run_extra, daemon=True)
    worker.start()
    worker.join(timeout=1.0)
    assert not worker.is_alive() and owner == [worker.ident]
    assert observed == ["open", "rollback", "refresh", "rollback", "close"]


def test_a_regime_scaled_clip_refuses_before_any_reservation(conn, tmp_path, monkeypatch):
    """The lane's leg is prefetched for the full clip; a regime scale-down would build
    leg_over_clip after reserving. It refuses first, with no reservation row, and names why."""
    from sniper.services import memecoin_market_state as ms
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    real = ms.classify_funded_entry

    def scaled(*a, **k):
        c = real(*a, **k)
        return dataclasses.replace(c, risk_scale=0.75)
    import dataclasses
    monkeypatch.setattr(ms, "classify_funded_entry", scaled)
    result = ln.handle(_buy())
    assert result["reason"] == lane.SCALED_CLIP_REASON and not sent
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    row = conn.execute("SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts").fetchone()
    assert tuple(row) == ("rejected", lane.SCALED_CLIP_REASON)



def test_a_trial_wallet_rides_the_lane_sol_funded_at_the_trial_clip(conn, tmp_path, monkeypatch):
    """Trial arm (Alpha on 92646580c): a trial graded on the slow fallback lane would be graded
    under a handicap a promoted wallet does not have. SOL funding has no prefetched USDC leg, so the
    lane carries the trial itself: the reservation takes the trial clip and freezes the marker."""
    import json as _json
    from sniper.execution import exec_lock
    from sniper.services import memecoin_trial as T
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_SIZING", "fixed")     # $100 normal clip, so the
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})  # trial cap is what binds
    monkeypatch.setattr(lane.cfg, "MEMECOIN_TRADE_SIZE_USD", 100.0)
    tid = m.start_trial(conn, source_user_id="w1", chain="solana", clip_usd=20.0, rule=T.RULE,
                        actor="test", now_iso="2026-09-26T20:00:00+00:00")
    seen = {}

    def build_fn(buy, clip, _snap, *, funding_lamports=None):
        seen["clip"], seen["lamports"] = clip, funding_lamports
        return "NATIVE_MESSAGE", {"funding_asset": "sol", "funding_amount_atomic": funding_lamports,
                                  "target_mint": buy.mint}

    def execute(_conn, pending, _message, **_kw):
        seen["pending"] = pending
        return {"status": "filled"}
    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": execute}, build_fn=build_fn,
                       snapshot_fn=lambda: lane.PrefetchSnapshot(
                           sol_usd=100.0, usdc_balance=400.0, sol_balance=2.0, blockhash="Hash1",
                           last_valid_height=1, observed_mono={k: time.monotonic() for k in
                                                               ("sol_usd", "balances", "blockhash")}),
                       armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key="sol-test",
                       # the full clip is above the SOL buffer cap: the buffer judges the trial clip
                       mode="live", shadow_clip_fn=lambda: 300.0, sol_buffer_cap_usd=175.0)
    assert ln.handle(_buy())["status"] == "filled"
    assert seen["clip"] == pytest.approx(20.0) and seen["pending"]["funding_asset"] == "sol"
    contract = _json.loads(seen["pending"]["funded_contract_json"])
    assert contract["source"]["trial"]["trial_id"] == tid


def test_a_trial_wallet_without_sol_funding_is_refused_with_nothing_held(conn, tmp_path, monkeypatch):
    """No SOL path: the lane's USDC leg is prefetched for the full clip and cannot carry the trial
    clip, so it refuses before reserving; the watcher's one ordinary attempt takes it."""
    from sniper.services import memecoin_trial as T
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    m.start_trial(conn, source_user_id="w1", chain="solana", clip_usd=20.0, rule=T.RULE,
                  actor="test", now_iso="2026-09-26T20:00:00+00:00")
    result = ln.handle(_buy())
    assert result["reason"].startswith(T.TRIAL_UNMARKED) and not sent
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_a_refused_signal_is_a_decision_line_with_no_write(conn):
    """A frame the decoder refuses (two own buy events) reaches the lane as a rejection, so the
    lane's own log line names it (Gamma's triage counts these lines); nothing is read or written."""
    refused = _buy(refusal="multi_own_buy:pump_amm+pump_curve", handle="notdecu")
    result = _lane(conn, _snap(time.monotonic())).handle(refused)
    assert result == {"status": "rejected", "reason": "multi_own_buy:pump_amm+pump_curve"}
    assert _attempt_rows(conn) == 0
    line = lane.result_log_line(refused, result)
    assert "slot lane notdecu %s S=450000000 -> rejected multi_own_buy:pump_amm+pump_curve" % CURVE_EVENT["mint"][:8] in line


def test_a_filled_slot_buy_carries_the_roster_handle_not_the_executor_placeholder(
        conn, tmp_path, monkeypatch):
    """All 9 slot-lane positions to 2026-09-27 read source_handle '(real)': the executor knows only
    the mint, and the lane never stamped who it copied (the ordinary hook does)."""
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)

    def slot_execute_fn(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None,
                        boundary_fn=None, entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        pid = m.open_position(conn_, source_user_id="w1", source_handle="(real)", mint=CURVE_EVENT["mint"],
                              ticker="", entry_price=1e-5, entry_notional=50.0,
                              entry_fill_source="chain", entry_lag_s=0.5, their_size_usd=300.0,
                              opened_at=now_iso, strategy=pending["strategy"],
                              execution_mode="real", network_id="solana")
        return {"status": "filled", "position_id": pid}

    ln = lane.SlotLane(conn=conn, executor={"slot_execute_fn": slot_execute_fn},
                       build_fn=lambda buy, clip, snap: ("MESSAGE", {"blockhash": "Hash1"}),
                       snapshot_fn=lambda: _snap(time.monotonic()),
                       armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                       now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key="sol-test",
                       mode="live")
    result = ln.handle(_buy(handle="notdecu"))
    assert result["status"] == "filled", result
    row = conn.execute("SELECT source_handle FROM memecoin_positions WHERE id=?",
                       (result["position_id"],)).fetchone()
    assert row[0] == "notdecu"


def _scaled_lane(conn, tmp_path, monkeypatch, *, leg_scale, coin_scale):
    """A live lane whose prefetched leg is sized for `leg_scale` and whose buy classifies at
    `coin_scale`. Returns (lane, sent, built_clips)."""
    import dataclasses
    from sniper.services import memecoin_market_state as ms
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    real = ms.classify_funded_entry
    monkeypatch.setattr(ms, "classify_funded_entry",
                        lambda *a, **k: dataclasses.replace(real(*a, **k), risk_scale=coin_scale))
    built = []

    def build_fn(buy, clip, snap):
        built.append(clip)
        return "MESSAGE", {}
    ln.build_fn = build_fn
    ln.leg_scale_fn = lambda: leg_scale
    return ln, sent, built


def test_a_scaled_clip_builds_when_the_leg_was_prefetched_for_that_scale(conn, tmp_path, monkeypatch):
    """Dougherty 65991 (regime unknown, 0.75) was handed off at S+7 only because the lane's leg was
    always the full clip. With the leg prefetched for the market's scale, the scaled buy reserves the
    scaled clip and builds it, exactly as the ordinary path would size it."""
    ln, sent, built = _scaled_lane(conn, tmp_path, monkeypatch, leg_scale=0.75, coin_scale=0.75)
    result = ln.handle(_buy())
    assert result.get("reason") != lane.SCALED_CLIP_REASON, result
    assert sent and len(built) == 1
    reserved = conn.execute("SELECT reserved_notional_usd FROM memecoin_pending_entries").fetchone()[0]
    assert built[0] == pytest.approx(reserved)


@pytest.mark.parametrize("leg_scale, coin_scale", [(1.0, 0.75), (0.75, 1.0)])
def test_a_leg_sized_for_another_scale_refuses_before_any_reservation(
        conn, tmp_path, monkeypatch, leg_scale, coin_scale):
    """A per-coin hot_narrow in a normal market (full leg, scaled coin) or a regime flip before the
    leg re-quotes (scaled leg, full coin): the lane refuses with the reason the on-chain lane treats
    as provably unsent, so the buy gets its one ordinary attempt."""
    ln, sent, built = _scaled_lane(conn, tmp_path, monkeypatch, leg_scale=leg_scale,
                                   coin_scale=coin_scale)
    result = ln.handle(_buy())
    assert result["reason"] == lane.SCALED_CLIP_REASON and not sent and not built
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_the_prefetched_leg_is_sized_for_the_market_scale_and_the_knob_restores_full(
        conn, monkeypatch):
    """No feed observations = regime unknown = the configured scale-down; the leg is prefetched
    for the clip reserve_entry will approve at that scale. MEMECOIN_SLOT_LANE_SCALED_LEG=0 is the
    rollback: the full clip at scale 1.0, which refuses every scaled buy as before."""
    from sniper.services.memecoin_risk import scaled_funded_clip
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.75)
    snap, now = _snap(time.monotonic()), "2026-09-26T20:30:00+00:00"
    full = lane.expected_clip_usd(conn, snap, now[:10])
    assert full and full > lane.cfg.MEMECOIN_MIN_TRADE_FLOOR_USD / 0.75
    monkeypatch.setattr(lane.cfg, "MEMECOIN_SLOT_LANE_SCALED_LEG", True, raising=False)
    assert lane.leg_plan(conn, snap, now) == (scaled_funded_clip(
        base_clip_usd=full, risk_scale=0.75, floor_usd=lane.cfg.MEMECOIN_MIN_TRADE_FLOOR_USD), 0.75)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_SLOT_LANE_SCALED_LEG", False)
    assert lane.leg_plan(conn, snap, now) == (full, 1.0)


# ---- operator via Alpha 2026-10-02 (PACKZ: DegenCapitalLLC's $91 add-on after $1.0k refused "under
# absolute floor $197"): the slot lane's floors judge the trader's NET, as the hook's do, reading the
# prior net ONLY when a floor refuses the single buy ----

def _floors(monkeypatch, *, abs_min=0.0, abs_max=0.0, ratio=0.0, window=1800.0):
    _open_floors(monkeypatch)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", abs_min)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", abs_max, raising=False)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", ratio)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", window, raising=False)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", 0.0, raising=False)


def _admit(buy, prior_net_fn):
    return lane.admission_refusal(buy, armed_ids={"w1"}, sol_usd=150.0, kill_tripped=False,
                                  prior_net_fn=prior_net_fn)


def test_the_slot_lane_floor_judges_the_net_when_an_earlier_buy_exists(monkeypatch):
    _floors(monkeypatch, abs_min=500.0)                               # the buy is ~$300
    reason, _f = _admit(_buy(), None)
    assert reason is not None and "under absolute floor $500" in reason      # today, single buy
    reason, facts = _admit(_buy(), lambda buy, usd: 400.0)
    assert reason is None, reason                                     # ~$300 + $400 earlier
    assert facts["source_buy_floor"]["judged_usd"] == pytest.approx(facts["their_usd"] + 400.0)
    assert facts["source_buy_floor"]["prior_net_usd"] == 400.0


def test_the_ratio_floor_also_judges_the_net(monkeypatch):
    _floors(monkeypatch, ratio=0.1)                                   # $4,500 mcap: floor $450
    assert _admit(_buy(), lambda buy, usd: 400.0)[0] is None
    assert "under mcap floor" in _admit(_buy(), lambda buy, usd: 0.0)[0]


def test_wallet_ratio_override_reaches_slot_lane_without_a_prior_read(monkeypatch):
    _floors(monkeypatch, ratio=.1, abs_min=200)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET', {'w1': 0.0}, raising=False)
    reason, facts = _admit(_buy(), lambda *_: pytest.fail('ratio exemption must not read prior net'))
    assert reason is None, reason
    assert facts['source_buy_floor']['floor_usd'] is None
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_REAL_MIN_SOURCE_BUY_USD', 500.0)
    assert 'under absolute floor $500' in _admit(_buy(), None)[0]


@pytest.mark.parametrize('ceiling,refused', [(2_000_000., False), (4_000., True)])
def test_wallet_ratio_zero_never_bypasses_its_mcap_ceiling(monkeypatch, ceiling, refused):
    _floors(monkeypatch, ratio=.1)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET', {'w1': 0.0}, raising=False)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET', {'solana:w1': ceiling})
    reason, _ = _admit(_buy(), None)
    assert ('over ceiling' in (reason or '')) == refused
    assert reason is None or refused, reason


@pytest.mark.parametrize('mcap,refused', [(1_999_999, False), (2_000_000, False), (2_000_001, True)])
def test_wallet_two_million_cap_boundary_with_ratio_waived(monkeypatch, mcap, refused):
    _floors(monkeypatch, ratio=.008, abs_min=200)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET', {'w1': 0.0})
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET', {'solana:w1': 2_000_000.})
    buy = _buy(virtual_sol_reserves=int(mcap / 150 * 10**9))
    reason, _ = _admit(buy, None)
    assert ('over ceiling' in (reason or '')) == refused
    assert reason is None or refused, reason


def test_a_buy_that_clears_on_its_single_size_reads_nothing(monkeypatch):
    _floors(monkeypatch, abs_min=100.0)
    reason, facts = _admit(_buy(), lambda buy, usd: pytest.fail("prior read on a passing buy"))
    assert reason is None and "prior_net_usd" not in facts["source_buy_floor"]


@pytest.mark.parametrize("prior", [None, 0.0, -400.0, "raise"])
def test_no_positive_prior_or_a_read_error_judges_the_single_buy(monkeypatch, prior):
    _floors(monkeypatch, abs_min=500.0)

    def read(buy, usd):
        if prior == "raise":
            raise RuntimeError("ledger locked")
        return prior
    reason, _f = _admit(_buy(), read)
    assert reason is not None and "under absolute floor $500" in reason


def test_the_ceiling_stays_on_the_single_buy_and_reads_nothing(monkeypatch):
    _floors(monkeypatch, abs_max=200.0)
    reason, _f = _admit(_buy(), lambda buy, usd: pytest.fail("prior read for a ceiling refusal"))
    assert reason is not None and "over absolute ceiling $200" in reason


def test_the_lane_reads_the_prior_net_on_its_own_connection_keyed_on_its_origin(conn, monkeypatch):
    _floors(monkeypatch, abs_min=500.0)
    m.insert_event(conn, {"fomo_event_id": "e0", "source_user_id": "w1", "source_handle": "w",
                          "mint": CURVE_EVENT["mint"], "ticker": "P", "side": "buy", "their_price": 1.0,
                          "their_market_cap": 4500.0, "their_size_usd": 400.0, "is_first_buy": 0,
                          "created_at": "2026-09-26T20:20:00Z", "received_at": "2026-09-26T20:20:00Z",
                          "lag_s": 0.1, "network_id": "solana"})
    assert lane.slot_prior_net(conn, _buy(), 300.0) == 400.0
    seen = {}
    real = lane.admission_refusal

    def spy(buy, **kw):
        seen["prior"] = kw["prior_net_fn"](buy, 300.0)
        return real(buy, **kw)
    monkeypatch.setattr(lane, "admission_refusal", spy)
    _lane(conn, _snap(time.monotonic()), armed=()).handle(_buy())      # unarmed: refused anyway
    assert seen["prior"] == 400.0
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 0.0, raising=False)
    assert lane.slot_prior_net(conn, _buy(), 300.0) is None                # window off: single buy


def test_a_worker_thread_reuses_one_read_only_handle(monkeypatch, tmp_path):
    """Gamma LOW on 92164bee3: one handle per thread, opened once, never one per read."""
    import threading as _t
    from sniper.persistence import db_core
    path = tmp_path / "ledger.db"
    sqlite3.connect(path).close()
    opened = []
    monkeypatch.setattr(db_core, "get_readonly_connection",
                        lambda db_path=None: opened.append(1) or sqlite3.connect(path))
    monkeypatch.setattr(lane, "_THREAD_RO", _t.local())
    got = []

    def worker():
        got.extend([lane.thread_readonly_conn(), lane.thread_readonly_conn()])
    th = _t.Thread(target=worker)
    th.start()
    th.join()
    assert len(opened) == 1 and got[0] is got[1]
    lane.thread_readonly_conn()                       # another thread: its own handle
    assert len(opened) == 2


def test_a_closed_handle_is_reopened_once(monkeypatch, tmp_path):
    import threading as _t
    from sniper.persistence import db_core
    path = tmp_path / "ledger.db"
    sqlite3.connect(path).close()
    opened = []
    monkeypatch.setattr(db_core, "get_readonly_connection",
                        lambda db_path=None: opened.append(1) or sqlite3.connect(path))
    monkeypatch.setattr(lane, "_THREAD_RO", _t.local())
    first = lane.thread_readonly_conn()
    first.close()                                      # dead handle
    second = lane.thread_readonly_conn()
    assert second is not first and len(opened) == 2
    assert second.execute("SELECT 1").fetchone()[0] == 1
    assert lane.thread_readonly_conn() is second and len(opened) == 2


def test_a_pumpswap_buy_reads_the_prior_net_of_its_pools_coin(monkeypatch):
    """Gamma 2026-10-07 (jpegkite DVypDG8A e310b20a, filled S+3 by the on-chain lane, -$55.29): a
    PumpSwap SlotBuy names only its pool (mint None), so the prior-net read got mint None, which reads
    0.0, and the slot lane judged the single $99 buy while the on-chain lane judged $1,598. The read
    now takes the pool's base mint, which admission already knows."""
    from sniper.execution import pumpswap_buy as ps
    from test_slot_lane_handoff import FX, MINT_54, _Clock, _PoolRpc
    from test_slot_lane_handoff import _buy as handoff_buy
    _floors(monkeypatch, abs_min=600.0)                              # Tekkerrss's buy is $489
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 0.0)
    pools = ps.PumpSwapPoolCache(rpc=_PoolRpc())
    buy = handoff_buy(FX["tekkerrss"], _Clock())
    assert buy.venue == "pump_amm" and buy.mint is None
    assert pools.learn(buy.pool)
    seen = []

    def prior(b, usd):
        seen.append(b.mint)
        return 400.0 if b.mint == MINT_54 else 0.0
    reason, facts = lane.admission_refusal(
        buy, armed_ids={FX["tekkerrss"]["source_user_id"]}, sol_usd=120.0, kill_tripped=False,
        amm_pools=pools, prior_net_fn=prior)
    assert seen == [MINT_54]
    assert reason is None, reason
    assert facts["source_buy_floor"]["prior_net_usd"] == 400.0


@pytest.mark.parametrize('bad', [object(), {'recv_monotonic': 10 ** 1000},
                               {'nested': {'mutates': True}}, {'recv_monotonic': float('nan')}])
def test_malformed_receipt_metadata_cannot_change_funded_dispatch(conn, tmp_path, monkeypatch, bad):
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    result = ln.handle(_buy(receipt_evidence=bad))
    assert result['status'] == 'filled' and len(sent) == 1
    trace = json.loads(conn.execute('SELECT trace_json FROM memecoin_entry_attempts').fetchone()[0])
    assert 'recv_monotonic' not in trace['source_evidence'] and 'nested' not in trace['source_evidence']


def test_slot_span_journal_retains_required_writes_and_cold_queue_timing(conn, tmp_path, monkeypatch):
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    now = time.monotonic()
    buy = _buy(recv_monotonic=now - .02, head_at_recv=449_999_999,
               head_at_recv_mono=now - .03, receipt_evidence={
                   'producer_run_id': 'r', 'boot_id': 'b', 'clock_domain': 'monotonic',
                   'recv_monotonic': now - .02, 'connection_generation': 7})
    result = ln.handle(buy, enqueued_mono=now - .01)
    assert result['status'] == 'filled' and sent == [1]
    row = conn.execute('SELECT trace_json FROM memecoin_entry_attempts').fetchone()
    trace = json.loads(row[0])
    spans = result['stage_spans_ms']
    for field in ('attempt_shell_insert_ms', 'receipt_to_enqueue_ms', 'queue_ms', 'wallet_ownership_ms',
                  'classification_reads_ms', 'reservation_ms', 'unsigned_build_ms'):
        assert spans[field] is not None and spans[field] >= 0, field
    assert spans['first_transport_write_ms'] is None
    evidence = lane._receipt_facts(buy, result)
    assert evidence['stratum'] == 'native_local_proof' and evidence['source_slot'] == buy.slot
    assert evidence['stage_reservation_ms'] == spans['reservation_ms']
    assert trace['source_evidence']['source_wallet'] == buy.owner
    assert trace['source_evidence']['source_sig'] == buy.signature
    # The authoritative reservation contract is still durably stored before the fake executor.
    pending = conn.execute('SELECT funded_contract_json FROM memecoin_pending_entries').fetchone()
    assert pending[0]
