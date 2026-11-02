"""Dip SHADOW (Alpha go (B), 2026-09-30): a separate `#dip` buy-quote series after a chase refusal.

Gamma's dip-fill read (8c3bbaba2): no pre-registered W pays pooled, and DegenCapitalLLC (+22.4%, n=8,
every dip within 15 s) is under the n>=20 bar. So nothing live: the refusal-capture process records,
on the MEASUREMENT key, the exact BUY quote for our reserved clip every 2 s for 30 s after each Solana
chase_quote / slippage_fail refusal. The would-be dip fill is derived at read time: the first `#dip`
row whose quote is back within the attempt's own cap against its own reference.

The existing chase_quote series feeds pre-registered reads (the slot-end grade among them), so it
must stay byte-identical: the dip series is its own capture id, lane and budget.
"""
import pytest

from sniper.services import memecoin_refusal_capture as cap

REFUSED = "2026-09-30T12:00:00+00:00"
EPOCH = 1790769600.0                     # REFUSED as epoch seconds


def _kw(**over):
    kw = dict(mint="MINT", origin_event_id="O", gate="chase_quote", refused_at=REFUSED,
              network_id="solana", horizon_s=30, cadence_s=2, max_concurrent=2, now=0,
              now_epoch=EPOCH + 3, quote_input_usd=101.0)
    kw.update(over)
    return kw


def test_a_dip_capture_is_its_own_series_beside_the_base_capture():
    caps = {}
    base = cap.start(caps, **_kw(horizon_s=810, cadence_s=25, max_concurrent=6))
    dip = cap.start(caps, dip=True, **_kw())
    assert base is not None and dip is not None
    assert dip.capture_id == base.capture_id + "#dip"
    assert dip.basis == "dip" and dip.horizon_s == 30 and dip.cadence_s == 2
    assert dip.expected_obs == 15 and dip.quote_input_usd == 101.0
    assert base.basis is None and set(caps) == {base.capture_id, dip.capture_id}


@pytest.mark.parametrize("over", [
    {"gate": "roundtrip_cap"}, {"gate": "impact_cap"}, {"gate": "coin_too_young"},
    {"network_id": "4663", "mint": "0xabc"},
])
def test_only_solana_chase_and_slippage_refusals_get_a_dip_series(over):
    assert cap.start({}, dip=True, **_kw(**over)) is None


@pytest.mark.parametrize("gate", ["chase_quote", "slippage_fail"])
def test_both_dip_families_start(gate):
    assert cap.start({}, dip=True, **_kw(gate=gate)).gate == gate


def test_a_dip_series_needs_the_reserved_size():
    refused = []
    assert cap.start({}, dip=True, on_refuse=lambda *a: refused.append(a),
                     **_kw(quote_input_usd=None)) is None
    assert refused[0][0].endswith("#dip") and refused[0][1] == "not_captured_size_unknown"


def test_the_dip_lane_has_its_own_budget():
    caps, refused = {}, []
    on = lambda *a: refused.append(a)
    for i in range(6):                     # the base Solana lane is full
        assert cap.start(caps, **_kw(mint="S%d" % i, origin_event_id="s%d" % i, max_concurrent=6,
                                     horizon_s=810, cadence_s=25))
    assert cap.start(caps, dip=True, on_refuse=on, **_kw(mint="D0", origin_event_id="d0"))
    assert cap.start(caps, dip=True, on_refuse=on,
                     **_kw(mint="D1", origin_event_id="d1", gate="slippage_fail"))
    # full dip lane: refused and counted; a base capture is still refused only for base capacity
    assert cap.start(caps, dip=True, on_refuse=on, **_kw(mint="D2", origin_event_id="d2")) is None
    assert refused[-1][0] == "solana:D2:d2#dip" and refused[-1][1] == "not_captured_capacity"
    base_live = sum(1 for c in caps.values() if c.basis is None)
    assert base_live == 6


def test_the_low_priority_reserve_does_not_apply_inside_the_dip_lane():
    caps = {}
    assert cap.start(caps, dip=True, **_kw(mint="D0", origin_event_id="d0"))
    # one of two dip slots free: a slippage_fail dip still starts (its own lane, no reserve)
    assert cap.start(caps, dip=True, **_kw(mint="D1", origin_event_id="d1", gate="slippage_fail"))


def _pump(caps, quotes, n):
    rows, sizes = [], []
    for i in range(n):
        cap.pump(caps, now_mono=2.0 * i, now_iso="2026-09-30T12:00:%02d+00:00" % (3 + 2 * i),
                 now_epoch=EPOCH + 3 + 2 * i, http=lambda *_a, **_k: (200, {}, None), rpc_url="rpc",
                 clip_usd=20, record_fn=rows.append,
                 quote_fn=lambda mint, usd, **_: sizes.append(usd) or {"outAmount": str(quotes.pop(0))},
                 decimals_fn=lambda *_a, **_k: 6)
    return rows, sizes


def test_every_dip_observation_is_a_buy_of_the_reserved_clip():
    caps = {}
    dip = cap.start(caps, dip=True, **_kw())
    rows, sizes = _pump(caps, [100_000_000, 125_000_000, 80_000_000], 3)
    assert sizes == [101.0, 101.0, 101.0]
    assert [r["quote_side"] for r in rows] == ["buy", "buy", "buy"]
    assert [r["quote_price"] for r in rows] == pytest.approx([1.01, 0.808, 1.2625])
    assert [r["quote_output_atomic"] for r in rows] == ["100000000", "125000000", "80000000"]
    assert all(r["capture_id"].endswith("#dip") for r in rows)
    assert dip.tokens_atomic is None and rows[-1]["token_qty_atomic"] is None
    assert [r["age_s"] for r in rows] == [3.0, 5.0, 7.0]


def test_the_base_series_is_byte_identical_with_a_dip_series_beside_it():
    def base_rows(with_dip):
        caps = {}
        cap.start(caps, **_kw(horizon_s=810, cadence_s=2, max_concurrent=6))
        if with_dip:
            cap.start(caps, dip=True, **_kw())
        # keyed by mint: every quote for the base coin answers the same, dip or not
        rows = []
        for i in range(3):
            cap.pump(caps, now_mono=2.0 * i, now_iso="2026-09-30T12:00:%02d+00:00" % (3 + 2 * i),
                     now_epoch=EPOCH + 3 + 2 * i, http=lambda *_a, **_k: (200, {}, None),
                     rpc_url="rpc", clip_usd=20, record_fn=rows.append,
                     quote_fn=lambda mint, usd, **_: {"outAmount": "100000000"},
                     decimals_fn=lambda *_a, **_k: 6)
        # latency_ms is this process's own wall time for the call, not part of the series
        return [{k: v for k, v in r.items() if k != "latency_ms"}
                for r in rows if not r["capture_id"].endswith("#dip")]
    with_dip, alone = base_rows(True), base_rows(False)
    assert len(alone) == 3 and with_dip == alone


def test_start_dip_follows_its_knobs(monkeypatch):
    from sniper.settings import memecoin as cfg
    r = {"mint": "MINT", "origin_event_id": "O", "gate": "chase_quote", "refused_at": REFUSED,
         "network_id": "solana", "quote_input_usd": 101.0}
    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_DIP_WINDOW_S", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_DIP_CADENCE_S", 2.0)
    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_DIP_MAX_CONCURRENT", 2)
    caps = {}
    got = cap.start_dip(caps, r, now=0, now_epoch=EPOCH + 3)
    assert got.capture_id == "solana:MINT:O#dip" and got.horizon_s == 30 and got.cadence_s == 2
    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_DIP_WINDOW_S", 0.0)
    assert cap.start_dip({}, r, now=0, now_epoch=EPOCH + 3) is None


def test_dip_knob_defaults():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_CAPTURE_DIP_WINDOW_S == 30.0
    assert cfg.MEMECOIN_CAPTURE_DIP_CADENCE_S == 2.0
    assert cfg.MEMECOIN_CAPTURE_DIP_MAX_CONCURRENT == 2


def test_dip_rows_live_in_their_own_table_so_no_capture_reader_sees_them(tmp_path):
    """Readers select from memecoin_refusal_captures by origin_event_id and take the first buy
    (chase_refusal_outcomes), or group by capture_id (gate_instrument_audit). A dip row in that table
    would be picked as the entry, or counted as a second refusal."""
    from sniper.persistence import db_memecoin_captures as store
    conn = store.connect(tmp_path / "captures.db")
    caps = {}
    cap.start(caps, **_kw(horizon_s=810, cadence_s=2, max_concurrent=6))
    cap.start(caps, dip=True, **_kw())
    rows, _ = _pump(caps, [100_000_000, 100_000_000, 125_000_000, 125_000_000], 2)
    for row in rows:
        store.record_observation(conn, row)
    store.terminal_row(conn, "solana:MINT:O#dip", "handed_off_to_position", mint="MINT",
                       gate="chase_quote", refused_at=REFUSED)
    base = conn.execute("SELECT capture_id FROM memecoin_refusal_captures").fetchall()
    dip = conn.execute("SELECT capture_id, quote_side, outcome FROM memecoin_dip_shadow "
                       "ORDER BY id").fetchall()
    assert {r[0] for r in base} == {"solana:MINT:O"}
    assert [tuple(r) for r in dip] == [("solana:MINT:O#dip", "buy", None)] * 2 + [
        ("solana:MINT:O#dip", None, "handed_off_to_position")]


@pytest.mark.parametrize("handed_off", [False, True])
def test_the_loop_wires_a_dip_series_for_a_polled_chase_refusal(tmp_path, handed_off):
    """Gamma's smoke (review of cd4f6f8bc): the real loop, one pass, no network. Both lanes are at
    capacity 0, so each capture is refused and counted, and the dip refusal must land in its own
    table with its own window. It killed both wiring mutants: the start_dip call removed, and the
    on_refuse dip branch removed."""
    import datetime as dt
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    from sniper.persistence import db_memecoin as m

    (tmp_path / "data").mkdir()
    now = dt.datetime.now(dt.timezone.utc)
    refused = (now - dt.timedelta(seconds=5)).isoformat()
    ledger = sqlite3.connect(str(tmp_path / "sniper_tracker.db"))
    ledger.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(ledger)
    pid = m.reserve_pending(ledger, mint="MINT", source_user_id="u", strategy="alpha_b",
                            notional=101.0, reserved_at=refused, attempt_id="A")
    ledger.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, mint, origin_event_id, terminal_reason, "
        "decision_started_at, network_id, terminal_status, pending_id, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("A", "MINT", "O", "exact quote is +45% above source price (cap 30%)", refused, "solana",
         "skipped_chase", pid, refused))
    if handed_off:          # a funded position on the mint after the refusal owns the coin
        m.open_position(ledger, source_user_id="u", source_handle="(real)", mint="MINT",
                        ticker="", entry_price=1.0, entry_notional=100.0,
                        entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                        opened_at=now.isoformat(), strategy="alpha_b", execution_mode="real",
                        network_id="solana")
    ledger.commit()
    ledger.close()
    # This process's environment minus every endpoint, memecoin knob and state location, so the
    # loop sees only the fake ledger and the one capture endpoint (assert_pool_disjoint).
    env = {k: v for k, v in os.environ.items()
           if "RPC" not in k.upper() and not k.startswith(("MEMECOIN_", "SNIPER_"))
           and k != "DB_PATH"}
    env.update({"SNIPER_STATE_ROOT": str(tmp_path), "MEMECOIN_CAPTURE_ENABLED": "1",
           "MEMECOIN_CAPTURE_MAX_CONCURRENT": "0",
           "MEMECOIN_CAPTURE_DIP_MAX_CONCURRENT": "1" if handed_off else "0",
           "MEMECOIN_CAPTURE_RPC_POOL_URLS": "http://127.0.0.1:9"})
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, str(root / "scripts" / "memecoin_refusal_capture_loop.py"), "--once",
         "--since", (now - dt.timedelta(seconds=60)).isoformat()],
        env=env, cwd=str(root), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    store = sqlite3.connect(str(tmp_path / "data" / "memecoin_refusal_captures.db"))
    dip = store.execute("SELECT capture_id, outcome, horizon_s, expected_obs "
                        "FROM memecoin_dip_shadow").fetchall()
    want = "handed_off_to_position" if handed_off else "not_captured_capacity"
    assert dip == [("solana:MINT:O#dip", want, 30.0, 15)]
    base = store.execute("SELECT capture_id, outcome FROM memecoin_refusal_captures").fetchall()
    assert base == [("solana:MINT:O", "not_captured_capacity")]
