"""Ops CLI for the HL real leg: settle ambiguous intents, clear the breaker,
inspect the deployment."""
import importlib.util
import sys

from sniper.hyperlive import intel_store as IS
from sniper.hyperlive import real_store as RS


def _load():
    spec = importlib.util.spec_from_file_location("hl_real_resolve",
                                                  "scripts/hl_real_resolve.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hl_real_resolve"] = mod
    spec.loader.exec_module(mod)
    return mod


CLI = _load()


def _conn(tmp_path):
    c = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(c)
    RS.get_or_create_deployment(c, now_ms=1000)
    return c


def test_settle_requires_known_cloid_and_state(tmp_path, capsys):
    c = _conn(tmp_path)
    RS.record_intent(c, cloid="0xa1", deployment_id="d", copy_id=1, leg="open",
                     coin="ZEC", side="long", requested_usd=15.0, requested_sz=0.1,
                     state="ambiguous", reason=None, now_ms=1)
    assert CLI.run(c, ["--settle", "0xa1", "--as", "rejected"]) == 0
    assert RS.intent_for(c, 1, "open")["state"] == "rejected"
    assert CLI.run(c, ["--settle", "0xmissing", "--as", "rejected"]) == 1


def test_settle_as_filled_requires_fill_args(tmp_path):
    c = _conn(tmp_path)
    RS.record_intent(c, cloid="0xa2", deployment_id="d", copy_id=2, leg="open",
                     coin="ZEC", side="long", requested_usd=15.0, requested_sz=0.1,
                     state="ambiguous", reason=None, now_ms=1)
    assert CLI.run(c, ["--settle", "0xa2", "--as", "filled"]) == 1   # px+sz mandatory
    assert CLI.run(c, ["--settle", "0xa2", "--as", "filled",
                       "--px", "100.5", "--sz", "0.1"]) == 0
    row = c.execute("SELECT * FROM hl_real_fills WHERE cloid='0xa2'").fetchone()
    assert row["fill_px"] == 100.5 and row["fill_sz"] == 0.1


def test_clear_breaker(tmp_path):
    c = _conn(tmp_path)
    RS.latch_breaker(c, day_utc="2026-07-18", now_ms=5)
    assert CLI.run(c, ["--clear-breaker"]) == 0
    assert RS.breaker_latched(c) is None


def test_new_deployment_requires_yes(tmp_path):
    c = _conn(tmp_path)
    d1 = RS.get_or_create_deployment(c, now_ms=1000)
    assert CLI.run(c, ["--new-deployment"]) == 1                     # refused without --yes
    assert RS.get_or_create_deployment(c, now_ms=2000) == d1
    assert CLI.run(c, ["--new-deployment", "--yes", "--now-ms", "5000"]) == 0
    d2 = RS.get_or_create_deployment(c, now_ms=9000)
    assert d2 != d1 and d2["epoch_ms"] == 5000


def test_list_runs(tmp_path, capsys):
    c = _conn(tmp_path)
    RS.record_intent(c, cloid="0xa3", deployment_id="d", copy_id=3, leg="open",
                     coin="SOL", side="short", requested_usd=15.0, requested_sz=0.3,
                     state="ambiguous", reason=None, now_ms=1)
    assert CLI.run(c, ["--list"]) == 0
    out = capsys.readouterr().out
    assert "0xa3" in out and "ambiguous" in out
