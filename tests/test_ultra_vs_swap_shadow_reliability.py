"""Offline checks for the read-only Jupiter comparison sampler."""

import pytest

import json
import sqlite3
from datetime import datetime, timezone

from scripts.research import ultra_vs_swap_shadow as shadow


def test_missing_report_is_empty(tmp_path, capsys):
    assert shadow.report(tmp_path / "never-created.jsonl") == 0
    assert "pairs 0" in capsys.readouterr().out


def test_unknown_fee_cannot_be_scored_as_a_net_win():
    swap = {"out_atomic": 1000}
    ultra = {"out_atomic": 1010, "fee_bps": None}
    verdict, raw, net = shadow.verdict_with_fee(swap, ultra)
    assert (verdict, net) == ("unknown", None)
    assert raw == pytest.approx(1.0)
    assert shadow.ultra_fee_bps({"feeBps": "10"}) == 10.0


def test_declared_fee_does_not_prove_out_amount_fee_semantics():
    verdict, raw, net = shadow.verdict_with_fee(
        {"out_atomic": 1000}, {"out_atomic": 1010, "fee_bps": 10})
    assert verdict == "unknown"
    assert raw == pytest.approx(1.0)
    assert net is None


def test_non_metis_router_without_route_is_unclassified():
    assert shadow.ultra_route_type("some-new-router", []) == "unknown"


def test_quote_rejects_wrong_identity_and_nonpositive_output(monkeypatch):
    def fake(payload):
        monkeypatch.setattr(shadow, "_get", lambda *_: (payload, None, 1.0))
        return shadow.quote_swap("MINT", 1000000, 300, "fake")

    base = {"inputMint": shadow.USDC, "outputMint": "MINT", "inAmount": "1000000"}
    assert fake({**base, "outAmount": "0"})["error"] == "invalid_quote"
    assert fake({**base, "outputMint": "OTHER", "outAmount": "123"})["error"] == "invalid_quote"
    assert fake({**base, "inAmount": 1000000.5, "outAmount": "123"})["error"] == "invalid_quote"
    assert fake({**base, "outAmount": "123"})["out_atomic"] == 123


def test_ultra_quote_rejects_malformed_optional_metadata(monkeypatch):
    monkeypatch.setattr(shadow, "_get", lambda *_: ({
        "inputMint": shadow.USDC, "outputMint": "MINT", "inAmount": "1000000",
        "outAmount": "123", "routePlan": "bad", "platformFee": "bad"}, None, 1.0))
    assert shadow.quote_ultra("MINT", 1000000, 300, "fake")["error"] == "invalid_quote"


def test_research_key_matching_funded_key_refuses_before_sampling(tmp_path, monkeypatch):
    monkeypatch.setenv(shadow.KEY_VAR, "same-key")
    monkeypatch.setenv("MEMECOIN_JUPITER_API_KEY", "same-key")
    monkeypatch.setattr(shadow.sys, "argv", ["shadow", "--out", str(tmp_path / "rows.jsonl"),
                                          "--max-pairs", "0"])
    assert shadow.main() == 2
    assert not (tmp_path / "rows.jsonl").exists()


def test_refusal_uses_frozen_approved_clip_and_keeps_zero_unknown():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, mint TEXT, ticker TEXT, "
              "terminal_reason TEXT, decision_started_at TEXT, classification_json TEXT, "
              "funded_contract_json TEXT, network_id TEXT, terminal_status TEXT)")
    for attempt_id, contract in (
        ("approved", {"sizing": {"base_clip_usd": 100, "approved_clip_usd": 75}}),
        ("zero", {"sizing": {"base_clip_usd": 100, "approved_clip_usd": 0}}),
        ("unknown", None),
    ):
        c.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?,?)",
                  (attempt_id, "MINT", "M", "refused", "2026-09-24T10:00:00+00:00",
                   json.dumps({"facts": {"base_clip_usd": 100}}),
                   json.dumps(contract) if contract else None, "solana", "refused"))
    rows = {r["attempt_id"]: r for r in shadow.recent_refusals(c, "2026-09-24", 10)}
    assert rows["approved"]["clip_usd"] == 75
    assert rows["zero"]["clip_usd"] == 0
    assert rows["unknown"]["clip_usd"] is None


def test_skipped_amount_does_not_consume_pair_budget(tmp_path, monkeypatch, capsys):
    out = tmp_path / "rows.jsonl"
    out.write_text('{"event":"unquotable_amount","attempt_id":"old"}\n')
    monkeypatch.setenv(shadow.KEY_VAR, "research-key")
    monkeypatch.setenv("MEMECOIN_JUPITER_API_KEY", "funded-key")
    monkeypatch.setattr(shadow.sys, "argv", ["shadow", "--out", str(out), "--max-pairs", "0"])
    assert shadow.main() == 0
    assert "done: 0 pairs" in capsys.readouterr().out


def test_key_provenance_does_not_claim_unverified_paper_sharing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(shadow.KEY_VAR, "research-key")
    monkeypatch.setenv("MEMECOIN_JUPITER_API_KEY", "funded-key")
    monkeypatch.setenv(shadow.SHARES_KEY_WITH, "different-paper-key")
    monkeypatch.setattr(shadow.sys, "argv", ["shadow", "--out", str(tmp_path / "rows.jsonl"),
                                          "--max-pairs", "0"])
    assert shadow.main() == 0
    assert "shared byte-for-byte" not in capsys.readouterr().out


def test_report_keeps_unknown_router_identity_unknown(tmp_path, capsys):
    out = tmp_path / "rows.jsonl"
    out.write_text(json.dumps({"sampled_at": "2026-09-24T10:00:00+00:00",
                               "verdict": "ultra", "swap": {"latency_ms": 1},
                               "ultra_edge_pct": 1.0,
                               "ultra": {"latency_ms": 1, "route_type": "unknown"}}) + "\n")
    assert shadow.report(out) == 0
    assert "reached an RFQ route 0" not in capsys.readouterr().out


def test_sampler_lookback_uses_corrected_clock(tmp_path, monkeypatch):
    corrected = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(shadow, "real_utc_now", lambda: corrected, raising=False)
    seen = []
    class Done(Exception):
        pass
    def capture(_c, since, _limit):
        seen.append(since)
        raise Done
    monkeypatch.setattr(shadow, "recent_refusals", capture)
    class Conn:
        def close(self):
            pass
    from sniper.persistence import db_core
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda: Conn())
    monkeypatch.setenv(shadow.KEY_VAR, "research-key")
    monkeypatch.setenv("MEMECOIN_JUPITER_API_KEY", "funded-key")
    monkeypatch.setattr(shadow.sys, "argv", ["shadow", "--out", str(tmp_path / "rows.jsonl"),
                                          "--lookback-min", "30", "--max-hours", "1", "--poll-s", "0"])
    with pytest.raises(Done):
        shadow.main()
    assert seen == ["2026-09-24T09:30:00+00:00"]
