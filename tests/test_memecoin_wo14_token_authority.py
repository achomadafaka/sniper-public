"""WO-14 — token-authority observation collection (observation-only, zero decision authority).

Reuses the pre-entry Token-2022 parser (no second parser). Per held mint: a deduplicated current
snapshot + an append-only change-event history linked to affected positions. The first valid
observation is BASELINE (never a change); an unreadable response is EVIDENCE_GAP (never a change).
Nothing in the V5 decision/execution path reads this evidence.
"""
import sqlite3

from sniper.execution import solana_token_safety as sts
from sniper.services import memecoin_token_authority as ta


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ta.ensure_token_authority_tables(c)
    return c


def _acct(mint_auth=None, freeze_auth=None, supply="1000", owner="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          fee_bps=None, perm_delegate=None):
    info = {"mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "supply": supply}
    exts = []
    if fee_bps is not None:
        exts.append({"extension": "transferFeeConfig",
                     "state": {
                         "olderTransferFee": {"transferFeeBasisPoints": fee_bps},
                         "newerTransferFee": {"transferFeeBasisPoints": fee_bps},
                         "transferFeeConfigAuthority": None,
                     }})
    if perm_delegate is not None:
        exts.append({"extension": "permanentDelegate", "state": {"delegate": perm_delegate}})
    if exts:
        info["extensions"] = exts
    return {"owner": owner, "data": {"parsed": {"info": info}}}


# --- parser reuse -------------------------------------------------------------------------------

def test_parse_token_authority_extracts_all_fields():
    val = _acct(mint_auth="M1", freeze_auth="F1", supply="42",
                owner=sts.TOKEN_2022_PROGRAM, fee_bps=250, perm_delegate="PD1")
    s = sts.parse_token_authority(val)
    assert s["mint_authority"] == "M1" and s["freeze_authority"] == "F1" and s["supply"] == "42"
    assert s["token_program"] == sts.TOKEN_2022_PROGRAM and s["is_token_2022"] is True
    assert s["transfer_fee_bps"] == 250 and s["permanent_delegate"] == "PD1" and s["known"] is True


def test_parse_marks_unknown_when_unparseable():
    s = sts.parse_token_authority({"owner": None, "data": None})
    assert s["known"] is False and s["mint_authority"] is None


def test_entry_gate_transfer_fee_still_works_after_refactor():
    # regression: the pre-entry gate's transfer-fee check is unchanged (reused, not duplicated)
    assert sts._has_transfer_fee(_acct(owner=sts.TOKEN_2022_PROGRAM, fee_bps=100)["data"]["parsed"]["info"],
                                 sts.TOKEN_2022_PROGRAM) is True
    assert sts._has_transfer_fee({"extensions": []}, sts.TOKEN_2022_PROGRAM) is False


# --- collection: baseline / change / gap ---------------------------------------------------------

_META = dict(slot=1000, commitment="confirmed", provider_label="op_read_rpc_sol",
             observed_at="2026-07-25T00:00:00+00:00", raw_sha256="h0", parser_version=sts.PARSER_VERSION)


def test_first_observation_is_baseline_not_change():
    c = _conn()
    res = ta.observe_token_authority(c, "MINT", sts.parse_token_authority(_acct(freeze_auth=None)),
                                     position_ids=[7], **_META)
    assert res == "BASELINE"
    events = c.execute("SELECT event_type, position_id FROM memecoin_token_authority_events").fetchall()
    assert [(e["event_type"], e["position_id"]) for e in events] == [("BASELINE", 7)]
    snaps = c.execute("SELECT COUNT(*) FROM memecoin_token_authority_snapshot").fetchone()[0]
    assert snaps == 1


def test_unchanged_reobservation_dedups_snapshot_and_logs_no_change():
    c = _conn()
    snap = sts.parse_token_authority(_acct(freeze_auth=None))
    ta.observe_token_authority(c, "MINT", snap, position_ids=[7], **_META)
    res = ta.observe_token_authority(c, "MINT", snap, position_ids=[7],
                                     **{**_META, "slot": 1001, "raw_sha256": "h1"})
    assert res == "UNCHANGED"
    assert c.execute("SELECT COUNT(*) FROM memecoin_token_authority_snapshot").fetchone()[0] == 1  # deduped
    assert c.execute("SELECT COUNT(*) FROM memecoin_token_authority_events WHERE event_type='CHANGE'"
                     ).fetchone()[0] == 0


def test_newly_held_position_gets_its_own_baseline_on_unchanged_mint():
    c = _conn()
    snap = sts.parse_token_authority(_acct(freeze_auth=None))
    ta.observe_token_authority(c, "MINT", snap, position_ids=[7], **_META)

    res = ta.observe_token_authority(c, "MINT", snap, position_ids=[7, 9],
                                     **{**_META, "slot": 1001, "raw_sha256": "h1"})

    assert res == "UNCHANGED"
    rows = c.execute(
        "SELECT event_type,position_id FROM memecoin_token_authority_events "
        "ORDER BY position_id").fetchall()
    assert [(r["event_type"], r["position_id"]) for r in rows] == [
        ("BASELINE", 7), ("BASELINE", 9)]


def test_authority_change_appends_change_event_linked_to_positions():
    c = _conn()
    ta.observe_token_authority(c, "MINT", sts.parse_token_authority(_acct(freeze_auth=None)),
                               position_ids=[7], **_META)
    # freeze authority becomes live -> a CHANGE, linked to every affected position
    res = ta.observe_token_authority(
        c, "MINT", sts.parse_token_authority(_acct(freeze_auth="F_NEW")),
        position_ids=[7, 9], **{**_META, "slot": 1002, "raw_sha256": "h2"})
    assert res == "CHANGE"
    changes = c.execute("SELECT field, before, after, position_id FROM memecoin_token_authority_events "
                        "WHERE event_type='CHANGE' ORDER BY position_id").fetchall()
    assert {ch["field"] for ch in changes} == {"freeze_authority"}
    assert {ch["position_id"] for ch in changes} == {7, 9}
    assert changes[0]["before"] is None and changes[0]["after"] == "F_NEW"


def test_unreadable_response_is_evidence_gap_not_change():
    c = _conn()
    ta.observe_token_authority(c, "MINT", sts.parse_token_authority(_acct(freeze_auth=None)),
                               position_ids=[7], **_META)
    res = ta.observe_token_authority(c, "MINT", None, position_ids=[7],
                                     **{**_META, "slot": 1003, "raw_sha256": None})
    assert res == "EVIDENCE_GAP"
    assert c.execute("SELECT COUNT(*) FROM memecoin_token_authority_events WHERE event_type='EVIDENCE_GAP'"
                     ).fetchone()[0] == 1
    # the snapshot is NOT overwritten by a gap (still the last KNOWN good)
    row = c.execute("SELECT known FROM memecoin_token_authority_snapshot WHERE mint='MINT'").fetchone()
    assert row["known"] == 1


def test_reads_bounded_to_seven_held_mints():
    assert ta.MAX_HELD_MINT_POLLS == 7
    mints = [f"M{i}" for i in range(20)]
    assert len(ta.bounded_held_mints(mints)) == 7


# --- bounded poll wrapper (injected fetch; NO live calls in tests) --------------------------------

def test_poll_wrapper_is_bounded_and_injectable_baseline_then_gap():
    c = _conn()
    held = {f"M{i}": [i] for i in range(10)}     # 10 held mints -> bounded to 7
    def fetch_ok(mint, commitment):
        return {"value": _acct(freeze_auth=None), "context": {"slot": 5}}, 5
    res = ta.poll_held_mint_authority(c, held, fetch_ok, now_iso="t", provider_label="op")
    assert len(res) == 7 and set(res.values()) == {"BASELINE"}
    # an unreadable fetch on a known mint -> EVIDENCE_GAP, snapshot preserved
    def fetch_gap(mint, commitment):
        return None, None
    res2 = ta.poll_held_mint_authority(c, {"M0": [0]}, fetch_gap, now_iso="t2", provider_label="op")
    assert res2["M0"] == "EVIDENCE_GAP"


def test_collect_once_queries_only_open_real_solana_positions():
    c = _conn()
    c.execute("CREATE TABLE memecoin_positions(id INTEGER, mint TEXT, network_id TEXT, "
              "execution_mode TEXT, is_open INTEGER)")
    c.executemany("INSERT INTO memecoin_positions VALUES(?,?,?,?,?)", [
        (1, "SOL", "solana", "real", 1), (2, "SOL", "solana", "real", 1),
        (3, "EVM", "4663", "real", 1), (4, "PAPER", "solana", "paper", 1),
        (5, "CLOSED", "solana", "real", 0)])
    seen = []
    def fetch(mint, commitment):
        seen.append((mint, commitment))
        return {"value": _acct(), "context": {"slot": 5}}, 5

    result = ta.collect_held_token_authority(
        c, fetch, now_iso="t", provider_label="op", commitment="confirmed")

    assert seen == [("SOL", "confirmed")]
    assert result == {"SOL": "BASELINE"}
    links = c.execute("SELECT position_id FROM memecoin_token_authority_events").fetchall()
    assert [r[0] for r in links] == [1, 2]


# --- zero decision authority ---------------------------------------------------------------------

def test_no_decision_or_execution_path_reads_token_authority_evidence():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "memecoin_token_authority\\|token_authority_events\\|token_authority_snapshot",
         "sniper/services/memecoin_exit_brain.py", "sniper/analysis/exit_flex_v5.py",
         "sniper/services/exit_engine_hot.py", "sniper/services/memecoin_real_runner.py",
         "sniper/execution/"],
        capture_output=True, text=True).stdout
    assert hits == "", f"token-authority evidence must have ZERO decision/exec readers; found:\n{hits}"
