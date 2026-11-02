"""Hard-retirement guard for `backfill_polymarket_low_buys.py`.

The script is RETIRED — see `docs/audits/2026-04-29-legacy-remnant-audit.md`
section 1.12a. It must refuse to run unconditionally:
- no override flag (was --allow-legacy-low-buy-backfill; removed)
- no DB connection attempt (full retirement, not a runtime check)
- no BackfillRunner instantiation
- exit code 2 with a clear retired-message log line

These tests lock that behavior so an over-eager future cleanup can't
silently re-arm the script.
"""

from __future__ import annotations



def test_backfill_refuses_with_no_args(caplog):
    import backfill_polymarket_low_buys as mod

    rc = mod.main([])

    assert rc == 2
    assert "RETIRED" in caplog.text


def test_backfill_refuses_with_legacy_override_flag_unknown(caplog, capsys):
    """The --allow-legacy-low-buy-backfill flag MUST be removed.

    If a future change re-introduces it, this test fails because argparse
    will accept it instead of erroring.
    """
    import backfill_polymarket_low_buys as mod
    import pytest

    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--allow-legacy-low-buy-backfill"])

    # argparse exits 2 on unknown args; the script's hard-refusal also returns 2.
    # Either way, no successful run path exists. The captured stderr must
    # contain argparse's "unrecognized arguments" text proving the flag is gone.
    err = capsys.readouterr().err
    assert excinfo.value.code != 0
    assert "unrecognized arguments" in err or "unrecognized argument" in err


def test_backfill_refuses_without_opening_db(monkeypatch, caplog):
    """Hard-retire must not even open the DB. If `get_connection` is called,
    an old runtime-check pattern has crept back in."""
    import backfill_polymarket_low_buys as mod

    def boom(*args, **kwargs):
        raise AssertionError(
            "backfill_polymarket_low_buys must NOT open the DB; it is retired"
        )

    monkeypatch.setattr(mod, "get_connection", boom)

    rc = mod.main([])
    assert rc == 2


def test_backfill_does_not_instantiate_runner(monkeypatch):
    """The retired entry point must never construct BackfillRunner."""
    import backfill_polymarket_low_buys as mod

    class UnexpectedRunner:
        def __init__(self, **kwargs):
            raise AssertionError("legacy backfill runner must not start")

        def run(self):
            raise AssertionError("legacy backfill runner must not run")

    monkeypatch.setattr(mod, "BackfillRunner", UnexpectedRunner)

    rc = mod.main([])
    assert rc == 2


def test_backfill_module_has_retired_marker():
    """Quick guard that the retired-message constant is exported and obvious.

    Anyone re-arming the script would need to delete this constant; that's
    a strong signal in code review."""
    import backfill_polymarket_low_buys as mod

    assert hasattr(mod, "RETIRED_MESSAGE")
    assert "RETIRED" in mod.RETIRED_MESSAGE
