"""Dip retry, live for named wallets only (operator order 2026-09-30: "only for the most botted
accounts, not for everyone, make it live for degen").

A Solana buy the chase cap refused, from a wallet in MEMECOIN_REAL_DIP_RETRY_WALLETS, waits on the
retry queue as a dip row. A watch thread quotes our clip; when the premium over the gate's own
reference is back within the gate's cap, the retry lane runs the WHOLE decision once, so every gate
re-checks (the chase cap included, on a fresh funded quote). Gamma 8c3bbaba2: DegenCapitalLLC +22.4%
[+7.0, +32.2] on n=8, every dip within 15 s; pooled across wallets no window paid.
"""
import json
import sqlite3
import threading
from dataclasses import asdict

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_dip_retry as D
from sniper.services import memecoin_real_runner as R

DEGEN = "fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb"
SIGNAL = "2026-09-30T12:00:00.000Z"
T0 = 1790769600.0                                     # SIGNAL as epoch seconds


def _conn(tmp_path=None):
    c = sqlite3.connect(str(tmp_path / "ledger.db") if tmp_path else ":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _ev(fid="e1", uid=DEGEN, handle="DegenCapitalLLC", mint="MintDip", network="solana",
        created=SIGNAL):
    return CopyEvent(
        source_user_id=uid, source_handle=handle, mint=mint, ticker="", side="buy",
        their_price=1e-5, their_market_cap=50000.0, their_size_usd=500.0, is_first_buy=True,
        fomo_event_id=fid, created_at=created, received_at=created, lag_s=0.5,
        signal_source="follow_activity", network_id=network)


def _chase(conn, ev, *, pct=40.0, quote_price=1.4e-5, cap=30, notional=100.0):
    pid = m.reserve_pending(conn, mint=ev.mint, source_user_id=ev.source_user_id,
                            strategy="alpha_b", notional=notional,
                            reserved_at="2026-09-30T12:00:01+00:00", attempt_id="A-" + ev.fomo_event_id,
                            network_id=ev.network_id)
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, pending_id, "
                 "decision_started_at) VALUES (?,?,?,?,?)",
                 ("A-" + ev.fomo_event_id, ev.mint, ev.network_id, pid, "2026-09-30T12:00:01+00:00"))
    m.release_pending(conn, pid, "chase_quote")
    return {"status": "skipped_chase", "attempt_id": "A-" + ev.fomo_event_id,
            "reason": "exact quote is %+.0f%% above source price (cap %.0f%%)" % (pct, cap),
            "quote_chase_pct": pct, "quote_price": quote_price}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_WALLETS", ("degencapitalllc",))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_MAX_HITS", 3)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 6.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", 12)
    return monkeypatch


def _enqueue(conn, ev, res):
    return D.maybe_enqueue(conn, ev, res, "2026-09-30T12:00:01+00:00",
                           event_json=json.dumps(asdict(ev)))


def _dip(conn):
    rows = m.dip_retry_rows(conn)
    return [dict(r, dip=json.loads(r["dip_json"])) for r in rows]


# --- enqueue: scope ---

def test_an_in_scope_chase_refusal_becomes_a_dip_row():
    c = _conn()
    ev = _ev()
    assert _enqueue(c, ev, _chase(c, ev))
    [row] = _dip(c)
    assert row["fomo_event_id"] == "e1" and row["mint"] == "MintDip"
    # from the SIGNAL (12:00:00), not queue time (12:00:01); approx's default relative tolerance on
    # an epoch is ~1,790 s, so the bound is absolute
    assert row["dip"]["deadline"] == pytest.approx(T0 + 30.0, abs=1e-3)
    assert row["dip"]["reference_price"] == pytest.approx(1.4e-5 / 1.4)
    assert row["dip"]["cap_pct"] == 30.0 and row["dip"]["clip_usd"] == 100.0


@pytest.mark.parametrize("scope", ["id", "handle_other_case"])
def test_a_wallet_is_named_by_id_or_handle(_cfg, scope):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_WALLETS",
                 (DEGEN,) if scope == "id" else ("DEGENCAPITALLLC".lower(),))
    c = _conn()
    ev = _ev(handle="DegenCapitalLLC")
    assert _enqueue(c, ev, _chase(c, ev))


@pytest.mark.parametrize("case", ["other_wallet", "window_off", "no_wallets", "evm", "not_chase",
                                  "clip_unknown", "unparseable_cap"])
def test_out_of_scope_refusals_are_not_queued(_cfg, case):
    from sniper.settings import memecoin as cfg
    c = _conn()
    ev = _ev(uid="someone", handle="notdecu") if case == "other_wallet" else (
        _ev(network="4663", mint="0xabc") if case == "evm" else _ev())
    res = _chase(c, ev)
    if case == "window_off":
        _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 0.0)
    if case == "no_wallets":
        _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_WALLETS", ())
    if case == "not_chase":
        res = {"status": "skipped_mcap", "reason": "mcap $9 over ceiling $1"}
    if case == "clip_unknown":
        res["attempt_id"] = "missing"
    if case == "unparseable_cap":
        res["reason"] = "exact quote is +40% above source price"
    assert not _enqueue(c, ev, res)
    assert _dip(c) == []


def test_knob_defaults_are_off():
    """In a fresh interpreter with none of the knobs set: an import-time default must not be read
    through whatever this process's environment or earlier tests left behind."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMECOIN_REAL_DIP_RETRY")}
    out = subprocess.run(
        [sys.executable, "-c",
         "from sniper.settings import memecoin as c; print(c.MEMECOIN_REAL_DIP_RETRY_S, "
         "c.MEMECOIN_REAL_DIP_RETRY_WALLETS, c.MEMECOIN_REAL_DIP_RETRY_POLL_S, "
         "c.MEMECOIN_REAL_DIP_RETRY_MAX_HITS)"],
        env=env, capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.split() == ["0.0", "()", "1.0", "3"]


# --- the blind retry lane never touches a dip row ---

def test_dip_rows_are_never_due_or_reaped_by_the_blind_lane():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    assert m.due_entry_retries(c, "2026-09-30T12:05:00+00:00", window_s=600, max_attempts=12) == []
    assert m.reap_entry_retries(c, "2026-09-30T13:00:00+00:00", window_s=60, max_attempts=12) == []
    assert len(_dip(c)) == 1


# --- the watch ---

class _Quotes:
    def __init__(self, outs):
        self.outs, self.calls = list(outs), []

    def __call__(self, mint, usd):
        self.calls.append((mint, usd))
        out = self.outs.pop(0)
        return None if out is None else {"outAmount": str(out)}


def _watch(tmp_path, quotes, *, cooling=False, now=T0 + 5):
    w = D.DipWatch(str(tmp_path / "ledger.db"), quote_fn=quotes, decimals_fn=lambda mint: 6,
                   cooling_fn=lambda: cooling, clock=lambda: now)
    return w


def _seed(tmp_path, **kw):
    c = _conn(tmp_path)
    ev = _ev(**kw)
    _enqueue(c, ev, _chase(c, ev))
    c.commit()
    return c


# reference = 1.4e-5 / 1.4 = 1e-5 USD/token; clip $100 at 6 decimals:
#   out 7_700_000_000_000 -> price 1.2987e-5 -> +29.9% (within the 30% cap)
#   out 7_600_000_000_000 -> price 1.3158e-5 -> +31.6% (over)
WITHIN, OVER = 7_700_000_000_000, 7_600_000_000_000


def test_the_watch_marks_a_row_due_only_when_back_within_the_cap(tmp_path):
    _seed(tmp_path)
    q = _Quotes([OVER, WITHIN])
    w = _watch(tmp_path, q)
    w.tick()
    assert w.take_due() == set()
    w.tick()
    assert w.take_due() == {"e1"}
    assert q.calls == [("MintDip", 100.0), ("MintDip", 100.0)]      # our reserved clip, sized


def test_the_watch_never_quotes_while_a_funded_host_cools(tmp_path):
    _seed(tmp_path)
    q = _Quotes([WITHIN])
    w = _watch(tmp_path, q, cooling=True)
    w.tick()
    assert q.calls == [] and w.take_due() == set()


def test_an_unknown_quote_or_unknown_decimals_is_no_hit(tmp_path):
    _seed(tmp_path)
    w = _watch(tmp_path, _Quotes([None]))
    w.tick()
    assert w.take_due() == set()
    w2 = D.DipWatch(str(tmp_path / "ledger.db"), quote_fn=_Quotes([WITHIN]),
                    decimals_fn=lambda mint: None, cooling_fn=lambda: False, clock=lambda: T0 + 5)
    w2.tick()
    assert w2.take_due() == set()


def test_the_watch_takes_one_quote_per_tick_and_skips_expired_rows(tmp_path):
    c = _seed(tmp_path)
    ev2 = _ev(fid="e2", mint="MintTwo")
    _enqueue(c, ev2, _chase(c, ev2))
    c.commit()
    q = _Quotes([WITHIN, WITHIN, WITHIN])
    w = _watch(tmp_path, q)
    w.tick()
    w.tick()
    assert len(q.calls) == 2 and {mint for mint, _ in q.calls} == {"MintDip", "MintTwo"}
    late = _watch(tmp_path, q, now=T0 + 31)
    late.tick()
    assert len(q.calls) == 2                                    # past the deadline: never quoted


def test_the_watch_pauses_after_a_quote_that_left_a_host_cooling(tmp_path):
    _seed(tmp_path)
    state = {"cooling": False}

    def quotes(mint, usd):
        state["cooling"] = True               # our own request drew the 429
        return None
    w = D.DipWatch(str(tmp_path / "ledger.db"), quote_fn=quotes, decimals_fn=lambda mint: 6,
                   cooling_fn=lambda: state["cooling"], clock=lambda: T0 + 5)
    w.tick()
    state["cooling"] = False
    calls = []
    w.quote_fn = lambda mint, usd: calls.append(mint) or {"outAmount": str(WITHIN)}
    w.tick()
    assert calls == []                        # paused 60 s even after the host recovered


# --- hits: the whole decision runs once per hit ---

class _Watch:
    def __init__(self, due):
        self.due = set(due)

    def take_due(self):
        d, self.due = self.due, set()
        return d


def _attempts(results):
    seen = []
    it = iter(results)

    def attempt(conn, ev):
        seen.append(ev.fomo_event_id)
        return next(it)
    attempt.seen = seen
    return attempt


def _hits(c, attempt, now="2026-09-30T12:00:05+00:00", due=("e1",)):
    return D.run_hits(c, attempt, now, watch=_Watch(due),
                     event_from_json=lambda j: CopyEvent(**json.loads(j)))


def test_busy_source_keeps_dip_row_for_a_later_price_hit(_cfg):
    c, ev = _conn(), _ev()
    _enqueue(c, ev, _chase(c, ev))
    tally = _hits(c, _attempts([{"status": "duplicate_delivery"}]))
    assert tally["hits"] == 0 and len(_dip(c)) == 1


def test_a_hit_that_fills_is_dequeued_and_logged():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    attempt = _attempts([{"status": "filled", "position_id": 77}])
    tally = _hits(c, attempt)
    assert attempt.seen == ["e1"] and tally["filled"] == 1
    assert _dip(c) == []
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert log["fomo_event_id"] == "e1" and log["status"] == "filled" and log["position_id"] == 77


def test_a_hit_refused_again_by_the_chase_cap_stays_until_max_hits():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    again = {"status": "skipped_chase", "reason": "exact quote is +33% above source price (cap 30%)"}
    attempt = _attempts([again, again, again])
    for _ in range(3):
        _hits(c, attempt)
    assert attempt.seen == ["e1"] * 3
    assert _dip(c) == []                                        # the third hit spent MAX_HITS


def test_any_other_verdict_ends_the_row():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    _hits(c, _attempts([{"status": "skipped_mcap", "reason": "mcap $9 over ceiling $1"}]))
    assert _dip(c) == []


def test_a_row_not_marked_due_never_runs():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt, due=())
    assert attempt.seen == [] and len(_dip(c)) == 1


def test_an_expired_row_is_dropped_without_a_decision():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt, now="2026-09-30T12:00:31+00:00")
    assert attempt.seen == [] and _dip(c) == []


@pytest.mark.parametrize("where", ["j7", "watcher"])
def test_a_source_sell_after_the_signal_ends_the_row_without_a_decision(where):
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    if where == "j7":
        c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, "
                  "created_at, network_id) VALUES ('s1', ?, 'MintDip', 'sell', "
                  "'2026-09-30T12:00:03.000Z', 'solana')", (DEGEN,))
    else:
        m.record_onchain_sell(c, signature="sig", source_user_id=DEGEN, handle="d", address="a",
                              mint="MintDip", sold_atomic=5, pre_atomic=10, post_atomic=5, slot=1,
                              block_time=int(T0) + 3, detected_at="2026-09-30T12:00:03+00:00",
                              created_at="2026-09-30T12:00:03+00:00")
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt)
    assert attempt.seen == [] and _dip(c) == []


def test_a_sell_before_the_signal_does_not_count():
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, created_at, "
              "network_id) VALUES ('s0', ?, 'MintDip', 'sell', '2026-09-30T11:59:00.000Z', 'solana')",
              (DEGEN,))
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt)
    assert attempt.seen == ["e1"]


@pytest.mark.parametrize("knob, value", [("MEMECOIN_REAL_DIP_RETRY_S", 0.0),
                                         ("MEMECOIN_REAL_DIP_RETRY_WALLETS", ())])
def test_run_hits_with_knobs_off_drops_live_rows(_cfg, knob, value):
    from sniper.settings import memecoin as cfg
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    _cfg.setattr(cfg, knob, value)
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt)
    assert attempt.seen == [] and _dip(c) == []


# --- wiring into the runner ---

def test_the_runner_enqueues_a_scoped_chase_refusal_as_a_dip_row_only():
    c = _conn()
    ev = _ev()
    res = _chase(c, ev)
    assert R._maybe_enqueue_retry(c, ev, res, "2026-09-30T12:00:01+00:00")
    rows = [dict(r) for r in c.execute("SELECT fomo_event_id, dip_json FROM memecoin_entry_retry_queue")]
    assert len(rows) == 1 and rows[0]["dip_json"]


def test_the_retry_lane_runs_dip_hits_even_with_the_blind_window_off(_cfg, monkeypatch):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 0.0)
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    monkeypatch.setattr(D, "_WATCH", _Watch({"e1"}))
    seen = []

    def hook(conn, e):
        seen.append(e.fomo_event_id)
        return {"status": "filled", "position_id": 9}
    hook.attempt = hook
    R.run_entry_retry_lane(c, hook, "2026-09-30T12:00:05+00:00")
    assert seen == ["e1"] and _dip(c) == []


def test_ensure_watch_starts_one_daemon_thread(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(D, "_WATCH", None)
    monkeypatch.setattr(D.DipWatch, "start", lambda self: started.append(self))
    a = D.ensure_watch(str(tmp_path / "ledger.db"))
    b = D.ensure_watch(str(tmp_path / "ledger.db"))
    assert a is b and len(started) == 1 and a.daemon


def test_a_dip_hit_refusal_never_pages_and_the_flag_does_not_leak(monkeypatch):
    """Gamma LOW on d4de8ed89: a dip hit runs under the retry lane's page suppression, or a refused
    hit re-pages the operator up to MAX_HITS times per signal. The flag must be ON during the
    attempt and OFF after it, so a live entry later in the pass pages normally."""
    c = _conn()
    ev = _ev()
    _enqueue(c, ev, _chase(c, ev))
    monkeypatch.setattr(D, "_WATCH", _Watch({"e1"}))
    during = []

    def hook(conn, e):
        during.append(getattr(R._entry_alert_suppress, "on", False))
        return {"status": "skipped_chase", "reason": "exact quote is +33% above source price (cap 30%)"}
    hook.attempt = hook
    R.run_entry_retry_lane(c, hook, "2026-09-30T12:00:05+00:00")
    assert during == [True]
    assert getattr(R._entry_alert_suppress, "on", False) is False


# --- EVM extension (2026-09-30, operator order via Alpha): 4663, execution premium, source basis ---

EVM_MINT = "0x0401324674a0a3df33f1fc23bfcb04c13fe39fbc"


@pytest.fixture
def evm_on(_cfg):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663"))
    return _cfg


def _premium(conn, ev, *, ratio=1.6, cap="1.50", drift=60.0, quote_price=6.4e-5, basis="source price",
             notional=29.62):
    res = _chase(conn, ev, notional=notional)
    res.pop("quote_chase_pct")
    res.update(reason="route quote is %.2fx the %s (cap %sx)" % (ratio, basis, cap),
               quote_price=quote_price, source_drift_pct=drift,
               dip_route={"rung": "v4", "sell_atomic": str(10 ** 16), "buy_atomic": "1000",
                          "legs_path": None})
    return res


def _evm_ev(fid="x1", **kw):
    return _ev(fid=fid, mint=EVM_MINT, network="4663", **kw)


def test_a_4663_source_basis_premium_refusal_becomes_a_dip_row(evm_on):
    c = _conn()
    ev = _evm_ev()
    # the printed ratio is rounded (1.60x); the refusal's own drift is exact (60.247%, 346c152f)
    assert _enqueue(c, ev, _premium(c, ev, drift=60.247))
    [row] = _dip(c)
    assert row["network_id"] == "4663" and row["mint"] == EVM_MINT
    assert row["dip"]["reference_price"] == pytest.approx(6.4e-5 / 1.60247, rel=1e-9)
    assert row["dip"]["cap_pct"] == pytest.approx(50.0)                     # (1.50 - 1) x 100
    assert row["dip"]["clip_usd"] == 29.62
    assert row["dip"]["deadline"] == pytest.approx(T0 + 30.0, abs=1e-3)


def test_the_evm_form_needs_its_chain_in_the_knob():
    """Default MEMECOIN_REAL_DIP_RETRY_CHAINS is solana: EVM stays off until the operator adds it."""
    c = _conn()
    ev = _evm_ev()
    assert not _enqueue(c, ev, _premium(c, ev))
    assert _dip(c) == []


@pytest.mark.parametrize("case", ["chart_mid", "quote_over_source", "market_basis", "no_drift",
                                  "no_quote_price", "unparsed_cap"])
def test_other_evm_refusals_are_not_queued(evm_on, case):
    c = _conn()
    ev = _evm_ev()
    res = _premium(c, ev, basis="chart mid" if case == "chart_mid" else "source price")
    if case == "quote_over_source":
        res["reason"] = "our quote is +60% above source price (cap 50%)"
    if case == "market_basis":
        res["reason"] = "coin is +60% above source price (cap 50%, basis market)"
        res["quote_chase_pct"] = 60.0
    if case == "no_drift":
        res["source_drift_pct"] = None
    if case == "no_quote_price":
        res["quote_price"] = None
    if case == "unparsed_cap":
        res["reason"] = "route quote is 1.60x the source price"
    assert not _enqueue(c, ev, res)
    assert _dip(c) == []


@pytest.mark.parametrize("chains, queued", [(("solana", "4663"), True), (("4663",), False)])
def test_the_solana_percent_form_follows_the_chain_knob(_cfg, chains, queued):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", chains)
    c = _conn()
    ev = _ev()
    assert _enqueue(c, ev, _chase(c, ev)) is queued


def _evm_ctx(asked):
    return {"quote_token": "0xweth", "rpc_url": "rpc", "venue_quote_with_miss_fn": (
        lambda sell, buy, amount, **kw: (asked.append(buy) or (None, "no_route")))}


def _evm_watch(tmp_path, asked, chain="4663", now=T0 + 5):
    return D.EvmDipWatch(str(tmp_path / "ledger.db"), chain,
                         contexts_fn=lambda: {chain: _evm_ctx(asked)}, clock=lambda: now)


def _seed_evm(tmp_path):
    c = _conn(tmp_path)
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    c.commit()
    return c


def test_each_watch_quotes_only_its_own_chain(tmp_path, evm_on):
    from sniper.settings import memecoin as cfg
    evm_on.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663", "56"))
    c = _seed_evm(tmp_path)
    ev = _ev(fid="s1")
    _enqueue(c, ev, _chase(c, ev))
    c.commit()
    asked = []
    ew = _evm_watch(tmp_path, asked)
    ew.tick()
    ew.tick()
    assert asked == [EVM_MINT, EVM_MINT]
    _evm_watch(tmp_path, asked, chain="56").tick()          # another chain's thread: not its row
    assert asked == [EVM_MINT, EVM_MINT]
    q = _Quotes([OVER, OVER])
    sw = _watch(tmp_path, q)
    sw.tick()
    sw.tick()
    assert [mint for mint, _ in q.calls] == ["MintDip", "MintDip"]


def test_the_evm_watch_idles_once_its_chain_leaves_the_knob(tmp_path, evm_on):
    from sniper.settings import memecoin as cfg
    _seed_evm(tmp_path)
    evm_on.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana",))
    asked = []
    _evm_watch(tmp_path, asked).tick()
    assert asked == []


def _evm_sell(c, at):
    assert m.record_onchain_evm_sell_shadow(
        c, network_id="4663", tx_hash="0xs", address="0xa", source_user_id=DEGEN, handle="d",
        mint=EVM_MINT, sold_atomic=5, pre_atomic=10, post_atomic=5, proof="transfer_out+proceeds_in",
        proceeds_token="0xweth", proceeds_atomic=1, block_number=1, detected_at=at, decoded_at=at,
        created_at=at)


def test_a_4663_watcher_sell_after_the_signal_ends_the_row(evm_on):
    c = _conn()
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    now = "2026-09-30T12:00:03+00:00"
    _evm_sell(c, now)
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt, due=("x1",))
    assert attempt.seen == [] and _dip(c) == []


def test_a_4663_watcher_sell_before_the_signal_does_not_count(evm_on):
    c = _conn()
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    before = "2026-09-30T11:59:00+00:00"
    _evm_sell(c, before)
    attempt = _attempts([{"status": "filled", "position_id": 5}])
    _hits(c, attempt, due=("x1",))
    assert attempt.seen == ["x1"]


def test_a_row_whose_chain_left_the_knob_drops_without_a_decision(evm_on):
    from sniper.settings import memecoin as cfg
    c = _conn()
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    evm_on.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana",))
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt, due=("x1",))
    assert attempt.seen == [] and _dip(c) == []


def test_a_hit_logs_its_network(evm_on):
    c = _conn()
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    _hits(c, _attempts([{"status": "filled", "position_id": 8}]), due=("x1",))
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert log["network_id"] == "4663" and log["position_id"] == 8


def test_run_hits_takes_due_rows_from_both_watches(evm_on, monkeypatch):
    c = _conn()
    for ev, res in ((_evm_ev(), None), (_ev(fid="s1"), None)):
        _enqueue(c, ev, _premium(c, ev) if ev.network_id == "4663" else _chase(c, ev))
    monkeypatch.setattr(D, "_WATCH", _Watch({"s1"}))
    monkeypatch.setattr(D, "_EVM_WATCHES", {"4663": _Watch({"x1"})})
    attempt = _attempts([{"status": "filled"}, {"status": "filled"}])
    D.run_hits(c, attempt, "2026-09-30T12:00:05+00:00",
               event_from_json=lambda j: CopyEvent(**json.loads(j)))
    assert sorted(attempt.seen) == ["s1", "x1"]


def test_the_chain_knob_defaults_to_solana_and_the_dip_module_never_loads_the_capture_lane():
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMECOIN_REAL_DIP_RETRY")}
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; from sniper.settings import memecoin as c; "
         "import sniper.services.memecoin_dip_retry; "
         "print(c.MEMECOIN_REAL_DIP_RETRY_CHAINS, "
         "'sniper.services.memecoin_evm_exact_capture' in sys.modules)"],
        env=env, capture_output=True, text=True, timeout=60, check=True)
    assert out.stdout.split() == ["('solana',)", "False"]


# --- Gamma review of 491be6812: shadow chains (MEDIUM 1), a denominator (MEDIUM 2), route hint (LOW 2) ---

def test_a_shadow_chain_logs_a_would_be_hit_and_never_decides(evm_on):
    from sniper.settings import memecoin as cfg
    evm_on.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_SHADOW_CHAINS", ("4663",))
    c = _conn()
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt, due=("x1",))
    assert attempt.seen == [] and _dip(c) == []
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert (log["status"], log["network_id"], log["position_id"]) == ("shadow_hit", "4663", None)


@pytest.mark.parametrize("end, status", [("deadline", "expired"), ("knob", "knob_off"),
                                         ("sold", "source_sold")])
def test_every_queued_row_ends_with_one_log_row_and_its_watch_stats(evm_on, end, status):
    from sniper.settings import memecoin as cfg
    c = _conn()
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    now, due = "2026-09-30T12:00:05+00:00", ("x1",)
    if end == "deadline":
        now, due = "2026-09-30T12:00:31+00:00", ()
    if end == "knob":
        evm_on.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana",))
    if end == "sold":
        c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, "
                  "created_at, network_id) VALUES ('s9', ?, ?, 'sell', '2026-09-30T12:00:02.000Z', "
                  "'4663')", (DEGEN, EVM_MINT))
    w = _Watch(due)
    w.stats = {"x1": {"min_premium_pct": 51.2, "quotes": 4, "unknown": 1}}
    attempt = _attempts([{"status": "filled"}])
    D.run_hits(c, attempt, now, watch=w, event_from_json=lambda j: CopyEvent(**json.loads(j)))
    assert attempt.seen == [] and _dip(c) == []
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert (log["status"], log["network_id"]) == (status, "4663")
    assert (log["min_watch_premium_pct"], log["watch_quotes"], log["watch_unknown"]) == (51.2, 4, 1)


def test_a_hit_row_carries_the_watch_stats_too(evm_on):
    c = _conn()
    ev = _evm_ev()
    _enqueue(c, ev, _premium(c, ev))
    w = _Watch({"x1"})
    w.stats = {"x1": {"min_premium_pct": 48.0, "quotes": 2, "unknown": 0}}
    D.run_hits(c, _attempts([{"status": "filled", "position_id": 3}]), "2026-09-30T12:00:05+00:00",
               watch=w, event_from_json=lambda j: CopyEvent(**json.loads(j)))
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert (log["status"], log["min_watch_premium_pct"], log["watch_quotes"]) == ("filled", 48.0, 2)


def test_a_checksummed_evm_mint_is_queued_lowercase_so_the_sell_stop_matches(evm_on):
    """Gamma LOW on ceb2a7dd4: 2 of 34,253 live EVM attempts carry a mixed-case mint, while the EVM
    sell shadow and J7 store lowercase. A mixed-case row would never see its source's sale."""
    c = _conn()
    ev = _ev(fid="x1", mint="0x0401324674A0A3DF33F1FC23BFCB04C13FE39FBC", network="4663")
    _enqueue(c, ev, _premium(c, ev))
    [row] = _dip(c)
    assert row["mint"] == EVM_MINT
    _evm_sell(c, "2026-09-30T12:00:03+00:00")
    attempt = _attempts([{"status": "filled"}])
    _hits(c, attempt, due=("x1",))
    assert attempt.seen == [] and _dip(c) == []


# --- Alpha / Gamma, first live 4663 dip rows (2026-09-30 11:30Z): one source tx queued twice (J7 and
#     the on-chain lane) and both expired blind; the watch cannot price pons_v2 / Kyber routes. ---

SRC_TX = "0x9fd46970a3c3423097a35ab836169fa4e7fc6f653aa55ff5a8a248094717f7fc"


def _log(c):
    return [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log ORDER BY id")]


def test_one_source_tx_queues_once_across_lanes(evm_on):
    c = _conn()
    j7 = _ev(fid="swap:raybot:%s:0x8a71:%s" % (SRC_TX, EVM_MINT), mint=EVM_MINT, network="4663")
    chain = _ev(fid="onchain:4663:%s:%s" % (SRC_TX, EVM_MINT), mint=EVM_MINT, network="4663")
    assert _enqueue(c, j7, _premium(c, j7))
    assert not _enqueue(c, chain, _premium(c, chain))
    assert [r["fomo_event_id"] for r in _dip(c)] == [j7.fomo_event_id]
    [log] = _log(c)
    assert (log["fomo_event_id"], log["status"]) == (chain.fomo_event_id, "duplicate_source_tx")


def test_a_late_second_lane_after_the_first_row_ended_is_still_a_duplicate(evm_on):
    c = _conn()
    j7 = _ev(fid="swap:raybot:%s:0x8a71:%s" % (SRC_TX, EVM_MINT), mint=EVM_MINT, network="4663")
    _enqueue(c, j7, _premium(c, j7))
    D.run_hits(c, _attempts([]), "2026-09-30T12:00:31+00:00", watch=_Watch(()),
               event_from_json=lambda j: CopyEvent(**json.loads(j)))       # expires the first
    chain = _ev(fid="onchain:4663:%s:%s" % (SRC_TX, EVM_MINT), mint=EVM_MINT, network="4663")
    assert not _enqueue(c, chain, _premium(c, chain))
    assert [r["status"] for r in _log(c)] == ["expired", "duplicate_source_tx"]


# --- the watch on the PRODUCTION context (Gamma MEDIUM on 62fe9b21b) ---
# `_evm_ctx` above is a hand-made dict. The funded context is built by
# memecoin_evm_leg.build_real_evm_context, and it carried neither `venue_quote_with_miss_fn` nor
# `quote_token`: every ladder tick and every kyber, v3, v2, curve and mixed winner tick raised
# KeyError, counted as unknown, and the row expired unwatched.

def _production_ctx(monkeypatch, tmp_path, asked, chain="4663"):
    """`build_real_evm_context` itself, offline. The executor's ladder is swapped for a recorder
    BEFORE the context is built, so the context dict is the production one, key for key."""
    from sniper.execution import evm_real_seams as S
    from sniper.execution.evm_wallet import generate_keypair_file
    from sniper.services import memecoin_evm_leg as leg
    from sniper.settings import memecoin as cfg
    keypair = str(tmp_path / "evm.json")
    generate_keypair_file(keypair)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", keypair)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_" + chain, "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + chain, "https://rpc-" + chain)
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)
    real = S.build_real_evm_executor

    def executor(**kw):
        def ladder(sell, buy, amount, **kwargs):
            asked.append(("ladder", sell, buy, amount, kwargs))
            return {"buyAmount": "900", "_sniper_winner": "v3"}, None
        return dict(real(**kw), venue_quote_with_miss_fn=ladder)
    monkeypatch.setattr(S, "build_real_evm_executor", executor)

    def http(_method, _url, **kwargs):
        request = json.loads(kwargs["body"])
        return 200, {"result": hex(int(chain)) if request["method"] == "eth_chainId" else "0x"}, None
    return leg.build_real_evm_context(chain, http=http)


POOL, LEGS = "0x" + "ab" * 32, [["0xleg"]]


@pytest.mark.parametrize("route, first", [
    ({}, ("ladder", {"exclude_rungs": D.WATCH_EXCLUDED_RUNGS})),      # nothing pinned
    ({"rung": "v3"}, ("ladder", {"only_rung": "v3"})),                # any rung the ladder owns
    ({"rung": "kyber"}, ("kyber", {"timeout": 4, "chain": "4663"})),
    ({"rung": "v4", "pool_id": POOL}, ("v4", [POOL])),
    ({"rung": "v4_multihop", "legs_path": LEGS}, ("v4_multihop", LEGS)),
], ids=["ladder", "v3", "kyber", "v4", "v4_multihop"])
def test_the_watch_prices_every_route_kind_on_the_production_context(
        tmp_path, evm_on, monkeypatch, route, first):
    from sniper.execution import evm_kyber_quote as kyber
    from sniper.execution import evm_v4_multihop as hop
    from sniper.execution import evm_v4_quote as v4
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    asked = []
    monkeypatch.setattr(kyber, "_routes", lambda sell, buy, amount, **kw: (
        asked.append(("kyber", sell, buy, amount, kw)) or {"amountOut": "900"}))
    monkeypatch.setattr(v4, "pinned_buy_out", lambda chain, mint, sell, rpc, *, pool_ids=None, **kw: (
        asked.append(("v4", rpc, pool_ids)) or ((900, pool_ids[0]) if pool_ids else (None, None))))
    monkeypatch.setattr(hop, "pinned_path_out", lambda chain, path, sell, rpc, **kw: (
        asked.append(("v4_multihop", rpc, path)) or 900))
    ctx = _production_ctx(monkeypatch, tmp_path, asked)
    c = _conn(tmp_path)
    ev = _evm_ev()
    res = _premium(c, ev)
    res["dip_route"].update(route)
    _enqueue(c, ev, res)
    c.commit()
    watch = D.EvmDipWatch(str(tmp_path / "ledger.db"), "4663", contexts_fn=lambda: {"4663": ctx},
                          clock=lambda: T0 + 5)
    watch.tick()
    tick = watch.stats_for("x1")["series"][-1]
    assert tick["miss"] is None and tick["buy_atomic"] == "900", tick
    weth = CHAIN_QUOTE["4663"]["token"]
    kind, detail = first
    if kind in ("ladder", "kyber"):
        assert asked[0] == (kind, weth, EVM_MINT, 10 ** 16, detail), asked
    else:
        assert asked[0] == (kind, "https://rpc-4663", detail), asked


def test_a_fault_inside_the_watch_is_logged_once_per_row_and_is_not_a_provider_miss(
        tmp_path, evm_on, caplog):
    """A missing context key is OUR defect, not the market's. It is named on the tick (`miss`
    watch_fault, `fault` the exception and the key), counted apart from unknown provider
    answers, and logged with its traceback once per row, not once per tick."""
    _seed_evm(tmp_path)
    watch = D.EvmDipWatch(str(tmp_path / "ledger.db"), "4663",
                          contexts_fn=lambda: {"4663": {"rpc_url": "rpc"}}, clock=lambda: T0 + 5)
    with caplog.at_level("ERROR"):
        watch.tick()
        watch.tick()
    stats = watch.stats_for("x1")
    ticks = stats["series"][1:]
    assert [t["miss"] for t in ticks] == ["watch_fault", "watch_fault"], ticks
    assert all(t["fault"] == "KeyError:'venue_quote_with_miss_fn'" for t in ticks), ticks
    assert stats["faults"] == 2 and stats["unknown"] == 2 and stats["quotes"] == 2
    logged = [r for r in caplog.records if "venue_quote_with_miss_fn" in r.getMessage()]
    assert len(logged) == 1 and "x1" in logged[0].getMessage() and logged[0].exc_info


def test_a_missing_key_on_the_kyber_rung_is_a_fault_not_a_provider_transport_miss(tmp_path, evm_on):
    """On 62fe9b21b the kyber rung read `ctx["quote_token"]` inside its provider try, so the
    KeyError was reported as `provider_rpc_transport`: our defect, blamed on Kyber."""
    c = _conn(tmp_path)
    ev = _evm_ev()
    res = _premium(c, ev)
    res["dip_route"].update(rung="kyber")
    _enqueue(c, ev, res)
    c.commit()
    watch = D.EvmDipWatch(str(tmp_path / "ledger.db"), "4663",
                          contexts_fn=lambda: {"4663": {"rpc_url": "rpc"}}, clock=lambda: T0 + 5)
    watch.tick()
    tick = watch.stats_for("x1")["series"][-1]
    assert (tick["miss"], tick["fault"]) == ("watch_fault", "KeyError:'quote_token'"), tick


def test_a_seam_that_is_none_is_a_fault_too(tmp_path, evm_on):
    """The builder fills both keys with `ex.get(...)`: an executor that lacks the seam leaves the
    key present and None, and calling it raises TypeError (Gamma LOW 1 on 10c590564)."""
    _seed_evm(tmp_path)
    ctx = {"rpc_url": "rpc", "quote_token": "0xweth", "venue_quote_with_miss_fn": None}
    watch = D.EvmDipWatch(str(tmp_path / "ledger.db"), "4663", contexts_fn=lambda: {"4663": ctx},
                          clock=lambda: T0 + 5)
    watch.tick()
    tick = watch.stats_for("x1")["series"][-1]
    assert tick["miss"] == "watch_fault" and tick["fault"].startswith("TypeError:"), tick


def test_a_provider_miss_is_never_called_a_watch_fault(tmp_path, evm_on):
    _seed_evm(tmp_path)
    asked = []
    watch = _evm_watch(tmp_path, asked)                       # its ladder answers (None, "no_route")
    watch.tick()
    stats = watch.stats_for("x1")
    assert stats["series"][-1]["miss"] == "no_route" and "fault" not in stats["series"][-1]
    assert stats["faults"] == 0 and stats["unknown"] == 1


# --- the watch cap is the cap the rerun decision applies (Gamma, DegenCapitalLLC 4663, 20:35Z) ----


def _decide_4663(c, ev, tokens, *, fid):
    """The PRODUCTION EVM decision's price gates: `execute_entry_evm` on one 4663 quote of $10
    buying `tokens` against a source price of $1.00 (a chart mid of $0.50, so the premium's
    reference is the source). Returns the executor's result."""
    from sniper.execution.evm_swap_executor import execute_entry_evm
    attempt_id = "A-" + fid
    contract = {"contract_version": 1, "identity": {"network_id": "4663", "mint": EVM_MINT},
                "policy": {"policy_id": "micro_v1"},
                "classification": {"facts": {"display_price": 0.5}}}
    payload, digest = m.canonical_funded_json(contract)
    pid = m.reserve_pending(
        c, mint=EVM_MINT, source_user_id=ev.source_user_id, strategy="alpha_b", notional=10.0,
        reserved_at="2026-09-30T12:00:01+00:00", network_id="4663", attempt_id=attempt_id,
        source_price=1.0, funded_contract_version=1, funded_contract_hash=digest,
        funded_contract_json=payload)
    c.execute("INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, pending_id, "
              "decision_started_at) VALUES (?,?,?,?,?)",
              (attempt_id, EVM_MINT, "4663", pid, "2026-09-30T12:00:01+00:00"))
    m.set_entry_attempt_funded_contract(c, attempt_id, funded_contract_json=payload,
                                        funded_contract_hash=digest, funded_contract_version=1)
    quote = {"sellAmount": "10000000", "buyAmount": str(int(tokens * 10 ** 6)),
             "transaction": {"to": "0xrouter", "data": "0xdead"},
             "issues": {"allowance": {"spender": "0xspender"}}}
    balances = iter([(0.0, 6), (tokens, 6)])
    usdc = iter([100.0, 90.0])
    result = execute_entry_evm(
        c, pending_id=pid, mint=EVM_MINT, network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id=ev.source_user_id, now_iso="2026-09-30T12:00:01+00:00",
        quote_fn=lambda mint, usd: dict(quote), approve_fn=lambda spender, amount: True,
        sign_fn=lambda tx: ("0xraw", "0xhash", 7), send_fn=lambda raw: "0xhash",
        confirm_fn=lambda h: "confirmed", usdc_fn=lambda *a: next(usdc),
        token_fn=lambda *a: next(balances))
    return result


@pytest.mark.parametrize("over_source", ["30", None], ids=["4663-cap-30", "cap-unset"])
def test_the_watch_fires_only_where_the_rerun_decision_can_fill(evm_on, tmp_path, monkeypatch,
                                                                 over_source):
    """Gamma 2026-09-30 20:35Z (dip log 8/9): a DegenCapitalLLC 4663 row queued from a 1.50x
    execution-premium refusal fired at +38.6%, and the rerun refused `our quote is +49% above
    source price (cap 30%)`: the decision also caps our quote over the source
    (MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663=30). A hit in (30%, 50%] can never fill; it
    burns a decision, a funded quote and a MAX_HITS slot. The watch cap is now the tighter of the
    two, read from the decision's own settings function."""
    from sniper.settings import memecoin as cfg
    evm_on.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    if over_source:
        monkeypatch.setenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", over_source)
    else:
        monkeypatch.delenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", raising=False)
    c = _conn(tmp_path)
    ev = _evm_ev()
    refused = _decide_4663(c, ev, 6.25, fid="x1")                 # 1.60x the source: premium
    assert refused["status"] == "skipped_chase"
    assert refused["reason"] == "route quote is 1.60x the source price (cap 1.50x)"
    assert _enqueue(c, ev, refused)
    c.commit()
    [row] = _dip(c)
    assert row["dip"]["cap_pct"] == pytest.approx(30.0 if over_source else 50.0)
    # The rerun decision at +40% over the source: refused by the quote-over-source cap when set.
    rerun = _decide_4663(c, _evm_ev(fid="x2"), 10 / 1.4, fid="x2")
    if over_source:
        assert rerun["status"] == "skipped_chase"
        assert rerun["reason"].startswith("our quote is +40% above source price (cap 30%)")
    else:
        assert rerun["status"] == "filled"
    # The watch at the same +40%: a hit only where that decision can fill.
    outs = iter([int(10 / 1.4 * 10 ** 6), int(10 / 1.25 * 10 ** 6)])     # +40%, then +25%
    ctx = {"quote_token": "0xweth", "rpc_url": "rpc", "venue_quote_with_miss_fn": (
        lambda sell, buy, amount, **kw: ({"buyAmount": str(next(outs)), "_sniper_winner": "v2"},
                                         None))}
    watch = D.EvmDipWatch(str(tmp_path / "ledger.db"), "4663", contexts_fn=lambda: {"4663": ctx},
                          clock=lambda: T0 + 5)
    watch.tick()
    assert watch.take_due() == (set() if over_source else {"x1"})
    watch.tick()                                                   # +25%: inside both caps
    assert watch.take_due() == {"x1"}
