"""C01 (engineering review 2026-10-08, P1): an unavailable EVM tape must not skip the paper pass.

The tape was opened around the WHOLE position loop, so a missing or unreadable tape file visited
no position at all: no time rail closed (a 780 s max hold stayed open), and Solana/BSC positions,
which never read the tape, stopped too. Now an unavailable tape is a typed per-pass state: EVM
price decisions hold, time rails and the other chains run, and `tape_errors` still counts it.
"""
from test_memecoin_paper_ticks import T0, _engine, _ledger, _open, _row


class _Sol:
    """A Solana source that always answers no tick, and records that it was asked."""

    def __init__(self):
        self.asked, self.wanted = [], []

    def binding(self, pid):
        return None

    def tick(self, pos, binding, now):
        self.asked.append(int(pos["id"]))
        return None, "no_quote_yet"


def test_a_missing_tape_still_reaches_the_time_rail(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    eng, _ = _engine(c, str(tmp_path / "missing.db"), T0, iso="2026-10-08T07:14:00+00:00")
    eng.step()
    assert _row(c, pid)["is_open"] == 0
    assert eng.counters["tape_errors"] == 1


def test_an_unreadable_tape_file_still_reaches_the_time_rail(tmp_path):
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not a sqlite database" * 64)
    c, m = _ledger()
    pid = _open(c, m)
    eng, _ = _engine(c, str(bad), T0, iso="2026-10-08T07:14:00+00:00")
    eng.step()
    assert _row(c, pid)["is_open"] == 0
    assert eng.counters["tape_errors"] >= 1


def test_without_a_tape_no_price_exit_fires_before_the_time_rail(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    eng, _ = _engine(c, str(tmp_path / "missing.db"), T0, iso="2026-10-08T07:01:00+00:00")
    eng.step()
    assert _row(c, pid)["is_open"] == 1
    assert eng.counters["hold:no_tape"] == 1


def test_a_failing_evm_tape_does_not_stop_solana_positions(tmp_path):
    c, m = _ledger()
    evm_pid = _open(c, m)
    sol_pid = _open(c, m, network="solana", pool="SolPool111")
    eng, _ = _engine(c, str(tmp_path / "missing.db"), T0, iso="2026-10-08T07:01:00+00:00")
    eng.sol = _Sol()
    eng.step()
    assert eng.sol.asked == [sol_pid]
    assert _row(c, evm_pid)["is_open"] == 1 and _row(c, sol_pid)["is_open"] == 1


def test_an_unreadable_tape_still_closes_an_unbound_position_on_time(tmp_path):
    """The source-pool lookup reads the tape too: its failure must hold the price, not the rail."""
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not a sqlite database" * 64)
    c, m = _ledger()
    pid = _open(c, m, pool=None)
    eng, _ = _engine(c, str(bad), T0, iso="2026-10-08T07:14:00+00:00")
    eng.step()
    assert _row(c, pid)["is_open"] == 0
    assert eng.counters["errors"] == 0


class _Bsc:
    """A 56 source that answers no tick, and records that it was asked."""

    def __init__(self):
        self.asked, self.wanted, self.stopped = [], [], False

    def forget(self, live):
        pass

    def tick(self, pid):
        self.asked.append(int(pid))
        return None


def test_one_pass_with_a_failing_tape_still_asks_solana_and_bsc(tmp_path):
    """Gamma on C01: EVM, Solana and BSC in one pass with the EVM tape unavailable."""
    c, m = _ledger()
    evm_pid = _open(c, m)
    sol_pid = _open(c, m, network="solana", pool="SolPool111")
    bsc_pid = _open(c, m, network="56", pool=None, route={"venue": "v2"})
    eng, _ = _engine(c, str(tmp_path / "missing.db"), T0, iso="2026-10-08T07:01:00+00:00", bsc=_Bsc())
    eng.sol = _Sol()
    eng.step()
    assert eng.sol.asked == [sol_pid] and eng.bsc.asked == [bsc_pid]
    assert all(_row(c, p)["is_open"] == 1 for p in (evm_pid, sol_pid, bsc_pid))


def test_a_corrupt_tape_logs_one_traceback_per_pass(tmp_path, caplog):
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is not a sqlite database" * 64)
    c, m = _ledger()
    for network in ("4663", "8453"):
        _open(c, m, network=network)
    eng, _ = _engine(c, str(bad), T0, iso="2026-10-08T07:01:00+00:00")
    with caplog.at_level("ERROR", logger="memecoin"):
        eng.step()
    assert eng.counters["tape_errors"] == 2
    assert sum("tape read failed" in r.getMessage() for r in caplog.records) == 1
