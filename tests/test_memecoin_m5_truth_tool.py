"""The m5-truth tool must never print a zero for a window it could not price, and must never count
the copied trade as organic volume.

The second rule is here because the tool broke it. Its first run reported PATIENCE at $1,584.03 and
called a correct refusal a false one; $1,495.69 of that was the source trader's own buy (0.543010
native, pool 0xf7872b55, tx 0xda65692d). Organic was $88.14 against a $1,473 floor.

Measured on the ten 4663 m5-floor refusals in the 72 h to 2026-09-22 22:41Z: GIGABYTE had SIXTY V4
swaps in its window and none of them could be priced, PAWSINU four. Reporting those as $0.00 would
read as "no volume" when the truth is "we cannot see it" -- the exact error the volume module was
written to prevent, and the one that makes a refusal look justified when it is not.

No network. The RPC is a fake.
"""
import pytest

import scripts.research.memecoin_m5_truth_4663 as T


# --- the verdict, which is the whole point of the tool -----------------------------------------

def test_a_window_with_swaps_but_no_prices_is_UNDECIDABLE_not_zero():
    """THE PROPERTY THIS FILE EXISTS FOR (GIGABYTE, 60 unpriced swaps)."""
    assert T.verdict_for(0.0, priced=0, unpriced=60, floor=2846.0) == T.UNDECIDABLE


def test_a_window_with_no_swaps_at_all_is_a_real_zero():
    """Nothing traded is a measurement. It is under the floor, and cleanly so."""
    assert T.verdict_for(0.0, priced=0, unpriced=0, floor=2846.0) == "under (clean)"


def test_volume_at_or_above_the_floor_would_have_passed():
    assert T.verdict_for(1584.03, priced=8, unpriced=0, floor=1473.0) == "WOULD HAVE PASSED"
    assert T.verdict_for(1473.0, priced=8, unpriced=0, floor=1473.0) == "WOULD HAVE PASSED"


def test_a_partly_priced_window_under_the_floor_is_labelled_a_LOWER_BOUND():
    """CLANK: $746.64 priced with 22 swaps unpriced. It is under $2,944 on what we can see, and
    saying so without the caveat would overstate what was measured."""
    assert T.verdict_for(746.64, priced=4, unpriced=22, floor=2944.0) == "under (lower bound)"


def test_an_unparsed_floor_never_reports_a_pass():
    assert T.verdict_for(9999.0, priced=3, unpriced=0, floor=None) == "no floor parsed"


# --- the copied trade, which is never organic volume --------------------------------------------

SOURCE_TX = "0xda65692dbd63bc28f409a24e9c9cf3edb0912517a8a4311a94dafac4ba07a134"


@pytest.mark.parametrize("event_id,expected", [
    ("swap:raybot:%s:0xd750158927f791eab81a5a551c82a958a585c689:0x972d93a9" % SOURCE_TX, SOURCE_TX),
    ("swap:raybot:%s" % SOURCE_TX.upper(), SOURCE_TX),
    ("fomo:abc:def", None),
    ("", None),
    (None, None),
])
def test_the_source_transaction_is_read_out_of_the_origin_event_id(event_id, expected):
    assert T.source_tx_of(event_id) == expected


class _Logs:
    """One getLogs answer, then an empty one, so `window_volume` terminates."""

    def __init__(self, logs):
        self.logs, self.served = logs, False

    def __call__(self, method, params, retries=3):
        assert method == "eth_getLogs"
        if self.served:
            return {"result": []}
        self.served = True
        return {"result": self.logs}


def _swap_log(tx, native_amount):
    """A V4 Swap whose quote side is token0, priced in native."""
    word = "%064x" % (int(native_amount * 1e18) & ((1 << 256) - 1))
    return {"topics": ["0xswap", "0xpool"], "data": "0x" + word + "0" * 64,
            "transactionHash": tx}


_META = {"0xpool": {"token0": "0xnative", "token1": "0xcoin", "quote_side": 0,
                    "token0_decimals": 18, "token1_decimals": 18}}


def test_the_copied_trade_is_excluded_by_hash():
    """THE PROPERTY (PATIENCE). Without it the tool reports the trade it is copying as evidence
    that other people are trading."""
    rpc = _Logs([_swap_log(SOURCE_TX, 0.543010), _swap_log("0xother", 0.01)])
    usd, priced, _unpriced, _pools = T.window_volume(
        rpc, "0xmgr", "0xswap", ["0xpool"], 1, 2, _META, lambda *_a: 2754.45,
        exclude_tx=SOURCE_TX)
    assert priced == 1, "only the other trader's swap is organic"
    assert round(usd, 2) == round(0.01 * 2754.45, 2), usd


def test_without_an_exclusion_the_source_trade_is_still_counted():
    """The overstating behaviour must stay visible rather than silently applying a guess, which is
    why `main` NAMES every row it could not exclude."""
    rpc = _Logs([_swap_log(SOURCE_TX, 0.543010), _swap_log("0xother", 0.01)])
    usd, priced, _u, _p = T.window_volume(
        rpc, "0xmgr", "0xswap", ["0xpool"], 1, 2, _META, lambda *_a: 2754.45)
    assert priced == 2 and usd > 1400.0, usd


def test_the_exclusion_is_case_insensitive():
    """`origin_event_id` is not normalised, and a checksummed hash that failed to match would
    silently restore the bug."""
    rpc = _Logs([_swap_log(SOURCE_TX.upper(), 0.543010)])
    _usd, priced, _u, _p = T.window_volume(
        rpc, "0xmgr", "0xswap", ["0xpool"], 1, 2, _META, lambda *_a: 2754.45,
        exclude_tx=SOURCE_TX)
    assert priced == 0


# --- the floor parser ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,expected", [
    ("m5 organic volume $88 under floor $1473 (m5_turnover_under_floor, revival guard)", 1473.0),
    ("m5 organic volume $0 under floor $181 (m5_turnover_under_floor)", 181.0),
    ("m5 organic volume $1 under floor $2,944 (x)", 2944.0),
    ("no floor here at all", None),
    ("under floor $notanumber", None),
    (None, None),
])
def test_the_floor_is_read_from_the_refusal_text(reason, expected):
    assert T._floor_from(reason) == expected


# --- the block search, which must be anchored and not extrapolated ------------------------------

class _Chain:
    """12 blocks a second, so a timestamp maps to many blocks and only a search finds the edge."""

    def __init__(self, genesis=1_000_000, per_s=12):
        self.genesis, self.per_s = genesis, per_s
        self.reads = 0

    def __call__(self, method, params, retries=3):
        assert method == "eth_getBlockByNumber"
        self.reads += 1
        n = int(params[0], 16)
        return {"result": {"timestamp": hex(self.genesis + n // self.per_s)}}


def test_block_at_finds_the_LAST_block_at_or_before_the_timestamp():
    chain = _Chain()
    target = chain.genesis + 1000          # blocks 12000..12011 all carry this timestamp
    found = T.block_at(chain, target, 0, 50_000)
    assert found == 12_011, found
    assert int(chain(None if False else "eth_getBlockByNumber", [hex(found), False])
               ["result"]["timestamp"], 16) <= target


def test_block_at_is_logarithmic_not_a_scan():
    """A linear walk over a chain this size would be thousands of calls on a funded endpoint."""
    chain = _Chain()
    T.block_at(chain, chain.genesis + 1000, 0, 50_000)
    assert chain.reads < 25, chain.reads


# --- the budget, which must abort rather than overrun the funded lane ---------------------------

def test_getLogs_beyond_the_budget_raises_instead_of_overrunning(monkeypatch):
    rpc = T.Rpc("https://custody.example", budget=2)
    monkeypatch.setattr(T.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not reach the network in this test")))
    # the first two are allowed through to the (absent) network, the third must be refused BEFORE it
    for _ in range(2):
        with pytest.raises(AssertionError):
            rpc("eth_getLogs", [{}], retries=1)
    with pytest.raises(T.Budget):
        rpc("eth_getLogs", [{}], retries=1)


def test_timestamp_reads_are_not_charged_to_the_custody_budget(monkeypatch):
    """Only getLogs goes to custody; block reads must never consume that budget."""
    rpc = T.Rpc("https://custody.example", budget=1)
    seen = []

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"result":{"timestamp":"0x1"}}'
    monkeypatch.setattr(T.json, "load", lambda _f: {"result": {"timestamp": "0x1"}})
    monkeypatch.setattr(T.urllib.request, "urlopen",
                        lambda req, timeout=None: seen.append(req.full_url) or _Resp())
    for _ in range(5):
        rpc("eth_getBlockByNumber", ["0x1", False])
    assert rpc.log_calls == 0
    assert all(url.startswith(T.READ_RPC) for url in seen), seen


# --- the window, whose separator decides what it contains ---------------------------------------
#
# `--since '-2 hours'` returned refusals TWELVE hours old (Gamma, 2026-09-23). `datetime('now', ?)`
# renders a SPACE separator, `decision_started_at` is ISO with a 'T', and on the same calendar date
# 'T' (0x54) sorts above ' ' (0x20), so every same-day row passed the filter.

import sqlite3


def _attempts(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_entry_attempts(network_id TEXT, decision_started_at TEXT, "
              "ticker TEXT, mint TEXT, terminal_reason TEXT, classification_json TEXT, "
              "origin_event_id TEXT)")
    c.executemany("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?)", rows)
    return c


def _at(conn, hours_ago):
    """An ISO stamp shaped exactly like the real column, including the microseconds and offset."""
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S', datetime('now', ?)) || '.123456+00:00'",
        ("-%d hours" % hours_ago,)).fetchone()[0]


FLOOR = "m5 organic volume $1 under floor $1473 (m5_turnover_under_floor)"


def test_a_row_three_hours_old_is_excluded_by_a_two_hour_window():
    """THE PROPERTY. With the space separator this row came back."""
    c = _attempts([])
    rows = [("4663", _at(c, 1), "FRESH", "0xaa", FLOOR, None, None),
            ("4663", _at(c, 3), "STALE", "0xbb", FLOOR, None, None)]
    c.executemany("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?)", rows)
    got = [r["ticker"] for r in T.m5_refusals(c, "4663", "-2 hours", 50)]
    assert got == ["FRESH"], got


def test_a_twelve_hour_old_row_is_excluded_too():
    """The reported symptom, pinned directly: twelve hours old against a two-hour window."""
    c = _attempts([])
    c.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?)",
              ("4663", _at(c, 12), "TWELVE", "0xcc", FLOOR, None, None))
    assert T.m5_refusals(c, "4663", "-2 hours", 50) == []


def test_a_wide_window_still_returns_the_old_rows():
    """The fix must narrow the window, not break it."""
    c = _attempts([])
    for h, t in ((1, "FRESH"), (12, "TWELVE"), (40, "OLD")):
        c.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?)",
                  ("4663", _at(c, h), t, "0x%02d" % h, FLOOR, None, None))
    got = [r["ticker"] for r in T.m5_refusals(c, "4663", "-72 hours", 50)]
    assert got == ["OLD", "TWELVE", "FRESH"], got


def test_the_cutoff_carries_a_T_so_it_matches_the_column_shape():
    c = _attempts([])
    cut = T.since_cutoff(c, "-2 hours")
    assert cut[10] == "T", repr(cut)
    assert " " not in cut, repr(cut)


def test_another_chain_is_not_returned():
    c = _attempts([])
    c.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?)",
              ("8453", _at(c, 1), "OTHER", "0xdd", FLOOR, None, None))
    assert T.m5_refusals(c, "4663", "-2 hours", 50) == []
