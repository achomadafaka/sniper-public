"""Cohort D history blocks (Gamma shapes 2026-10-28): leak rule, lb/status/unknown mapping, dev vs curve creator, closed reasons, schema migration. Offline, temp stores only."""
import importlib.util
import json
import sqlite3
from pathlib import Path

from sniper.services import memecoin_desk_backfill_history as H
from sniper.services import memecoin_desk_history_facts as F

_hs = importlib.util.spec_from_file_location("bfh_tests", Path(__file__).resolve().parent / "test_desk_backfill_history.py")
HT = importlib.util.module_from_spec(_hs)
_hs.loader.exec_module(HT)
CUT, DEC, PASSP = 2000.0, 2010.0, 1990.0                 # corrected epoch: cut, decision, PASS persist
BT = 1000                                                # the coin's creation block_time
OFF = 0.5


def desk(tmp_path):
    (tmp_path / "backfill").mkdir(parents=True, exist_ok=True)
    con = H.connect(tmp_path / "backfill" / "history.db")
    con.executescript(open(Path(__file__).resolve().parents[1] / "sniper/services/memecoin_desk_g12_intake.py").read().split('_LOG = """')[1].split('"""')[0])
    return tmp_path, con


def coin(con, mint="M1", *, state="known", reason=None, slot=500, bt=BT, creator="CRE", fetched=1500.0, offset=OFF, via="inline"):
    con.execute("insert or replace into coin_creation values(?,?,?,?,?,?,?,?,?,?,?)", (mint, state, reason, "sig", slot if state == "known" else None, bt if state == "known" else None,
                F.WITNESS if state == "known" else None, creator if state == "known" else None, "PAYER", fetched, offset))
    if via:
        con.execute("insert or replace into g12_via values('coin',?,?,?)", (mint, fetched, via))
    con.commit()


def scan(con, creator="CRE", *, fetched, state="complete", pages=2, covered=None, order="asc", offset=OFF, via="inline", launches=()):
    for mint, slot, seen in launches:
        con.execute("insert or ignore into creator_launches values(?,?,?,?,?,?)", (creator, mint, "s" + mint, slot, 900, seen))
    con.execute("insert or replace into creator_scans values(?,?,?,?,?,?,?,?,?,?)", (creator, state, None, pages, 10, len(launches), fetched, covered, order, offset))
    con.execute("insert or ignore into creator_scan_log values(?,?,?,?,?,?,?,?)", (creator, fetched, state, None, pages, covered, order, offset))
    if via:
        con.execute("insert or replace into g12_via values('scan',?,?,?)", (creator, fetched, via))
    con.commit()


def hist(d, **k):
    k.setdefault("market_cut", CUT)
    k.setdefault("host_offset_s", OFF)
    return F.creator_history_fact(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, **k)


def age(d, **k):
    return F.coin_age_fact(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, **k)


# ---- coin age
def test_coin_age_is_the_cut_minus_the_on_chain_creation_with_a_witness(tmp_path):
    d, con = desk(tmp_path)
    coin(con, fetched=1500.0, offset=OFF)
    out = age(d)
    assert out["coin"] == {"state": "known", "reason": None, "basis": "onchain_creation_block_time", "age_s": CUT - BT, "occurred_at": BT, "known_at": 1500.0 - OFF}
    assert out["witness"] == {"signature": "sig", "slot": 500, "witness": "create_instruction_in_first_tx", "source": "helius_history"}


def test_a_coin_fetched_after_the_pass_persist_or_the_decision_is_unknown_never_used(tmp_path):
    d, con = desk(tmp_path)
    coin(con, fetched=PASSP + OFF + 1)                                   # corrected = PASSP + 1: after the PASS stage was persisted
    assert age(d)["coin"]["reason"] == "fetched_after_decision" and age(d)["coin"]["age_s"] is None and age(d)["witness"] is None
    coin(con, fetched=PASSP + OFF)                                       # exactly at the limit: allowed
    assert age(d)["coin"]["state"] == "known"
    assert F.coin_age_fact(d, "M1", sheet_as_of=CUT, decision_at=1000.0, pass_persist_at=PASSP)["coin"]["reason"] == "fetched_after_decision"     # decision earlier than the fetch


def test_the_clock_offset_is_applied_and_a_missing_one_is_clock_unknown_never_zero(tmp_path):
    d, con = desk(tmp_path)
    coin(con, fetched=PASSP + 3.0, offset=5.0)                           # host stamp is after PASSP but corrected it is 2 s before: usable
    assert age(d)["coin"]["state"] == "known" and age(d)["coin"]["known_at"] == PASSP - 2.0
    coin(con, fetched=1500.0, offset=None)
    assert age(d)["coin"]["reason"] == "clock_unknown"


def test_coin_age_unknown_reasons_are_typed(tmp_path):
    d, con = desk(tmp_path)
    assert age(d)["coin"]["reason"] == "not_attempted"                    # history.db exists, no row, no inline log row
    con.execute("insert into g12_inline_log values(?,?,?,?,?,?,?,?,?,?)", (1.0, "d", "M1", "skipped", "knob_off", None, None, 0, 0.0, 0.0))
    con.commit()
    assert age(d)["coin"]["reason"] == "knob_off"                         # the ticket's own inline reason surfaces
    coin(con, state="unknown", reason="first_tx_not_creation")
    assert age(d)["coin"]["reason"] == "first_tx_not_creation"
    coin(con, bt=CUT + 5, fetched=1500.0)
    assert age(d)["coin"]["reason"] == "creation_future"
    coin(con, bt=0, fetched=1500.0)
    assert age(d)["coin"]["reason"] == "block_time_unusable"
    assert F.coin_age_fact(tmp_path / "nowhere", "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP)["coin"]["reason"] == "not_attempted"
    con.close()
    for suffix in ("", "-wal", "-shm"):                                                  # a corrupt FILE, not a stale WAL replaying committed rows
        (tmp_path / "backfill" / ("history.db" + suffix)).unlink(missing_ok=True)
    (tmp_path / "backfill" / "history.db").write_bytes(b"not a database" * 100)
    assert age(d)["coin"]["reason"] == "history_unreadable" and hist(d)["history_reason"] == "history_unreadable"


# ---- creator history
def test_a_complete_scan_gives_a_lower_bound_of_prior_launches_with_none_seen_only_when_complete(tmp_path):
    d, con = desk(tmp_path)
    coin(con, slot=500)
    scan(con, fetched=1500.0, launches=[("A", 100, 1500.0), ("B", 200, 1500.0), ("C", 600, 1500.0)])
    out = hist(d)
    assert out["creator_prior_launches_lb"] == {"lb": 2, "status": "complete", "pages": 2, "source": "create_ix_arg", "reason": None}      # C is after the coin
    assert out["dev_wallet"] == {"value": "CRE", "source": "create_ix_arg"} and out["fetched_at"] == 1500.0 and out["clock_offset_s"] == OFF and out["fetched_via"] == "inline"
    assert out["history_reason"] is None


def test_a_capped_scan_is_complete_only_before_the_slot_it_reached(tmp_path):
    d, con = desk(tmp_path)
    coin(con, slot=500)
    scan(con, fetched=1500.0, state="capped", covered=300, launches=[("A", 100, 1500.0)])
    capped = hist(d)["creator_prior_launches_lb"]
    assert capped["status"] == "capped" and capped["lb"] == 1                      # at least 1; 'none seen' would mean nothing here
    coin(con, slot=250)
    assert hist(d)["creator_prior_launches_lb"]["status"] == "complete"            # the ascending scan already passed this coin
    con.execute("update creator_scan_log set sort_order='desc'")
    con.commit()
    assert hist(d)["creator_prior_launches_lb"]["status"] == "capped"              # a non-ascending scan never claims it


def test_the_earliest_valid_scan_is_chosen_and_a_later_rescan_never_leaks_its_finds(tmp_path):
    d, con = desk(tmp_path)
    coin(con, slot=500)
    scan(con, fetched=1500.0, launches=[("A", 100, 1500.0)])
    scan(con, fetched=1700.0, launches=[("A", 100, 1500.0), ("B", 300, 1700.0)])     # the later scan also saw B
    assert hist(d)["creator_prior_launches_lb"]["lb"] == 1 and hist(d)["fetched_at"] == 1500.0
    assert F.creator_history_fact(d, "M1", sheet_as_of=CUT, decision_at=1550.0, pass_persist_at=1550.0)["creator_prior_launches_lb"]["lb"] == 1


def test_the_unknown_reasons_for_a_creator_scan(tmp_path):
    d, con = desk(tmp_path)
    coin(con, slot=500)
    assert hist(d)["creator_prior_launches_lb"] == {"lb": None, "status": "unknown", "pages": None, "source": "create_ix_arg", "reason": "no_scan"}
    scan(con, fetched=1500.0, state="unknown")
    assert hist(d)["history_reason"] == "unknown_scan"
    con.execute("delete from creator_scan_log")
    scan(con, fetched=BT + 30.0 + OFF)                                              # corrected BT+30 < coin block_time + 60
    assert hist(d)["history_reason"] == "scan_before_coin"
    con.execute("delete from creator_scan_log")
    scan(con, fetched=PASSP + 10 + OFF)                                             # corrected after the PASS persist
    assert hist(d)["history_reason"] == "rescan_after_decision" and hist(d)["creator_prior_launches_lb"]["lb"] is None
    con.execute("delete from creator_scan_log")
    scan(con, fetched=1500.0, offset=None)
    assert hist(d)["history_reason"] == "clock_unknown"
    coin(con, creator=None)
    assert hist(d)["history_reason"] == "creator_unknown"


def test_fetched_via_comes_from_the_scan_and_an_old_row_without_it_is_nightly(tmp_path):
    d, con = desk(tmp_path)
    coin(con)
    scan(con, fetched=1500.0, via="nightly")
    assert hist(d)["fetched_via"] == "nightly"
    con.execute("delete from g12_via")
    con.commit()
    assert hist(d)["fetched_via"] == "nightly"                                           # no via row = a nightly row from before the sibling table


# ---- dev vs curve creator
def test_dev_and_curve_creator_are_two_fields_and_never_inferred_from_each_other(tmp_path):
    d, con = desk(tmp_path)
    ldb = tmp_path / "launches"
    ldb.mkdir()
    import datetime
    day = datetime.datetime.fromtimestamp(CUT, datetime.timezone.utc).date().isoformat()
    l = sqlite3.connect(ldb / (day + ".db"))
    l.execute("create table launches(mint text primary key, curve text, creator text, slot integer, signature text, block_time integer, received_at real, symbol text, name text, dev text, dev_source text)")
    l.execute("insert into launches values('M1','c','CURVE',1,'s',1,1.0,'S','N','DEVW','create_event_user')")
    l.commit()
    l.close()
    out = F.dev_block(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, market_cut=CUT, host_offset_s=OFF)
    assert out == {"curve_creator": "CURVE", "dev": "DEVW", "dev_equals_creator": False, "dev_source": "create_event_user", "curve_creator_source": "launches.creator"}
    l = sqlite3.connect(ldb / (day + ".db"))
    l.execute("update launches set dev=NULL, dev_source=NULL")
    l.commit()
    l.close()
    out = F.dev_block(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, market_cut=CUT, host_offset_s=OFF)
    assert out["dev"] is None and out["dev_equals_creator"] is None and out["curve_creator"] == "CURVE"        # absent side stays absent: the curve creator is not the dev


def test_the_dev_falls_back_to_the_history_create_argument_under_the_leak_rule(tmp_path):
    d, con = desk(tmp_path)
    coin(con, creator="IXARG", fetched=1500.0)
    out = F.dev_block(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, market_cut=CUT, host_offset_s=OFF)
    assert out["dev"] == "IXARG" and out["dev_source"] == "create_ix_arg" and out["curve_creator"] is None and out["dev_equals_creator"] is None
    coin(con, creator="IXARG", fetched=PASSP + OFF + 5)
    assert F.dev_block(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, market_cut=CUT, host_offset_s=OFF)["dev"] is None


def test_the_curve_creator_falls_back_to_the_ps_tape_coin_creator(tmp_path):
    d, con = desk(tmp_path)
    (tmp_path / "pumpswap").mkdir()
    t = sqlite3.connect(tmp_path / "pumpswap" / "tape.db")
    t.execute("create table swaps_ps(pool text, mint text, coin_creator text, received_at real)")
    t.execute("insert into swaps_ps values('P1','M1','PSCREATOR', 100.0)")
    t.commit()
    t.close()
    coin(con, creator="PSCREATOR")
    out = F.dev_block(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, pool="P1", market_cut=CUT, host_offset_s=OFF)
    assert out["curve_creator"] == "PSCREATOR" and out["curve_creator_source"] == "swaps_ps.coin_creator" and out["dev_equals_creator"] is True
    assert hist(d, pool="P1")["curve_creator"] == {"value": "PSCREATOR", "source": "swaps_ps.coin_creator"} and hist(d, pool="P1")["dev_equals_creator"] is True
    assert hist(d)["curve_creator"] == {"value": None, "source": None} and hist(d)["dev_equals_creator"] is None      # no pool: unknown, not guessed


# ---- closed reasons, definition, migration, via
def test_every_reason_the_blocks_can_emit_is_in_the_closed_set_and_the_definition_hash_binds_it(tmp_path):
    d, con = desk(tmp_path)
    seen = set()
    for kw in ({}, {"state": "unknown", "reason": "no_history"}, {"state": "unknown", "reason": "some_new_unlisted_error"}, {"offset": None}, {"bt": CUT + 9}):
        coin(con, **kw)
        seen |= {age(d)["coin"]["reason"], hist(d)["history_reason"]}
    assert seen - {None} <= F.REASONS and "inline_error" in seen                                              # an unlisted reason is mapped, never passed through
    import hashlib
    DEFN = dict(F.DEFINITION)
    assert hashlib.sha256(json.dumps(DEFN, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == F.DEFINITION_SHA256
    DEFN["reasons"] = DEFN["reasons"][1:]
    assert hashlib.sha256(json.dumps(DEFN, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != F.DEFINITION_SHA256


def test_the_new_writer_never_alters_the_nightly_tables_so_the_older_positional_writer_keeps_working(tmp_path):      # Alpha/Delta writer concern, brain trap db80aa04
    (tmp_path / "backfill").mkdir()
    old = sqlite3.connect(tmp_path / "backfill" / "history.db")
    old.executescript("""create table coin_creation(mint text primary key, state text not null, reason text, signature text, slot integer, block_time integer, witness text, creator text, payer text,
      fetched_at real not null, clock_offset_s real);
      create table creator_scans(creator text primary key, state text not null, reason text, pages integer, txs integer, launches integer, fetched_at real not null, covered_slot integer,
      sort_order text not null default 'asc', clock_offset_s real);
      create table creator_scan_log(creator text not null, fetched_at real not null, state text not null, reason text, pages integer, covered_slot integer, sort_order text not null, clock_offset_s real,
      primary key(creator, fetched_at));
      create table creator_launches(creator text not null, mint text not null, signature text not null, slot integer not null, block_time integer not null, seen_at real not null default 0, primary key(creator, mint));
      insert into coin_creation values('M1','known',NULL,'sig',500,1000,'w','CRE','P',1500.0,0.5);
      insert into creator_scan_log values('CRE',1500.0,'complete',NULL,2,NULL,'asc',0.5);""")
    old.commit()
    old.close()
    assert age(tmp_path)["coin"]["state"] == "known" and hist(tmp_path)["fetched_via"] == "nightly"          # the NEW reader reads the OLD schema (no g12_via yet)
    con = H.connect(tmp_path / "backfill" / "history.db")                                                      # the NEW writer opens it
    for t, n in (("coin_creation", 11), ("creator_scans", 10), ("creator_scan_log", 8), ("creator_launches", 6)):
        assert len(list(con.execute("pragma table_info(%s)" % t))) == n                                       # not one column added to a nightly table
    # the OLDER release's literal positional inserts still work against the file the new writer opened
    con.execute("insert or replace into coin_creation values(?,?,?,?,?,?,?,?,?,?,?)", ("M2", "unknown", "no_history", None, None, None, None, None, None, 1600.0, 0.5))
    con.execute("insert or replace into creator_scans values(?,?,?,?,?,?,?,?,?,?)", ("CRE2", "complete", None, 1, 5, 0, 1600.0, None, "asc", 0.5))
    con.execute("insert or ignore into creator_scan_log values(?,?,?,?,?,?,?,?)", ("CRE2", 1600.0, "complete", None, 1, None, "asc", 0.5))
    con.execute("insert or ignore into creator_launches values(?,?,?,?,?,?)", ("CRE2", "X", "s", 1, 1, 1600.0))
    con.commit()


def test_the_fetcher_stores_inline_or_nightly_beside_every_row(tmp_path):
    from sniper.services import memecoin_desk_backfill_ledger as L
    c = HT.cycle(tmp_path)
    ledger = L.Ledger(tmp_path / "credits.db", c, clock=lambda: HT.NOW, costs=HT.COSTS)
    con = H.connect(tmp_path / "h.db")
    for via in ("inline", "nightly"):
        f = H.Fetcher(con, ledger, HT.Fake({"M_" + via: [HT.coin_page("M_" + via, 5, 1_793_100_000)], HT.CR: [HT.full_page([])]}).transport, clock=lambda: HT.NOW, via=via)
        f.fetch_coin("M_" + via)
    assert dict(con.execute("select key, via from g12_via where kind='coin'")) == {"M_inline": "inline", "M_nightly": "nightly"}


def test_the_committed_definition_file_matches_the_module_and_its_sha256(tmp_path):
    import hashlib
    path = Path(__file__).resolve().parents[1] / "docs/audits/frozen/2026-10-28-history-facts-v1-definition.json"
    on_disk = json.loads(path.read_text())
    assert on_disk == F.DEFINITION                                                                              # a drifted reason/rule fails here before any D ticket
    assert hashlib.sha256(json.dumps(on_disk, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == F.DEFINITION_SHA256


def test_the_block_state_as_of_hash_and_field_unknown_follow_amendment_2(tmp_path):
    d, con = desk(tmp_path)
    coin(con, slot=500)
    scan(con, fetched=1500.0, launches=[("A", 100, 1500.0)])
    out = hist(d)
    assert out["state"] == "known" and out["reason"] is None and out["as_of"] == CUT and out["definition_sha256"] == F.DEFINITION_SHA256
    assert out["field_unknown"] == {"curve_creator": "creator_unknown", "create_event_user": "not_attempted"}          # only the genuinely null fields are named
    assert out["create_event_user"] == {"value": None, "source": None}
    con.execute("delete from creator_scan_log")
    con.commit()
    unk = hist(d)
    assert unk["state"] == "unknown" and unk["reason"] == "no_scan" and unk["creator_prior_launches_lb"]["lb"] is None
    assert unk["field_unknown"]["creator_prior_launches_lb"] == "no_scan" and set(unk["field_unknown"].values()) <= F.REASONS


def test_create_event_user_comes_from_launches_dev_and_is_a_third_field(tmp_path):
    import datetime
    d, con = desk(tmp_path)
    coin(con, creator="IXARG")
    scan(con, creator="IXARG", fetched=1500.0)
    ldb = tmp_path / "launches"
    ldb.mkdir()
    day = datetime.datetime.fromtimestamp(CUT, datetime.timezone.utc).date().isoformat()
    l = sqlite3.connect(ldb / (day + ".db"))
    l.execute("create table launches(mint text primary key, curve text, creator text, slot integer, signature text, block_time integer, received_at real, symbol text, name text, dev text, dev_source text)")
    l.execute("insert into launches values('M1','c','CURVE',1,'s',1,1.0,'S','N','EVUSER','create_event_user')")
    l.commit()
    l.close()
    out = hist(d)
    assert out["dev_wallet"]["value"] == "IXARG" and out["curve_creator"]["value"] == "CURVE" and out["create_event_user"] == {"value": "EVUSER", "source": "create_event_user"}
    assert out["dev_equals_creator"] is False                                  # ix arg vs curve creator only; the event user is never substituted
    assert "create_event_user" not in out["field_unknown"] and "curve_creator" not in out["field_unknown"]


def test_a_fetch_after_the_sheet_as_of_is_not_used_either(tmp_path):
    d, con = desk(tmp_path)
    coin(con, fetched=1500.0)
    assert F.coin_age_fact(d, "M1", sheet_as_of=1400.0, decision_at=DEC, pass_persist_at=PASSP)["coin"]["reason"] == "fetched_after_decision"


def test_prior_means_strictly_before_this_coins_creation_slot_never_the_coin_itself_or_later(tmp_path):         # Zeta: exclude the current mint, define 'prior'
    d, con = desk(tmp_path)
    coin(con, slot=500)
    scan(con, fetched=1500.0, launches=[("M1", 500, 1500.0), ("SAMESLOT", 500, 1500.0), ("BEFORE", 499, 1500.0), ("LATE", 501, 1500.0), ("LATER", 900, 1500.0)])
    assert hist(d)["creator_prior_launches_lb"]["lb"] == 1                                                 # only BEFORE (499): the current mint, a same-slot launch and later ones are not prior
    assert "STRICTLY BEFORE" in F.DEFINITION["prior_definition"]


def test_the_policy_entry_is_the_version_and_the_exact_sha_of_the_committed_definition_and_the_accessor_copies(tmp_path):
    import hashlib
    entry = F.policy_entry()
    assert entry == {"version": "history-facts-v1", "definition_sha256": F.DEFINITION_SHA256} and set(entry) == {"version", "definition_sha256"}
    copy = F.definition()
    assert copy == F.DEFINITION and copy is not F.DEFINITION
    copy["reasons"].append("tampered")
    assert "tampered" not in F.DEFINITION["reasons"] and F.policy_entry()["definition_sha256"] == F.DEFINITION_SHA256
    path = Path(__file__).resolve().parents[1] / "docs/audits/frozen/2026-10-28-history-facts-v1-definition.json"
    assert hashlib.sha256(json.dumps(json.loads(path.read_text()), sort_keys=True, separators=(",", ":")).encode()).hexdigest() == entry["definition_sha256"]


# ---- D Amendment 13 (Zeta 9f2bed284 witnesses)
def _launches(tmp_path, *, received, creator="CURVE", dev=None, dev_source=None):
    import datetime
    ldb = tmp_path / "launches"
    ldb.mkdir(exist_ok=True)
    day = datetime.datetime.fromtimestamp(CUT, datetime.timezone.utc).date().isoformat()
    l = sqlite3.connect(ldb / (day + ".db"))
    l.execute("create table if not exists launches(mint text primary key, curve text, creator text, slot integer, signature text, block_time integer, received_at real, symbol text, name text, dev text, dev_source text)")
    l.execute("insert or replace into launches values('M1','c',?,1,'s',1,?,'S','N',?,?)", (creator, received, dev, dev_source))
    l.commit()
    l.close()


def test_a_creation_slot_that_is_missing_or_unusable_is_slot_unusable_for_complete_and_capped_scans_never_zero_never_an_exception(tmp_path):
    for bad in (None, 0, -5):
        for state, covered in (("complete", None), ("capped", 900)):
            d, con = desk(tmp_path / ("%s%s" % (state, bad)))
            coin(con, slot=500)
            con.execute("update coin_creation set slot=?", (bad,))
            scan(con, fetched=1500.0, state=state, covered=covered, launches=[("A", 100, 1500.0)])
            out = hist(d)
            assert out["creator_prior_launches_lb"] == {"lb": None, "status": "unknown", "pages": None, "source": "create_ix_arg", "reason": "slot_unusable"}, (bad, state, out)
            assert out["state"] == "unknown" and out["history_reason"] == "slot_unusable" and out["dev_wallet"]["value"] == "CRE"


def test_a_launches_row_known_after_the_sheet_is_future_source_for_creator_dev_and_the_equality(tmp_path):
    d, con = desk(tmp_path)
    coin(con, creator="IXARG")
    scan(con, creator="IXARG", fetched=1500.0)
    _launches(tmp_path, received=CUT + 100 + OFF, dev="EVUSER", dev_source="create_event_user")       # received 100 s after the sheet (corrected)
    out = hist(d)
    assert out["curve_creator"]["value"] is None and out["create_event_user"]["value"] is None and out["dev_equals_creator"] is None
    assert out["field_unknown"]["curve_creator"] == "future_source" and out["field_unknown"]["create_event_user"] == "future_source"
    blk = F.dev_block(d, "M1", sheet_as_of=CUT, decision_at=DEC, pass_persist_at=PASSP, market_cut=CUT, host_offset_s=OFF)
    assert blk["curve_creator"] is None and blk["dev_equals_creator"] is None and blk["dev"] == "IXARG"            # the history dev stays, the future curve row does not leak
    _launches(tmp_path, received=CUT - 5 + OFF, dev="EVUSER", dev_source="create_event_user")          # received before the sheet: counts
    assert hist(d)["curve_creator"] == {"value": "CURVE", "source": "launches.creator"} and hist(d)["create_event_user"]["value"] == "EVUSER"


def test_a_launches_row_without_a_sampled_offset_is_clock_unknown_never_an_implicit_zero(tmp_path):
    d, con = desk(tmp_path)
    coin(con)
    scan(con, fetched=1500.0)
    _launches(tmp_path, received=CUT - 5)
    out = hist(d, host_offset_s=None)
    assert out["curve_creator"]["value"] is None and out["field_unknown"]["curve_creator"] == "clock_unknown"


def test_a_ps_tape_creator_row_received_after_the_market_cut_is_future_source(tmp_path):
    d, con = desk(tmp_path)
    (tmp_path / "pumpswap").mkdir()
    t = sqlite3.connect(tmp_path / "pumpswap" / "tape.db")
    t.execute("create table swaps_ps(pool text, mint text, coin_creator text, received_at real)")
    t.execute("insert into swaps_ps values('P1','M1','LATECREATOR', ?)", (CUT + 30,))
    t.commit()
    t.close()
    coin(con, creator="LATECREATOR")
    scan(con, creator="LATECREATOR", fetched=1500.0)
    out = hist(d, pool="P1")
    assert out["curve_creator"]["value"] is None and out["field_unknown"]["curve_creator"] == "future_source" and out["dev_equals_creator"] is None
    assert hist(d, pool="P1", market_cut=CUT + 31)["curve_creator"]["value"] == "LATECREATOR"                       # known by a later cut: allowed
    assert hist(d, pool="P1", market_cut=None)["field_unknown"]["curve_creator"] == "clock_unknown"                 # no market cut given: unknown, not assumed


def test_an_inline_log_reason_written_after_the_decision_is_ignored_and_a_clockless_one_is_clock_unknown(tmp_path):
    d, con = desk(tmp_path)
    con.execute("insert into g12_inline_log values(?,?,?,?,?,?,?,?,?,?)", (DEC + 100 + OFF, "d", "M1", "skipped", "rate_limited", None, None, 0, 0.0, OFF))     # logged 100 s after the decision
    con.commit()
    assert age(d)["coin"]["reason"] == "not_attempted" and hist(d)["history_reason"] == "not_attempted"              # the future reason does not alter the past unknown
    con.execute("insert into g12_inline_log values(?,?,?,?,?,?,?,?,?,?)", (DEC - 50 + OFF, "d", "M1", "skipped", "daily_cap", None, None, 0, 0.0, OFF))
    con.commit()
    assert age(d)["coin"]["reason"] == "daily_cap"
    con.execute("insert into g12_inline_log values(?,?,?,?,?,?,?,?,?,?)", (DEC - 10 + OFF, "d", "M1", "skipped", "knob_off", None, None, 0, 0.0, None))
    con.commit()
    assert age(d)["coin"]["reason"] == "clock_unknown"                                                              # the newest usable row has no offset


def test_a_capped_scan_with_lb_zero_is_known_capped_zero_and_flagged_as_no_information_in_the_definition(tmp_path):
    d, con = desk(tmp_path)
    coin(con, slot=500)
    scan(con, fetched=1500.0, state="capped", covered=100, launches=[])
    out = hist(d)["creator_prior_launches_lb"]
    assert out["lb"] == 0 and out["status"] == "capped"                                                             # 'at least 0': a valid block that carries NO information
    assert "capped with lb 0" in F.DEFINITION["lb_zero"] and "complete and lb 0 only" in F.DEFINITION["lb_zero"]
