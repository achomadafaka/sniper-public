import base64
import pytest
from scripts.research.evm_source_visibility import comparisons, feed_hashes, listen, paired, raw_transactions


def test_nested_feed_and_truncation():
    raw = b"\x02signed"
    msg = b"\x04" + raw
    batch = b"\x03" + len(msg).to_bytes(8, "big") + msg
    assert list(raw_transactions(batch)) == [raw]
    for bad in (batch[:-1], batch + b"x", b"\x04"):
        with pytest.raises(ValueError):
            list(raw_transactions(bad))
    with pytest.raises(ValueError):
        list(raw_transactions(batch, depth=9))


def test_feed_hash_matches_signed_bytes():
    from eth_hash.auto import keccak
    raw = b"\x02signed"
    frame = {"messages": [{"message": {"message": {"l2Msg":
              base64.b64encode(b"\x04" + raw).decode()}}}]}
    assert feed_hashes(frame) == ["0x" + keccak(raw).hex()]
    frame["messages"][0]["message"]["message"]["l2Msg"] = "!"
    with pytest.raises(ValueError):
        feed_hashes(frame)


def test_pair_keeps_unmatched_and_negative_leads():
    result = paired({"early": 11., "late": 14.},
                    {"early": 12., "late": 13., "missing": 14., "boundary": 9.}, 10., 20.)
    assert result == {"eligible_sealed": 3, "matched": 2, "seen_before_sealed": 1,
                      "missing": 1, "lead_ms": {"n": 2, "p50": 1000., "p90": 1000.,
                                                 "min": -1000., "max": 1000.}}


def test_zero_and_unknown_stay_distinct():
    assert paired({"a": 10.}, {"a": 10.}, 9., 11.)["lead_ms"]["p50"] == 0.
    result = paired({}, {"a": 10.}, 9., 11.)
    assert result["missing"] == 1 and result["lead_ms"]["p50"] is None
    assert paired({}, {}, 9., 11.)["eligible_sealed"] == 0


def test_failed_stream_censors_comparison():
    good = {"start": 0., "end": 30., "errors": [], "events": {}}
    result = {k: dict(good) for k in ("bsc_public", "base_preconf", "base_sealed", "rh_feed", "rh_sealed")}
    result["rh_feed"] = {**good, "start": None, "end": 1., "errors": ["identity_refused"]}
    report = comparisons(result)["rh_feed"]
    assert report["complete"] is False and report["overlap_s"] == 0
    assert report["v2_v3_swap_transactions"]["lead_ms"]["p50"] is None


def test_pair_censors_union_of_retracted_transactions():
    good = {"start": 1., "end": 30., "errors": [], "events": {}, "removed": []}
    result = {k: dict(good) for k in ("bsc_public", "base_preconf", "base_sealed", "rh_feed", "rh_sealed")}
    result["base_preconf"] = {**good, "removed": ["reorg"]}
    result["base_sealed"] = {**good, "events": {"swap": {"reorg": 10., "fresh": 11.}}}
    report = comparisons(result)["base_preconf"]
    assert report["v2_v3_swap_transactions"]["eligible_sealed"] == 1
    assert report["retracted_censored"] == 1


def test_zero_start_is_valid_clock_value():
    good = {"start": 0., "end": 30., "errors": [], "events": {"swap": {"h": 10.}}}
    result = {k: dict(good) for k in ("bsc_public", "base_preconf", "base_sealed", "rh_feed", "rh_sealed")}
    assert comparisons(result)["base_preconf"]["v2_v3_swap_transactions"]["matched"] == 1


def test_output_created_during_capture_is_not_overwritten(tmp_path, monkeypatch):
    import sys
    import scripts.research.evm_source_visibility as module
    import sniper.persistence.db_core
    import sniper.services.axiom_address_verify
    out = tmp_path / "proof.json"

    class Connection:
        def execute(self, *a):
            return self

        def fetchone(self):
            return (0,)

        def __iter__(self):
            return iter(())

        def close(self):
            pass

    async def capture(*args):
        out.write_text("earlier proof")
        return {}

    monkeypatch.setattr(sys, "argv", ["probe", "--out", str(out)])
    monkeypatch.setattr(sniper.persistence.db_core, "get_readonly_connection", lambda *a: Connection())
    monkeypatch.setattr(sniper.services.axiom_address_verify, "evm_watched_owners", lambda *a: {})
    monkeypatch.setattr(module, "capture", capture)
    monkeypatch.setattr(module, "comparisons", lambda *a: {})
    with pytest.raises(FileExistsError):
        module.main()
    assert out.read_text() == "earlier proof"


@pytest.mark.parametrize("reply,reason", [
    ({"result": "0x1"}, "wrong_chain"),
    ({"error": {"code": -32429}}, "identity_refused"),
])
def test_wrong_or_rejected_chain_never_subscribes(monkeypatch, reply, reason):
    import asyncio
    import json
    import websockets
    calls = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, raw):
            calls.append(json.loads(raw)["method"])

        async def recv(self):
            return json.dumps(reply)

    monkeypatch.setattr(websockets, "connect", lambda *a, **kw: Socket())
    result = {}
    asyncio.run(listen("test", "wss://example.invalid", "56", [("heads", ["newHeads"])], 15, result))
    assert calls == ["eth_chainId"]
    assert result["test"]["errors"] == [reason]
