"""4663 fast lane M2 step 3 on its PRODUCTION entry point (Alpha 2026-10-01): build_lane over the
production 4663 context (memecoin_evm_leg.build_real_evm_context), the dispatch the runner thread
calls, and the runner's own funded hook (make_real_hook) with every gate today's 4663 entry passes.

The network is guarded: nothing may leave the host before the sequencer write (Alpha's I/O rule).
Each gate Gamma named on d6e567683 has one test here that fails if the lane sends past it, and a
gate whose data the lane lacks in memory falls back before any funded row (Alpha's order)."""
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sniper.persistence import db_memecoin as db  # noqa: E402
from sniper.services import memecoin_rh_fast_lane as lane  # noqa: E402

MINT = "0x" + "3" * 40
POOL = "0x" + "a1" * 32
KEY = dict(currency0="0x" + "00" * 20, currency1=MINT, fee=0, tick_spacing=200,
           hooks="0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
SQRT = 874745060896749537239184449426278          # ~$20.5k mcap on 1e9 coins at $2,500 ETH


class Net:
    """Every way out of the host, recorded with whether the sequencer write had happened."""
    def __init__(self):
        self.sent, self.calls = False, []


def _stats(**over):
    """Market stats in the shape dexscreener_stats returns for a listed 4663 pair."""
    st = dict(price=2.05e-05, market_cap=20500.0, fdv=20500.0, liquidity=9000.0,
              volume_m5=4000.0, volume_h1=20000.0, volume_24h=40000.0, txns_m5_buys=30,
              txns_m5_sells=10, pair_address=POOL, chain="robinhood", dex_id="uniswap",
              pair_created_at=(time.time() - 3 * 3600) * 1000.0, n_pairs=1)
    st.update(over)
    return st


@pytest.fixture
def entry(monkeypatch, tmp_path):
    return _setup(monkeypatch, tmp_path, "live")


def _setup(monkeypatch, tmp_path, mode, dex_base=None, log=None):
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_context_keys import _production_context
    monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda chain=None: True)
    ctx = _production_context(monkeypatch, tmp_path, "4663")

    def today_executor(*a, **k):
        raise AssertionError("the hook ran today's 4663 executor for a lane signal")
    ctx["execute_fn"] = today_executor
    # Armed, allowlisted, no kill switch, no quiet window (each gate test flips one).
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_QUIET_HOURS_UTC", "", raising=False)
    monkeypatch.setenv("MEMECOIN_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    # Each test its own state root: the detector heartbeat and the lane's own SQLite live there,
    # and xdist workers otherwise share one (a sibling's pid in the beat drops every datagram).
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(runner, "_gate_balance_cache", {})
    net = Net()

    def guard(name):
        def blocked(*a, **k):
            net.calls.append((name, net.sent))
            raise OSError("network blocked in this test: %s" % name)
        return blocked
    monkeypatch.setattr(socket.socket, "connect", guard("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", guard("socket.create_connection"))
    monkeypatch.setattr(urllib.request, "urlopen", guard("urlopen"))
    from sniper.execution import evm_wallet
    monkeypatch.setattr(evm_wallet, "_http", guard("evm_wallet._http"))
    from sniper.execution.fast_http import PooledHttpClient
    monkeypatch.setattr(PooledHttpClient, "request",
                        lambda _self, *a, **k: evm_wallet._http(*a, **k))

    class Sender:
        def __init__(self):
            self.raws = []

        def send(self, raw):
            net.sent = True
            self.raws.append(raw)
            return ("rejected", "insufficient funds for gas * price + value")
    sender = Sender()
    path = str(tmp_path / "ledger.db")
    ledger = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    db.add_wallet(ledger, "uid", "Degen")
    db.set_wallet_real_approved(ledger, "uid", True, chains=["4663"], force=True)
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "https://research.invalid")

    def ro():
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, check_same_thread=False,
                            isolation_level=None)
        c.row_factory = sqlite3.Row
        return c
    built = lane.build_lane({"4663": ctx}, log=log or (lambda *_: None), mode=mode, root=tmp_path,
                            connect=lambda: ledger, sender=sender, ro_connect=ro,
                            dex_base=dex_base)
    for h in (built["inline_paced"].hosts if built.get("inline_paced") is not None else ()):
        h.admission = "admitted"          # the identity pass runs on a background thread start_lane owns; these tests start none and block the network
    beat = Path(built["dispatch"].beat)
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"ts": time.time() + 3600, "pid": os.getpid()}))
    cache = built["cache"]
    cache._verdicts[MINT] = (True, None, time.time(), 0.0)
    cache._facts[MINT] = (18, 10 ** 27)
    cache._stats[MINT] = (_stats(), time.time())
    return built, sender, net, ledger


_SEQ = iter(range(10 ** 6))


def _payload(**over):
    now = time.time()
    n = next(_SEQ)
    p = dict(tx_hash="0x%064x" % (0xc0ffee + n), seq=1000, wallet="0x" + "ef" * 20, mint=MINT, handle="Degen",
             source_user_id="uid", permit_nonce=9 + n,
             arrival_utc=__import__("sniper.util.clock", fromlist=["x"]).real_utc_now_iso(),
             pool=POOL, key=KEY, cap_num=7, cap_den=3 * 10 ** 6, clip_wei=10 ** 16,
             min_out=1, state_kind="tape_post", tape_lag_blocks=0, sent_epoch=now,
             snapshot=dict(native_wei=10 ** 18, weth_wei=10 ** 18, nonce=42,
                           gas_price_wei=40_000_000, read_at=now - 1),
             eth_usd=2500.0, eth_usd_at=now, post_sqrt=SQRT, src_in_wei=25854436920000000,
             src_out=3182688391454788035426447, paid_usd=65.3)
    p.update(over)
    from rh_source_fill_fixture import proof_for, bind_native_prices
    p = bind_native_prices(p)
    p.setdefault("source_fill", proof_for(p))
    return p


def _fresh_after_send():
    """A later signal whose snapshot was read after the lane's own last send (the generation rule
    otherwise falls back: that snapshot may predate our spend)."""
    p = _payload()
    p["snapshot"] = dict(p["snapshot"], read_at=time.time() + 2.0)
    return p


@pytest.mark.parametrize("mutation,why", [
    ("missing", "source_fill_unproved"), ("wallet", "source_fill_unproved"),
    ("net", "source_fill_mismatch"), ("split", "source_post_unbound"),
    ("upper", "source_payment_unproved"),
])
def test_receipt_proof_failures_never_call_a_provider_or_reserve(entry, mutation, why):
    built, sender, net, ledger = entry
    p = _payload()
    if mutation == "missing":
        p.pop("source_fill")
    elif mutation == "wallet":
        p["source_fill"]["wallet"] = "0x" + "dd" * 20
    elif mutation == "net":
        p["source_fill"]["acquired_atomic"] = str(int(p["src_out"]) - 1)
    elif mutation == "upper":
        p["source_fill"]["payment_basis"] = "transaction_value_upper_bound"
        p["source_fill"]["payment_token"] = "0x" + "00" * 20
    else:
        p["source_fill"]["source_post"] = {"reason": "split_route"}
    rec = _go(built, p)
    assert rec["reason"] == why and sender.raws == [] and net.calls == []
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["cap_num", "post_sqrt", "src_in_wei", "post_wei_num"])
def test_receipt_proof_also_binds_native_price_and_cap(entry, field):
    built, sender, net, ledger = entry
    p = _payload()
    from fractions import Fraction
    price = Fraction(1 << 192, p["post_sqrt"]**2)
    p["post_wei_num"], p["post_wei_den"] = price.numerator, price.denominator
    p[field] *= 1000000
    result = _go(built, p)
    assert result["reason"] == "source_price_mismatch"
    assert sender.raws == [] and net.calls == []
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def _go(built, payload):
    return built["dispatch"].on_datagram(payload, os.getpid(), os.getuid(), time.time())


def _attempts(ledger):
    return [dict(r) for r in ledger.execute(
        "SELECT signal_source, terminal_status, terminal_reason FROM memecoin_entry_attempts "
        "ORDER BY rowid")]


def test_nothing_leaves_the_host_before_the_sequencer_write(entry):
    built, sender, net, ledger = entry
    rec = _go(built, _payload())
    assert sender.raws, (rec, _attempts(ledger))                # the lane reached the send
    assert [c for c in net.calls if not c[1]] == [], net.calls  # no network before it
    pending = ledger.execute("SELECT * FROM memecoin_pending_entries").fetchall()
    assert len(pending) == 1                                    # one reservation, the hook's
    assert pending[0]["signal_source"] == "rh_fast_lane" and pending[0]["network_id"] == "4663"
    (attempt,) = _attempts(ledger)
    assert attempt["signal_source"] == "rh_fast_lane" and attempt["terminal_status"] == "aborted"
    assert rec["reason"].startswith("rejected:")


# ---- Gamma's BLOCK on d6e567683: each gate refuses the lane exactly as it refuses today ----

def _kill(monkeypatch, tmp_path, cfg):
    (tmp_path / "kill").write_text("tripped")


def _disable(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)


def _quiet(monkeypatch, tmp_path, cfg):
    hour = time.gmtime().tm_hour
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_QUIET_HOURS_UTC", "%d-%d" % (hour, (hour + 1) % 24),
                        raising=False)


def _ceiling(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {"4663:uid": 10000.0},
                        raising=False)


def _floor(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET", {"4663:uid": 50000.0},
                        raising=False)


def _volume(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 10 ** 9, raising=False)


def _source_floor(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 200.0)


def _source_band(monkeypatch, tmp_path, cfg):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0.008)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND",
                        {"nano": 0.05}, raising=False)


@pytest.mark.parametrize("flip, status, why", [
    (_kill, "disarmed", "kill_switch_tripped"),
    (_disable, "disarmed", "real_disabled"),
    (_quiet, "disarmed", "quiet_hours"),
    (_ceiling, "skipped_mcap", "over ceiling"),
    (_floor, "skipped_mcap", "under floor"),
    (_volume, "rejected", "volume"),
    (_source_floor, "rejected", "source buy"),
    (_source_band, "rejected", "source buy"),
], ids=["kill_switch", "arming_off", "quiet_hours", "wallet_mcap_ceiling", "wallet_mcap_floor",
        "volume_floor", "source_buy_floor", "source_buy_band"])
def test_each_gate_refuses_the_lane_before_its_send(entry, monkeypatch, tmp_path, flip, status, why):
    from sniper.settings import memecoin as cfg
    built, sender, net, ledger = entry
    flip(monkeypatch, tmp_path, cfg)
    rec = _go(built, _payload())
    assert sender.raws == [], rec
    assert rec["reason"].startswith("hook:%s:" % status), rec
    assert why in rec["reason"].lower(), rec
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries WHERE status='reserved'"
                          ).fetchone()[0] == 0


def test_operator_disarms_and_the_lane_stops_on_the_next_signal(entry):
    built, sender, net, ledger = entry
    _go(built, _payload())
    assert len(sender.raws) == 1                                # armed: the lane sent
    db.set_wallet_real_approved(ledger, "uid", False, chains=["4663"], force=True)
    rec = _go(built, _fresh_after_send())
    assert len(sender.raws) == 1 and rec["reason"].startswith("hook:not_approved:"), rec


# ---- a gate input the lane lacks in memory falls back before any funded row ----

@pytest.mark.parametrize("age_s", [None, 3600.0], ids=["absent", "stale"])
def test_missing_or_stale_market_stats_fall_back_before_any_funded_row(entry, age_s):
    built, sender, net, ledger = entry
    if age_s is None:
        built["cache"]._stats.pop(MINT)
    else:
        built["cache"]._stats[MINT] = (_stats(), time.time() - age_s)
    rec = _go(built, _payload())
    assert rec["reason"] == "no_market_stats" and sender.raws == [] and net.calls == []
    assert _attempts(ledger) == []
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


@pytest.mark.parametrize("over, reason", [
    (dict(eth_usd_at=time.time() - 3600), "eth_usd_stale"),                 # Alpha invariant 2
    (dict(state_kind="pre"), "estimated_state"),
    (dict(paid_usd=None), "no_source_trade"),
])
def test_alphas_invariants_fall_back_before_any_side_effect(entry, over, reason):
    built, sender, net, ledger = entry
    rec = _go(built, _payload(**over))
    assert rec["reason"] == reason and sender.raws == [] and net.calls == []
    assert _attempts(ledger) == []


def test_no_fresh_safety_verdict_falls_back(entry):                       # Alpha invariant 4
    built, sender, net, ledger = entry
    built["cache"]._verdicts[MINT] = (True, None, time.time() - 3600, 0.0)
    assert _go(built, _payload())["reason"] == "safety_stale" and sender.raws == []


def test_a_post_peak_label_falls_back_and_frees_the_reservation(entry, monkeypatch):
    from sniper.services import memecoin_market_state as ms
    built, sender, net, ledger = entry
    monkeypatch.setattr(ms, "confirm_post_peak", lambda c, exact_quote_price: type(
        "P", (), {"nominated": True})())
    rec = _go(built, _payload())
    assert rec["reason"] == "post_peak" and sender.raws == []
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries WHERE status='reserved'"
                          ).fetchone()[0] == 0


# ---- Alpha: DexScreener is gate data only; it never feeds min-out, cap or booking ----

def test_the_market_stats_price_never_reaches_the_signed_buy(entry):
    import rlp
    built, sender, net, ledger = entry
    fixed_now = time.time()
    built["live"].host_clock = lambda: fixed_now  # compare prices at the same transaction deadline

    def calldata(raw):
        body = bytes.fromhex(raw[2:])
        return rlp.decode(body[1:] if body[0] < 0xC0 else body)

    built["cache"]._stats[MINT] = (_stats(price=2.05e-05), time.time())
    first = _go(built, _payload())
    built["cache"]._stats[MINT] = (_stats(price=2.4e-05), time.time())   # the display price +17%
    second = _go(built, _fresh_after_send())
    assert len(sender.raws) == 2, (first["reason"], second["reason"])
    # Approved clip and receipt-bound cap do not depend on the display price.
    assert first["clip_wei"] == second["clip_wei"]
    cap = _payload()
    assert first["min_out"] == second["min_out"] == first["clip_wei"] * cap["cap_den"] // cap["cap_num"]
    tx1, tx2 = calldata(sender.raws[0]), calldata(sender.raws[1])
    assert tx1[5] == tx2[5] and tx1[4] == tx2[4]         # the same calldata and value


# ---- Gamma on the gates SHA: the hook never runs its own executor, never a second buy ----

def test_the_hook_reserves_once_and_never_calls_todays_executor(entry):
    built, sender, net, ledger = entry
    rec = _go(built, _payload())                         # the fixture's today executor raises
    assert len(sender.raws) == 1, rec
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 1


def test_a_lane_refusal_never_arms_a_revival_watch(entry, monkeypatch):
    from sniper.services import memecoin_revival_watch as rw
    from sniper.settings import memecoin as cfg
    built, sender, net, ledger = entry
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(cfg, "MEMECOIN_REVIVAL_WATCH_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REVIVAL_WATCH_MAX_MCAP_USD", 0.0, raising=False)
    # The m5 floor refusal the watch exists for (the gate's own reason string).
    monkeypatch.setattr(runner, "volume_alive_decision",
                        lambda *a, **k: (False, 5000.0, rw.FLOOR_REASON))
    monkeypatch.setattr(rw, "WATCHES", {})
    rec = _go(built, _payload())
    assert rec["reason"].startswith("hook:rejected:") and "m5" in rec["reason"], rec
    assert rw.WATCHES == {}


# ---- runner_shadow on the same entry point: every hook gate, no funded write, the staleness cost ----

def _ledger_rows(ledger):
    return {t: ledger.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            for t in ("memecoin_entry_attempts", "memecoin_pending_entries", "memecoin_positions",
                      "memecoin_events")}


def test_runner_shadow_runs_every_hook_gate_and_writes_nothing(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    asked = []                       # the after-decision staleness GET goes through the budget
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "runner_shadow",
                                        dex_base=lambda *a, **k: asked.append(a) or (200, {}, None))
    before = _ledger_rows(ledger)
    assert _go(built, _payload())["reason"] == "built"
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {"4663:uid": 10000.0},
                        raising=False)
    assert _go(built, _payload())["reason"].startswith("hook:skipped_mcap:")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {}, raising=False)
    db.set_wallet_real_approved(ledger, "uid", False, chains=["4663"], force=True)
    after_disarm = _ledger_rows(ledger)
    assert _go(built, _payload())["reason"].startswith("hook:not_approved:")
    assert _ledger_rows(ledger) == after_disarm and before["memecoin_entry_attempts"] == 0
    assert after_disarm["memecoin_entry_attempts"] == after_disarm["memecoin_pending_entries"] == 0
    assert sender.raws == [] and net.calls == []
    built["dispatch"].shadow.join_checks()
    assert len(asked) == built["dex"].counts["calls"] == 3      # one per hook verdict, counted


def test_runner_shadow_records_whether_fresh_stats_change_the_verdict(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_safety import _ds_pair
    fresh = {"volume_h1": 20000.0}

    def base(method, url, headers=None, body=None):
        net_ok.append(url)
        return 200, {"pairs": [_ds_pair(MINT, volume_h1=fresh["volume_h1"])]}, None
    net_ok = []
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "runner_shadow", dex_base=base)
    from sniper.execution import solana_quote as sq
    monkeypatch.setattr(sq, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 5000.0, raising=False)
    shadow = built["dispatch"].shadow
    assert _go(built, _payload())["reason"] == "built"           # the cached 20k hour passes
    shadow.join_checks()                  # the first check reads 20k before the coin goes quiet (Gamma)
    fresh["volume_h1"] = 10.0                                     # the coin went quiet since
    assert _go(built, _payload())["reason"] == "built"
    shadow.join_checks()
    rows = sqlite3.connect(built["db_path"]).execute(
        "SELECT cached_verdict, fresh_verdict, differ FROM rh_fast_lane_stats_checks "
        "ORDER BY id").fetchall()
    assert rows[0] == ("pass", "pass", 0)
    assert rows[1][0] == "pass" and rows[1][1].startswith("hook:rejected:") and rows[1][2] == 1
    assert len(net_ok) == 2 and built["dex"].counts["calls"] == 2


# ---- the hook's on-chain m5 bound finds the lane's source transaction on its origin ----

def test_the_volume_gate_reads_the_lane_events_source_tx_for_its_chain_and_coin_only():
    from sniper.services import memecoin_volume_writer as V
    p = _payload()
    ev = lane.LaneEvent(p, decimals=18, supply=10 ** 27)
    assert V._gate_source_tx("4663", MINT, ev.fomo_event_id) == p["tx_hash"]
    assert V._gate_source_tx("8453", MINT, ev.fomo_event_id) is None            # another chain
    assert V._gate_source_tx("4663", "0x" + "4" * 40, ev.fomo_event_id) is None  # another coin


def test_the_lane_event_has_the_watchers_basis_and_the_tape_mcap():
    p = _payload()
    ev = lane.LaneEvent(p, decimals=18, supply=10 ** 27)
    assert ev.their_size_usd == 65.3 and ev.signal_source == "rh_fast_lane"
    assert ev.their_price == pytest.approx(65.3 / (3182688391454788035426447 / 1e18))
    # post-trade price (wei per atomic) x supply x ETH/USD: ~$20.5k on these fixtures
    post = (1 << 192) / SQRT ** 2
    assert ev.their_market_cap == pytest.approx(10 ** 27 * post / 1e18 * 2500.0)
    ref = ev.evm_chase_reference.post()
    assert ref["pool"] == p["pool"] and ref["venue"] == "v4" and 0.98 <= ref["ratio"] <= 50


def test_a_lane_refusal_never_pages_and_leaves_the_switch_as_it_found_it(entry, monkeypatch, tmp_path):
    from sniper.services import memecoin_real_runner as runner
    built, sender, net, ledger = entry
    pages = []
    monkeypatch.setattr(runner, "_safe_alert", lambda *a, **k: pages.append(a))
    monkeypatch.setattr("sniper.services.memecoin_telegram.notification_lifecycle_active",
                        lambda: False)
    (tmp_path / "kill").write_text("tripped")                 # today's lane pages this one
    rec = _go(built, _payload())
    assert rec["reason"].startswith("hook:disarmed:") and pages == []
    assert getattr(runner._entry_alert_suppress, "on", False) is False


# ---- Alpha: the hook is warmed at lane-thread start, off any signal (first call was 376 ms cold) ----

def test_the_warm_runs_one_shadow_verdict_for_an_armed_wallet_and_writes_nothing(monkeypatch, tmp_path):
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "live")
    before = _ledger_rows(ledger)
    seen = []
    shadow = built["dispatch"].shadow
    real = shadow.hook_factory

    def factory(contexts, stats):
        hook = real(contexts, stats)
        raw = hook.raw
        hook.raw = lambda conn, ev: seen.append((ev.source_user_id, ev.delivery_lane)) or raw(conn, ev)
        return hook
    shadow.hook_factory = factory
    logs = []
    ms, verdict = lane.warm_hook(shadow, log=logs.append)
    assert seen == [("uid", "j7_shadow")]                # an armed wallet: the whole gate stack
    assert verdict == "pass" and ms >= 0 and logs and "warm" in logs[0]
    assert _ledger_rows(ledger) == before and sender.raws == [] and net.calls == []


def test_the_warm_never_raises_into_the_lane(monkeypatch, tmp_path):
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "live")
    shadow = built["dispatch"].shadow

    def broken():
        raise sqlite3.OperationalError("ledger busy")
    shadow.ro_connect = broken
    logs = []
    assert lane.warm_hook(shadow, log=logs.append) == (None, None)
    assert logs and "warm failed" in logs[0]


def test_start_lane_warms_the_hook_before_it_listens(monkeypatch, tmp_path):
    import threading
    order, warmed = [], threading.Event()
    monkeypatch.setattr(lane, "warm_hook", lambda shadow, log: order.append("warm") or warmed.set()
                        or (1.0, "pass"))
    real_bind = lane.bind_or_log
    monkeypatch.setattr(lane, "bind_or_log", lambda *a, **k: order.append("bind") or real_bind(*a, **k))
    built = _setup(monkeypatch, tmp_path, "live")[0]
    monkeypatch.setattr(lane, "build_lane", lambda contexts, **k: built)
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", "live")
    try:
        assert lane.start_lane({}, log=lambda *_: None, root=tmp_path) is not None
        assert warmed.wait(5.0)
        deadline = time.time() + 5.0
        while "bind" not in order and time.time() < deadline:
            time.sleep(0.01)
        assert order[:2] == ["warm", "bind"]
    finally:
        # The lane's four loops used to outlive this test and call the network inside a later
        # test's guard (deploy e2f5ae905 refused); conftest's _no_leaked_threads now fails that.
        assert lane.stop_lane() == []


def _never_started_lane(monkeypatch, tmp_path):
    """start_lane with Thread.start patched out, as test_memecoin_fast_lane_stop does: its threads and
    its shared worker's thread are registered but never started."""
    import threading

    class Live:
        stopped, sender, routes = False, None, frozenset({"v3_shared"})
        shared_connect = staticmethod(lambda: None)
    built = {"live": Live(), "dispatch": None, "cache": None, "safety_fn": None, "paced": None,
             "db_path": str(tmp_path / "l.db"), "address_name": "\0never-started", "url": "https://r",
             "dex": None}
    monkeypatch.setattr(lane, "build_lane", lambda contexts, **kw: built)
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", "live")
    with monkeypatch.context() as patched:
        patched.setattr(threading.Thread, "start", lambda self: None)
        lane.start_lane({}, log=lambda *a: None, root=tmp_path)


def test_stop_lane_drops_threads_that_never_started(monkeypatch, tmp_path):
    """Gamma REFUSE on c7f4626c3: `pytest -n 0 tests/test_memecoin_fast_lane_stop.py
    "tests/test_memecoin_rh_fast_entry.py::test_start_lane_warms_the_hook_before_it_listens"` raised
    "cannot join thread before it is started" in stop_lane and left the real lane running."""
    monkeypatch.setattr(lane, "_LANE_RUNS", [], raising=False)
    _never_started_lane(monkeypatch, tmp_path)
    [(_stop, threads, worker)] = lane._LANE_RUNS
    assert threads and all(t.ident is None for t in threads) and worker.thread.ident is None
    worker.close()                                   # joins only a started thread: never raises
    assert lane.stop_lane() == [] and lane._LANE_RUNS == []


def test_a_thread_that_outlives_stop_lane_stays_stopped(monkeypatch):
    """Gamma LOW: the stop was cleared after a timed-out join, so a survivor resumed its loop. Each
    start_lane owns its stop; stop_lane sets it and never clears it."""
    import threading
    gate, stop = threading.Event(), threading.Event()
    slow = threading.Thread(target=gate.wait, name="rh-fast-slow", daemon=True)
    slow.start()
    monkeypatch.setattr(lane, "_LANE_RUNS", [(stop, [slow], None)], raising=False)
    try:
        assert lane.stop_lane(timeout_s=0.05) == ["rh-fast-slow"]
        assert stop.is_set() and lane._LANE_RUNS == []
    finally:
        gate.set()
        slow.join(5.0)


def test_the_listener_closes_its_socket_when_the_lane_stops(monkeypatch, tmp_path):
    """Gamma LOW: run() returned without closing its socket, so the abstract name freed only when the
    socket was collected; the next start_lane in that process could find it in use."""
    closed = []

    class Sock:
        def recv(self, timeout):
            time.sleep(0.01)
            return None

        def close(self):
            closed.append(True)
    monkeypatch.setattr(lane, "_LANE_RUNS", [], raising=False)
    monkeypatch.setattr(lane, "warm_hook", lambda shadow, log: (None, None))
    monkeypatch.setattr(lane, "bind_or_log", lambda *a, **k: Sock())
    built = _setup(monkeypatch, tmp_path, "runner_shadow")[0]
    monkeypatch.setattr(lane, "build_lane", lambda contexts, **k: built)
    monkeypatch.setenv("MEMECOIN_RH_FAST_LANE", "runner_shadow")
    assert lane.start_lane({}, log=lambda *_: None, root=tmp_path) is not None
    time.sleep(0.05)
    assert lane.stop_lane() == [] and closed == [True]


def test_the_production_lane_holds_the_lock_through_the_receipt_for_three_seconds_at_most(entry):
    # Gamma LOW on 55c3db447: LiveLane's own default is not what production runs; build_lane is.
    built, sender, net, ledger = entry
    assert built["live"].lock_receipt_cap_s == 3.0


# ---- Alpha 2026-10-02: a short native float blinds only the LIVE lane; the shadow still judges ----

SHORT_FLOAT = 10 ** 15                     # 0.001 ETH: under a $50 clip plus gas at $2,500


def _short_float(**over):
    p = _payload(**over)
    p["snapshot"] = dict(p["snapshot"], native_wei=SHORT_FLOAT)
    return p


def test_live_stops_at_float_short_before_the_hook_and_any_funded_row(entry):
    built, sender, net, ledger = entry

    def hook_factory(contexts, stats):
        raise AssertionError("a short float reached the funded hook on the live lane")
    built["live"].hook_factory = hook_factory
    rec = _go(built, _short_float())
    assert rec["reason"] == "float_short" and sender.raws == [] and net.calls == []
    assert _ledger_rows(ledger)["memecoin_entry_attempts"] == 0
    assert _ledger_rows(ledger)["memecoin_pending_entries"] == 0


def test_runner_shadow_records_float_short_and_still_runs_the_hook_and_the_staleness_check(
        monkeypatch, tmp_path):
    asked = []
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "runner_shadow",
                                        dex_base=lambda *a, **k: asked.append(a) or (200, {}, None))
    before = _ledger_rows(ledger)
    seen = []
    shadow = built["dispatch"].shadow
    real = shadow.hook_factory

    def factory(contexts, stats):
        hook = real(contexts, stats)
        raw = hook.raw
        hook.raw = lambda conn, ev: seen.append(ev.mint) or raw(conn, ev)
        return hook
    shadow.hook_factory = factory
    rec = _go(built, _short_float())
    assert rec["reason"] == "float_short+built" and seen == [MINT]
    db.set_wallet_real_approved(ledger, "uid", False, chains=["4663"], force=True)
    assert _go(built, _short_float())["reason"].startswith("float_short+hook:not_approved:")
    shadow.join_checks()
    assert len(asked) == 2                                       # one staleness GET per verdict
    assert _ledger_rows(ledger)["memecoin_entry_attempts"] == before["memecoin_entry_attempts"]
    assert sender.raws == [] and net.calls == []


def test_runner_shadow_names_a_later_fallback_after_float_short(monkeypatch, tmp_path):
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "runner_shadow",
                                        dex_base=lambda *a, **k: (200, {}, None))
    built["cache"]._stats.pop(MINT)
    assert _go(built, _short_float())["reason"] == "float_short+no_market_stats"


# ---- Alpha 2026-10-02: a cache miss is checked inline, only on a dedicated safety host ----

def _miss(built):
    cache = built["cache"]
    cache._verdicts.pop(MINT, None)
    cache._facts.pop(MINT, None)
    cache._stats.pop(MINT, None)


def test_on_the_research_host_a_miss_still_falls_back(entry):
    built, sender, net, ledger = entry
    assert built["live"].inline_safety_fn is None
    _miss(built)
    rec = _go(built, _payload())
    assert rec["reason"] == "no_safety_verdict" and not sender.raws


@pytest.fixture
def dedicated(monkeypatch, tmp_path):
    # Operator 2026-10-02 (via Alpha): the second key does inline checks only; the cache stays put.
    # These tests are the inline path's own: cached-only (2026-10-14, default on) is pinned off.
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", False, raising=False)
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", "https://own-4663.example.test/key")
    return _setup(monkeypatch, tmp_path, "live")


def test_inline_gate_inputs_overlap_with_the_same_deadline(dedicated):
    import threading
    built, sender, net, ledger = dedicated
    _miss(built)
    live = built['live']
    stats_started = threading.Event()
    rec = {}
    market_stats = _stats()

    def facts(mint):
        assert stats_started.wait(.5), 'market read was serialized behind chain facts'
        assert lane._INLINE.deadline == rec['_inline_deadline']
        return 18, 10 ** 27

    def stats(mint):
        assert lane._INLINE.deadline == rec['_inline_deadline']
        stats_started.set()
        return market_stats

    live.inline_facts_fn, live.stats_fn = facts, stats
    assert live._inline_inputs(MINT, rec) is None
    assert built['cache'].facts(MINT) == (18, 10 ** 27)
    assert built['cache'].stats(MINT, now=time.time())[0] == market_stats
    assert built['cache'].reason(MINT, now=time.time()) == 'no_safety_verdict'
    assert sender.raws == []  # Inputs never grant token safety or submit funds.


def test_late_inline_market_read_cannot_publish_after_the_deadline(dedicated):
    import threading
    built, sender, net, ledger = dedicated
    _miss(built)
    live = built['live']
    release = threading.Event()
    live.inline_facts_fn = lambda mint: (18, 10 ** 27)
    live.stats_fn = lambda mint: release.wait(.5) and _stats()
    rec = {'_inline_deadline': time.monotonic() + .03}
    try:
        assert live._inline_inputs(MINT, rec) == 'inline_unsafe:safety_deadline_exceeded'
        # Facts are now warm; that must not bypass the still-running market worker.
        assert live._inline_inputs(MINT, {}) == 'inline_market_busy'
    finally:
        release.set()
        live._inputs_pool.shutdown(wait=True)
    assert built['cache'].stats(MINT, now=time.time()) is None
    assert built['cache'].reason(MINT, now=time.time()) == 'no_safety_verdict'
    assert sender.raws == []


@pytest.mark.parametrize("stale", [False, True], ids=["absent", "stale"])
def test_on_a_dedicated_host_a_miss_is_checked_inline_then_sent_and_cached(dedicated, stale):
    built, sender, net, ledger = dedicated
    live = built["live"]
    assert live.inline_safety_fn is not None and live.inline_deadline_s == 2.0
    _miss(built)
    if stale:
        built["cache"]._verdicts[MINT] = (True, None, time.time() - 3600, 0.0)
    asked = []
    live.inline_safety_fn = lambda m, usd=None: asked.append(("safety", m, usd)) or (True, None)
    live.inline_facts_fn = lambda m: asked.append(("facts", m)) or (18, 10 ** 27)
    live.stats_fn = lambda m: asked.append(("stats", m)) or _stats()
    rec = _go(built, _payload())
    assert sender.raws and rec["reason"].startswith("rejected:"), rec
    assert rec["safety_path"] == "inline" and rec["safety_ms"] >= 0
    assert sorted(a[0] for a in asked[:2]) == ["facts", "stats"]
    assert asked[2] == ("safety", MINT, 50.0)
    assert built["cache"].reason(MINT, now=time.time()) is None
    (path,) = sqlite3.connect(built["db_path"]).execute(
        "SELECT safety_path FROM rh_fast_lane_live").fetchone()
    assert path == "inline"


def test_a_cold_signal_the_hook_refuses_before_safety_keeps_its_input_legs(dedicated, monkeypatch):
    """Cut 4's post-deploy read (2026-10-14): the legs were stamped only in the safety step, so a cold
    signal the hook refused first (mcap, cooldown) recorded none. Absent stays absent for a cached leg."""
    import json
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 1_000.0)          # the hook refuses on mcap
    built, sender, net, ledger = dedicated
    _miss(built)
    live = built["live"]
    checked = []
    live.inline_safety_fn = lambda m, usd=None: checked.append(m) or (True, None)
    live.inline_facts_fn = lambda m: (18, 10 ** 27)
    live.stats_fn = lambda m: _stats()
    rec = _go(built, _payload())
    assert rec["reason"].startswith("hook:skipped_mcap:") and checked == [] and sender.raws == [], rec
    evidence = rec["execution_evidence"]
    assert 0 <= evidence["inline_facts_ms"] <= evidence["inline_inputs_path_ms"]
    assert 0 <= evidence["inline_stats_ms"] <= evidence["inline_inputs_path_ms"]
    (row,) = sqlite3.connect(built["db_path"]).execute(
        "SELECT execution_evidence_json FROM rh_fast_lane_live").fetchone()
    assert json.loads(row)["inline_inputs_path_ms"] == evidence["inline_inputs_path_ms"]
    # Warm facts and stats: neither leg was read, so neither is stamped (never a 0).
    rec = _go(built, _payload())
    assert "inline_facts_ms" not in rec.get("execution_evidence", {})
    assert "inline_stats_ms" not in rec.get("execution_evidence", {})


@pytest.mark.parametrize("safety, reason", [
    (lambda m, usd=None: (False, "honeypot"), "inline_unsafe:honeypot"),
    (lambda m, usd=None: 1 / 0, "inline_safety_error:ZeroDivisionError")])
def test_an_inline_refusal_or_failure_falls_back_and_is_never_cached(dedicated, safety, reason):
    built, sender, net, ledger = dedicated
    _miss(built)
    built["live"].inline_facts_fn = lambda m: (18, 10 ** 27)
    built["live"].stats_fn = lambda m: _stats()
    built["live"].inline_safety_fn = safety
    assert _go(built, _payload())["reason"] == reason and not sender.raws
    assert built["cache"].reason(MINT, now=time.time()) == "no_safety_verdict"


def test_a_float_short_signal_never_spends_an_inline_read(dedicated):
    built, sender, net, ledger = dedicated
    _miss(built)
    built["live"].inline_safety_fn = lambda m, usd=None: pytest.fail("inline read before the float")
    p = _payload()
    p["snapshot"] = dict(p["snapshot"], native_wei=10 ** 12)
    assert _go(built, p)["reason"] == "float_short"


def test_the_inline_verdict_reads_only_the_dedicated_host_inside_its_deadline(dedicated, monkeypatch):
    from sniper.execution import evm_wallet
    built, sender, net, ledger = dedicated
    asked = []

    def record(method, url, headers=None, body=None, timeout=10):
        asked.append((url, timeout))
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x"}, {}
    monkeypatch.setattr(evm_wallet, "_http", record)
    live = built["live"]
    _miss(built)
    built["dispatch"].registry[MINT] = (KEY, POOL)
    rec = {}
    live._inline(MINT, rec)
    assert asked and {u for u, _t in asked} == {"https://own-4663.example.test/key"}
    assert all(t <= 2.0 for _u, t in asked)



def test_an_inline_pass_without_market_stats_still_falls_back_before_any_funded_row(dedicated):
    built, sender, net, ledger = dedicated
    _miss(built)
    live = built["live"]
    live.inline_safety_fn = lambda m, usd=None: (True, None)
    live.inline_facts_fn = lambda m: (18, 10 ** 27)
    live.stats_fn = lambda m: None
    assert _go(built, _payload())["reason"] == "no_market_stats" and not sender.raws
    assert ledger.execute("SELECT count(*) FROM memecoin_entry_attempts").fetchone()[0] == 0


# ---- Gamma MEDIUM on 220471669: never another role's or a funded budget ----

OWN_KEY = "https://second-key.robinhood-mainnet.quiknode.pro/abc/"


@pytest.mark.parametrize("env, reason", [
    ({"MEMECOIN_EVM_RPC_URL_4663": OWN_KEY}, "MEMECOIN_EVM_RPC_URL_4663"),      # the funded custody
    ({"MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663": OWN_KEY + "," + "https://x.test"},
     "MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663"),                                    # another role
    ({"MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663": OWN_KEY.rstrip("/")},
     "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663")])                                # the research key
def test_a_safety_url_another_role_uses_is_refused_by_full_url(env, reason):
    full = dict(env, MEMECOIN_RH_FAST_SAFETY_RPC_URL=OWN_KEY)
    why = lane.safety_host_refusal(OWN_KEY, full)
    assert why and reason in why and "abc" not in why                          # never the key


def test_a_funded_pool_member_and_a_free_endpoint_are_refused(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": ["https://pool.test/k1"]})
    assert "read-pool member" in lane.safety_host_refusal("https://pool.test/k1/", {})
    assert "free-submission" in lane.safety_host_refusal("https://rpc.mainnet.chain.robinhood.com", {})


def test_a_second_quicknode_key_beside_the_research_one_is_accepted():
    env = {"MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663": "https://wandering.robinhood-mainnet.quiknode.pro/k1"}
    assert lane.safety_host_refusal(OWN_KEY, env) is None


def test_the_custody_url_as_safety_host_keeps_the_research_host_and_no_inline(monkeypatch, tmp_path):
    # _production_context sets the funded custody RPC to https://rpc-4663; the operator pastes it
    monkeypatch.setenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", "https://rpc-4663/")
    lines = []
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "live", log=lines.append)
    assert built["live"].inline_safety_fn is None
    assert built["url"] == "https://research.invalid"                          # the research key
    assert any("MEMECOIN_RH_FAST_SAFETY_RPC_URL refused" in line for line in lines)


# ---- Operator 2026-10-02 (via Alpha 08:14Z): the free QuickNode key does inline checks ONLY; the
# safety cache keeps the research host, its gap and its K ----

INLINE = "https://own-4663.example.test/key"


def _recorder(monkeypatch):
    from sniper.execution import evm_wallet
    asked = []

    def record(method, url, headers=None, body=None, timeout=10):
        asked.append(url)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x"}, {}
    monkeypatch.setattr(evm_wallet, "_http", record)
    return asked


def test_inline_reads_only_the_inline_host_and_the_cache_only_the_research_host(dedicated, monkeypatch):
    from sniper.execution import evm_wallet
    built, sender, net, ledger = dedicated
    assert built["url"] == "https://research.invalid" and built["inline_url"] == INLINE
    built["dispatch"].registry[MINT] = (KEY, POOL)
    _miss(built)
    asked = _recorder(monkeypatch)
    built["live"]._inline(MINT, {})                       # the safety verdict
    assert asked and set(asked) == {INLINE}
    del asked[:]
    built["live"].inline_facts_fn(MINT)                   # a new coin's decimals and supply
    assert asked and set(asked) == {INLINE}
    del asked[:]
    built["paced"].base = evm_wallet._http                # the cache's budget bound _http at build
    built["paced"].min_gap_s = 0.0
    built["cache"].run_one(MINT, now=time.time())         # the background verdict
    rpc = {u for u in asked if "binance.com" not in u}    # its ETH/USD price read is no RPC host
    assert rpc == {"https://research.invalid"} and INLINE not in asked


def test_an_inline_429_pauses_inline_only_and_a_research_429_never_pauses_inline(dedicated, monkeypatch):
    from sniper.execution import evm_wallet
    built, sender, net, ledger = dedicated
    built["dispatch"].registry[MINT] = (KEY, POOL)
    monkeypatch.setattr(evm_wallet, "_http", lambda *a, **k: (429, None, {}))
    built["live"]._inline(MINT, {})
    assert built["inline_paced"].paused() and not built["paced"].paused()
    built["inline_paced"].hosts[0].paced._paused_until = 0.0          # the rotation set holds one pacer per host
    built["paced"].pause()                                # the research key answered 429 elsewhere
    asked = _recorder(monkeypatch)
    built["live"]._inline(MINT, {})
    assert asked and set(asked) == {INLINE}


@pytest.mark.parametrize("value, why", [
    ("https://research.invalid/", "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663"),   # the research key
    ("https://rpc-4663/", "MEMECOIN_EVM_RPC_URL_4663"),                        # the funded custody
    ("http://plain.example.test/key", "not an https URL")])
def test_a_refused_inline_url_leaves_inline_off_and_the_cache_where_it_was(monkeypatch, tmp_path, value, why):
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", value)
    lines = []
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "live", log=lines.append)
    assert built["live"].inline_safety_fn is None and built["url"] == "https://research.invalid"
    assert any("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL refused" in ln and why in ln for ln in lines), lines


def test_the_old_safety_knob_alone_no_longer_turns_inline_on(monkeypatch, tmp_path):
    # MEMECOIN_RH_FAST_SAFETY_RPC_URL keeps its meaning (the cache host); inline needs its own knob
    monkeypatch.setenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", "https://cache-4663.example.test/k")
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "live")
    assert built["url"] == "https://cache-4663.example.test/k" and built["live"].inline_safety_fn is None


def test_one_url_on_both_knobs_is_refused_for_both(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_RH_FAST_SAFETY_RPC_URL", INLINE)
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", INLINE)
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "live")
    assert built["url"] == "https://research.invalid" and built["live"].inline_safety_fn is None


# ---- Gamma LOW on b71642fe5: inline spend was invisible (lane_inline_http never counted, and the
# heartbeat wrote only the research pacer) ----

def test_inline_spend_is_counted_on_its_pacer_and_written_to_the_heartbeat(dedicated, monkeypatch):
    import sqlite3
    built, sender, net, ledger = dedicated
    built["dispatch"].registry[MINT] = (KEY, POOL)
    _miss(built)
    asked = _recorder(monkeypatch)
    built["live"]._inline(MINT, {})
    built["live"].inline_facts_fn(MINT)
    inline = built["inline_paced"]
    assert asked and inline.counts["calls"] == len(asked) and inline.counts["http_429"] == 0
    assert built["paced"].counts["calls"] == 0                       # never the research budget
    lane.write_heartbeat(built["db_path"], built["dispatch"], built["cache"], built["paced"], inline)
    row = sqlite3.connect(built["db_path"]).execute(
        "SELECT research_calls, research_429, inline_calls, inline_429 FROM rh_fast_lane_heartbeat").fetchone()
    assert row == (0, 0, len(asked), 0)


def test_a_refused_paused_or_late_inline_read_is_not_counted():
    from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
    paced = lane.PacedHttp(lambda *a, **k: pytest.fail("the pacer's own seam"), min_gap_s=0.0)
    http = lane.lane_inline_http("https://own.test/k", paced, base=lambda *a, **k: (200, {}, {}))
    http("POST", "https://own.test/k", body="{}")
    with pytest.raises(RuntimeError):
        http("POST", "https://elsewhere.test/", body="{}")
    with lane.inline_deadline(time.monotonic() - 1):
        with pytest.raises(EvmSafetyDeadlineExceeded):
            http("POST", "https://own.test/k", body="{}")
    paced.pause()
    with pytest.raises(lane.ResearchPaused):
        http("POST", "https://own.test/k", body="{}")
    assert paced.counts["calls"] == 1 and paced.counts["http_429"] == 1


def test_without_an_inline_host_the_heartbeat_says_unknown_not_zero(entry):
    import sqlite3
    built, sender, net, ledger = entry
    assert built["inline_paced"] is None
    lane.write_heartbeat(built["db_path"], built["dispatch"], built["cache"], built["paced"], None)
    row = sqlite3.connect(built["db_path"]).execute(
        "SELECT inline_calls, inline_429 FROM rh_fast_lane_heartbeat").fetchone()
    assert row == (None, None)


def test_the_heartbeat_adds_the_inline_columns_to_a_lane_db_written_before_them(tmp_path):
    import sqlite3
    path = str(tmp_path / "lane.db")
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE rh_fast_lane_heartbeat (at TEXT NOT NULL, candidates_msgs INTEGER, "
                      "signals INTEGER, cache_size INTEGER, verdicts_ok INTEGER, verdicts_unsafe INTEGER, "
                      "verdict_errors INTEGER, safety_cpu_ms REAL, research_calls INTEGER, "
                      "research_429 INTEGER);")                    # the table before 544f68367
    old.execute("INSERT INTO rh_fast_lane_heartbeat VALUES ('t0',1,2,3,4,5,6,7.0,8,9)")
    old.commit()
    old.close()

    class D:
        counts = {"candidates_msgs": 0, "signals": 0}
    cache = lane.SafetyCache(lambda m, usd=None: (True, None), ttl_s=60.0, usd=50.0)
    research = lane.PacedHttp(lambda *a, **k: (200, {}, {}), min_gap_s=0.0)
    inline = lane.PacedHttp(lambda *a, **k: (200, {}, {}), min_gap_s=0.0)
    inline.counts["calls"] = 3
    lane.write_heartbeat(path, D(), cache, research, inline)
    rows = sqlite3.connect(path).execute(
        "SELECT at, research_calls, inline_calls, inline_429 FROM rh_fast_lane_heartbeat ORDER BY rowid").fetchall()
    assert rows[0] == ("t0", 8, None, None) and rows[1][1:] == (0, 3, 0)


def test_the_lane_loop_writes_the_inline_pacer_beside_the_research_one():
    src = Path(lane.__file__).read_text()
    assert 'write_heartbeat(db_path, dispatch, cache, paced, lane["inline_paced"],\n' in src


# ---- Alpha 2026-10-02 (degen_ak AGNET 4663): a lane attempt's alert said "bought unknown size"
# because the view found no source event for the rhfast: origin; the lane now carries its facts ----

def test_a_lane_refusal_view_names_the_source_size(entry, monkeypatch):
    from sniper.services.memecoin_entry_view import entry_view
    from sniper.settings import memecoin as cfg
    built, sender, net, ledger = entry
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 1000.0, raising=False)
    rec = _go(built, _payload())
    (attempt_id, reason) = ledger.execute(
        "SELECT attempt_id, terminal_reason FROM memecoin_entry_attempts WHERE signal_source='rh_fast_lane'").fetchone()
    assert "under absolute floor $1000" in reason and not sender.raws, rec
    view = entry_view(ledger, attempt_id=attempt_id)
    assert view["source_size_usd"] == 65.3 and view["source_user_id"] == "uid"


def test_the_lane_origin_resolves_only_its_own_transaction():
    import json as _json
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    tx = "0x" + "c0" * 32
    facts = {"their_size_usd": 99.62, "source_user_id": "uid", "network_id": "4663", "mint": MINT,
             "signature": tx}
    for prefix, chain in (("rhfast", "4663"), ("bscfast", "56")):
        origin = "%s:%s:%s:%s" % (prefix, chain, tx, MINT)
        attempt = {"origin_event_id": origin, "network_id": chain, "mint": MINT, "source_user_id": "uid",
                   "trace_json": _json.dumps({"source_evidence": dict(facts, network_id=chain)})}
        got = m.resolve_source_evidence(c, origin, network_id=chain, mint=MINT, source_user_id="uid",
                                        attempt=attempt)
        assert got["their_size_usd"] == 99.62, prefix
        other = dict(attempt, trace_json=_json.dumps({"source_evidence": dict(facts, network_id=chain,
                                                                               signature="0x" + "d1" * 32)}))
        assert m.resolve_source_evidence(c, origin, network_id=chain, mint=MINT, source_user_id="uid",
                                         attempt=other) == {}                 # another tx: not ours
        assert m.resolve_source_evidence(c, "%s:%s:%s" % (prefix, chain, tx), network_id=chain, mint=MINT,
                                         source_user_id="uid", attempt=attempt) == {}   # malformed origin


# ---- Alpha/Gamma 2026-10-02 (degen_ak 09:21:22Z, busy 610 ms, refused on the floor): on a cache
# miss the lane runs the hook's network-free absolute source-buy band BEFORE the inline check ----

def test_the_inline_host_is_never_called_for_a_buy_the_absolute_band_refuses(dedicated, monkeypatch):
    from sniper.settings import memecoin as cfg
    built, sender, net, ledger = dedicated
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 1000.0, raising=False)
    _miss(built)
    built["live"].inline_safety_fn = lambda m, usd=None: pytest.fail("inline read for a band refusal")
    built["live"].inline_facts_fn = lambda m: pytest.fail("inline facts for a band refusal")
    rec = _go(built, _payload())                                     # paid $65.30 < $1000
    assert rec["reason"] == "source_band:source buy $65 under absolute floor $1000", rec
    assert not sender.raws and "safety_ms" not in rec


def test_a_prior_net_that_clears_the_floor_still_goes_inline(dedicated, monkeypatch):
    from sniper.settings import memecoin as cfg
    built, sender, net, ledger = dedicated
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 100.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 1800.0)
    asked = []
    monkeypatch.setattr(lane_m(), "source_prior_net_buy_usd", lambda *a, **k: asked.append(k) or 498.0)
    _miss(built)
    built["live"].inline_facts_fn = lambda m: (18, 10 ** 27)
    built["live"].stats_fn = lambda m: _stats()
    built["live"].inline_safety_fn = lambda m, usd=None: asked.append("inline") or (False, "x")
    rec = _go(built, _payload())                                     # $65.30 + $498 earlier >= $100
    assert rec["reason"] == "inline_unsafe:x" and "inline" in asked
    assert asked[0]["exclude_event_id"].startswith("rhfast:4663:")    # the J7 twin is not "earlier"


def test_an_exempt_wallet_still_goes_inline(dedicated, monkeypatch):
    from sniper.settings import memecoin as cfg
    built, sender, net, ledger = dedicated
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 1000.0, raising=False)
    monkeypatch.setattr(lane_m(), "entry_gate_exemptions", lambda *a, **k: frozenset({"source_buy_floor"}))
    _miss(built)
    built["live"].inline_facts_fn = lambda m: (18, 10 ** 27)
    built["live"].stats_fn = lambda m: _stats()
    built["live"].inline_safety_fn = lambda m, usd=None: (False, "x")
    assert _go(built, _payload())["reason"] == "inline_unsafe:x"     # the hook would waive the floor


def lane_m():
    from sniper.persistence import db_memecoin as m
    return m


def test_the_events_table_records_cache_or_inline_per_signal(dedicated, tmp_path):
    import sqlite3 as _sq
    built, sender, net, ledger = dedicated
    path = str(tmp_path / "old_events.db")
    old = _sq.connect(path)
    old.executescript("CREATE TABLE rh_fast_lane_events (id INTEGER PRIMARY KEY, tx_hash TEXT, mode TEXT, "
                      "mint TEXT, pool TEXT, reason TEXT, sent_epoch REAL, recv_epoch REAL, handoff_ms REAL, "
                      "busy_start_epoch REAL, busy_end_epoch REAL, busy_ms REAL, cpu_ms REAL, lock_busy INTEGER, "
                      "clip_wei TEXT, min_out TEXT, tx_bytes INTEGER, written_at TEXT);")
    old.close()
    base = dict(tx_hash=None, mode="live", mint=MINT, pool=POOL, reason="x", sent_epoch=1.0, recv_epoch=1.0,
                handoff_ms=0.0, busy_start_epoch=1.0, busy_end_epoch=1.0, busy_ms=1.0, cpu_ms=1.0, lock_busy=0,
                clip_wei=1, min_out=None, tx_bytes=None)
    lane.write_record(path, dict(base, safety_path="inline", safety_ms=612.5))
    lane.write_record(path, dict(base, safety_path="cache"))
    lane.write_record(path, base)
    rows = _sq.connect(path).execute("SELECT safety_path, safety_ms FROM rh_fast_lane_events ORDER BY id").fetchall()
    assert rows == [("inline", 612.5), ("cache", None), (None, None)]


# ---- operator 2026-10-02 (via Alpha): the research key rotates across numbered spare slots ----

def test_the_lane_cache_rotates_to_a_spare_slot_and_the_heartbeat_names_it(monkeypatch, tmp_path):
    import json as _json
    from sniper.execution import evm_wallet
    spare = "https://spare-4663.example.test/k2"
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663_2", spare)
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", INLINE)
    built, sender, net, ledger = _setup(monkeypatch, tmp_path, "live")
    assert [n for n, _u in built["rotation"].slots] == [1, 2]
    built["dispatch"].registry[MINT] = (KEY, POOL)
    asked = []

    def record(method, url, headers=None, body=None, timeout=10):
        asked.append(url)
        if url == "https://research.invalid":
            return 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32003,
                                                              "message": "daily request limit reached"}}, {}
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x"}, {}
    monkeypatch.setattr(evm_wallet, "_http", record)
    built["paced"].min_gap_s = 0.0
    built["cache"].run_one(MINT, now=time.time())
    rpc = [u for u in asked if "binance.com" not in u]
    assert rpc[0] == "https://research.invalid" and set(rpc[1:]) == {spare}
    assert INLINE not in asked and not built["paced"].paused()
    lane.write_heartbeat(built["db_path"], built["dispatch"], built["cache"], built["paced"],
                         built["inline_paced"], built["rotation"])
    slot, slots = sqlite3.connect(built["db_path"]).execute(
        "SELECT research_slot, research_slots FROM rh_fast_lane_heartbeat").fetchone()
    assert slot == 2 and [s["slot"] for s in _json.loads(slots)] == [1, 2] and "k2" not in slots


@pytest.mark.parametrize("paid", [float("inf"), float("nan"), -5.0])
def test_an_unusable_source_size_never_refuses_before_inline(dedicated, monkeypatch, paid):
    """Gamma LOW on 1b333512e: the hook sanitizes their_usd (non-finite or negative becomes None
    and passes the band); the lane's pre-inline band must not refuse what the hook would not."""
    from sniper.settings import memecoin as cfg
    built, sender, net, ledger = dedicated
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 197.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 2500.0, raising=False)
    assert built["live"]._band_refusal(_payload(paid_usd=paid)) is None


def test_live_classifies_a_mined_revert_with_the_funded_replay_on_the_custody_host(entry, monkeypatch):
    # Codex EVM-04: a lane revert's class is the replay's, on the same host the receipt came from.
    from sniper.execution import evm_real_seams as seams
    built, _sender, _net, _ledger = entry
    asked = []
    monkeypatch.setattr(seams, "evm_revert_reason",
                        lambda h, url, http=None: asked.append((h, url)) or ("min_out", "x"))
    assert built["live"].revert_reason_fn("0xabc") == ("min_out", "x")
    assert asked == [("0xabc", built["live"].ctx["rpc_url"])] and asked[0][1]


def test_execution_evidence_roundtrips_without_an_exact_quote(tmp_path):
    path = str(tmp_path / "lane.db")
    evidence = {"signed_floors": {"cap": "123", "copy": "234", "binding": ["copy"]},
                "quote_price": None, "replay_basis": "block_end"}
    lane.write_live_record(path, {"execution_evidence": evidence})
    conn = sqlite3.connect(path)
    try:
        assert json.loads(conn.execute("SELECT execution_evidence_json FROM rh_fast_lane_live").fetchone()[0]) == evidence
    finally:
        conn.close()


@pytest.mark.parametrize('copy_floor,expected_binding', [(0, ['cap']), (200, ['copy']), (100, ['cap', 'copy'])])
def test_rh_floor_evidence_reports_the_immutable_signed_bound(copy_floor, expected_binding):
    payload = dict(route_kind='v4_native', cap_num=1, cap_den=1, key=KEY,
                   latest_same_pool={'sqrt': 1, 'block_no': 999999999})
    live = lane.LiveLane.__new__(lane.LiveLane)
    _tx, minimum = lane.lane_buy_tx(payload, 100, 1790000060, '0x' + '12' * 20, floor=copy_floor)
    evidence = live._floor_evidence(payload, 100, copy_floor, minimum)
    assert evidence == {'cap': '100', 'copy': str(copy_floor), 'signed_minimum': str(max(100,copy_floor)), 'binding': expected_binding}
