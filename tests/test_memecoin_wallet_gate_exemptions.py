"""A wallet may be excused from a named SELECTION gate. Never from a safety gate.

Operator order, 2026-08-28. Trigger: "disable volume floor for bigwarzeth" was not executable —
no per-wallet machinery existed, so the stopgap was a GLOBAL fail-open on the m5 floor. This makes
the surgical version real so the global stopgap can be reverted.

The exemptible set is exactly the families a manual entry already bypasses, which is the same
selection-versus-safety line drawn everywhere else: a gate judging whether the coin is a GOOD BUY
may be waived for a trusted wallet; a gate protecting the capital may not.
"""
import pytest
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_refusal_capture import classify_gate

NOW = "2026-08-28T12:00:00+00:00"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    c.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES('w1','bigwarzeth')")
    c.commit()
    return c


# ------------------------------------------------------------------ the taxonomy is shared

@pytest.mark.parametrize("reason,family", [
    ("m5 volume $828 under floor $99855 (m5_turnover_under_floor, revival guard)",
     "m5_turnover_under_floor"),
    ("coin is 2.6h old (ceiling 1.0h) — no-movement class", "coin_age_ceiling"),
    ("coin band is not reservable: 'nano'", "coin_band_unreservable"),
    ("sell share 78% of m5 txns over 20 trades (cap 60%)", "sell_share"),
    ("losing re-entry: this coin lost real money", "losing_reentry"),
    ("source buy $0 (0.000%) under mcap floor 0.500% ($500)", "source_buy_floor"),
    ("source buy $99 under unknown-mcap floor $100", "source_buy_floor"),
])
def test_every_exemptible_gate_is_nameable_from_its_own_refusal(reason, family):
    """One vocabulary across exemptions, refusal reasons and capture grading."""
    assert classify_gate(reason) == family
    assert family in m.EXEMPTIBLE_GATE_FAMILIES


def test_a_contract_failure_that_embeds_the_band_text_is_not_a_coin_band_refusal():
    """THE ANCHOR IS LOAD-BEARING. Live rows carry both strings; a substring matcher would file
    contract failures under a selection family and make exempted-entry outcomes unattributable."""
    assert classify_gate("coin band is not reservable: 'micro'") == "coin_band_unreservable"
    assert classify_gate("invalid_funded_contract: coin band is not reservable: 'micro'") is None


def test_the_sell_share_allow_paths_are_not_refusals():
    """Both allow and refuse begin "sell share ", so the matcher keys on the refusal's own shape."""
    assert classify_gate("sell share 78% of m5 txns over 20 trades (cap 60%)") == "sell_share"
    for allowed in ("sell share gate off",
                    "sell share 78% of only 3 m5 txns (floor 8, not evaluated)",
                    "sell share 12% of m5 txns (cap 60%)"):
        assert classify_gate(allowed) is None


# ------------------------------------------------------------------ grant, revoke, audit

def test_grant_and_revoke_round_trip_with_an_audit_row(conn):
    m.set_wallet_gate_exemption(conn, "w1", "m5_turnover_under_floor", active=True,
                                actor="operator", reason="bigwarzeth", now_iso=NOW)
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset({"m5_turnover_under_floor"})
    m.set_wallet_gate_exemption(conn, "w1", "m5_turnover_under_floor", active=False,
                                actor="operator", reason="done", now_iso=NOW)
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset()
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM memecoin_wallet_approvals ORDER BY id")]
    assert actions == ["exempt_grant", "exempt_revoke"]


def test_a_safety_gate_cannot_be_granted(conn):
    for safety in ("execution_premium", "route_over_mid", "not_a_real_gate"):
        with pytest.raises(ValueError):
            m.set_wallet_gate_exemption(conn, "w1", safety, active=True, actor="op",
                                        reason="x", now_iso=NOW)


def test_a_hand_written_safety_row_is_inert(conn):
    """Rule 6 structurally rather than by a guard: the reader filters against the exemptible set,
    so a row naming a safety gate is never returned and nothing can act on it."""
    conn.execute("INSERT INTO memecoin_wallet_gate_exemptions"
                 "(source_user_id, gate_family, active) VALUES('w1','route_over_mid',1)")
    conn.commit()
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset()


def test_an_unknown_family_never_fails_open_across_a_real_one(conn):
    conn.execute("INSERT INTO memecoin_wallet_gate_exemptions"
                 "(source_user_id, gate_family, active) VALUES('w1','',1)")
    m.set_wallet_gate_exemption(conn, "w1", "sell_share", active=True, actor="op",
                                reason="x", now_iso=NOW)
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset({"sell_share"})


def test_an_unreadable_table_grants_nothing(conn):
    """Fail CLOSED: if the lookup raises, every gate applies."""
    conn.execute("DROP TABLE memecoin_wallet_gate_exemptions")
    conn.commit()
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset()


def test_no_wallet_grants_nothing(conn):
    assert m.wallet_gate_exemptions(conn, None) == frozenset()


# ------------------------------------------------------------------ exclusion clears

def test_exclusion_clears_every_exemption(conn):
    m.set_wallet_gate_exemption(conn, "w1", "sell_share", active=True, actor="op",
                                reason="x", now_iso=NOW)
    m.set_wallet_excluded(conn, "w1", True, actor="op", now_iso=NOW)
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset()


def test_re_inclusion_does_not_restore_them(conn):
    """An excluded wallet carries no waiver into a future re-arm; the re-arm decides fresh."""
    m.set_wallet_gate_exemption(conn, "w1", "sell_share", active=True, actor="op",
                                reason="x", now_iso=NOW)
    m.set_wallet_excluded(conn, "w1", True, actor="op", now_iso=NOW)
    m.set_wallet_excluded(conn, "w1", False, actor="op", now_iso=NOW)
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset()


def test_clearing_also_removes_an_inert_row(conn):
    """Validation belongs on the GRANT path only. Routing the clear through the validating setter
    made it raise on a stored safety-gate row, which would have left it behind forever."""
    conn.execute("INSERT INTO memecoin_wallet_gate_exemptions"
                 "(source_user_id, gate_family, active) VALUES('w1','route_over_mid',1)")
    conn.commit()
    assert m.clear_wallet_gate_exemptions(conn, "w1", actor="op", now_iso=NOW) == 1
    left = conn.execute("SELECT COUNT(*) FROM memecoin_wallet_gate_exemptions "
                        "WHERE source_user_id='w1' AND active=1").fetchone()[0]
    assert left == 0


# ------------------------------------------------------------------ attribution

def test_exemptions_applied_is_its_own_column_not_the_contract_blob():
    """classification_json is hashed into the funded contract: adding a field there would change
    contract bytes and every pinned prior release would fail to reconcile positions this one
    wrote. A column also makes "how do exempted entries perform" a plain query."""
    import sqlite3 as _s
    c = _s.connect(":memory:")
    m._ensure_memecoin_tables_in_transaction(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_entry_attempts)")}
    assert "exemptions_applied" in cols


def test_the_attempt_updater_refuses_terminal_and_unknown_columns(conn):
    conn.execute("INSERT INTO memecoin_entry_attempts(attempt_id, decision_started_at, "
                 "observed_at) VALUES('a1',?,?)", (NOW, NOW))
    conn.commit()
    assert m.update_entry_attempt_fields(conn, "a1", exemptions_applied="sell_share") is True
    for bad in ({"terminal_status": "x"}, {"nope": "x"}):
        with pytest.raises(ValueError):
            m.update_entry_attempt_fields(conn, "a1", **bad)


# ------------------------------------------------------------- source-buy floor (2026-08-28)

def test_the_source_buy_floor_matcher_is_anchored_and_shape_specific():
    """Both renderings of the refusal match; a contract-prefixed variant and an unrelated
    "source buy" line do not."""
    assert classify_gate("source buy $0 (0.000%) under mcap floor 0.500% ($500)") == "source_buy_floor"
    assert classify_gate("source buy $99 under unknown-mcap floor $100") == "source_buy_floor"
    assert classify_gate("invalid_funded_contract: source buy $0 under mcap floor 0.500% ($500)") is None
    assert classify_gate("source buy $500 recorded") is None


def test_the_source_buy_floor_is_exemptible_and_consulted(conn):
    m.set_wallet_gate_exemption(conn, "w1", "source_buy_floor", active=True, actor="op",
                                reason="frankdegods 13:58", now_iso=NOW)
    assert "source_buy_floor" in m.wallet_gate_exemptions(conn, "w1")
    # and the gate actually reads it — the family name must appear in the guard's condition
    from _source_guard import code_of
    from sniper.services import memecoin_real_runner as RR
    code = code_of(RR.real_entry_hook)
    line = next(l for l in code.splitlines()
                if "source_reason is not None" in l)
    window = code[code.index(line):code.index(line) + 220]
    # Matched on the family NAME reaching the waiver, not on one spelling of the check. The gate
    # was rewritten from `"x" not in _exempt` to `not _waived("x")` so the runner can record which
    # waivers actually decided; a literal-string guard failed on the rename while the behaviour it
    # guards was intact. Either call form counts — both consult the same resolved set.
    assert ('_waived("source_buy_floor")' in window
            or "_waived('source_buy_floor')" in window
            or '"source_buy_floor" not in _exempt' in window
            or "'source_buy_floor' not in _exempt" in window)


def test_exempting_the_floor_also_waives_the_ABSENT_SIZE_refusal():
    """STATED, NOT INCIDENTAL. The gate fails CLOSED on a missing or zero their_size_usd — absent
    size cannot be told apart from bag work. So the exemption covers both the genuine small-buy
    refusal and every refusal caused by the feed not reporting a size, which is what frankdegods'
    "$0" actually was. Granting it says: copy this wallet even when we cannot see what they staked.
    """
    assert classify_gate("source buy unknown under mcap floor 0.500% ($500)") == "source_buy_floor"
    assert classify_gate("source buy $0 (0.000%) under mcap floor 0.500% ($500)") == "source_buy_floor"


def test_the_panel_warns_that_the_source_buy_exemption_waives_unknown_stakes():
    """The one family whose name understates what it waives. Operator asked for the line; this
    keeps it attached to the family rather than to a position in a list."""
    import pathlib
    app = pathlib.Path("static/app.js").read_text()
    assert "source_buy_floor: 'copies this wallet even when the stake size is unknown'" in app
    # and it must be the ONLY family carrying a warning, or the marking loses its meaning
    import re
    block = app.split("const FAMILY_WARNINGS = {", 1)[1].split("};", 1)[0]
    marked = re.findall(r"^\s*(\w+):", block, re.M)
    assert marked == ["source_buy_floor"], f"unexpected marked families: {marked}"


def test_exemption_panel_uses_the_selected_chain_and_live_armed_route():
    """The modal must load the same chain board that its write path changes."""
    import pathlib

    app = pathlib.Path("static/app.js").read_text()
    panel = app.split("async _walletExemptions(ds)", 1)[1].split("async _realBuy", 1)[0]
    assert "/api/memecoin/armed?chain=" in panel
    assert "this._chainFilter || 'solana'" in panel
    assert "armed-roster" not in panel
    assert "this._chain ||" not in panel


def test_exclusion_and_waiver_clearing_are_one_transaction(monkeypatch):
    """REGRESSION (operator review 2026-08-28). The exclusion committed first and the waiver clear
    ran afterwards inside a swallowing except, so a failure there left the wallet EXCLUDED AND
    STILL EXEMPT — the one combination set_wallet_excluded exists to prevent, and the one nothing
    surfaces. A re-arm would inherit the waiver.
    """
    import sqlite3

    from sniper.persistence import db_memecoin as mm

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    mm.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES ('u','w')")
    mm.set_wallet_gate_exemption(conn, "u", "chase_past_entry", active=True, actor="t",
                                 reason="under test", now_iso="2026-08-28T00:00:00+00:00")
    assert mm.wallet_gate_exemptions(conn, "u")

    def _boom(*a, **k):
        raise RuntimeError("clear failed")

    monkeypatch.setattr(mm, "clear_wallet_gate_exemptions", _boom)
    with pytest.raises(RuntimeError):
        mm.set_wallet_excluded(conn, "u", True, actor="t")

    # NEITHER half landed. The old code left excluded=1 with the waiver still active.
    row = conn.execute(
        "SELECT COALESCE(excluded,0) FROM memecoin_wallets WHERE source_user_id='u'").fetchone()
    assert row[0] == 0
    assert mm.wallet_gate_exemptions(conn, "u")


def test_a_successful_exclusion_still_clears_every_waiver():
    """The transaction must not have bought atomicity by doing nothing."""
    import sqlite3

    from sniper.persistence import db_memecoin as mm

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    mm.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES ('u','w')")
    mm.set_wallet_gate_exemption(conn, "u", "chase_past_entry", active=True, actor="t",
                                 reason="under test", now_iso="2026-08-28T00:00:00+00:00")
    mm.set_wallet_excluded(conn, "u", True, actor="t")

    row = conn.execute(
        "SELECT COALESCE(excluded,0) FROM memecoin_wallets WHERE source_user_id='u'").fetchone()
    assert row[0] == 1
    assert not mm.wallet_gate_exemptions(conn, "u")


# --- chain-wide exemptions (2026-09-13, operator: "remove all the refusal reasons that made money") ---

def test_a_chain_exemption_applies_to_every_wallet_on_that_chain_only(conn, monkeypatch):
    monkeypatch.setenv("MEMECOIN_GATE_EXEMPT_FAMILIES_SOLANA", "chase_first_seen, sell_share")
    assert m.entry_gate_exemptions(conn, "w1", "solana") == {"chase_first_seen", "sell_share"}
    assert m.entry_gate_exemptions(conn, "nobody", "solana") == {"chase_first_seen", "sell_share"}
    assert m.entry_gate_exemptions(conn, "w1", "4663") == frozenset()


def test_a_chain_exemption_adds_to_the_wallet_exemptions(conn, monkeypatch):
    monkeypatch.setenv("MEMECOIN_GATE_EXEMPT_FAMILIES_SOLANA", "sell_share")
    m.set_wallet_gate_exemption(conn, "w1", "reentry_cooldown", active=True, actor="t",
                                reason="r", now_iso=NOW)
    assert m.entry_gate_exemptions(conn, "w1", "solana") == {"sell_share", "reentry_cooldown"}


def test_a_safety_gate_named_chain_wide_is_inert(conn, monkeypatch):
    monkeypatch.setenv("MEMECOIN_GATE_EXEMPT_FAMILIES_SOLANA",
                       "mcap_over_ceiling,roundtrip_cap,execution_premium,kill_switch,sell_share")
    assert m.entry_gate_exemptions(conn, "w1", "solana") == {"sell_share"}


@pytest.mark.parametrize("chain", ["solana", "1", "56", "8453", "5042", "4663"])
def test_first_seen_wallet_allowlist_is_chain_scoped_and_revocable(conn, monkeypatch, chain):
    key = "MEMECOIN_FIRST_SEEN_CHASE_EXEMPT_WALLETS_" + chain.upper()
    monkeypatch.setenv(key, " w1, w2, ,w3 ")
    assert m.entry_gate_exemptions(conn, "w1", chain) == {"chase_first_seen"}
    assert m.entry_gate_exemptions(conn, "w2", chain) == {"chase_first_seen"}
    for uid in (None, "", "w", "w10", "unselected"):
        assert m.entry_gate_exemptions(conn, uid, chain) == frozenset()
    for other_chain in ("unknown", None, "56" if chain != "56" else "8453"):
        assert m.entry_gate_exemptions(conn, "w1", other_chain) == frozenset()
    assert m.wallet_gate_exemptions(conn, "w1") == frozenset()
    assert conn.execute("SELECT COALESCE(real_approved,0) FROM memecoin_wallets "
                        "WHERE source_user_id='w1'").fetchone()[0] == 0
    monkeypatch.setenv(key, "")
    assert m.entry_gate_exemptions(conn, "w1", chain) == frozenset()
