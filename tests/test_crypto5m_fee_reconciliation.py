"""Phase 1B Deliverable #2 — fee-source reconciliation (CLOB canonical, WS secondary, Gamma/last-trade
diagnostic). Fail-closed: a usable rate originates ONLY from a fee descriptor; raw config integers
(takerBaseFee=1000) and realized fee_rate_bps only confirm/conflict, never originate a rate.

These tests are RED until reconcile_fee_sources / parse_ws_fee_schedule / taker_fee exist. They must NOT
touch the legacy decode_fee vocabulary (covered by tests/test_crypto5m_fee_model.py).
"""
import pytest

from sniper.algos.polymarket_crypto5m import fee_model


# ---- parse_ws_fee_schedule -------------------------------------------------

def test_ws_fee_schedule_parse_extracts_descriptor():
    msg = {
        "fees_enabled": True,
        "taker_base_fee": 1000,
        "fee_schedule": {"rate": 0.001, "exponent": 1.0, "taker_only": True, "rebate_rate": 0.0},
    }
    d = fee_model.parse_ws_fee_schedule(msg)
    assert d["rate"] == 0.001
    assert d["exponent"] == 1.0
    assert d["taker_only"] is True
    assert d["rebate_rate"] == 0.0
    assert d["fees_enabled"] is True


def test_ws_fee_schedule_parse_none_on_garbage():
    assert fee_model.parse_ws_fee_schedule(None) is None
    assert fee_model.parse_ws_fee_schedule([]) is None


# ---- reconcile_fee_sources: descriptor-based (known) -----------------------

def test_clob_descriptor_rate_is_known_from_clob():
    clob = {"fd": {"rate": 0.001, "exponent": 1.0, "taker_only": True}}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] == "known"
    assert r["fee_source"] == "clob_market_info"
    assert r["fee_rate"] == 0.001
    assert r["fee_exponent"] == 1.0
    assert r["taker_only"] is True
    assert fee_model.is_known(r) is True


def test_clob_and_ws_agree_is_reconciled():
    clob = {"fd": {"rate": 0.001}}
    ws = {"fee_schedule": {"rate": 0.001}}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob,
                                        ws_fee_schedule=fee_model.parse_ws_fee_schedule(ws))
    assert r["fee_status"] == "known"
    assert r["fee_source"] == "reconciled"
    assert r["fee_rate"] == 0.001


def test_clob_and_ws_disagree_is_conflict():
    clob = {"fd": {"rate": 0.001}}
    ws = {"fee_schedule": {"rate": 0.002}}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob,
                                        ws_fee_schedule=fee_model.parse_ws_fee_schedule(ws))
    assert r["fee_status"] == "conflict"
    assert r["fee_rate"] is None
    assert fee_model.is_known(r) is False


def test_ws_only_descriptor_is_known_from_ws():
    ws = {"fee_schedule": {"rate": 0.0005}}
    r = fee_model.reconcile_fee_sources(condition_id="c1",
                                        ws_fee_schedule=fee_model.parse_ws_fee_schedule(ws))
    assert r["fee_status"] == "known"
    assert r["fee_source"] == "websocket_fee_schedule"
    assert r["fee_rate"] == 0.0005


# ---- reconcile_fee_sources: the documented 1000-vs-0 finding ----------------

def test_config_nonzero_no_observation_is_missing():
    # CLOB flat config says fee-enabled (takerBaseFee=1000) but no descriptor and no realized trade to
    # validate -> params unavailable -> missing (blocked). NEVER decode 1000 as a rate.
    clob = {"taker_base_fee": 1000, "fees_enabled": True}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] == "missing"
    assert r["fee_rate"] is None
    assert fee_model.is_known(r) is False


def test_config_nonzero_observed_zero_is_conflict():
    # The empirical finding: config says 1000/enabled, realized fee_rate_bps == 0 -> sources disagree.
    clob = {"taker_base_fee": 1000, "fees_enabled": True}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob, observed_fee_rate_bps=0)
    assert r["fee_status"] == "conflict"
    assert r["fee_rate"] is None


def test_raw_1000_never_interpreted_as_rate():
    # Across every code path, a raw 1000 must never become a fee_rate of 1000, 0.1, or 0.001.
    clob = {"taker_base_fee": 1000, "fees_enabled": True}
    for kwargs in ({}, {"observed_fee_rate_bps": 0}, {"observed_fee_rate_bps": 50}):
        r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob, **kwargs)
        assert r["fee_rate"] in (None,)  # only None until a descriptor or agreeing observation exists


# ---- reconcile_fee_sources: genuine no-fee / disabled -----------------------

def test_fees_disabled_is_known_zero():
    clob = {"fees_enabled": False, "taker_base_fee": 1000}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] == "known"
    assert r["fee_rate"] == 0.0


def test_config_zero_is_known_zero():
    clob = {"fees_enabled": True, "taker_base_fee": 0}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] == "known"
    assert r["fee_rate"] == 0.0


# ---- reconcile_fee_sources: unavailable / unsupported -----------------------

def test_no_sources_is_missing_unavailable():
    r = fee_model.reconcile_fee_sources(condition_id="c1")
    assert r["fee_status"] == "missing"
    assert r["fee_source"] == "unavailable"
    assert r["fee_rate"] is None


def test_unsupported_fee_type():
    clob = {"fd": {"rate": 0.001}, "fee_type": "dynamic_amm"}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] == "unsupported"
    assert r["fee_rate"] is None
    assert fee_model.is_known(r) is False


def test_bps_shaped_descriptor_rate_is_rejected_not_decoded():
    # If fd.rate arrives bps-shaped (e.g. 10), _sane_rate rejects it -> never decoded as a 1000% rate.
    clob = {"fd": {"rate": 10}}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] in ("missing", "unsupported")
    assert r["fee_rate"] is None


# ---- lineage / metadata fields ---------------------------------------------

def test_raw_source_payloads_and_metadata_fields_present():
    clob = {"fd": {"rate": 0.001}}
    ws = {"fee_schedule": {"rate": 0.001}}
    gamma = {"feesEnabled": True, "takerBaseFee": 1000, "feeType": "flat"}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob,
                                        ws_fee_schedule=fee_model.parse_ws_fee_schedule(ws),
                                        gamma=gamma, observed_fee_rate_bps=10)
    assert r["rounding_precision"] == 5
    assert "decoder_version" in r
    assert set(r["raw_source_payloads"].keys()) >= {"clob", "ws", "gamma", "last_trade_fee_rate_bps"}
    assert r["raw_source_payloads"]["clob"] == clob


def test_stale_is_missing_with_caveat():
    clob = {"fd": {"rate": 0.001}}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob,
                                        fetched_ts=1000, now_ts=1000 + 10_000, max_age_s=3600)
    assert r["fee_status"] == "missing"
    assert r.get("caveat") == "stale"


# ---- exact 5-decimal fee rounding ------------------------------------------

def test_taker_fee_rounds_to_five_decimals():
    # fee = shares * fee_rate * price * (1-price), rounded to 5 dp.
    # shares=7, rate=0.001, price=0.37 -> 7*0.001*0.37*0.63 = 0.00163170 -> 0.00163
    fee = fee_model.taker_fee(shares=7, fee_rate=0.001, price=0.37)
    assert fee == round(7 * 0.001 * 0.37 * 0.63, 5)
    assert fee == 0.00163


def test_taker_fee_zero_rate_is_zero():
    assert fee_model.taker_fee(shares=100, fee_rate=0.0, price=0.5) == 0.0


# ---- review fixes (adversarial pass) ---------------------------------------

def test_nan_observation_is_conflict_not_known():
    # a NaN realized fee must NOT be read as agreement (NaN comparisons are all False) -> fail closed
    clob = {"fd": {"rate": 0.001}}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob,
                                        observed_fee_rate_bps=float("nan"))
    assert r["fee_status"] == "conflict"
    assert r["fee_rate"] is None


def test_ws_nan_observation_is_conflict():
    ws = fee_model.parse_ws_fee_schedule({"fee_schedule": {"rate": 0.001}})
    r = fee_model.reconcile_fee_sources(condition_id="c1", ws_fee_schedule=ws,
                                        observed_fee_rate_bps=float("nan"))
    assert r["fee_status"] == "conflict"


def test_fees_disabled_with_unknown_fee_type_is_known_zero():
    # a fee-DISABLED market has no fee model to be 'unsupported' -> known 0.0, not unsupported
    clob = {"fees_enabled": False, "fee_type": "dynamic_amm"}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] == "known" and r["fee_rate"] == 0.0


def test_non_unit_exponent_fails_closed():
    # we only model fee = rate*p*(1-p) (exponent 1); a descriptor exponent != 1 must NOT be 'known'
    clob = {"fd": {"rate": 0.001, "exponent": 2.0}}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] != "known" and r["fee_rate"] is None


def test_unit_and_absent_exponent_still_known():
    assert fee_model.reconcile_fee_sources(
        condition_id="c1", clob={"fd": {"rate": 0.001, "exponent": 1.0}})["fee_status"] == "known"
    assert fee_model.reconcile_fee_sources(
        condition_id="c1", clob={"fd": {"rate": 0.001}})["fee_status"] == "known"


def test_descriptor_short_keys_r_e_to():
    # CLOB docs use the short fd form {r, e, to}
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob={"fd": {"r": 0.001, "e": 1.0, "to": True}})
    assert r["fee_status"] == "known" and r["fee_rate"] == 0.001 and r["taker_only"] is True


def test_crypto_fees_v2_camel_fee_schedule_is_known_when_descriptor_supported():
    # Recorded BTC 5m Gamma payloads use feeType=crypto_fees_v2 and camelCase feeSchedule.
    clob = {
        "feesEnabled": True,
        "feeType": "crypto_fees_v2",
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True, "rebateRate": 0.2},
    }
    r = fee_model.reconcile_fee_sources(condition_id="c1", clob=clob)
    assert r["fee_status"] == "known"
    assert r["fee_source"] == "clob_market_info"
    assert r["fee_rate"] == 0.07
    assert r["fee_exponent"] == 1
    assert r["taker_only"] is True


def test_ws_only_zero_fee_is_known_zero():
    ws = fee_model.parse_ws_fee_schedule({"taker_base_fee": 0, "fees_enabled": True})
    r = fee_model.reconcile_fee_sources(condition_id="c1", ws_fee_schedule=ws)
    assert r["fee_status"] == "known" and r["fee_rate"] == 0.0


def test_fees_enabled_false_string_is_disabled():
    r = fee_model.reconcile_fee_sources(condition_id="c1",
                                        clob={"fees_enabled": "false", "taker_base_fee": 1000})
    assert r["fee_status"] == "known" and r["fee_rate"] == 0.0


# ---- persistence (additive columns + reconciliation writer) ----------------

def _conn(tmp_path):
    import os
    from sniper.algos.polymarket_crypto5m import persistence as P, sim_persistence as SP
    conn = P.connect(os.path.join(str(tmp_path), "f.sqlite"))
    P.ensure_schema(conn)
    SP.ensure_sim_schema(conn)
    return conn


def test_fee_config_reconciliation_columns_present(tmp_path):
    conn = _conn(tmp_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(crypto5m_fee_configs)").fetchall()}
    assert {"fee_source", "fee_exponent", "taker_only", "rounding_precision",
            "raw_source_payloads"} <= cols


def test_fee_config_columns_migrated_onto_legacy_table(tmp_path):
    # A pre-1B DB has the old fee_configs schema; ensure_sim_schema must ALTER in the new columns.
    import os
    from sniper.algos.polymarket_crypto5m import persistence as P, sim_persistence as SP
    conn = P.connect(os.path.join(str(tmp_path), "legacy.sqlite"))
    conn.executescript(
        """CREATE TABLE crypto5m_fee_configs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, condition_id TEXT, fetched_ts INTEGER,
              fees_enabled INTEGER, taker_base_fee REAL, fee_rate REAL, fee_status TEXT,
              observed_fee_rate_bps REAL, decoder_version TEXT, source TEXT, raw_payload TEXT,
              superseded INTEGER DEFAULT 0, persist_ts INTEGER);""")
    conn.commit()
    SP.ensure_sim_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(crypto5m_fee_configs)").fetchall()}
    assert "fee_source" in cols and "raw_source_payloads" in cols


def test_record_fee_reconciliation_roundtrip(tmp_path):
    import json
    from sniper.algos.polymarket_crypto5m import sim_persistence as SP
    conn = _conn(tmp_path)
    clob = {"fd": {"rate": 0.001, "exponent": 1.0, "taker_only": True}, "fees_enabled": True}
    fee = fee_model.reconcile_fee_sources(condition_id="0xc", clob=clob, fetched_ts=10)
    rid = SP.record_fee_reconciliation(conn, fee, now_ts=99)
    row = conn.execute(
        """SELECT condition_id, fee_rate, fee_status, fee_source, fee_exponent, taker_only,
                  rounding_precision, raw_source_payloads FROM crypto5m_fee_configs WHERE id=?""",
        (rid,)).fetchone()
    assert row[0] == "0xc"
    assert row[1] == 0.001
    assert row[2] == "known"
    assert row[3] == "clob_market_info"
    assert row[4] == 1.0
    assert row[5] == 1                              # taker_only stored as int
    assert row[6] == 5
    assert json.loads(row[7])["clob"] == clob


# ---- Phase 1C: canonical fee synthesis (GPT-Pro statuses) -------------------

def test_synthesis_missing_when_no_decodable_descriptor():
    # NO descriptor at all (canonical None) -> missing, blocked. (The CURRENT live state actually HAS a
    # Gamma feeSchedule 0.07, which synthesizes to conflict vs realized 0 — see the conflict tests.)
    s = fee_model.synthesize_fee_status(fees_enabled=True, canonical_rate=None, ws_rate=None,
                                        observed_fee_rate_bps=0)
    assert s["fee_status"] == "missing" and s["fee_rate"] is None


def test_synthesis_present_but_invalid_realized_fails_closed():
    # a present-but-unparseable / non-finite realized value can't CONFIRM the descriptor -> conflict
    for bad in (float("nan"), float("inf"), "bad"):
        s = fee_model.synthesize_fee_status(fees_enabled=True, canonical_rate=0.07,
                                            observed_fee_rate_bps=bad)
        assert s["fee_status"] == "conflict" and s["fee_rate"] is None
        assert fee_model.is_known(s) is False


def test_synthesis_disabled_is_known_zero():
    s = fee_model.synthesize_fee_status(fees_enabled=False)
    assert s["fee_status"] == "known" and s["fee_rate"] == 0.0


def test_synthesis_canonical_and_ws_agree_is_reconciled():
    # gate-compatible: fee_status="known" (is_known True) + confidence carries the reconciliation
    s = fee_model.synthesize_fee_status(fees_enabled=True, canonical_rate=0.07, ws_rate=0.07)
    assert s["fee_status"] == "known" and s["fee_rate"] == 0.07 and s["confidence"] == "reconciled"
    assert fee_model.is_known(s) is True


def test_synthesis_canonical_only_has_confidence_and_caveat():
    s = fee_model.synthesize_fee_status(fees_enabled=True, canonical_rate=0.07, ws_rate=None)
    assert s["fee_status"] == "known" and s["confidence"] == "canonical_only" and s.get("caveat")


def test_synthesis_canonical_vs_ws_conflict():
    s = fee_model.synthesize_fee_status(fees_enabled=True, canonical_rate=0.07, ws_rate=0.05)
    assert s["fee_status"] == "conflict" and s["fee_rate"] is None


def test_synthesis_descriptor_vs_realized_zero_is_conflict():
    # the LIVE BTC 5m state: Gamma feeSchedule rate=0.07 but realized fee_rate_bps=0 -> conflict, blocked
    s = fee_model.synthesize_fee_status(fees_enabled=True, canonical_rate=0.07, observed_fee_rate_bps=0)
    assert s["fee_status"] == "conflict" and s["fee_rate"] is None
    assert fee_model.is_known(s) is False


def test_synthesis_descriptor_confirmed_by_realized_is_known():
    s = fee_model.synthesize_fee_status(fees_enabled=True, canonical_rate=0.07, observed_fee_rate_bps=700)
    assert s["fee_status"] == "known" and s["fee_rate"] == 0.07   # 700 bps == 0.07 confirms


def test_fee_per_share_table_matches_curve():
    tbl = fee_model.fee_per_share_table(0.07)
    assert tbl[0.50] == round(0.07 * 0.50 * 0.50, 5)        # 0.0175
    assert tbl[0.10] == round(0.07 * 0.10 * 0.90, 5)        # 0.0063
    assert set(tbl) == {0.10, 0.25, 0.50, 0.75, 0.90}
