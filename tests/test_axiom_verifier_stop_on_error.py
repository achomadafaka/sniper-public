"""verify-j7-56 on a public receipt host stops at the first failed read (operator 2026-10-14).

The dRPC 56 key answered null receipts, so the hourly 56 run proved nothing for ~10 days. The
replacement is a public host (bsc.meowrpc.com) paced 1/s that must stop on any error, never burst.
"""
from scripts.research import verify_axiom_addresses as cli
from test_axiom_j7_verifier_schedule import ledger  # noqa: F401


def test_a_failed_read_stops_every_later_read_and_a_null_answer_does_not():
    answers = iter([("0x1", None), (None, None), (None, "HTTP 429"), ("0x2", None)])
    made = []

    def call(*args):
        made.append(args)
        return next(answers)
    read = cli.bounded_rpc(call, max_calls=10, max_seconds=60, stop_on_error=True)
    assert read("u", "m", []) == ("0x1", None)
    assert read("u", "m", []) == (None, None)              # a null receipt is unknown, not an error
    assert read("u", "m", []) == (None, "HTTP 429")
    assert read("u", "m", []) == (None, "rpc_stopped_after_error")
    assert len(made) == 3 and read.stopped()


def test_without_the_flag_reads_continue_as_today():
    answers = iter([(None, "HTTP 429"), ("0x2", None)])
    read = cli.bounded_rpc(lambda *a: next(answers), max_calls=10, max_seconds=60)
    assert read("u", "m", []) == (None, "HTTP 429")
    assert read("u", "m", []) == ("0x2", None)
    assert not read.stopped()


def test_the_cli_flag_reaches_the_bounded_reader(ledger, monkeypatch, capsys):  # noqa: F811
    seen = {}
    real = cli.bounded_rpc

    def spy(call, **kw):
        seen.update(kw)
        return real(call, **kw)
    monkeypatch.setattr(cli, "bounded_rpc", spy)
    monkeypatch.setenv("MEMECOIN_CAPTURE_EVM_READ_URL_56", "https://rpc.public-reads.example")
    cli.main(["--db", ledger["path"], "--confirm", "--j7", "--chain", "56",
              "--rpc-url-env", "MEMECOIN_CAPTURE_EVM_READ_URL_56", "--summary", "--stop-on-error",
              "--max-rpc-calls", "150"])
    assert seen["stop_on_error"] is True and seen["max_calls"] == 150
