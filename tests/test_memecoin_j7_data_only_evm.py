"""J7 as data only on EVM (spec docs/superpowers/specs/2026-09-30-memecoin-j7-data-only-evm-design.md).

Operator 2026-09-30: J7 never touches a funded decision; a J7-triggered watcher reconnect is
allowed. On a chain in MEMECOIN_J7_DATA_ONLY_EVM_CHAINS every J7 follow buy shadows (no coverage or
health fall-through to live), a J7 sell never reaches a mirror exit, and the D2 cross-check runs.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import axiom_address_verify as axv
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

from test_memecoin_j7_shadow_evm import UID, _beat, _j7, armed  # noqa: F401  (armed is a fixture)


@pytest.fixture
def data_only(armed, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "4663", raising=False)
    return armed


# ---------------------------------------------------------------- buys

def test_an_empty_knob_is_todays_tagger(armed, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "", raising=False)
    assert R.j7_shadow_event(None, _j7()) is not None          # covered, healthy: shadow
    assert R.j7_shadow_event(None, _j7("uid-other")) is None   # uncovered: live, as today


def test_a_listed_chain_shadows_an_uncovered_wallet(data_only):
    tagged = R.j7_shadow_event(None, _j7("uid-other"))
    assert tagged is not None and tagged.delivery_lane == "j7_shadow"


def test_a_listed_chain_shadows_without_reading_coverage_or_health(data_only, monkeypatch):
    """Unknown or bad health must never make J7 live on a listed chain, and the reads it skips
    are the work the change removes from the decision path."""
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_EVM_CHAINS", "", raising=False)
    monkeypatch.setattr(axv, "evm_watched_owners", lambda *a: pytest.fail("read coverage"))
    monkeypatch.setattr(R, "evm_watcher_healthy", lambda *a, **k: pytest.fail("read health"))
    assert R.j7_shadow_event(None, _j7()) is not None


def test_a_stale_watcher_still_shadows_on_a_listed_chain(data_only):
    _owners, beat_path = data_only
    from datetime import datetime, timedelta, timezone
    beat_path.write_text(json.dumps(_beat(ts=datetime.now(timezone.utc) - timedelta(minutes=10))))
    R._EVM_WATCHER_HEALTH.clear()
    assert R.j7_shadow_event(None, _j7()) is not None


@pytest.mark.parametrize("why, ev", [
    ("an unlisted chain keeps today's rule (uncovered -> live)", _j7("uid-other", network="56")),
    ("a sell is not the tagger's", _j7(side="sell")),
    ("not J7", _j7(source="onchain_watch")),
    ("solana is never an EVM data-only chain", _j7("uid-other", network="solana")),
])
def test_everything_else_is_unchanged(data_only, why, ev):
    assert R.j7_shadow_event(None, ev) is None, why


# ---------------------------------------------------------------- the sell guard

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _real_position(c, network_id):
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint="0xabc", ticker="T", entry_price=1.0,
        entry_notional=10.0, entry_fill_source="chain", entry_lag_s=0.1, their_size_usd=0.0,
        opened_at="2026-09-30T00:00:00+00:00", strategy="alpha_b", execution_mode="real",
        network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="2026-09-30T00:00:00+00:00")
    return pid


def _j7_sell(network_id="4663"):
    # J7 sells often carry no chain; the guard must read the POSITION's chain.
    return SimpleNamespace(source_user_id="u1", mint="0xabc", network_id=network_id, side="sell",
                           signal_source="follow_activity", delivery_lane="j7_wallets",
                           their_price=1.2, fomo_event_id="ev-sell-1")


@pytest.fixture
def mirror(monkeypatch):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_POLICY", "alpha_b")
    monkeypatch.setattr(R, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "check_wallet_demotion", lambda *a, **k: None)
    calls = []
    contexts = {n: {"exit_fn": lambda c, pos, reason, now: calls.append((pos["id"], reason))
                    or {"status": "closed"}} for n in ("4663", "56")}
    return calls, contexts


@pytest.mark.parametrize("sell_chain", ["4663", None])
def test_a_j7_sell_never_exits_on_a_listed_chain(mirror, monkeypatch, sell_chain):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "4663", raising=False)
    calls, contexts = mirror
    c = _conn()
    pid = _real_position(c, "4663")
    res = R.real_mirror_on_sell(c, _j7_sell(sell_chain), contexts,
                                now_fn=lambda: "2026-09-30T01:00:00+00:00")
    assert res == {"status": "j7_data_only", "position_id": pid} and calls == []


def test_an_unlisted_chain_keeps_todays_mirror(mirror, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    monkeypatch.setattr(cfg, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "4663", raising=False)
    calls, contexts = mirror
    c = _conn()
    pid = _real_position(c, "56")
    res = R.real_mirror_on_sell(c, _j7_sell("56"), contexts, now_fn=lambda: "2026-09-30T01:00:00+00:00")
    assert calls == [(pid, "mirror")] and res == {"status": "closed"}


def test_a_non_j7_sell_on_a_listed_chain_keeps_todays_mirror(mirror, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    """Only J7 is data only. A sell from our own lanes (the watcher, later) still mirrors."""
    monkeypatch.setattr(cfg, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "4663", raising=False)
    calls, contexts = mirror
    c = _conn()
    pid = _real_position(c, "4663")
    ev = _j7_sell()
    ev.signal_source, ev.delivery_lane = "onchain_watch", "onchain"
    R.real_mirror_on_sell(c, ev, contexts, now_fn=lambda: "2026-09-30T01:00:00+00:00")
    assert calls == [(pid, "mirror")]


def test_an_empty_knob_keeps_todays_mirror(mirror, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)  # legacy feed mode
    monkeypatch.setattr(cfg, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS", "", raising=False)
    calls, contexts = mirror
    c = _conn()
    pid = _real_position(c, "4663")
    R.real_mirror_on_sell(c, _j7_sell(), contexts, now_fn=lambda: "2026-09-30T01:00:00+00:00")
    assert calls == [(pid, "mirror")]


# ---------------------------------------------------------------- the reconnect

@pytest.mark.parametrize("xcheck, data_only, want", [
    ("", "4663", {"4663"}),
    ("56", "4663,8453", {"56", "4663", "8453"}),
    ("", "solana,4663", {"4663"}),
    ("4663", "", {"4663"}),
])
def test_every_data_only_chain_runs_the_j7_cross_check(xcheck, data_only, want):
    env = {"MEMECOIN_ONCHAIN_EVM_J7_XCHECK_CHAINS": xcheck, "MEMECOIN_J7_DATA_ONLY_EVM_CHAINS": data_only}
    assert cfg.memecoin_onchain_evm_j7_xcheck_chains(env) == frozenset(want)


def test_the_shipped_default_is_off():
    assert cfg.MEMECOIN_J7_DATA_ONLY_EVM_CHAINS == ""


# ---------------------------------------------------------------- the forward gate

def _gate_events(n, *, net="4663", uid="u1", trader="0xt1", start=1_790_000_000):
    from datetime import datetime, timezone
    return [{"fomo_event_id": "swap:b:0x%04x:%s:0xm" % (i, trader), "source_user_id": uid,
             "network_id": net, "delivery_lane": "j7_wallets",
             "received_at": datetime.fromtimestamp(start + 100 * i, timezone.utc).isoformat()}
            for i in range(n)]


def _gate(events, detected, watched=lambda t: frozenset({"0xt1"})):
    from scripts.research import j7_watcher_parity as P
    return P.gate_view(events, detected, watched, "4663")


def test_the_gate_passes_at_50_and_98_percent():
    ev = _gate_events(50)
    det = {("4663", "0x%04x" % i): 1_790_000_000 + 100 * i - 0.3 for i in range(49)}   # 49 of 50
    g = _gate(ev, det)
    assert (g["n"], g["hits"], g["pass"]) == (50, 49, True)


def test_the_gate_fails_under_50_or_under_98_percent():
    det = {("4663", "0x%04x" % i): 1_790_000_000 + 100 * i for i in range(60)}
    assert _gate(_gate_events(49), det)["pass"] is False                       # n too small
    ev = _gate_events(100)
    det = {("4663", "0x%04x" % i): 1_790_000_000 + 100 * i for i in range(97)}
    assert _gate(ev, det)["pass"] is False                                     # 97%


def test_a_recovery_after_60s_is_a_miss_and_inside_60s_is_late():
    ev = _gate_events(2)
    det = {("4663", "0x0000"): 1_790_000_000 + 61.0, ("4663", "0x0001"): 1_790_000_100 + 30.0}
    g = _gate(ev, det)
    assert (g["hits"], g["late"]) == (1, 1)


def test_only_buys_on_an_address_acked_at_that_moment_count():
    """Amendment 2026-09-30 (POST HOC): 'watched NOW' over a past window counted buys from before
    the watcher subscribed the address (4663 read 15.6%). The acked set at the buy's instant
    decides, and a relay address outside it is not in the population."""
    ev = _gate_events(3) + _gate_events(2, trader="0xrelay", start=1_790_001_000)
    g = _gate(ev, {})
    assert (g["n"], g["hits"]) == (3, 0)


def test_a_restart_window_counts_and_is_a_miss_unless_detected():
    """Gamma MEDIUM 2: an unknown instant (a process start before its first acked line) is where
    misses live. It counts when the address is in the nearest known acked set; undetected = miss."""
    from scripts.research import j7_watcher_parity as P
    history = [(1_790_000_050.0, frozenset({"0xt1"})), (1_790_000_150.0, None),
               (1_790_000_250.0, frozenset({"0xt1"}))]
    ev = _gate_events(3)                              # t = +0 (no state yet), +100, +200 (restart)
    det = {("4663", "0x0001"): 1_790_000_100.5}
    g = P.gate_view(ev, det, P.watched_at_fn(history), "4663", around=P.watched_around_fn(history))
    assert (g["n"], g["hits"], g["unknown_watch_state"]) == (3, 1, 2)
    assert g["pass"] is False


def test_a_restart_window_counts_an_address_the_dead_process_was_subscribed_to():
    """In the window the old process held the previous set; the new one may not re-ack it yet.
    Counting the previous set's addresses biases toward FAIL, never PASS."""
    from scripts.research import j7_watcher_parity as P
    history = [(1_790_000_050.0, frozenset({"0xt1"})), (1_790_000_150.0, None),
               (1_790_000_250.0, frozenset({"0xother"}))]
    g = P.gate_view(_gate_events(3)[2:], {}, P.watched_at_fn(history), "4663",
                    around=P.watched_around_fn(history))
    assert (g["n"], g["hits"], g["unknown_watch_state"]) == (1, 0, 1)


def test_the_gate_is_forward_only_whatever_the_caller_asks():
    """Gamma MEDIUM 1: the population amendment came after the first live read (POST HOC), so no
    receipt before its commit may count toward n."""
    from scripts.research import j7_watcher_parity as P
    assert P.GATE_FROM == "2026-09-30T08:20:59+00:00"
    assert P.gate_start("2026-09-01T00:00:00+00:00") == P.ts(P.GATE_FROM)
    assert P.gate_start("2026-10-02T00:00:00+00:00") == P.ts("2026-10-02T00:00:00+00:00")


def test_the_acked_history_is_read_from_the_watcher_log(tmp_path):
    from scripts.research import j7_watcher_parity as P
    log = tmp_path / "w.log"
    log.write_text(
        "[onchain-evm] watching 2 verified address(es)\n"                     # no stamp: skipped
        "2026-09-29T21:06:08.5+00:00 [onchain-evm] watching 2 verified address(es)\n"
        "2026-09-29T21:06:08.6+00:00 [onchain-evm] acked transfer set (2): 0xAA,0xbb\n"
        "2026-09-29 21:07:00,000Z push lane: socket error; reconnecting in 1s\n"
        "2026-09-29T22:00:00+00:00 [onchain-evm] watching 1 verified address(es)\n"
        "2026-09-29T22:00:00.1+00:00 [onchain-evm] acked transfer set (1): 0xcc\n")
    at = P.watched_at_fn(P.acked_history(log))
    t = P.ts
    assert at(t("2026-09-29T21:06:08.55+00:00")) is None
    assert at(t("2026-09-29T21:30:00+00:00")) == frozenset({"0xaa", "0xbb"})
    assert at(t("2026-09-29T22:00:00.05+00:00")) is None
    assert at(t("2026-09-30T00:00:00+00:00")) == frozenset({"0xcc"})
    assert at(t("2026-09-29T00:00:00+00:00")) is None


def test_a_j7_buy_without_a_transaction_in_its_id_is_counted_apart_and_never_in_the_gate():
    """Amendment 2026-09-30: 'swap:<uuid>' ids carry no tx and no trader, so they are in neither n nor
    hits. Since the gate start they were 6 of 45 J7 buys on 4663, 7 of 20 on 56, 2 of 15 on 8453. The
    PASS rule is unchanged; the count says how many buys a PASS says nothing about."""
    from datetime import datetime, timezone
    from scripts.research import j7_watcher_parity as P
    start = 1_790_000_000
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()       # noqa: E731
    ev = _gate_events(50)
    det = {("4663", "0x%04x" % i): start + 100 * i - 0.3 for i in range(50)}
    blind = [{"fomo_event_id": "swap:7699193d-8440-4ed5-b866-46f2c9bf41b%d" % i, "source_user_id": uid,
              "network_id": net, "delivery_lane": lane, "received_at": iso(t)}
             for i, (uid, net, lane, t) in enumerate([
                 ("u1", "4663", "j7_wallets", start + 5), ("u2", "4663", "j7_wallets", start + 6),
                 ("u9", "4663", "j7_wallets", start + 7), ("u1", "56", "j7_wallets", start + 8),
                 ("u1", "4663", "j7_pump", start + 9), ("u1", "4663", "j7_wallets", start - 50),
                 ("u1", "4663", "j7_wallets", start + 9000)])]
    watched = lambda t: frozenset({"0xt1"})                                      # noqa: E731
    g = P.gate_view(ev + blind, det, watched, "4663", since_t=start, until_t=start + 8000, armed={"u1", "u2"})
    assert (g["n"], g["hits"], g["pass"]) == (50, 50, True), "the PASS rule does not read them"
    assert (g["no_tx_id"], g["no_tx_id_armed"]) == (3, 2), "this chain, this lane, this window"
    plain = P.gate_view(ev + blind, det, watched, "4663")
    assert (plain["no_tx_id"], plain["no_tx_id_armed"]) == (5, None), "no roster given: the armed count is unknown"
