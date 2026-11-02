import json
from contextlib import AbstractContextManager
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from eth_abi import encode
from sniper.research.memecoin_opportunity.decoders import base58_encode
from sniper.services import memecoin_opportunity_live as live

from sniper.services.memecoin_opportunity_live import (
    bounded_solana_recovery,
    evm_logs_subscribe_frame,
    parse_evm_log_notification,
    parse_solana_log_notification,
    program_data_logs,
    solana_program_subscribe_frame,
    json_rpc_http,
    json_rpc_batch_http,
    make_bags_4663_state_enricher,
    make_bags_4663_remote_probe,
    make_bags_4663_quote_provider,
    read_bags_4663_token_state,
)
from sniper.research.memecoin_opportunity.bags_4663 import BAGS_LENS
from sniper.research.memecoin_opportunity.decoders import DecodedVenueEvent
from sniper.research.memecoin_opportunity.types import EventKind, LifecycleState, VenueId


def test_solana_subscription_is_program_level_and_confirmed():
    frame = json.loads(solana_program_subscribe_frame("Program1", request_id=7))
    assert frame["method"] == "logsSubscribe"
    assert frame["params"] == [{"mentions": ["Program1"]}, {"commitment": "confirmed"}]


def test_program_data_logs_respect_program_invocation_stack():
    logs = [
        "Program Foreign invoke [1]",
        "Program data: Zm9yZWlnbg==",
        "Program Foreign success",
        "Program Target invoke [1]",
        "Program data: dGFyZ2V0",
        "Program Other invoke [2]",
        "Program data: b3RoZXI=",
        "Program Other success",
        "Program data: dGFyZ2V0Mg==",
        "Program Target success",
    ]
    assert program_data_logs(logs, "Target") == (
        (4, "Program data: dGFyZ2V0"),
        (8, "Program data: dGFyZ2V0Mg=="),
    )


def test_program_cpi_event_logs_extract_current_anchor_event_payload_and_order():
    event = bytes.fromhex("e432f655cb428625") + bytes(range(137))
    transaction = {
        "transaction": {"message": {"accountKeys": ["payer", "Program1"]}},
        "meta": {
            "loadedAddresses": {"writable": [], "readonly": []},
            "innerInstructions": [
                {
                    "index": 7,
                    "instructions": [
                        {"programId": "Foreign", "data": base58_encode(b"foreign")},
                        {
                            "programId": "Program1",
                            "data": base58_encode(
                                bytes.fromhex("e445a52e51cb9a1d") + event
                            ),
                        },
                    ],
                }
            ],
        },
    }

    rows = live.program_cpi_event_logs(transaction, "Program1")

    assert rows == ((
        7 << 16 | 1,
        "Program data: 5DL2VctChiUAAQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyAhIiMkJSYnKCkqKywtLi8wMTIzNDU2Nzg5Ojs8PT4/QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl9gYWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXp7fH1+f4CBgoOEhYaHiA==",
    ),)


def test_solana_notification_parser_preserves_signature_slot_and_error():
    msg = {
        "params": {
            "subscription": 42,
            "result": {
                "context": {"slot": 123},
                "value": {"signature": "Sig1", "err": None, "logs": ["Program X success"]},
            },
        }
    }
    parsed = parse_solana_log_notification(msg)
    assert parsed == {"subscription": 42, "slot": 123, "signature": "Sig1", "err": None, "logs": ["Program X success"]}


def test_evm_subscription_and_notification_preserve_native_order():
    frame = json.loads(evm_logs_subscribe_frame(["0xaaa", "0xbbb"], request_id=9))
    assert frame["method"] == "eth_subscribe"
    assert frame["params"][1]["topics"] == [["0xaaa", "0xbbb"]]
    msg = {
        "params": {"result": {
            "address": "0xABC", "topics": ["0xaaa"], "data": "0x",
            "blockNumber": "0x10", "transactionIndex": "0x2", "logIndex": "0x3",
            "blockHash": "0xblock", "transactionHash": "0xtx", "removed": False,
        }}
    }
    parsed = parse_evm_log_notification(msg)
    assert parsed["block_or_slot"] == 16
    assert parsed["transaction_index"] == 2
    assert parsed["event_index"] == 3
    assert parsed["confirmation"] == "tentative"


def test_bounded_solana_recovery_does_not_claim_completion_at_page_bound():
    pages = {
        None: ([{"signature": "new"}, {"signature": "middle"}], "middle"),
        "middle": ([{"signature": "old"}], "old"),
    }

    result = bounded_solana_recovery(
        lambda before: pages[before],
        previous_durable_signature="older-than-old",
        max_pages=2,
    )
    assert result.signatures == ("new", "middle", "old")
    assert result.complete is False
    assert result.resume_cursor == "old"


def test_bounded_solana_recovery_resumes_after_the_persisted_page_cursor():
    requested = []
    pages = {
        "resume-here": (
            [{"signature": "older"}, {"signature": "durable"}],
            "durable",
        ),
    }

    def fetch_page(before):
        requested.append(before)
        return pages[before]

    result = bounded_solana_recovery(
        fetch_page,
        previous_durable_signature="durable",
        max_pages=1,
        start_before="resume-here",
    )

    assert requested == ["resume-here"]
    assert result.signatures == ("older",)
    assert result.complete is True
    assert result.resume_cursor is None


def test_bounded_solana_recovery_stops_at_bootstrap_time_boundary():
    rows = [
        {"signature": "new", "blockTime": 101},
        {"signature": "before-start", "blockTime": 99},
    ]

    result = bounded_solana_recovery(
        lambda _before: (rows, "before-start"),
        previous_durable_signature=None,
        not_before_timestamp_s=100,
        max_pages=1,
    )

    assert result.signatures == ("new",)
    assert result.complete is True
    assert result.resume_cursor is None


def test_json_rpc_http_sends_provider_compatible_user_agent(monkeypatch):
    seen = {}

    class Response(AbstractContextManager):
        def read(self):
            return b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'

        def __exit__(self, *_args):
            return False

    def open_request(request, *, timeout):
        seen["user_agent"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)

    assert json_rpc_http("https://fixture.invalid", "eth_blockNumber", []) == "0x1"
    assert seen == {"user_agent": "Sniper-Opportunity-Radar/1", "timeout": 10.0}


def test_json_rpc_http_retries_rate_limit_without_dropping_observation(monkeypatch):
    attempts = 0
    sleeps = []

    class Response(AbstractContextManager):
        def read(self):
            return b'{"jsonrpc":"2.0","id":1,"result":"0x2"}'

        def __exit__(self, *_args):
            return False

    def open_request(_request, *, timeout):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise HTTPError("https://fixture.invalid", 429, "rate limited", {}, None)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    monkeypatch.setattr("time.sleep", sleeps.append)

    assert json_rpc_http("https://fixture.invalid", "eth_blockNumber", []) == "0x2"
    assert attempts == 3
    assert sleeps == [0.25, 0.5]


def test_json_rpc_batch_http_preserves_call_order(monkeypatch):
    class Response(AbstractContextManager):
        def read(self):
            return b'[{"jsonrpc":"2.0","id":2,"result":"b"},{"jsonrpc":"2.0","id":1,"result":"a"}]'

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda _request, *, timeout: Response())

    assert json_rpc_batch_http(
        "https://fixture.invalid",
        [("getTransaction", ["a"]), ("getTransaction", ["b"])],
    ) == ["a", "b"]


def test_json_rpc_batch_http_promotes_item_rate_limit_for_governor_backoff(monkeypatch):
    class Response(AbstractContextManager):
        def read(self):
            return b'[{"jsonrpc":"2.0","id":1,"error":{"code":429,"message":"rate limited"}}]'

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda _request, *, timeout: Response())

    with pytest.raises(HTTPError) as error:
        json_rpc_batch_http(
            "https://fixture.invalid",
            [("getTransaction", ["a"])],
        )
    assert error.value.code == 429


def test_bags_4663_lens_state_decodes_exact_reserves_and_progress():
    token = "0x1111111111111111111111111111111111111111"
    curve = "0x2222222222222222222222222222222222222222"
    fee_share = "0x3333333333333333333333333333333333333333"
    pool_id = bytes.fromhex("aa" * 32)
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return "0x" + encode(
            ["bool", "bool", "address", "address", "bytes32"] + ["uint256"] * 8,
            [
                True, False, curve, fee_share, pool_id,
                10_000, 3_700, 630_000, 900_000, 550_000, 611, 37, 3_700,
            ],
        ).hex()

    state = read_bags_4663_token_state(rpc, token)

    assert calls[0][0] == "eth_call"
    assert calls[0][1][0]["to"] == BAGS_LENS
    assert state["curve"] == curve
    assert state["real_token_reserves"] == 630_000
    assert state["virtual_token_reserves"] == 900_000
    assert state["virtual_quote_reserves"] == 550_000
    assert state["progress_bps"] == 3_700


def test_bags_4663_state_enricher_reads_the_event_block():
    token = "0x1111111111111111111111111111111111111111"
    curve = "0x2222222222222222222222222222222222222222"
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return "0x" + encode(
            ["bool", "bool", "address", "address", "bytes32"] + ["uint256"] * 8,
            [
                True, False, curve, "0x3333333333333333333333333333333333333333",
                bytes(32), 10_000, 3_700, 630_000, 900_000, 550_000, 611, 37, 3_700,
            ],
        ).hex()

    decoded = DecodedVenueEvent(
        event_kind=EventKind.BUY,
        lifecycle=LifecycleState.PRE_MIGRATION,
        token_id=token,
        curve_id=curve,
        decoder_version="fixture",
    )
    enriched = make_bags_4663_state_enricher(rpc)(
        SimpleNamespace(venue_id=VenueId.BAGS_ROBINHOOD_4663, block_or_slot=123),
        decoded,
    )

    assert calls[0][1][1] == "0x7b"
    assert enriched.real_token_reserves == 630_000
    assert enriched.progress_bps == 3_700


def test_bags_4663_state_enricher_keeps_event_when_lens_times_out():
    token = "0x1111111111111111111111111111111111111111"

    def rpc(_method, _params):
        raise TimeoutError("fixture timeout")

    decoded = DecodedVenueEvent(
        event_kind=EventKind.BUY,
        lifecycle=LifecycleState.PRE_MIGRATION,
        token_id=token,
        curve_id="0x2222222222222222222222222222222222222222",
        decoder_version="fixture",
    )

    enriched = make_bags_4663_state_enricher(rpc)(
        SimpleNamespace(venue_id=VenueId.BAGS_ROBINHOOD_4663, block_or_slot=123),
        decoded,
    )

    assert enriched.token_id == token
    assert enriched.blocker is None
    assert enriched.facts["state_enrichment_status"] == "unavailable"
    assert enriched.facts["state_enrichment_error"] == "TimeoutError"


def test_bags_4663_state_enricher_marks_exact_lens_success_available():
    token = "0x1111111111111111111111111111111111111111"
    curve = "0x2222222222222222222222222222222222222222"

    def rpc(_method, _params):
        return "0x" + encode(
            ["bool", "bool", "address", "address", "address", *(["uint256"] * 8)],
            [
                True, False, curve, "0x3333333333333333333333333333333333333333",
                "0x0000000000000000000000000000000000000000",
                1_000_000, 10_000, 630_000, 900_000, 550_000, 7, 37, 370_000,
            ],
        ).hex()

    decoded = DecodedVenueEvent(
        event_kind=EventKind.BUY,
        lifecycle=LifecycleState.PRE_MIGRATION,
        token_id=token,
        curve_id=curve,
        decoder_version="fixture",
    )

    enriched = make_bags_4663_state_enricher(rpc)(
        SimpleNamespace(venue_id=VenueId.BAGS_ROBINHOOD_4663, block_or_slot=123),
        decoded,
    )

    assert enriched.facts["state_enrichment_status"] == "available"


def test_bags_4663_remote_probe_persists_complete_block_scoped_receipt():
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x123"
        if sum(call[0] == "eth_call" for call in calls) == 1:
            return "0x" + encode(["uint256"] * 5, [1_000, 0, 0, 0, 0]).hex()
        return "0x" + encode(["uint256"] * 3, [900, 0, 0]).hex()

    state = SimpleNamespace(
        launch_id="bags-launch", venue_id=VenueId.BAGS_ROBINHOOD_4663,
        token_id="0x1111111111111111111111111111111111111111",
        curve_id="0x2222222222222222222222222222222222222222",
        quote_mint="native-4663",
    )

    result = make_bags_4663_remote_probe(rpc)(
        state, SimpleNamespace(quote_in_atomic=1_000)
    )

    assert result.provider == "bags_curve_eth_call_v2"
    assert result.quote_in_atomic == 1_000
    assert result.tokens_out_atomic == 1_000
    assert result.immediate_exit_quote_atomic == 900
    assert result.round_trip_return_bps == -1_000
    assert result.block_identity == "0x123"
    assert all(call[1][-1] == "0x123" for call in calls if call[0] == "eth_call")


def test_bags_provider_quotes_entry_and_exact_quantity_exit():
    token = "0x1111111111111111111111111111111111111111"
    curve = "0x2222222222222222222222222222222222222222"
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x123"
        data = params[0]["data"]
        if data.startswith("0x313ce567"):
            return "0x" + encode(["uint8"], [18]).hex()
        if data.startswith("0x" + live._selector("quoteBuy(uint256)").hex()):
            return "0x" + encode(["uint256"] * 5, [1_000, 0, 0, 0, 0]).hex()
        return "0x" + encode(["uint256"] * 3, [900, 0, 0]).hex()

    state = SimpleNamespace(
        launch_id="bags-launch",
        venue_id=VenueId.BAGS_ROBINHOOD_4663,
        token_id=token,
        curve_id=curve,
        quote_mint=None,
    )
    provider = make_bags_4663_quote_provider(rpc)

    entry = provider.entry(state, 1_000, "state-1")
    exit_quote = provider.exit(
        token_id=token,
        quantity_atomic=1_000,
        token_decimals=18,
        quote_decimals=18,
        route_binding=entry["route_binding"],
    )

    assert entry["expected_out_atomic"] == 1_000
    assert entry["route_binding"]["curve_id"] == curve
    assert exit_quote["expected_out_atomic"] == 900
    assert exit_quote["quote_out_asset"] == live.PONS_WETH
