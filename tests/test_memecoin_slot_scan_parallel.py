"""The slot scan walked 14 candidates one RPC round trip at a time (2026-09-03).

`evm.token_safety` is the largest substage on the funded 4663 entry path: p50 2,835 ms, p95 13,399,
max 20,409 over 73 filled entries. `_SLOT_CANDIDATES` is `range(12) + (51, 52)` and `_find_slot`
probed them serially, so an OpenZeppelin-upgradeable token (balances at 51) paid TWELVE failed
probes before its hit. At ~200 ms per round trip on 4663 that is ~2.4 s, which is the observed cost.

Why not a result cache instead: 74% of mints that run safety in 30 days run it exactly ONCE (122 of
164), so a cache can never serve them, and a re-check inside 5 minutes measured 3,560 ms against a
first check's 3,557 ms. A cache buys ~10-19% of runs and puts a staleness window on a real-money
safety gate. Parallelising serves 100% of runs and every check stays live.

The happy path stays SERIAL and therefore costs exactly what it costs today: standard ERC-20s hit on
slot 0 or 1, and firing the whole fan-out speculatively would multiply funded RPC calls on the
common case for no gain. Only the tail — reached only when the cheap prefix misses — goes wide.

Semantics that must not move: first hit wins IN CANDIDATE ORDER; a transport failure that survives
retries raises `_SlotScanTransportError` (never the not-found sentinel, which gets negative-cached
forever, 2026-07-15); a clean scan finding nothing returns None.
"""
import time

import pytest

from sniper.execution import evm_token_safety as S


def _probe(hits, *, delay=0.0, errors=(), malformed=(), slow=()):
    """read_back(key) -> the fabricated value when key is a hit, None on a clean revert.

    `slow` delays specific keys so RESPONSE order differs from CANDIDATE order."""
    seen = []

    def read_back(key):
        seen.append(key)
        if key in slow:
            time.sleep(slow[key] if isinstance(slow, dict) else 0.2)
        if delay:
            time.sleep(delay)
        if key in errors:
            raise RuntimeError("rpc down")
        if key in malformed:
            return "not-hex"
        return hex(S._BIG) if key in hits else None
    read_back.seen = seen
    return read_back


def _idx(i):
    return "0x%064x" % i if isinstance(i, int) else i


def test_the_happy_path_still_costs_exactly_one_probe():
    """Standard ERC-20s hit slot 0. Going wide here would multiply funded RPC for nothing."""
    rb = _probe({_idx(0)})
    assert S._find_slot(rb, _idx) == _idx(0)
    assert rb.seen == [_idx(0)], "the happy path fired %d probes" % len(rb.seen)


def test_a_slot_one_token_costs_two_probes():
    rb = _probe({_idx(1)})
    assert S._find_slot(rb, _idx) == _idx(1)
    assert rb.seen == [_idx(0), _idx(1)]


def test_the_upgradeable_tail_resolves_in_one_round_trip_not_twelve():
    """Slot 51 is the OZ-Upgradeable layout. Serially that is 12 failed probes before the hit."""
    rb = _probe({_idx(51)}, delay=0.05)
    t0 = time.monotonic()
    assert S._find_slot(rb, _idx) == _idx(51)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05 * 6, (
        "the tail took %.2fs — that is serial, not one concurrent wave" % elapsed)


def test_first_hit_wins_in_candidate_order_not_in_response_order():
    """Two slots would both answer. The earlier candidate must win regardless of which returns
    first, or the scan becomes nondeterministic under load."""
    # slot 3 answers LAST. Resolving in response order would hand back 51.
    rb = _probe({_idx(3), _idx(51)}, slow={_idx(3): 0.25})
    assert S._find_slot(rb, _idx) == _idx(3)


def test_a_transport_error_beats_a_later_hit():
    """Serial raised at the failing candidate and never reached the later one. Going wide must not
    let a later hit paper over an earlier inconclusive probe."""
    rb = _probe({_idx(51)}, errors={_idx(4)})
    with pytest.raises(S._SlotScanTransportError):
        S._find_slot(rb, _idx)


def test_a_malformed_result_still_raises_rather_than_returning_not_found():
    rb = _probe(set(), malformed={_idx(5)})
    with pytest.raises(S._SlotScanTransportError):
        S._find_slot(rb, _idx)


def test_a_clean_scan_that_finds_nothing_still_returns_none():
    rb = _probe(set())
    assert S._find_slot(rb, _idx) is None


def test_deterministic_bad_input_is_a_clean_not_found():
    def boom(_i):
        raise ValueError("bad index")
    assert S._find_slot(_probe(set()), boom) is None


def test_extra_slots_are_still_tried_before_the_tail():
    """Solady clones pass hashed layouts as extra_slots; they must keep their priority."""
    rb = _probe({"0xsolady", _idx(51)})
    assert S._find_slot(rb, _idx, extra_slots=("0xsolady",)) == "0xsolady"


# --- 2026-09-04: a malformed answer and a dead primary must not end the scan on one node ---

def _probe_by_url(answers):
    """read_back(key, url=None): `answers[url]` is a callable(key) -> raw. Records (key, url)."""
    seen = []

    def read_back(key, url=None):
        seen.append((key, url))
        return answers[url](key)
    read_back.seen = seen
    return read_back


def test_a_malformed_first_answer_is_retried_and_the_second_answer_wins():
    calls = {"n": 0}

    def read_back(key):
        calls["n"] += 1
        return "0x" if calls["n"] == 1 else hex(S._BIG)     # empty body once, then the real value
    assert S._find_slot(read_back, _idx) == _idx(0)
    assert calls["n"] == 2


def test_every_retry_moves_to_the_next_alternate_endpoint():
    hit = _idx(0)
    rb = _probe_by_url({
        None: lambda k: (_ for _ in ()).throw(RuntimeError("primary down")),
        "alt1": lambda k: "0x",                                # answers, but with garbage
        "alt2": lambda k: hex(S._BIG) if k == hit else None,
    })
    assert S._find_slot(rb, _idx, alternates=("alt1", "alt2")) == hit
    assert [u for _k, u in rb.seen][:3] == [None, "alt1", "alt2"]


def test_all_alternates_failing_still_raises_transport_never_not_found():
    rb = _probe_by_url({
        None: lambda k: "not-hex", "alt1": lambda k: "not-hex", "alt2": lambda k: "not-hex"})
    with pytest.raises(S._SlotScanTransportError):
        S._find_slot(rb, _idx, alternates=("alt1", "alt2"))
    urls = [u for k, u in rb.seen if k == _idx(0)]
    assert urls == [None, "alt1", "alt2", "alt1"], urls  # 4 attempts, rotating, primary first


def test_the_safety_deadline_is_never_retried_or_rotated():
    rb = _probe_by_url({None: lambda k: (_ for _ in ()).throw(S.EvmSafetyDeadlineExceeded())})
    with pytest.raises(S.EvmSafetyDeadlineExceeded):
        S._find_slot(rb, _idx, alternates=("alt1",))
    assert len(rb.seen) == 1


def test_no_pool_means_no_alternates_and_the_one_arg_read_back_still_works(monkeypatch):
    from sniper.execution import evm_rpc_pool
    monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda url: [url])
    assert S._slot_probe_alternates("https://primary") == ()
    monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda url: ["https://primary", "https://b", "https://c"])
    assert S._slot_probe_alternates("https://primary") == ("https://b", "https://c")
    assert S._slot_probe_alternates("https://b") == ("https://primary", "https://c")


def test_the_bound_safety_hands_the_pool_alternates_to_the_slot_scan(monkeypatch):
    from sniper.execution import evm_rpc_pool
    monkeypatch.setattr(evm_rpc_pool, "pool_for",
                        lambda url: ["https://primary", "https://alt"])
    captured = {}

    def fake_find_slot(read_back, slot_for_index, **kw):
        captured["alternates"] = kw.get("alternates")
        captured["url_kw_ok"] = True
        try:
            read_back("0x" + "0" * 64, url="https://alt")   # the closure must accept a url
        except Exception as exc:
            captured["url_kw_ok"] = not isinstance(exc, TypeError)
        return None
    monkeypatch.setattr(S, "_find_slot", fake_find_slot)

    def http(_m, _url, body=None, **_kw):
        return 200, {"result": "0x6000"}, None            # any call: contract code / a revert
    fn = S.build_evm_token_safety(
        chain_id="4663", rpc_url="https://primary", taker="0x" + "11" * 20, http=http,
        quote_fn=lambda *_a, **_k: {"transaction": {"to": "0x" + "22" * 20, "data": "0x"},
                                    "sellAmount": "1"})
    fn("0x" + "33" * 20)
    assert captured["alternates"] == ("https://alt",)
    assert captured["url_kw_ok"] is True


def test_a_quarantined_alternate_is_skipped_at_retry_time(monkeypatch):
    from sniper.execution import evm_rpc_pool
    monkeypatch.setattr(evm_rpc_pool, "available", lambda url: url != "alt1")
    hit = _idx(0)
    rb = _probe_by_url({
        None: lambda k: (_ for _ in ()).throw(RuntimeError("primary down")),
        "alt1": lambda k: (_ for _ in ()).throw(AssertionError("must not be asked")),
        "alt2": lambda k: hex(S._BIG) if k == hit else None,
    })
    assert S._find_slot(rb, _idx, alternates=("alt1", "alt2")) == hit
    assert [u for _k, u in rb.seen][:2] == [None, "alt2"]


def test_when_every_alternate_is_quarantined_the_configured_order_is_still_tried(monkeypatch):
    from sniper.execution import evm_rpc_pool
    monkeypatch.setattr(evm_rpc_pool, "available", lambda url: False)
    rb = _probe_by_url({None: lambda k: "0x", "alt1": lambda k: hex(S._BIG)})
    assert S._find_slot(rb, _idx, alternates=("alt1",)) == _idx(0)


def test_a_malformed_permit2_allowance_answer_is_retried_on_the_next_endpoint(monkeypatch):
    """LIGER 2026-09-04 23:14:51: the Permit2 allowance read parsed `0x` outside the retry."""
    from sniper.execution import evm_rpc_pool
    import sniper.execution.evm_real_seams as seams
    monkeypatch.setattr(evm_rpc_pool, "pool_for", lambda url: ["https://primary", "https://alt"])
    calls = []

    def fake_eth_call_raw(to, data, rpc_url, *, http=None, overrides=None, from_addr=None, raise_on_error=False):
        calls.append(rpc_url)
        if data.startswith("0x" + "dd62ed3e"):              # allowance(owner, spender)
            return "0x" if rpc_url == "https://primary" else "0x" + "%064x" % (10 ** 30)
        return None                                         # every slot probe: clean not-found
    monkeypatch.setattr(seams, "eth_call_raw", fake_eth_call_raw)

    def http(_m, _url, body=None, **_kw):
        return 200, {"result": "0x6000"}, None
    fn = S.build_evm_token_safety(
        chain_id="4663", rpc_url="https://primary", taker="0x" + "11" * 20, http=http,
        quote_fn=lambda *_a, **_k: {"transaction": {"to": "0x" + "22" * 20, "data": "0x"},
                                    "sellAmount": "1",
                                    "issues": {"allowance": {"spender": "0x000000000022D473030F116dDEE9F6B43aC78BA3"}}})
    ok, reason = fn("0x" + "33" * 20)
    assert "sell simulation failed" not in reason, reason
    assert "https://alt" in calls, "the second endpoint must have been asked"


def test_on_bsc_the_upgradeable_layout_leads_so_a_four_meme_token_costs_one_probe():
    """73851 (2026-10-14, 56): slot discovery took 298 ms of a 317 ms inline safety check, because
    185 of 209 stored 56 layouts are OZ-Upgradeable (balances 51, allowances 52; all 184 '...7777'
    mints) and the serial prefix walked 0, 1 and Solady before the tail found 51. On 56 the
    measured layout leads; elsewhere the order is unchanged."""
    lead = S._lead_slots("56", "0x" + "ab" * 18 + "7777", _idx, b20_index=4, which="balance")
    assert lead == (_idx(51),)
    assert S._lead_slots("56", "0x" + "ab" * 20, _idx, b20_index=5, which="allowance") == (_idx(52),)
    assert S._lead_slots("4663", "0x" + "ab" * 20, _idx, b20_index=4, which="balance") == ()
    rb = _probe({_idx(51)})
    assert S._find_slot(rb, _idx, lead_slots=lead) == _idx(51)
    assert rb.seen == [_idx(51)], "a 56 upgradeable token fired %d probes" % len(rb.seen)
    rb = _probe({_idx(0)})                       # a slot-0 token on 56 pays exactly one extra probe
    assert S._find_slot(rb, _idx, lead_slots=lead) == _idx(0)
    assert rb.seen == [_idx(51), _idx(0)]
