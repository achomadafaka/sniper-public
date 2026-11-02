"""Market identity safety: immutable identity, change quarantine, deterministic market selection."""
from __future__ import annotations

import dataclasses
import json
import os

from sniper.algos.polymarket_crypto5m import identity as ID
from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m.models import RuleReason

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")
CODE_V = "test-1"


def _spec(name="event_resolved_up.json"):
    with open(os.path.join(FX, name)) as f:
        spec, _ = MR.parse_event(json.load(f))
    return spec


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "id.sqlite"))
    P.ensure_schema(conn)
    return conn


# --- identity diff --------------------------------------------------------------------------

def test_diff_identity_flags_immutable_changes_only():
    spec = _spec()
    existing = {f: getattr(spec, f) for f in ID.IMMUTABLE_FIELDS}
    existing["closed"] = 1  # operational noise should be ignored
    assert ID.diff_identity(existing, spec) == []     # identical identity
    changed_spec = dataclasses.replace(spec, up_token_id="999", window_start_ts=spec.window_start_ts + 1)
    diffs = ID.diff_identity(existing, changed_spec)
    fields = {d[0] for d in diffs}
    assert fields == {"up_token_id", "window_start_ts"}


def test_operational_fields_match_the_sql_update_set_no_drift():
    # the OPERATIONAL_FIELDS constant must equal the columns the ON CONFLICT DO UPDATE SET updates,
    # and must never overlap immutable fields (this caught a 'slug' divergence).
    import inspect
    import re
    src = inspect.getsource(P.upsert_market)
    set_clause = src.split("DO UPDATE SET", 1)[1].split('"""', 1)[0]
    # operational columns are updated either directly (col=excluded.col) or preservingly
    # (col=COALESCE(excluded.col, col)) — both forms count as updating that column.
    sql_cols = set(re.findall(r"(\w+)=(?:excluded\.|COALESCE\(excluded\.)", set_clause))
    assert sql_cols == set(ID.OPERATIONAL_FIELDS)
    assert not (set(ID.OPERATIONAL_FIELDS) & set(ID.IMMUTABLE_FIELDS))


def test_slug_is_immutable_and_quarantined(tmp_path):
    conn = _conn(tmp_path)
    spec = _spec()
    P.upsert_market(conn, spec, code_version=CODE_V, source_lineage="gamma", now_ts=1)
    tampered = dataclasses.replace(spec, slug="btc-updown-5m-9999999999")
    changes = P.upsert_market(conn, tampered, code_version=CODE_V, source_lineage="gamma", now_ts=2)
    assert any(c[0] == "slug" for c in changes)
    assert P.get_market(conn, spec.condition_id)["slug"] == spec.slug   # not overwritten


def test_operational_change_is_not_an_identity_change():
    spec = _spec()
    existing = {f: getattr(spec, f) for f in ID.IMMUTABLE_FIELDS}
    op_changed = dataclasses.replace(spec, closed=True, accepting_orders=False, price_to_beat=1.0)
    assert ID.diff_identity(existing, op_changed) == []


# --- safe upsert ----------------------------------------------------------------------------

def test_upsert_first_insert_returns_no_changes(tmp_path):
    conn = _conn(tmp_path)
    assert P.upsert_market(conn, _spec(), code_version=CODE_V, source_lineage="gamma", now_ts=1) == []
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_markets").fetchone()[0] == 1


def test_upsert_updates_operational_keeps_identity(tmp_path):
    conn = _conn(tmp_path)
    spec = _spec()
    P.upsert_market(conn, spec, code_version=CODE_V, source_lineage="gamma", now_ts=1)
    # same identity, new operational state (window closed; oracle now present)
    op2 = dataclasses.replace(spec, closed=True, accepting_orders=False, price_to_beat=63456.05)
    changes = P.upsert_market(conn, op2, code_version=CODE_V, source_lineage="gamma", now_ts=2)
    assert changes == []
    row = P.get_market(conn, spec.condition_id)
    assert row["closed"] == 1 and row["price_to_beat"] == 63456.05      # operational updated
    assert row["up_token_id"] == spec.up_token_id                       # identity intact
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_identity_changes").fetchone()[0] == 0


def test_upsert_identity_change_is_quarantined_not_overwritten(tmp_path):
    conn = _conn(tmp_path)
    spec = _spec()
    P.upsert_market(conn, spec, code_version=CODE_V, source_lineage="gamma", now_ts=1)
    tampered = dataclasses.replace(spec, up_token_id="DIFFERENT_TOKEN")
    changes = P.upsert_market(conn, tampered, code_version=CODE_V, source_lineage="gamma", now_ts=2)
    assert ("up_token_id", spec.up_token_id, "DIFFERENT_TOKEN") in changes
    # the stored token id must NOT have been overwritten
    assert P.get_market(conn, spec.condition_id)["up_token_id"] == spec.up_token_id
    # and the change is recorded for review
    rows = conn.execute("SELECT field, old_value, new_value FROM crypto5m_identity_changes").fetchall()
    assert (rows[0]["field"], rows[0]["new_value"]) == ("up_token_id", "DIFFERENT_TOKEN")


# --- deterministic market selection ---------------------------------------------------------

def test_resolver_selects_single_btc_market_among_extras(tmp_path):
    raw = json.load(open(os.path.join(FX, "event_resolved_up.json")))
    btc = raw["markets"][0]
    decoy = dict(btc)
    decoy["outcomes"] = json.dumps(["Yes", "No"])   # not a BTC Up/Down market
    raw["markets"] = [decoy, btc]
    spec, v = MR.parse_event(raw)
    assert v.rule_supported is True and spec is not None  # the real BTC market is selected


def test_resolver_rejects_ambiguous_two_btc_markets():
    raw = json.load(open(os.path.join(FX, "event_resolved_up.json")))
    raw["markets"] = [raw["markets"][0], dict(raw["markets"][0])]  # two BTC Up/Down candidates
    spec, v = MR.parse_event(raw)
    assert spec is None
    assert v.reason is RuleReason.AMBIGUOUS_MARKET
