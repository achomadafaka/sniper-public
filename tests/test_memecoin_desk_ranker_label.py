"""Desk cohort U ranker tape label (pre-registration 2026-10-21-desk-u-ranker §4)."""
import math

from sniper.services import memecoin_desk_ranker_label as L

T0 = 1000.0


def _label(prices, **kw):
    """prices: [(seconds after t0, price)] for the coin; a healthy tape and no producer gaps."""
    swaps = [(T0 + dt, p, kw.pop("actor", "a"), 5) for dt, p in prices] + kw.pop("extra", [])
    return L.label(swaps, t0=T0, silences=kw.pop("silences", []), gaps=kw.pop("gaps", []), **kw)


def test_y2_and_d20_from_the_first_price_five_seconds_after_band_entry():
    up = _label([(0, 9.0), (6, 1.0), (100, 0.85), (300, 2.1)])          # E = 1.0 at t0+6, never -20 %
    assert (up["status"], up["y2"], up["d20"], up["e_price"]) == ("labelled", 1, 0, 1.0)
    assert math.isclose(up["max_ln"], math.log(2.1))
    down = _label([(6, 1.0), (50, 0.79), (90, 3.0)])                       # -20 % before +20 %
    assert (down["y2"], down["d20"]) == (1, 1)
    flat = _label([(6, 1.0), (50, 1.1)])
    assert (flat["y2"], flat["d20"]) == (0, 0)


def test_no_swap_inside_the_entry_wait_is_no_tape_never_a_loser():
    got = _label([(2, 1.0), (66, 1.0)])
    assert (got["status"], got["y2"], got["d20"]) == ("no_tape", None, None)
    assert _label([(2, 1.0), (64, 1.0)])["status"] == "labelled"         # the shared [t0+5, t0+65]


def test_a_price_after_the_window_never_counts():
    got = _label([(6, 1.0), (6 + L.WINDOW_S + 1, 5.0)])
    assert (got["status"], got["y2"]) == ("labelled", 0)


def test_a_tape_silence_or_a_gap_row_censors_and_graduation_censors_at_graduation():
    assert _label([(6, 1.0)], silences=[(T0 + 100, T0 + 112)])["status"] == "censored_gap"
    lost = _label([(6, 1.0)], gaps=[(T0 + 300, "ring_overflow")])      # frames lost, no silence shows
    assert (lost["status"], lost["gap_cause"]) == ("censored_gap", "ring_overflow")
    assert _label([(6, 1.0)], gaps=[(T0 + 300, "")])["gap_cause"] == "unknown"
    for cause in ("worker_start", "stream:RpcError"):                      # shows as silence, or nothing
        assert _label([(6, 1.0)], gaps=[(T0 + 300, cause)])["status"] == "labelled"
    assert _label([(6, 1.0)], gaps=[(T0 + 6 + L.WINDOW_S + 1, "ring_overflow")])["status"] == "labelled"
    grad = _label([(6, 1.0), (90, 3.0)], extra=[(T0 + 200, 3.0, "a", 0)])
    assert (grad["status"], grad["graduated_at"], grad["y2"]) == ("censored_graduation", T0 + 200, None)


def test_our_own_swaps_are_not_the_price_path():
    got = _label([(6, 1.0)], extra=[(T0 + 30, 9.0, "us", 5)], exclude_actors={"us"})
    assert got["y2"] == 0


def test_tape_silences_include_the_edges():
    assert L.tape_silences([5, 8, 30], start=0, end=45) == [(8, 30), (30, 45)]
    assert L.tape_silences([], start=0, end=5) == []


def test_the_labeller_labels_every_ranker_candidate_once_from_the_tape(tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path
    import test_memecoin_desk_ranker as RT
    spec = importlib.util.spec_from_file_location(
        "labeller", Path(__file__).resolve().parents[1] / "scripts" / "research" / "desk_u_ranker_labels.py")
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    tape = RT.Tape(tmp_path)
    run_up = RT._candidate(tape, 0, RT.T0 + 10)
    dud = RT._candidate(tape, 1, RT.T0 + 10)
    for k in range(0, 700, 5):                                 # a live tape every 5 s across the window
        tape.heartbeat(RT.T0 + 10 + k)
    tape.swap(run_up["mint"], RT.T0 + 17)                       # E = the base price
    tape.swap(run_up["mint"], RT.T0 + 200, price_x=2.5)
    tape.swap(run_up["mint"], RT.T0 + 300, price_x=2.5, actor="us")
    tape.swap(dud["mint"], RT.T0 + 17)
    RT.W.run_cycle(tmp_path, RT.T0 + 60)
    early = lab.run(tmp_path, now=RT.T0 + 300, exclude_actors=["us"])
    assert early["labelled"] == 0                               # the window has not closed
    got = lab.run(tmp_path, now=RT.T0 + 10 + lab.SPAN_S + lab.SETTLE_S + 1, exclude_actors=["us"])
    assert got == {"labelled": 2, "by_status": {"labelled": 2}}
    conn = sqlite3.connect(str(tmp_path / "ranker_labels.db"))
    rows = dict(conn.execute("SELECT opportunity_id, y2 FROM labels").fetchall())
    conn.close()
    assert rows == {run_up["ticket_id"]: 1, dud["ticket_id"]: 0}
    assert lab.run(tmp_path, now=RT.T0 + 5000)["labelled"] == 0   # labelled once, never rewritten


def test_the_labeller_excludes_our_wallet_from_the_runtime_keypair_and_refuses_without_it(tmp_path, monkeypatch,
                                                                                         capsys):
    import importlib.util
    from pathlib import Path
    from sniper.execution.solana_wallet import generate_keypair_file
    spec = importlib.util.spec_from_file_location(
        "labeller", Path(__file__).resolve().parents[1] / "scripts" / "research" / "desk_u_ranker_labels.py")
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "missing.json"))
    assert lab.main(["--once"]) == 2 and "refused" in capsys.readouterr().out
    kp_path = tmp_path / "kp.json"
    kp = generate_keypair_file(str(kp_path))
    monkeypatch.setenv("MEMECOIN_SOLANA_KEYPAIR", str(kp_path))
    seen = {}
    monkeypatch.setattr(lab, "run", lambda desk, *, now, exclude_actors: seen.setdefault("x", exclude_actors) or {})
    assert lab.main(["--once", "--exclude-actor", "other"]) == 0
    assert seen["x"] == sorted([str(kp.pubkey()), "other"])
    ours = str(kp.pubkey())                                   # our buy on the labelled mint is excluded
    got = L.label([(T0 + 6, 1.0, "a", 5), (T0 + 30, 9.0, ours, 5)], t0=T0, silences=[], gaps=[],
                  exclude_actors=seen["x"])
    assert (got["status"], got["y2"]) == ("labelled", 0)


def test_our_actors_come_from_our_own_fills_on_the_tape_before_the_keypair(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sqlite3
    from pathlib import Path
    import test_memecoin_desk_ranker as RT
    from sniper.persistence import db_memecoin as ledger
    spec = importlib.util.spec_from_file_location(
        "labeller", Path(__file__).resolve().parents[1] / "scripts" / "research" / "desk_u_ranker_labels.py")
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    desk = tmp_path / "data" / "desk"
    tape = RT.Tape(desk)
    tape.swap("MINT", RT.T0 + 5, actor="OurWallet", sig="our-fill-sig")
    tape.swap("MINT", RT.T0 + 6, actor="SomeoneElse", sig="their-sig")
    tape.swap("MINT", RT.T0 + 7, actor="TrackedWallet", sig="source-sig")
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    ledger.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, created_at, tx_sig, network_id) "
                 "VALUES(1,'entry',1,1,25,'2026-10-21T13:00:00+00:00','our-fill-sig','solana')")
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, created_at, tx_sig, network_id) "
                 "VALUES(2,'entry',1,1,25,'2026-10-21T13:00:00+00:00','their-sig','8453')")   # another chain
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, created_at, tx_sig, network_id) "
                 "VALUES(3,'entry',1,1,25,'2026-10-21T13:00:00+00:00','source-sig','solana')")  # a paper fill
    for pid, mode in ((1, "real"), (2, "real"), (3, "paper")):
        conn.execute("INSERT INTO memecoin_positions(id, mint, entry_price, entry_notional, opened_at, execution_mode) "
                     "VALUES(?,?,1,25,'2026-10-21T13:00:00+00:00',?)", (pid, "MINT%d" % pid, mode))
    conn.commit()
    conn.close()
    assert lab.fill_actors(db, desk) == {"OurWallet"}
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "missing.json"))   # never read
    seen = {}
    monkeypatch.setattr(lab, "run", lambda d, *, now, exclude_actors: seen.setdefault("x", exclude_actors) or {})
    assert lab.main(["--once"]) == 0 and seen["x"] == ["OurWallet"]
    assert "own actors from fills: 1" in capsys.readouterr().out
