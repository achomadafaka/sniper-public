"""Version routing only: temporary paper ledgers, no model or provider calls."""
from datetime import datetime, timezone
import sqlite3

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_paper as paper
from sniper.services import memecoin_desk_ps_paper as PSP
from sniper.services import memecoin_desk_risk_tape as risk_tape
from sniper.services import memecoin_desk_ps_tape as PT
from sniper.util import clock
from test_desk_paper_pumpswap import book, cand, NOW, T0, TOKENS, SOLD
from test_desk_ps_asof import MINT, POOL
from test_desk_ps_loss_integration import provision
from test_memecoin_desk_ps_evidence import fixture

V1, V2 = "pumpswap-facts-v1", "pumpswap-facts-v2"


def strict_book(tmp_path, monkeypatch, version, *, tokens=TOKENS, decimals=9):
    desk_dir, conn = book(tmp_path, monkeypatch, decimals=decimals)
    store.register_cohort(conn, cohort_id="strict", producer="U", prompt_version="desk-judge-v28",
        policy={"venue": "pumpswap", "cumulative_kill_usd": -200, "ps_inputs": version},
        created_at=paper._iso(T0-500))
    conn.execute("UPDATE desk_paper_positions SET cohort_id='strict'")
    conn.commit()
    quotes, cuts = [], []

    def buy(_dir, _mint, spend, at, *, evaluated_at, **kw):
        quotes.append(kw)
        return {"tokens_atomic": str(tokens), "spend_lamports": str(spend), "cash_left_atomic": "0",
                "state_at": at, "pool": POOL, "proof": {}}, None

    def cut(_dir, now, venue, **kw):
        cuts.append((venue, kw))
        return now

    monkeypatch.setattr(PSP, "buy", buy)
    monkeypatch.setattr(paper, "_cut", cut)
    return desk_dir, conn, quotes, cuts


@pytest.mark.parametrize("version", [V1, V2])
def test_supported_version_keeps_strict_entry_and_actual_risk_stamp(tmp_path, monkeypatch, version):
    desk_dir, conn, buys, cuts = strict_book(tmp_path, monkeypatch, version)
    [result] = paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0, candidate_fn=cand)
    assert result["state"] == "open"
    assert buys == [{"integrity": True}] * 2
    assert cuts == [("ps", {"pool": POOL, "integrity": True})]
    [(pos, _, _, mark)] = paper.risk_positions(conn.execute("PRAGMA database_list").fetchone()[2])
    assert pos["desk_ps_inputs"] == version and pos["desk_base_decimals"] == 9
    assert pos["open_qty"] == TOKENS / 10**9 and mark["open_qty"] == pos["open_qty"]
    conn.close()


def test_v2_mark_and_delayed_exit_keep_exact_large_atomic_quantity(tmp_path, monkeypatch):
    tokens = 2**63 + 513
    desk_dir, conn, _, cuts = strict_book(tmp_path, monkeypatch, V2, tokens=tokens)
    [filled] = paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0, candidate_fn=cand)
    assert filled["state"] == "open"
    assert tuple(conn.execute("SELECT qty_atomic,TYPEOF(qty_atomic) FROM desk_paper_positions").fetchone()) == (str(tokens), "text")
    sells = []

    def sell(_dir, _mint, qty, at, *, evaluated_at, **kw):
        sells.append((qty, kw))
        return {"lamports": str(SOLD)}, None

    monkeypatch.setattr(PSP, "sell", sell)
    [marked] = paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0,
                          risk_exits={"desk:U:intake:x": paper.RISK_EXIT_REASON})
    [closed] = paper.tick(conn, desk_dir=desk_dir, now=NOW+2, sol_usd=100, sol_at=T0)
    assert marked["state"] == "exit_requested" and closed["state"] == "closed"
    assert sells == [(tokens, {"integrity": True})] * 2
    assert cuts == [("ps", {"pool": POOL, "integrity": True})] * 3
    conn.close()


@pytest.mark.parametrize("proven_zero", [False, True])
def test_v2_unknown_mark_and_proven_zero_preserve_existing_semantics(tmp_path, monkeypatch, proven_zero):
    desk_dir, conn, _, _ = strict_book(tmp_path, monkeypatch, V2)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0, candidate_fn=cand)
    before = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())

    def sell(*args, evaluated_at, **kw):
        assert kw == {"integrity": True}
        return ({"lamports": "0"}, None) if proven_zero else (None, "pool_stale")

    monkeypatch.setattr(PSP, "sell", sell)
    paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0)
    after = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    assert (after["peak_price_usd"], after["peak_at"], after["last_quote_at"]) == (
        before["peak_price_usd"], before["peak_at"], before["last_quote_at"])
    assert (after["last_mark_usd"], after["last_mark_at"]) == (
        (0, paper._iso(NOW)) if proven_zero else (before["last_mark_usd"], before["last_mark_at"]))
    conn.close()


def test_unknown_registered_version_refuses_before_legacy_quote(tmp_path, monkeypatch):
    desk_dir, conn, buys, _ = strict_book(tmp_path, monkeypatch, "pumpswap-facts-v99")
    with pytest.raises(ValueError, match="ps_inputs_version_unknown"):
        paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0, candidate_fn=cand)
    assert buys == []
    conn.close()


@pytest.mark.parametrize("version", [V1, V2, None])
def test_risk_keeps_strict_non_six_decimal_mode_even_if_marker_missing(tmp_path, monkeypatch, version):
    desk, _ = fixture(tmp_path)
    provision(desk)
    monkeypatch.setattr(clock, "offset_s", lambda: 0.)
    with sqlite3.connect(desk / "blocks" / "2027-01-15.db") as conn:
        conn.execute("UPDATE blocks SET received_at=?", (NOW-3,))
    tokens = 2**63 + 513
    with sqlite3.connect(PT.tape_path(desk)) as conn:
        conn.execute("UPDATE swaps_ps SET base_atomic=?,quote_atomic=?", (str(tokens), "2000000000"))
    observed, proofs = [], []
    load = PT.load_ps_swaps

    def read(*a, **kw):
        observed.append(kw)
        rows, gaps = load(*a, **kw)
        assert rows[0]["token_atomic"] == tokens and rows[0]["quote_atomic"] == 2_000_000_000
        return rows, gaps

    def proof(*a, **kw):
        proofs.append(kw)
        return {"from": NOW-600, "through": NOW-.5}

    monkeypatch.setattr(PT, "load_ps_swaps", read)
    pos = {"id": 1, "mint": MINT, "opened_at": datetime.fromtimestamp(NOW-100, timezone.utc).isoformat(),
           "entry_price": 1, "desk_base_decimals": 9}
    if version is not None:
        pos["desk_ps_inputs"] = version
    evidence = risk_tape.evidence_of(pos, desk_dir=desk, now=NOW, sol_usd=100,
                                    fill_sigs_of=lambda _: ("ours",), sb=0, proof_fn=proof)
    assert any(p.get("integrity") is True and p.get("pool") == POOL and p["window_s"] == 0 for p in proofs)
    assert observed and observed[0]["integrity"] is True and observed[0]["base_decimals"] == 9
    assert evidence["fresh"] is True and evidence["buy_sol_30s"] == 2
    assert evidence["last_price"] == pytest.approx(40 / (100_000_000_000_000 / 10**9))


def test_risk_unknown_version_is_typed_blind_without_reading_legacy_tape(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "offset_s", lambda: 0.)
    pos = {"id": 1, "mint": MINT, "opened_at": datetime.fromtimestamp(NOW-100, timezone.utc).isoformat(),
           "entry_price": 1, "desk_ps_inputs": "pumpswap-facts-v99", "desk_base_decimals": 9}

    def forbidden(*args, **kwargs):
        pytest.fail("unknown version must not fall back to a legacy tape proof")

    result = risk_tape.evidence_of(pos, desk_dir=tmp_path, now=NOW, sol_usd=100,
                                   fill_sigs_of=lambda _: ("ours",), sb=0, proof_fn=forbidden)
    assert result["fresh"] is False and "ps_inputs_version_unknown" in result["unknown"]


@pytest.mark.parametrize("version", [V1, V2])
def test_unknown_cohort_does_not_hide_other_open_risk_positions(tmp_path, monkeypatch, version):
    desk_dir, conn, _, _ = strict_book(tmp_path, monkeypatch, version)
    paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0, candidate_fn=cand)
    store.register_cohort(conn, cohort_id="unknown", producer="U", prompt_version="invalid",
        policy={"ps_inputs": "pumpswap-facts-v99"}, created_at=paper._iso(T0-500))
    bad = dict(conn.execute("SELECT * FROM desk_paper_positions").fetchone())
    bad.pop("id")
    bad.update(ticket_id="desk:U:intake:unknown", intent_id="unknown", cohort_id="unknown")
    conn.execute("INSERT INTO desk_paper_positions (%s) VALUES (%s)" % (
        ",".join(bad), ",".join("?" for _ in bad)), tuple(bad.values()))
    conn.commit()
    positions = paper.risk_positions(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    assert len(positions) == 2
    by_cohort = {pos["desk_cohort_id"]: pos for pos, _, _, _ in positions}
    assert by_cohort["strict"]["desk_ps_inputs"] == version
    assert by_cohort["unknown"]["desk_ps_inputs"] not in (None, V1, V2)
    monkeypatch.setattr(clock, "offset_s", lambda: 0.)

    def forbidden(*args, **kwargs):
        pytest.fail("unknown cohort must stay blind before any tape proof")

    evidence = risk_tape.evidence_of(by_cohort["unknown"], desk_dir=desk_dir, now=NOW, sol_usd=100,
        fill_sigs_of=lambda _: ("ours",), sb=0, proof_fn=forbidden)
    assert evidence["fresh"] is False and "ps_inputs_version_unknown" in evidence["unknown"]
