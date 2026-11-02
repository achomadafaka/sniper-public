"""Coin Radar Stage 3: LLM triage — fact sheet, verdict validation, persistence, top-N bound."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_radar_triage as T


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _rows(n=3):
    return [{"network_id": "solana", "mint": f"M{i}", "ticker": f"T{i}", "market_cap": 1e6 * i,
             "liquidity": 1e5 * i, "volume_24h": 1e5 * i, "buyers": 10 * i, "armed_buyers": i,
             "age_s": 3600.0 * i, "source": "wallet"} for i in range(1, n + 1)]


def test_build_fact_sheet_shape():
    c = _c()
    fs = T.build_fact_sheet(c, _rows(1)[0])
    assert fs["mint"] == "M1" and fs["armed_buyers"] == 1 and fs["age_hours"] == 1.0
    assert "armed_handles" in fs and fs["volume_24h"] == 1e5


def test_build_fact_sheet_preserves_unknown_age():
    c = _c()
    row = _rows(1)[0]
    row["age_s"] = None
    assert T.build_fact_sheet(c, row)["age_hours"] is None


def test_triage_persists_valid_verdicts_and_drops_junk():
    c = _c()
    rows = _rows(3)
    llm = lambda sheets: [
        {"mint": "M1", "verdict": "Promising", "reason": "3 sharp buyers, real liq"},   # case-normalized
        {"mint": "M2", "verdict": "skip", "reason": "thin"},
        {"mint": "M3", "verdict": "banana", "reason": "invalid verdict -> dropped"},
        {"mint": "GHOST", "verdict": "promising", "reason": "unknown mint -> dropped"},
    ]
    res = T.triage(c, rows, llm_fn=llm, now_iso="2026-07-19T00:00:00+00:00")
    assert res["n"] == 2 and res["llm"] == "ok"
    tm = m.radar_triage_map(c)
    assert tm[("solana", "M1")]["verdict"] == "promising"
    assert tm[("solana", "M2")]["verdict"] == "skip"
    assert ("solana", "M3") not in tm and ("solana", "GHOST") not in tm


def test_triage_distinguishes_unconfigured_from_provider_silence(monkeypatch):
    """A provider that answered with nothing is not the same as no provider at all.

    Collapsing both into "unconfigured" meant a 401 or a malformed response reported
    itself as a feature nobody had turned on, so a broken triage looked identical to a
    disabled one."""
    c = _c()
    # Provider was called and returned nothing.
    res = T.triage(c, _rows(2), llm_fn=lambda sheets: [], now_iso="2026-07-19T00:00:00+00:00")
    assert res["n"] == 0 and res["llm"] == "no_verdicts"
    assert m.radar_triage_map(c) == {}

    # No key at all: the default provider declares itself unconfigured.
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = T.triage(c, _rows(2), now_iso="2026-07-19T00:00:00+00:00")
    assert res["n"] == 0 and res["llm"] == "unconfigured"
    assert res["error"]
    assert m.radar_triage_map(c) == {}


def test_triage_reports_a_failing_provider_as_an_error_not_as_absent():
    c = _c()

    def boom(sheets):
        raise T.TriageProviderError("grok-2-latest: HTTPError: 401")

    res = T.triage(c, _rows(2), llm_fn=boom, now_iso="2026-07-19T00:00:00+00:00")
    assert res["n"] == 0 and res["llm"] == "provider_error"
    assert "401" in res["error"]
    assert m.radar_triage_map(c) == {}


def test_triage_respects_top_n():
    c = _c()
    seen = {}
    def llm(sheets):
        seen["count"] = len(sheets)
        return []
    T.triage(c, _rows(10), llm_fn=llm, top_n=4, now_iso="2026-07-19T00:00:00+00:00")
    assert seen["count"] == 4


def test_triage_keys_verdicts_by_network_and_mint():
    c = _c()
    rows = [
        {**_rows(1)[0], "network_id": "8453", "mint": "0xSAME"},
        {**_rows(1)[0], "network_id": "4663", "mint": "0xSAME"},
    ]
    llm = lambda sheets: [
        {"network_id": "8453", "mint": "0xSAME", "verdict": "watch", "reason": "base"},
        {"network_id": "4663", "mint": "0xSAME", "verdict": "skip", "reason": "robinhood"},
    ]

    res = T.triage(c, rows, llm_fn=llm, now_iso="2026-07-19T00:00:00+00:00")

    assert {(v["network_id"], v["verdict"]) for v in res["verdicts"]} == {
        ("8453", "watch"), ("4663", "skip"),
    }


def test_triage_rejects_ambiguous_or_malformed_llm_items():
    c = _c()
    rows = [
        {**_rows(1)[0], "network_id": "8453", "mint": "0xSAME"},
        {**_rows(1)[0], "network_id": "4663", "mint": "0xSAME"},
    ]
    llm = lambda sheets: [
        None,
        "not-an-object",
        {"network_id": "8453", "mint": "0xSAME", "verdict": ["watch"]},
        {"mint": "0xSAME", "verdict": "promising", "reason": "ambiguous chain"},
        {"network_id": "8453", "mint": "0xSAME", "verdict": "watch", "reason": ["bad type"]},
    ]

    res = T.triage(c, rows, llm_fn=llm, now_iso="2026-07-19T00:00:00+00:00")

    assert res["n"] == 1
    assert res["verdicts"][0]["network_id"] == "8453"
    assert isinstance(res["verdicts"][0]["reason"], str)


def test_triage_map_roundtrip_and_upsert():
    c = _c()
    m.upsert_radar_triage(c, [{"network_id": "solana", "mint": "X", "verdict": "watch", "reason": "r"}],
                          "2026-07-19T00:00:00+00:00")
    assert m.radar_triage_map(c)[("solana", "X")]["verdict"] == "watch"
    m.upsert_radar_triage(c, [{"network_id": "solana", "mint": "X", "verdict": "promising", "reason": "r2"}],
                          "2026-07-19T00:05:00+00:00")
    tm = m.radar_triage_map(c)
    assert tm[("solana", "X")]["verdict"] == "promising"   # upsert refreshed, one row
