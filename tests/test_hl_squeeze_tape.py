"""Squeeze tape: shape, idempotency, missing-not-zero, and loud provider failure."""
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "run_hl_squeeze_tape",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_hl_squeeze_tape.py")
tape = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tape)


def _payload(**overrides):
    ctx = {
        "markPx": "1.1875", "midPx": "1.18705", "oraclePx": "1.189316",
        "premium": "-0.0014747973", "funding": "-0.0000566118",
        "openInterest": "9050940.0", "dayNtlVlm": "19386071.93", "dayBaseVlm": "16597708.0",
        "prevDayPx": "1.048", "impactPxs": ["1.186564", "1.187562"],
    }
    ctx.update(overrides)
    return ({"universe": [{"name": "KAITO"}, {"name": "JELLY", "isDelisted": True}]},
            [ctx, dict(ctx, openInterest="0.0")])


@pytest.fixture()
def conn(tmp_path):
    return tape.connect(str(tmp_path / "t.db"))


def test_captures_every_required_squeeze_field(conn):
    tape.tick_once(conn, now_ms=1_000, fetch=lambda: _payload())
    row = conn.execute("SELECT * FROM hl_squeeze_marks WHERE coin='KAITO'").fetchone()
    assert row["open_interest"] == pytest.approx(9050940.0)
    assert row["oracle_px"] == pytest.approx(1.189316)     # basis leg
    assert row["premium"] == pytest.approx(-0.0014747973)
    assert row["impact_bid"] == pytest.approx(1.186564)    # replenishment proxy
    assert row["impact_ask"] == pytest.approx(1.187562)
    assert row["is_delisted"] == 0


def test_delisting_is_dated_not_inferred(conn):
    tape.tick_once(conn, now_ms=1_000, fetch=lambda: _payload())
    assert conn.execute(
        "SELECT is_delisted FROM hl_squeeze_marks WHERE coin='JELLY'").fetchone()[0] == 1
    assert conn.execute("SELECT delisted FROM hl_squeeze_ticks").fetchone()[0] == 1


def test_zero_open_interest_is_not_missing(conn):
    """0 OI is a real observation; None is absence. Conflating them fakes cascade fuel."""
    tape.tick_once(conn, now_ms=1_000, fetch=lambda: _payload())
    assert conn.execute(
        "SELECT open_interest FROM hl_squeeze_marks WHERE coin='JELLY'").fetchone()[0] == 0.0


def test_garbage_field_stays_missing(conn):
    tape.tick_once(conn, now_ms=1_000, fetch=lambda: _payload(openInterest="n/a", impactPxs=None))
    row = conn.execute("SELECT * FROM hl_squeeze_marks WHERE coin='KAITO'").fetchone()
    assert row["open_interest"] is None and row["impact_bid"] is None


def test_replayed_tick_is_idempotent(conn):
    for _ in range(3):
        tape.tick_once(conn, now_ms=1_000, fetch=lambda: _payload())
    assert conn.execute("SELECT COUNT(*) FROM hl_squeeze_marks").fetchone()[0] == 2


def test_provider_failure_records_a_hole_instead_of_silence(conn):
    def boom():
        raise TimeoutError("provider down")

    result = tape.tick_once(conn, now_ms=2_000, fetch=boom, last_ts_ms=1_000)
    assert result["status"] == "error"
    row = conn.execute("SELECT * FROM hl_squeeze_ticks WHERE ts_ms=2000").fetchone()
    assert row["status"] == "error" and "TimeoutError" in row["detail"]
    assert row["gap_s"] == pytest.approx(1.0)
    assert conn.execute("SELECT COUNT(*) FROM hl_squeeze_marks").fetchone()[0] == 0


def test_unpaired_context_tail_is_dropped(conn):
    """HL pairs assetCtxs positionally with meta.universe; an unpaired ctx has no coin identity."""
    meta, ctxs = _payload()
    tape.tick_once(conn, now_ms=1_000, fetch=lambda: (meta, ctxs + [dict(ctxs[0])]))
    assert conn.execute("SELECT COUNT(*) FROM hl_squeeze_marks").fetchone()[0] == 2


def test_cadence_bound_is_enforced(capsys):
    with pytest.raises(SystemExit):
        tape.main(["--interval-seconds", "600", "--once"])


def test_provider_symbol_casing_is_preserved(conn):
    """HL symbols are case-sensitive: `candleSnapshot('kPEPE')` returns candles, `'KPEPE'` 500s.
    Uppercasing the universe silently deletes the largest meme perps from every downstream study.
    """
    meta = {"universe": [{"name": "kPEPE"}, {"name": "kBONK"}, {"name": "KAITO"}]}
    ctx = {"markPx": "1", "openInterest": "1"}
    tape.tick_once(conn, now_ms=1_000, fetch=lambda: (meta, [ctx, ctx, ctx]))
    stored = {r[0] for r in conn.execute("SELECT coin FROM hl_squeeze_marks")}
    assert stored == {"kPEPE", "kBONK", "KAITO"}
