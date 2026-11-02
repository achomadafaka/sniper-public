"""Only registered new cohorts select strict pool evidence for fills, marks, exits and PAPER RISK."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk_paper as paper
from sniper.services import memecoin_desk_ps_paper as PSP
from test_desk_paper_pumpswap import book, cand, NOW, T0, TOKENS, SOLD
from test_desk_ps_asof import MINT, POOL
from test_desk_ps_loss_integration import provision
from test_desk_ps_loss_scope import receipt, batch


def opened(tmp_path, monkeypatch, *, strict):
    desk_dir, conn = book(tmp_path, monkeypatch)
    if strict:
        D.register_cohort(conn, cohort_id="strict", producer="U", prompt_version="desk-judge-v27",
            policy={"venue": "pumpswap", "cumulative_kill_usd": -200, "ps_inputs": "pumpswap-facts-v1"},
            created_at=paper._iso(T0 - 500))
        conn.execute("UPDATE desk_paper_positions SET cohort_id='strict'")
        conn.commit()
    buys = []
    def buy(_dir, _mint, spend, at, *, evaluated_at, **kw):
        buys.append(kw)
        return {"tokens_atomic": str(TOKENS), "spend_lamports": str(spend), "cash_left_atomic": "0",
                "state_at": at, "pool": "pool", "proof": {}}, None
    monkeypatch.setattr(PSP, "buy", buy)
    monkeypatch.setattr(paper, "_cut", lambda _dir, now, _venue, **kw: now)
    [result] = paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0, candidate_fn=cand)
    assert result["state"] == "open"
    return desk_dir, conn, buys


@pytest.mark.parametrize("strict", [True, False])
def test_registered_policy_selects_strict_entry_quotes_and_preserves_legacy_call_shape(tmp_path, monkeypatch, strict):
    _, _, buys = opened(tmp_path, monkeypatch, strict=strict)
    assert buys == ([{"integrity": True}] * 2 if strict else [{}, {}])


@pytest.mark.parametrize("strict", [True, False])
def test_registered_policy_propagates_to_marks_and_delayed_risk_exit(tmp_path, monkeypatch, strict):
    desk_dir, conn, _ = opened(tmp_path, monkeypatch, strict=strict)
    calls = []
    def sell(*args, evaluated_at, **kw):
        calls.append(kw)
        return {"lamports": str(SOLD)}, None
    monkeypatch.setattr(PSP, "sell", sell)
    [first] = paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0,
                         risk_exits={"desk:U:intake:x": paper.RISK_EXIT_REASON})
    [last] = paper.tick(conn, desk_dir=desk_dir, now=NOW + 2, sol_usd=100, sol_at=T0)
    assert first["state"] == "exit_requested" and last["state"] == "closed"
    assert calls == ([{"integrity": True}] * 2 if strict else [{}, {}])


@pytest.mark.parametrize("decimals", [0, 9, None, True])
def test_new_paper_risk_marks_explicit_registered_mode_and_only_known_fill_decimals(tmp_path, monkeypatch, decimals):
    _, conn, _ = opened(tmp_path, monkeypatch, strict=True)
    fill = json.loads(conn.execute("SELECT fill_json FROM desk_paper_positions").fetchone()[0])
    fill["decimals"] = decimals
    conn.execute("UPDATE desk_paper_positions SET fill_json=?", (json.dumps(fill),))
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    [(pos, _, _, mark)] = paper.risk_positions(path)
    expected = decimals if type(decimals) is int else None
    assert pos["desk_ps_inputs"] == "pumpswap-facts-v1" and pos["desk_base_decimals"] == expected
    assert pos["open_qty_known"] is (expected is not None)
    if expected is None:
        assert pos["open_qty"] is None and mark is None
    else:
        assert pos["open_qty"] == TOKENS / 10**expected


def test_legacy_risk_shape_has_no_new_metadata_defaults(tmp_path, monkeypatch):
    _, conn, _ = opened(tmp_path, monkeypatch, strict=False)
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    [(pos, _, _, _)] = paper.risk_positions(path)
    assert "desk_ps_inputs" not in pos and "desk_base_decimals" not in pos


@pytest.mark.parametrize("problem", ["other", "same", "old_same", "second"])
def test_actual_paper_parent_cut_and_strict_quote_share_pool_loss_contract(tmp_path, monkeypatch, problem):
    desk_dir, conn = book(tmp_path, monkeypatch)
    D.register_cohort(conn, cohort_id="strict", producer="U", prompt_version="desk-judge-v27",
        policy={"venue": "pumpswap", "cumulative_kill_usd": -200, "ps_inputs": "pumpswap-facts-v1"},
        created_at=paper._iso(T0 - 500))
    conn.execute("UPDATE desk_paper_positions SET cohort_id='strict'")
    conn.commit()
    provision(desk_dir)
    from sniper.services import memecoin_desk_capture as capture
    captures = []
    real_capture = capture.capture_proof
    def capture_spy(*args, **kwargs):
        captures.append(kwargs)
        return real_capture(*args, **kwargs)
    monkeypatch.setattr(capture, "capture_proof", capture_spy)
    with sqlite3.connect(desk_dir / "pumpswap/tape.db") as tape:
        if problem == "second":
            receipt(tape, at=NOW-12, pools=[MINT])
            receipt(tape, "two", NOW-5, [POOL])
            batch(tape, ("one", "two"), at=NOW-12)
        else:
            at = NOW-200 if problem == "old_same" else NOW-5
            receipt(tape, at=at, pools=[MINT] if problem == "other" else [POOL])
            batch(tape, at=at)
        if problem == "second":
            tape.execute("UPDATE ps_loss_batches SET through_at=?", (NOW-4,))
    if problem in ("same", "second"):
        # a same-pool loss retries the frozen fill moment for FRONTIER_WAIT_S (c17714291), then fails
        assert paper.fill_pending(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0, candidate_fn=cand) == []
        assert conn.execute("SELECT state FROM desk_paper_positions").fetchone()[0] == "pending_fill"
    later = NOW + paper.FRONTIER_WAIT_S + 1 if problem in ("same", "second") else NOW
    [result] = paper.fill_pending(conn, desk_dir=desk_dir, now=later, sol_usd=100, sol_at=T0, candidate_fn=cand)
    assert captures[0] == {"venues": ("ps",), "pool": POOL, "integrity": True, "window_s": 0}
    if problem in ("other", "old_same"):
        assert result["state"] == "open", result
        calls = []
        def sell(*args, evaluated_at, **kw):
            calls.append(kw)
            return {"lamports": str(SOLD)}, None
        monkeypatch.setattr(PSP, "sell", sell)
        [marked] = paper.tick(conn, desk_dir=desk_dir, now=NOW, sol_usd=100, sol_at=T0,
                              risk_exits={"desk:U:intake:x": paper.RISK_EXIT_REASON})
        assert marked["state"] == "exit_requested" and calls == [{"integrity": True}]
        path = desk_dir / "universe.heartbeat"
        beat = json.loads(path.read_text())
        beat.update(ts=NOW+2, ps_committed_through=NOW+2)
        path.write_text(json.dumps(beat))
        [closed] = paper.tick(conn, desk_dir=desk_dir, now=NOW+2, sol_usd=100, sol_at=T0)
        assert closed["state"] == "closed" and calls == [{"integrity": True}, {"integrity": True}]
        assert captures[-2:] == [captures[0], captures[0]]
    else:
        assert result["state"] == "failed" and "capture" in result["refusal"], result

