"""The canary log-gap recorder's accounting matches its pre-registration
(docs/audits/2026-09-30-evm-canary-log-gap-preregistration.md). Pure: no socket, no RPC."""
from scripts.research import evm_canary_log_gap as g


def _book(*events):
    """events: ('log', t, block) | ('head', t, n) | ('drop', t, reason) | ('conn', t)."""
    book, out = g.GapBook(), []
    for ev in events:
        kind, t = ev[0], ev[1]
        if kind == "conn":
            book.on_connect(t)
        elif kind == "log":
            out.append(book.on_log(t, 1_790_000_000.0 + t, ev[2]))
        elif kind == "head":
            book.on_head(t, ev[2])
        else:
            out.append(book.on_disconnect(t, 1_790_000_000.0 + t, ev[2]))
    return book, out


def test_logs_of_one_block_are_one_delivery():
    book, _ = _book(("conn", 0), ("log", 1.0, 100), ("log", 1.01, 100), ("log", 1.02, 100),
                    ("log", 3.0, 101), ("log", 3.5, 100))
    assert dict(book.hist) == {"2.00": 1}          # 1.0 -> 3.0 only; same block and a reorg are not gaps


def test_a_silence_cut_by_a_disconnect_is_censored_not_a_gap():
    book, out = _book(("conn", 0), ("log", 1.0, 100), ("drop", 50.0, "error:X"),
                      ("conn", 51.0), ("log", 52.0, 130), ("log", 54.0, 131))
    assert dict(book.hist) == {"2.00": 1}
    assert [c["kind"] for c in book.censored] == ["error:X"]
    assert out[1] is None                             # only a forced silence is verified


def test_a_forced_silence_is_verified_over_the_heads_it_saw():
    events = [("conn", 0), ("log", 1.0, 100)] + [("head", 2.0 * i, 100 + i) for i in range(1, 70)]
    book, _ = _book(*events)
    assert book.should_force(121.5)
    rec = book.on_disconnect(121.5, 0.0, "forced")
    assert rec["from_block"] == 100 and rec["to_block"] == 169 and rec["heads"] == 69


def test_no_force_while_heads_are_also_silent():
    book, _ = _book(("conn", 0), ("log", 1.0, 100), ("head", 2.0, 101))
    assert not book.should_force(500.0)              # a dark socket is the silent-socket rule's job


def test_a_failed_check_is_unknown_never_natural():
    rec = {"kind": "gap", "gap_s": 40.0, "heads": 20}
    assert g.classify(dict(rec, verify="unknown:HTTPError")) == "unknown"
    assert g.classify(dict(rec, verify=None)) == "unknown"
    assert g.classify(dict(rec, verify=0)) == "natural"
    assert g.classify(dict(rec, verify=3)) == "dead"
    assert g.classify(dict(rec, verify=3, gap_s=12.0)) == "missed_short"
    assert g.classify(dict(rec, verify=3, heads=1)) == "missed_short"


def test_verdict_follows_the_preregistered_rule():
    fast = {"%.2f" % 2.0: 9990, "%.2f" % 4.0: 10}
    base = {"gap_hist": fast, "connected_s": 23 * 3600.0, "long": [], "censored": []}
    assert g.verdict(base)["verdict"] == "qualifies"
    assert g.verdict(dict(base, connected_s=19 * 3600.0))["verdict"] == "insufficient"
    two_quiet = [{"kind": "gap", "gap_s": 45.0, "heads": 20, "verify": 0}] * 2
    assert g.verdict(dict(base, long=two_quiet))["verdict"] == "does_not_qualify"
    one_unknown = [{"kind": "gap", "gap_s": 45.0, "heads": 20, "verify": "unknown:X"}] * 2
    assert g.verdict(dict(base, long=one_unknown))["verdict"] == "qualifies"
    slow = {"%.2f" % 2.0: 900, "%.2f" % 8.0: 100}
    assert g.verdict(dict(base, gap_hist=slow))["verdict"] == "does_not_qualify"
    assert g.verdict({"gap_hist": {}, "connected_s": 0})["verdict"] == "insufficient"


def test_a_socket_gap_between_adjacent_blocks_is_a_delivery_stall_not_natural():
    """Addendum 4: 8453 interim 2026-09-30, three 16.8-28.7 s gaps between blocks N and N+1 whose chain
    timestamps sit 2 s apart. No block lies between them, so the empty getLogs range proves nothing."""
    stall = {"kind": "gap", "gap_s": 45.0, "heads": 1, "verify": 0, "from_block": 500, "to_block": 501}
    quiet = dict(stall, from_block=500, to_block=530, heads=20)
    assert g.classify(stall) == "delivery_stall"
    assert g.classify(quiet) == "natural"
    assert g.classify(stall, poll=True) == "natural"     # chain-side polling: an adjacent gap is a block stall
    base = {"gap_hist": {"2.00": 9990, "4.00": 10}, "connected_s": 23 * 3600.0, "censored": []}
    out = g.verdict(dict(base, long=[stall] * 2))
    assert out["verdict"] == "qualifies" and out["natural_at_or_over_T"] == 0
    assert out["checked"] == {"delivery_stall": 2} and out["delivery_stalls"] == [[None, 45.0]] * 2
    polled = g.verdict(dict(base, long=[stall] * 2, mode="poll"))
    assert polled["verdict"] == "does_not_qualify" and polled["checked"] == {"natural": 2}
    assert polled["delivery_stalls"] == []


def test_quantile():
    assert g.quantile({"1.00": 99, "9.00": 1}, 0.99) == 1.0
    assert g.quantile({"1.00": 98, "9.00": 2}, 0.99) == 9.0
    assert g.quantile({}, 0.5) is None


def test_the_http_twin_keeps_host_and_key():
    assert g.http_from_ws("wss://bsc-mainnet.nodereal.io/ws/v1/abc123") == "https://bsc-mainnet.nodereal.io/v1/abc123"
    assert g.http_from_ws("wss://base.gateway.tenderly.co") == "https://base.gateway.tenderly.co"
    assert g.http_from_ws("wss://x.quiknode.pro/k/") == "https://x.quiknode.pro/k/"


def test_only_the_lanes_own_var_leaves_the_taken_set(monkeypatch):
    import pytest
    url = "wss://bsc-mainnet.nodereal.io/ws/v1/SECRETKEY"
    monkeypatch.setenv("MEMECOIN_RESEARCH_WS_URL_56", url)
    g.refuse_taken([url, g.http_from_ws(url)], own_vars=["MEMECOIN_RESEARCH_WS_URL_56"])
    with pytest.raises(SystemExit) as refused:
        g.refuse_taken([url], own_vars=[])
    assert "SECRETKEY" not in str(refused.value)
    monkeypatch.setenv("SOME_FUNDED_URL", "https://bsc-mainnet.nodereal.io/v1/other")
    with pytest.raises(SystemExit):                    # another role on the same domain still refuses
        g.refuse_taken([url], own_vars=["MEMECOIN_RESEARCH_WS_URL_56"])


def test_block_stats_counts_coverage_and_the_longest_empty_run():
    assert g.block_stats({10, 11, 15}, 10, 19) == {"coverage": 0.3, "longest_empty_run": 4}


def test_frames_give_the_daily_cost():
    book = g.GapBook()
    book.on_connect(0.0)
    for i in range(10):
        book.on_frame(1_790_000_000.0 + i, 500)
    st = book.state(3600.0)
    assert st["frames"] == 10 and st["bytes"] == 5000 and sum(st["hour_frames"].values()) == 10
    out = g.verdict(dict(st, gap_hist={"2.00": 5}))
    assert out["frames_per_day"] == 240 and out["mb_per_day"] == 0.1


class _Chain:
    """A fake chain: 1 s blocks from ts 1_790_000_000; `canary` holds the blocks with a USDC Transfer."""

    def __init__(self, canary, head=1000, fail_from=None, fail_times=0, ts=None):
        self.canary, self.head, self.fail_from, self.fail_times, self.getlogs = set(canary), head, fail_from, fail_times, []
        self.ts = ts or (lambda b: 1_790_000_000.0 + b)

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            t = self.ts(int(params[0], 16))
            return {"timestamp": hex(int(t)), "milliTimestamp": hex(int(round(t * 1000)))}
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        self.getlogs.append((lo, hi))
        if lo == self.fail_from and self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("down")
        return [{"blockNumber": hex(b), "blockTimestamp": hex(int(self.ts(b)))}
                for b in sorted(self.canary) if lo <= b <= hi for _ in range(2)]


def _poll(chain, heads):
    book = g.GapBook()
    poll = g.ChainPoll(book, "0xusdc")
    for h in heads:
        chain.head = h
        poll.cycle(chain, sleep=lambda s: None)
    return book, poll


def test_poll_gaps_are_chain_time_between_canary_blocks():
    lag = g.POLL_LAG_BLOCKS
    chain = _Chain({996, 997, 1000, 1030, 1031}, head=995 + lag)
    book, poll = _poll(chain, [995 + lag, 1040 + lag])
    assert dict(book.hist) == {"1.00": 2, "3.00": 1, "30.00": 1}
    assert [r["verify"] for r in book.long] == [0] and g.classify(book.long[0]) == "natural"
    assert poll.next_block == 1041 and chain.getlogs == [(996, 1040)]


def test_poll_chunks_the_range():
    lag = g.POLL_LAG_BLOCKS
    chain = _Chain(set(), head=100 + lag)
    _poll(chain, [100 + lag, 100 + lag + 450])
    assert chain.getlogs == [(101, 300), (301, 500), (501, 550)]


def test_one_failed_read_is_retried_from_the_same_block():
    lag = g.POLL_LAG_BLOCKS
    chain = _Chain({996, 1010, 1020}, head=995 + lag, fail_from=996, fail_times=1)
    book, poll = _poll(chain, [995 + lag, 1005 + lag, 1025 + lag])
    assert chain.getlogs == [(996, 1005), (996, 1025)]
    assert dict(book.hist) == {"14.00": 1, "10.00": 1} and poll.unread == []


def test_a_range_failing_three_cycles_is_unread_and_its_gap_censored():
    lag = g.POLL_LAG_BLOCKS
    chain = _Chain({996, 1500, 1510}, head=995 + lag, fail_from=997, fail_times=3)
    book, poll = _poll(chain, [995 + lag, 996 + lag])          # reads 996: a canary block
    for h in (1100, 1101, 1102):
        chain.head = h + lag
        poll.cycle(chain, sleep=lambda s: None)
    assert poll.unread == [[997, 1102]]                         # the third failure's chunk ends at hi
    chain.head = 1510 + lag
    poll.cycle(chain, sleep=lambda s: None)
    assert dict(book.hist) == {"10.00": 1}                      # 1500 -> 1510 only; 996 -> 1500 censored
    assert [c["kind"] for c in book.censored] == ["unread"]
    assert book.state(poll.chain_now())["connected_s"] == 2 + 408   # 995..997 and 1102..1510; unread excluded


def test_verdict_reports_hour_p99_and_cu_per_day():
    book = g.GapBook()
    book.on_connect(0.0)
    for i in range(1, 5):
        book.on_log(float(i), 1_790_000_000.0 + i, i)
    st = dict(book.state(3600.0), calls={"eth_getLogs": 10, "eth_blockNumber": 10, "eth_getBlockByNumber": 10})
    out = g.verdict(st)
    assert out["cu_per_day"] == 24 * 700 and list(out["hour_p99"].values()) == [1.0]


def test_a_block_stall_survives_the_window_mean():
    """Gamma MEDIUM (addendum 3): 0.45 s blocks, a 12 s stall between 1060 and 1061, a log in every
    block. The window mean alone priced every gap at 0.54 s and the stall vanished."""
    lag = g.POLL_LAG_BLOCKS
    ts = lambda b: 1_790_000_000.3 + 0.45 * (b - 1000) + (12.0 if b >= 1061 else 0.0)  # noqa: E731
    chain = _Chain(set(range(1000, 1131)), head=999 + lag, ts=ts)
    book, poll = _poll(chain, [999 + lag, 1130 + lag])
    gaps = sorted(float(k) for k, n in book.hist.items() for _ in range(n))
    assert gaps[-1] == 12.45                                     # exact: both ends' milliTimestamp
    assert gaps[-2] < 2.0 and gaps[len(gaps) // 2] < 0.6 and len(book.long) == 1
    assert poll.clamped > 0 and 0.53 < poll.window_max_s < 0.56 and poll.windows_over == 0
    assert poll.exact == 1 and poll.exact_failed == 0


def test_a_failed_exact_read_keeps_the_clamped_gap_and_is_counted():
    lag = g.POLL_LAG_BLOCKS
    ts = lambda b: 1_790_000_000.3 + 0.45 * (b - 1000) + (12.0 if b >= 1061 else 0.0)  # noqa: E731
    chain = _Chain(set(range(1000, 1131)), head=999 + lag, ts=ts)
    book, poll = _poll(chain, [999 + lag])
    inner = chain.__call__

    def flaky(method, params):
        if method == "eth_getBlockByNumber" and int(params[0], 16) in (1060, 1061):
            raise OSError("down")
        return inner(method, params)
    chain.head = 1130 + lag
    poll.cycle(flaky, sleep=lambda s: None)
    gaps = sorted(float(k) for k, n in book.hist.items() for _ in range(n))
    assert 10.45 <= gaps[-1] <= 14.45 and poll.exact_failed == 1 and poll.exact == 0


def test_anchors_use_milliseconds():
    lag = g.POLL_LAG_BLOCKS
    ts = lambda b: 1_790_000_000.0 + 0.45 * (b - 1000)  # noqa: E731
    chain = _Chain({1001, 1002}, head=1000 + lag, ts=ts)
    book, poll = _poll(chain, [1000 + lag, 1010 + lag])
    assert dict(book.hist) == {"0.45": 1} and abs(poll.window_max_s - 0.45) < 1e-9
