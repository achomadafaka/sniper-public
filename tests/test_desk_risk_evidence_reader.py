"""Local-only, as-of evidence reader; every database is a temporary fixture."""

import importlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_ps_tape as PT
from sniper.util import clock


NOW = 1_800_000_000.0
MINT = "AEPBdj3RViMJKCHM1xiVaM1VNuP7VbZRSN68gu5xpump"
SOL_QUOTE = "11111111111111111111111111111111"
POOL = PT.canonical_pool(MINT)


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setattr(clock, "offset_s", lambda: 100)
    return importlib.import_module("sniper.services.memecoin_desk_risk_tape")


def position(age=200):
    opened = datetime.fromtimestamp(NOW - age - 100, timezone.utc).isoformat()
    return {"id": 1, "mint": MINT, "opened_at": opened, "entry_price": 100}


def curve(desk, rows=None):
    rows = [(-201, "anchor", 1, 1, 1.0, SOL_QUOTE, 5e9),
            (-20, "buy", 1, 1, 1.1, SOL_QUOTE, 5e9),
            (-10, "sell", 0, 2, 1.05, SOL_QUOTE, 5e9),
            (-3, "ours", 0, 7, 1.2, SOL_QUOTE, 5e9),
            (-1, "future", 1, 99, 9.0, SOL_QUOTE, 5e9)] if rows is None else rows
    groups = {}
    for row in rows:
        day = datetime.fromtimestamp(NOW + row[0], timezone.utc).date().isoformat()
        groups.setdefault(day, []).append(row)
    for day, items in groups.items():
        path = desk / "swaps" / (day + ".db")
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE swaps(received_at REAL, signature TEXT, mint TEXT, is_buy INTEGER, quote_atomic INTEGER, "
                      "actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, real_quote INTEGER, quote_mint TEXT)")
            c.executemany("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?)", [
                (NOW + rel, sig, MINT, buy, int(sol * 1e9), sig, int(px * 1e9), 1_000_000, int(real), quote)
                for rel, sig, buy, sol, px, quote, real in items])
    return desk


def ps(desk, rows=((-120, 1), (-100, 1), (-20, 1), (-10, 1)), *, loss=None, pool_column=False):
    path = PT.tape_path(desk)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE swaps_ps(pool, mint, quote_mint, slot, signature, ordinal, tx_index, is_buy, quote_atomic, "
                  "base_atomic, actor, post_base_reserve_atomic, post_quote_reserve_atomic, block_time, coin_creator, "
                  "pre_base_reserve_atomic, pre_quote_reserve_atomic, received_at, payload_json)")
        c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_received ON swaps_ps(received_at)")
        c.execute("CREATE INDEX IF NOT EXISTS swaps_ps_pool_slot ON swaps_ps(pool,slot)")
        c.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER%s)" % (", pool TEXT" if pool_column else ""))
        c.execute("CREATE TABLE ps_decode_losses(signature TEXT,cause TEXT,slot INTEGER,received_at REAL, "
                  "PRIMARY KEY(signature,cause)) WITHOUT ROWID")
        c.execute("CREATE INDEX gaps_ps_at ON gaps_ps(at)")
        c.execute("CREATE INDEX ps_decode_losses_received ON ps_decode_losses(received_at)")
        payload = json.dumps({"ix_name": "buy", "virtual_quote_source": PT.VQ_SOURCE,
                              "virtual_quote_atomic": "0", "virtual_quote_reserves": 0})
        for i, (rel, buy) in enumerate(rows):
            c.execute("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                POOL, MINT, PT.WSOL_MINT, i + 1, "ps:%s" % i, 0, 0, buy, "2000000000", "1000", "actor:%s" % i,
                "1000000" if buy else None, "1000000000" if buy else None, int(NOW + rel), "creator",
                "1000000", "1000000000", NOW + rel, payload if i == 0 else "{}"))
        if loss is not None:
            rel, pool = loss
            if pool_column:
                c.execute("INSERT INTO gaps_ps VALUES(?,?,0,?)", (NOW + rel, "decode_loss", pool))
            else:
                c.execute("INSERT INTO gaps_ps VALUES(?,?,0)", (NOW + rel, "virtual_quote_unproven"))
    return desk


def read(reader, desk, *, proof=None, fills=lambda _: ("ours",), pos=None, sol_usd=100):
    proof_fn = proof if callable(proof) else lambda *a, **kw: {"from": NOW - 1000, "through": NOW - 1}
    return reader.evidence_of(position() if pos is None else pos, desk_dir=desk, now=NOW, sol_usd=sol_usd,
                              fill_sigs_of=fills, sb=0, proof_fn=proof_fn)


def test_curve_native_amounts_clocks_own_fills_and_future_cut(reader, tmp_path):
    e = read(reader, curve(tmp_path))
    assert e["fresh"] is True and e["venue"] == "curve"
    assert e["entry_price"] == 1 and e["held_s"] == 198
    assert e["buy_sol_30s"] == 1 and e["sell_sol_30s"] == 2
    assert e["swaps_60s"] == 2 and e["distinct_sellers_30s"] == 1
    assert e["last_price"] == pytest.approx(1.05)
    assert e["known_through"] == NOW - 2


@pytest.mark.parametrize("failure", ["missing", "corrupt", "overflow"])
def test_missing_error_or_bounded_row_overflow_is_unknown(reader, tmp_path, monkeypatch, failure):
    if failure != "missing":
        curve(tmp_path)
    if failure == "corrupt":
        next((tmp_path / "swaps").glob("*.db")).write_bytes(b"not sqlite")
    if failure == "overflow":
        monkeypatch.setattr(reader, "MAX_ROWS", 2)
    e = read(reader, tmp_path)
    assert e["fresh"] is False and e["swaps_60s"] is None


@pytest.mark.parametrize("fills", [None, lambda _: (), lambda _: (None,), lambda _: (_ for _ in ()).throw(ValueError("bad"))])
def test_unknown_own_fill_identity_is_blind(reader, tmp_path, fills):
    e = read(reader, curve(tmp_path), fills=fills)
    assert e["fresh"] is False and "ours_unknown" in e["unknown"]
    assert e["buy_sol_30s"] is None


@pytest.mark.parametrize("proof", [None, {"from": NOW - 1000, "through": NOW - 6},
                                    {"from": NOW + 1, "through": NOW},
                                    {"from": NOW - 1000, "through": NOW + 1},
                                    {"from": float("nan"), "through": NOW - 1}])
def test_absent_stale_or_malformed_capture_proof_is_blind(reader, tmp_path, proof):
    e = read(reader, curve(tmp_path), proof=lambda *a, **kw: proof)
    assert e["fresh"] is False and e["known_through"] is None


def test_recent_capture_can_prove_short_flow_without_inventing_long_history(reader, tmp_path):
    e = read(reader, curve(tmp_path), proof=lambda *a, **kw: {"from": NOW - 32, "through": NOW - 1})
    assert e["fresh"] is True and e["sell_sol_30s"] == 2
    assert e["n5"] is None and e["high_since_fill"] is None and e["swaps_60s"] is None


@pytest.mark.parametrize("quote", [None, "unknown", "USDC"])
def test_curve_without_explicit_native_denomination_is_blind(reader, tmp_path, quote):
    desk = curve(tmp_path, [(-10, "other", 1, 1, 1, quote, 5e9)])
    assert read(reader, desk)["fresh"] is False


def test_canonical_ps_uses_native_atomic_quote_without_usd_conversion(reader, tmp_path):
    desk = ps(tmp_path)
    e = read(reader, desk)
    assert e["fresh"] is True and e["venue"] == "ps"
    assert e["last_price"] == pytest.approx(1)
    assert e["buy_sol_30s"] == 4 and e["swaps_60s"] == 2
    rows = PT.load_ps_swaps(desk, MINT, NOW - 360, NOW - 2, 100)
    assert rows[-1]["quote_atomic"] == 2_000_000_000


@pytest.mark.parametrize("grad_source", ["curve", "ps_grads"])
def test_known_graduation_without_proven_pool_data_cannot_reuse_curve_prices(reader, tmp_path, grad_source):
    real = 85e9 if grad_source == "curve" else 5e9
    curve(tmp_path, [(-10, "grad", 1, 1, 1, SOL_QUOTE, real)])
    if grad_source == "ps_grads":
        ps(tmp_path, rows=())
        with sqlite3.connect(PT.tape_path(tmp_path)) as c:
            c.execute("CREATE TABLE ps_grads(pool, mint, received_at)")
            c.execute("INSERT INTO ps_grads VALUES(?,?,?)", (POOL, MINT, NOW - 5))
    proof = lambda *a, **kw: ({"from": NOW - 1000, "through": NOW - 1} if kw["venues"] == ("curve",) else None)
    e = read(reader, tmp_path, proof=proof)
    assert e["fresh"] is False and e["last_price"] is None
    assert "graduated_no_ps" in e["unknown"]


@pytest.mark.parametrize("pool,affected", [(POOL, True), (None, True), ("other-pool", False)])
def test_pool_and_unknown_pool_losses_only_blind_the_affected_windows(reader, tmp_path, pool, affected):
    e = read(reader, ps(tmp_path, loss=(-15, pool), pool_column=True))
    assert e["fresh"] is True
    assert (e["buy_sol_30s"] is None) is affected
    assert (e["swaps_60s"] is None) is affected


def test_unknown_pool_legacy_loss_is_not_silently_ignored(reader, tmp_path):
    e = read(reader, ps(tmp_path, loss=(-15, None)))
    assert e["fresh"] is True and e["buy_sol_30s"] is None


def test_price_only_loss_prints_are_not_activity_and_do_not_poison_newer_clean_windows(reader, tmp_path):
    ps(tmp_path)
    with sqlite3.connect(PT.tape_path(tmp_path)) as c:
        c.execute("UPDATE swaps_ps SET pre_quote_reserve_atomic='2000000000' WHERE signature='ps:1'")
    e = read(reader, tmp_path)
    assert e["fresh"] is True and e["swaps_60s"] == 2
    assert e["buy_sol_30s"] == 4 and e["high_since_fill"] is None
    assert e["coverage_from"] > NOW - 100


def test_ps_last_sell_cannot_use_a_future_received_buy_as_its_post_state(reader, tmp_path):
    e = read(reader, ps(tmp_path, rows=((-20, 1), (-10, 0), (-1, 1))))
    assert e["fresh"] is True and e["last_price"] is None
    assert e["sell_sol_30s"] == 2 and e["swaps_60s"] == 2


@pytest.mark.parametrize("real_quote", [None, -1, "unknown"])
def test_unknown_latest_curve_graduation_state_is_not_proof_of_a_live_curve(reader, tmp_path, real_quote):
    curve(tmp_path)
    with sqlite3.connect(next((tmp_path / "swaps").glob("*.db"))) as c:
        c.execute("UPDATE swaps SET real_quote=? WHERE signature='ours'", (real_quote,))
    assert read(reader, tmp_path)["fresh"] is False


def test_bare_signature_string_is_not_a_complete_fill_signature_list(reader, tmp_path):
    e = read(reader, curve(tmp_path), fills=lambda _: "ours")
    assert e["fresh"] is False and "ours_unknown" in e["unknown"]


def test_exact_five_second_proof_age_is_accepted_and_old_positions_have_bounded_history(reader, tmp_path):
    calls = []

    def proof(*args, **kwargs):
        calls.append(kwargs["window_s"])
        return {"from": NOW - 6000, "through": NOW - 5}

    e = read(reader, curve(tmp_path), proof=proof, pos=position(age=5000))
    assert e["fresh"] is True and e["known_through"] == NOW - 5
    assert calls == [3600, 3600] and e["coverage_from"] == NOW - 3600
    assert e["high_since_fill"] is None


def test_missing_native_ps_amount_never_uses_the_known_usd_field(reader, tmp_path, monkeypatch):
    ps(tmp_path)
    load = PT.load_ps_swaps

    def without_native(*args, **kwargs):
        rows, gaps = load(*args, **kwargs)
        for r in rows:
            r.pop("quote_atomic", None)
        return rows, gaps

    monkeypatch.setattr(PT, "load_ps_swaps", without_native)
    e = read(reader, tmp_path)
    assert e["fresh"] is True and e["last_price"] == pytest.approx(1)
    assert e["buy_sol_30s"] is None and e["sell_sol_30s"] is None


def test_unknown_sol_usd_does_not_erase_independent_native_ps_flow(reader, tmp_path):
    e = read(reader, ps(tmp_path), sol_usd=None)
    assert e["fresh"] is True and e["entry_price"] is None
    assert e["buy_sol_30s"] == 4
