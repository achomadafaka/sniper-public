from sniper.execution.evm_real_seams import classify_quote_miss as C


def test_a_config_flag_reads_off_and_a_missing_build_reads_unavailable():
    """Chain 56 reported "(launchpad_curve/v4/v4_multihop off)" for three rungs that are not
    flags: it has no V4 deployment addresses and the launchpad curve is gated to 4663 in code.
    A reader must be able to tell a switch from a build without opening the source."""
    code, detail = C([
        {"rung": "v3", "hit": False, "skipped": False, "unavailable": False},
        {"rung": "v2", "hit": False, "skipped": True, "unavailable": False},
        {"rung": "v4", "hit": False, "skipped": True, "unavailable": True},
        {"rung": "v4_multihop", "hit": False, "skipped": True, "unavailable": True},
    ])
    assert code == "no_route"
    assert detail == "no route on v3 (v2 off) (v4/v4_multihop unavailable on this chain)", detail


def test_all_off_is_unchanged_when_nothing_is_unavailable():
    code, detail = C([
        {"rung": "v3", "hit": False, "skipped": False},
        {"rung": "v4", "hit": False, "skipped": True},
    ])
    assert (code, detail) == ("no_route", "no route on v3 (v4 off)")


def test_a_reader_that_never_sets_unavailable_keeps_the_old_meaning():
    """`unavailable` is a SUBSET of `skipped`. A record without the key behaves exactly as before."""
    code, detail = C([{"rung": "v3", "hit": False, "skipped": True}])
    assert (code, detail) == ("no_route", "no route on no rung (v3 off)")
