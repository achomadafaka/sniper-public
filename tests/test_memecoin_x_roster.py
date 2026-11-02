from sniper.analysis.memecoin_x import roster as R


def test_roster_counts():
    c = R.roster_counts()
    assert c["tier1_trader"] == 27
    assert c["tier2_source"] == 6
    assert c["official_context"] == 2


def test_oht_handle_is_corrected():
    # Real handle is @_OHT_; identity storage is lowercase.
    handles = {e["handle"] for e in R.INITIAL_ROSTER}
    assert "_oht_" in handles and "oht" not in handles
    oht = next(e for e in R.INITIAL_ROSTER if e["handle"] == "_oht_")
    assert oht["display_handle"] == "_OHT_"


def test_handles_are_canonical_and_unique():
    handles = [e["handle"] for e in R.INITIAL_ROSTER]
    assert handles == [h.lower() for h in handles]
    assert len(handles) == len(set(handles)) == 35


def test_official_context_excluded_role():
    off = [e for e in R.INITIAL_ROSTER if e["role"] == "official_context"]
    assert {e["handle"] for e in off} == {"vladtenev", "robinhoodapp"}
