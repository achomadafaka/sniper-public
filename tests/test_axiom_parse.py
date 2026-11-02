"""Tests for sniper/feeds/axiom_parse.py.

Real shapes come from tests/fixtures/axiom/ws_frames.jsonl (Alpha, 2026-09-16,
not committed) and data/axiom_raw.db (table axiom_raw_messages,
transport='rest', read in place — not committed, not this repo's tracked
data). This file embeds small, trimmed excerpts of real public
per-token/per-caller/per-trader data, not the operator's own account state.
"""
from __future__ import annotations

import json
import logging

import pytest

from sniper.feeds import axiom_parse as ap

LIST_PARSERS = [
    ap.parse_trader_directory,
    ap.parse_trader_positions,
    ap.parse_discovery,
    ap.parse_kol_callouts,
    ap.parse_wallet_transactions,
]


@pytest.fixture(autouse=True)
def _reset_refusal_dedup():
    """_refuse() dedupes by (context, first-40-bytes) for the process's
    life, by design (Alpha, 2026-09-16: stop the 30/s log flood). Tests share
    one process, so reset the module-level state each test to keep them
    independent."""
    ap._LOGGED_REFUSALS.clear()
    yield
    ap._LOGGED_REFUSALS.clear()


class TestParseMoneyCompact:
    def test_millions(self):
        assert ap.parse_money_compact("$1.68M") == 1680000.0

    def test_plus_thousands(self):
        assert ap.parse_money_compact("+$434K") == 434000.0

    def test_em_dash_is_none(self):
        assert ap.parse_money_compact("—") is None

    def test_negative_thousands(self):
        assert ap.parse_money_compact("-$12.3K") == -12300.0

    def test_plain_number(self):
        assert ap.parse_money_compact("1234.5") == 1234.5

    def test_billions(self):
        assert ap.parse_money_compact("$2.1B") == 2100000000.0

    def test_empty_string_is_none(self):
        assert ap.parse_money_compact("") is None

    def test_none_input_is_none(self):
        assert ap.parse_money_compact(None) is None

    def test_non_numeric_text_is_none(self):
        assert ap.parse_money_compact("not a number") is None

    def test_non_string_input_is_none(self):
        assert ap.parse_money_compact(1680000.0) is None


class TestNormalizeChain:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("sol", "solana"),
            ("SOL", "solana"),
            ("solana", "solana"),
            ("robinhood", "4663"),
            ("bnb", "56"),
            ("BNB", "56"),
            ("base", "8453"),
        ],
    )
    def test_known_chains(self, raw, expected):
        assert ap.normalize_chain(raw) == expected

    def test_unknown_chain_is_none(self):
        assert ap.normalize_chain("ethereum") is None

    def test_none_input_is_none(self):
        assert ap.normalize_chain(None) is None

    def test_empty_string_is_none(self):
        assert ap.normalize_chain("") is None


class TestTruncatedAddress:
    def test_truncated_pattern_detected(self):
        assert ap._looks_truncated_address("Cqu5uK...QSsR") is True

    def test_full_address_not_flagged(self):
        full = "Cqu5uKJKz1mF3P8XyN6tR9WvL2hDpGxAeBcMdQnUxYzS"
        assert ap._looks_truncated_address(full) is False

    def test_none_not_flagged(self):
        assert ap._looks_truncated_address(None) is False

    def test_plain_word_not_flagged(self):
        assert ap._looks_truncated_address("solana") is False


class TestRefuseUnknownShape:
    """Refusal has two tiers (module docstring):
    - LOUD: input that looks like JSON (starts with "{" or "[") but does not
      parse, or parses to an unrecognized shape. Logs the first 200 bytes,
      once per (parser, first 40 bytes) for the process's life.
    - QUIET: input that does not look like JSON at all (e.g. the binary
      frames observed on wss://pulse-unified2.axiom.trade/ws). No log.
    """

    @pytest.mark.parametrize("parser", LIST_PARSERS)
    def test_non_json_garbage_returns_empty_list_quietly(self, parser, caplog):
        with caplog.at_level(logging.WARNING):
            result = parser("not json at all, plain garbage text")
        assert result == []
        assert caplog.text == ""

    @pytest.mark.parametrize("parser", LIST_PARSERS)
    def test_none_input_returns_empty_list_quietly(self, parser, caplog):
        with caplog.at_level(logging.WARNING):
            result = parser(None)
        assert result == []
        assert caplog.text == ""

    @pytest.mark.parametrize("parser", LIST_PARSERS)
    def test_json_ish_but_invalid_returns_empty_list_loudly(self, parser, caplog):
        with caplog.at_level(logging.WARNING):
            result = parser("{not valid json")
        assert result == []
        assert "not valid json" in caplog.text

    @pytest.mark.parametrize("parser", LIST_PARSERS)
    def test_unknown_top_level_key_returns_empty_list_loudly(self, parser, caplog):
        payload = json.dumps({"totally_unrecognized_key": [1, 2, 3]})
        with caplog.at_level(logging.WARNING):
            result = parser(payload)
        assert result == []
        assert caplog.text

    @pytest.mark.parametrize("parser", LIST_PARSERS)
    def test_log_truncates_to_200_bytes(self, parser, caplog):
        long_garbage = "{" + "x" * 300  # JSON-ish (loud) but invalid
        with caplog.at_level(logging.WARNING):
            parser(long_garbage)
        logged = caplog.text
        assert ("x" * 199) in logged
        assert ("x" * 250) not in logged

    @pytest.mark.parametrize("parser", LIST_PARSERS)
    def test_refusal_logs_once_then_stays_silent(self, parser, caplog):
        payload = "{not valid json, repeated"
        with caplog.at_level(logging.WARNING):
            parser(payload)
            first_log_count = len(caplog.records)
            for _ in range(5):
                parser(payload)
        assert len(caplog.records) == first_log_count  # no new log lines

    def test_parse_wallet_event_quiet_on_non_json_garbage(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = ap.parse_wallet_event("binary transport noise, not json")
        assert result is None
        assert caplog.text == ""

    def test_parse_wallet_event_loud_on_json_ish_invalid(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = ap.parse_wallet_event("{not valid json either")
        assert result is None
        assert "not valid json" in caplog.text

    def test_parse_wallet_event_none_returns_none(self):
        assert ap.parse_wallet_event(None) is None

    def test_parse_wallet_event_unknown_shape_returns_none_loudly(self, caplog):
        payload = json.dumps({"room": "not_a_wallet_room", "content": {}})
        with caplog.at_level(logging.WARNING):
            result = ap.parse_wallet_event(payload)
        assert result is None
        assert caplog.text


class TestKnownIrrelevantRooms:
    """Alpha, 2026-09-16 (live worker): these rooms are known-not-ours infra
    traffic. parse_wallet_event must return None with ZERO logging for them
    -- not even the once-per-shape dedup log."""

    @pytest.mark.parametrize(
        "room",
        [
            "block_hash",
            "sol-priority-fee-v3",
            "rh-v2|gas-prices",
            "bnb-v2|gas-prices",
            "base|gas-prices",
            "jito-bribe-fee",
            "online-users-count",
            "connection_monitor",
            "thumbnail",
            "rh-v2|thumbnail",
        ],
    )
    def test_irrelevant_room_is_silent(self, room, caplog):
        payload = json.dumps({"room": room, "content": {"anything": 1}})
        with caplog.at_level(logging.WARNING):
            result = ap.parse_wallet_event(payload)
        assert result is None
        assert caplog.text == ""

    def test_relevant_looking_room_still_refuses_loudly(self, caplog):
        # a room that is NOT on the known-irrelevant list still gets the
        # normal (deduped, logged-once) refusal -- it might be a real gap.
        payload = json.dumps({"room": "v:SomeWalletAddress", "content": {}})
        with caplog.at_level(logging.WARNING):
            result = ap.parse_wallet_event(payload)
        assert result is None
        assert caplog.text


# ---------------------------------------------------------------------------
# Real, trimmed excerpts of public data (Alpha, 2026-09-16): ws_frames.jsonl
# and data/axiom_raw.db (transport='rest'). Only the fields this module
# currently reads are kept; production rows carry more.
# ---------------------------------------------------------------------------

SOL_DISCOVERY_WS_FRAME = json.dumps(
    {
        "room": "sol-discover:trending:5m",
        "content": [
            ["Co6zVnKBtsX3tiyCLioendsirR8AYy4tbjC3142Q43r6", "5PTsE6UM92yZNRMPKfn7c6BSPYtZBeptop1vMkiwpump"],
        ],
    }
)

# A real Solana discovery row (Artificial Elon / mint ...wpump), indices
# 0-27 retain the original excerpt. Unused fields are placeholders.
# The full 57-field shape matches the saved REST response.
SOL_DISCOVERY_FULL_ROW = [
    "Co6zVnKBtsX3tiyCLioendsirR8AYy4tbjC3142Q43r6",  # 0 pairAddress -> pair
    "5PTsE6UM92yZNRMPKfn7c6BSPYtZBeptop1vMkiwpump",  # 1 tokenAddress -> mint
    "Artificial Elon", "AE", "img", "uri", 6, "Pump AMM", "{}",  # 2-8 unused
    "2026-09-16T07:25:47.244Z", "web", "x", "tg", None, None, None,  # 9-15 unused
    False, "LfEcaUf77iEhnz6gFpLqYgDb5Uk6Ekc5n69wu7Qa9Uw",  # 16-17 unused
    936725602.67387, 64.913953931929, 100, "{}", 1043, 1319.7954933119997,  # 18-23 unused
    1183.2526079885906,  # 24 marketCapSol -> mcap
    596, 447,  # 25-26 unused
    150.486151428,  # 27 liquiditySol -> liquidity
] + [None] * 29

BNB_DISCOVERY_WS_FRAME = json.dumps(
    {
        "room": "bnb-v2|discover-trending:5m",
        "content": [["0xeccbb861c0dda7efd964010085488b69317e4444", "unused_name_field"]],
    }
)

BNB_DISCOVERY_REST = json.dumps(
    [
        {
            "tokenAddress": "0xeccbb861c0dda7efd964010085488b69317e4444",
            "bestPairAddress": "0x22af7297243c4eef12e2d5a4f888b92e56bf127c",
            "pairMarketCapUsd": 205675277.32398945,
            "pairLiquidityUsd": 1929332.488947453,
            "tokenVolumeUsd": 45509.539789691706,
            "tokenName": "龙虾",
        }
    ]
)

SOL_NEW_TRENDING_REST = json.dumps(
    [
        ["Gx7XRnFb2w5EedWtDF1ULGY9wzR5LAFiaQ1nuFh2e5fp", "HpYdUftXwEaG7jekkjFvFLWeJ5dfqaFvtDHoRC6jd99s"] + [None] * 55,
    ]
)

SOL_KOL_CALLOUT_WS_FRAME = json.dumps(
    {
        "room": "gmgn-callouts",
        "content": {
            "revision": 1789343067666,
            "callout": {
                "id": "01M2MJ75DH17GSSKXW3W7BR7F6",
                "chain": "sol",
                "tokenAddress": "5NYBrbe2kbs5cy6GSpTWejNZxsYnXtcdGTGMm49qhj5P",
                "tokenTicker": "ASTRA",
                "body": "so how is this not teleporting?",
                "callerWallet": "4sAUSQFdvWRBxR8UoLBYbw8CcXuwXWxnN8pXa4mtm5nU",
                "callerHandle": "XScharo",
                "isKol": True,
                "callMarketCapUsd": 15956.883867722594,
            },
        },
    }
)

ROBINHOOD_KOL_CALLOUT_WS_FRAME = json.dumps(
    {
        "room": "rh-v2|gmgn-callouts",
        "content": {
            "revision": 1789182899704,
            "callout": {
                "id": "01M2MJ68CZS6NAFDABA3T6ZM4J",
                "chain": "robinhood",
                "tokenAddress": "0x7ab99d14d34ccdaaa0289ba46cb2a4ae5096b63d",
                "tokenTicker": "Relay",
                "body": "dev previous hit 100k yesterday.",
                "callerWallet": "0x7b5e09b9c71c188e3f52b0ae2463afedca8f4b63",
                "callerHandle": "MegaQuarterlySubmarine",
                "isKol": False,
                "callMarketCapUsd": 24068.638,
            },
        },
    }
)

KOL_CALLOUTS_REST_FEED = json.dumps(
    {
        "callouts": [
            {
                "id": "01M2MJFE5S93PCS56SCGBQA0FE",
                "chain": "sol",
                "tokenAddress": "7ezivZWpR1GJvqDcncsrV4c1U6sAjndfaamm6CqMzsz2",
                "tokenTicker": "GIN",
                "body": "This is the good PAID, why it doesnt go ? ",
                "callerHandle": "Romeo10MPnL",
                "callMarketCapUsd": 3360.8445,
            },
            {
                "id": "01M2MJF5X8K8KJVRXF6731S3M0",
                "chain": "sol",
                "tokenAddress": "bykrhkExmjWco2mFxKGX3XPmPVaZB9JJp9MkRe8pump",
                "tokenTicker": "Shrek",
                "body": "Elon fucks w shrek ",
                "callerHandle": "drippyUnc",
                "callMarketCapUsd": 765650.6618303066,
            },
        ]
    }
)

# One real, trimmed page of REST */callouts-feed (Axiom's native schema, a
# bare top-level list -- NOT a {"callouts": [...]} wrapper around the gmgn
# object). The second row's empty callerHandle and the third row's
# kind="manifesto" are both real, both meant to be dropped.
NATIVE_CALLOUTS_FEED_REST = json.dumps(
    [
        {
            "id": "46e4dde2-dc43-4cd0-8324-1d214ee1c6ca",
            "chain": "sol",
            "kind": "callout",
            "body": "truno where u at",
            "callerHandle": "chesterer",
            "tokenAddress": "7YEX6FHygjyPvLbWPJpBeVUburdCUhwWSUVZ8MxKR8sB",
            "marketCapUsd": 9887.648217187518,
            "marketCapUsdAtPost": 7921.5985189933,
        },
        {
            "id": "c30c360a-35db-491b-9d0b-1ea54b06c390",
            "chain": "sol",
            "kind": "callout",
            "body": "crank",
            "callerHandle": "",
            "xHandle": None,
            "tokenAddress": "HpYdUftXwEaG7jekkjFvFLWeJ5dfqaFvtDHoRC6jd99s",
            "marketCapUsdAtPost": 43340.2720973493,
        },
        {
            "id": "manifesto-example",
            "chain": "sol",
            "kind": "manifesto",
            "body": "a caller bio, not a trade call",
            "callerHandle": "somecaller",
            "tokenAddress": "SomeMintThatShouldNeverAppearInOutput11111",
            "marketCapUsdAtPost": 1234.0,
        },
    ]
)

# One real, trimmed tracked-wallet-transactions-v4 row (25 elements),
# api6.axiom.trade (unprefixed host -> solana).
WALLET_TX_ROW_BUY = [
    "2026-09-16T07:38:47.872Z",
    "4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD",
    "4P98Z8TSf9ATB9TDT4uPGXQQu3JUmDhyheqFUTgH7MPA84Jk4HxcyATCsUmb31SkbN5f5hZhMnmiHYD1eDiib1EM",
    0.00021889520570568527, 0.02123940180962264, 5156.192601, 1.153645006698959,
    111.93817499999999, 0.001666966, "buy", "Buy More",
    "5NKuJAfCVYn8c8obNVdxahAhk43KKaMUnDxCCAax6pYP",
    "2026-09-16T07:31:38.344Z",
    37046.91890893626, 16002.037626864361, -327.94067196270794,
    "4MMQY9bwkxxTtsK3W227Q5ABT6yFY8Pmn9Ze7wmAXKY8", "ALLINU", "ALLINU",
    "https://axiomtrading.sfo3.cdn.digitaloceanspaces.com/3jt62QcVQvTu2gCCYFWPBnVNgQ2uv5fVxkSa9hFepump.webp",
    6, 999784324.03991, "Meteora DLMM", 0, 93.7438516541276,
]

WALLET_TX_ROW_SELL = [
    "2026-09-16T07:20:46.703Z",
    "MTWazEsbPPei3XPAcJzD1n3VpUeNinGYid4UVPx7FtY",
    "2PMLFhucG7iaQPBmg8UxHW9BQGdCHkFR6xfSA7FLMAFEM5o6cUK1KmrrCtYPEL8SJEDVU8FouMvtNruvtsgZpxxa",
    0.00010604957681298305, 0.010292111429700004, 43619.051212, 4.591692474,
    445.6237546017, 8.8289e-05, "sell", "Sell Partial",
    "EjTLLhayt7s5F9iA143PsCbb9UzvzqG7Q3Q1KJTiLmWh",
    "2026-09-16T01:08:54.625Z",
    108294.17545999515, 71876.36905517813, -604.882769825,
    "98kfF7rmsg1QDUEoCqNE7g7M1FdrTt92TEp2CLzypump", "Paid", "PAID",
    "https://axiomtrading.sfo3.cdn.digitaloceanspaces.com/98kfF7rmsg1QDUEoCqNE7g7M1FdrTt92TEp2CLzypump.webp",
    6, 974146486.74571, "Meteora DLMM", 0, 519.973119943,
]

SMART_MONEY_TRADERS_REST = json.dumps(
    {
        "source": "kol",
        "chain": "sol",
        "window": "7d",
        "rows": [
            {
                "id": "J23qr98GjGJJqKq9CBEnyRhHbmkaVxtTJNNxKu597wsA",
                "handle": "gr3gor14n",
                "displayName": "Gr3g",
                "wallets": {
                    "solana": {"status": "verified", "address": "J23qr98GjGJJqKq9CBEnyRhHbmkaVxtTJNNxKu597wsA"},
                    "evm": {"status": "not_applicable", "address": None},
                },
                "windows": {"24h": {"rank": 128, "pnlUsd": -2672.729728329089}, "7d": {"rank": 1, "pnlUsd": 158467.6250070188}},
                "volumeUsd": 584020.889491868,
                "trades": 291,
                "followers": None,
            },
            {
                "id": "6eded5cb-8146-5627-9112-8e954ad3756d",
                "handle": "OmakaseOnly",
                "displayName": "Omakase",
                "wallets": {
                    "solana": {"status": "verified", "address": "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv"},
                    "evm": {"status": "verified", "address": "0x6802190b213325612bc709b4972f7e6f408456a9"},
                },
                "windows": {"24h": {"rank": 2, "pnlUsd": 407607.4621399344}},
                "volumeUsd": 1679011.334825,
                "trades": 398,
                "followers": 13278,
            },
            {
                "id": "unverified-example",
                "handle": "nobody_verified",
                "displayName": "Nobody",
                "wallets": {
                    "solana": {"status": "unresolved", "address": None},
                    "evm": {"status": "unresolved", "address": None},
                },
                "windows": {"24h": {"pnlUsd": 0}},
                "volumeUsd": 0,
                "trades": 0,
                "followers": None,
            },
        ],
    }
)


class TestParseDiscoveryRealShape:
    def test_solana_ws_short_row_is_refused(self):
        assert ap.parse_discovery(SOL_DISCOVERY_WS_FRAME) == []

    def test_solana_ws_full_row_resolves_mcap_and_liquidity(self):
        payload = json.dumps({"room": "sol-discover:trending:5m", "content": [SOL_DISCOVERY_FULL_ROW]})
        row = ap.parse_discovery(payload)[0]
        assert row["mcap"] == 1183.2526079885906  # marketCapSol, SOL not USD
        assert row["liquidity"] == 150.486151428  # liquiditySol, SOL not USD
        assert row["volume_m5"] is None  # no confirmed field for Solana
        assert row["filters"] == "sol-discover:trending:5m"  # the WS room name

    def test_solana_rest_positional_full_row_resolves_mcap_and_liquidity(self):
        payload = json.dumps([SOL_DISCOVERY_FULL_ROW])
        row = ap.parse_discovery(payload, source_url="https://api2.axiom.trade/sol-new-trending")[0]
        assert row["mcap"] == 1183.2526079885906
        assert row["liquidity"] == 150.486151428
        assert row["filters"] == "sol-new-trending"

    def test_solana_rest_positional_filters_default_without_source_url(self):
        # the endpoint is Solana-only by name, so filters doesn't need source_url
        payload = json.dumps([SOL_DISCOVERY_FULL_ROW])
        row = ap.parse_discovery(payload)[0]
        assert row["filters"] == "sol-new-trending"

    def test_evm_ws_undocumented_layout_is_refused(self):
        assert ap.parse_discovery(BNB_DISCOVERY_WS_FRAME) == []

    def test_short_ws_row_is_dropped_not_crashed(self):
        payload = json.dumps({"room": "sol-discover:trending:5m", "content": [["only_one_field"]]})
        assert ap.parse_discovery(payload) == []

    def test_empty_ws_content_returns_empty_list(self):
        payload = json.dumps({"room": "sol-discover:trending:5m", "content": []})
        assert ap.parse_discovery(payload) == []

    def test_rest_evm_row_full_confidence(self):
        rows = ap.parse_discovery(BNB_DISCOVERY_REST, source_url="https://bnb-api3.axiom.trade/discover-trending")
        assert len(rows) == 1
        row = rows[0]
        assert row["chain"] == "56"
        assert row["mint"] == "0xeccbb861c0dda7efd964010085488b69317e4444"
        assert row["pair"] == "0x22af7297243c4eef12e2d5a4f888b92e56bf127c"
        assert row["mcap"] == 205675277.32398945
        assert row["liquidity"] == 1929332.488947453
        assert row["volume_m5"] is None
        assert row["volume_value"] == 45509.539789691706
        assert row["volume_unit"] == "USD"
        assert row["volume_window_s"] is None
        assert row["mcap_unit"] == row["liquidity_unit"] == "USD"
        assert row["parser_version"] == "axiom-discovery-v2"
        assert row["age_s"] is None
        assert row["filters"] == "discover-trending"

    def test_rest_without_source_url_chain_is_none(self):
        rows = ap.parse_discovery(BNB_DISCOVERY_REST)
        assert rows[0]["chain"] is None
        assert rows[0]["mint"] == "0xeccbb861c0dda7efd964010085488b69317e4444"
        assert rows[0]["filters"] is None  # no source_url, so no endpoint name either

    def test_rest_empty_list_returns_empty_quietly(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = ap.parse_discovery("[]")
        assert result == []
        assert caplog.text == ""

    def test_rest_wrong_shape_list_refuses_loudly(self, caplog):
        payload = json.dumps([{"unrelated": "shape"}])
        with caplog.at_level(logging.WARNING):
            result = ap.parse_discovery(payload)
        assert result == []
        assert caplog.text

    def test_rest_sol_positional_row_confirmed_fields(self):
        rows = ap.parse_discovery(
            SOL_NEW_TRENDING_REST, source_url="https://api2.axiom.trade/sol-new-trending"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["chain"] == "solana"
        assert row["mint"] == "HpYdUftXwEaG7jekkjFvFLWeJ5dfqaFvtDHoRC6jd99s"
        assert row["pair"] == "Gx7XRnFb2w5EedWtDF1ULGY9wzR5LAFiaQ1nuFh2e5fp"
        assert row["mcap"] is None

    def test_rest_sol_positional_defaults_to_sol_without_source_url(self):
        # unprefixed api*.axiom.trade is the only host this endpoint is
        # fetched from, so a missing source_url still resolves to solana.
        rows = ap.parse_discovery(SOL_NEW_TRENDING_REST)
        assert rows[0]["chain"] == "solana"

    def test_rest_positional_short_row_dropped(self):
        payload = json.dumps([["only_one_field"]])
        assert ap.parse_discovery(payload) == []


class TestParseKolCalloutsRealShape:
    def test_ws_solana_callout(self):
        rows = ap.parse_kol_callouts(SOL_KOL_CALLOUT_WS_FRAME)
        assert rows == [
            {
                "kol_handle": "XScharo",
                "mint": "5NYBrbe2kbs5cy6GSpTWejNZxsYnXtcdGTGMm49qhj5P",
                "chain": "solana",
                "called_mcap": 15956.883867722594,
                "text": "so how is this not teleporting?",
                "source_id": "01M2MJ75DH17GSSKXW3W7BR7F6",
                "called_at": None, "source_kind": "gmgn",
                "identity_kind": "source_id", "payload_sha": None,
            }
        ]

    def test_ws_robinhood_callout_chain_maps_to_4663(self):
        rows = ap.parse_kol_callouts(ROBINHOOD_KOL_CALLOUT_WS_FRAME)
        assert len(rows) == 1
        assert rows[0]["chain"] == "4663"
        assert rows[0]["mint"] == "0x7ab99d14d34ccdaaa0289ba46cb2a4ae5096b63d"

    def test_ws_missing_callout_key_refuses(self, caplog):
        payload = json.dumps({"room": "gmgn-callouts", "content": {"revision": 1}})
        with caplog.at_level(logging.WARNING):
            result = ap.parse_kol_callouts(payload)
        assert result == []
        assert caplog.text

    def test_rest_feed_multiple_callouts(self):
        rows = ap.parse_kol_callouts(KOL_CALLOUTS_REST_FEED)
        assert len(rows) == 2
        assert rows[0]["kol_handle"] == "Romeo10MPnL"
        assert rows[0]["mint"] == "7ezivZWpR1GJvqDcncsrV4c1U6sAjndfaamm6CqMzsz2"
        assert rows[0]["chain"] == "solana"
        assert rows[1]["kol_handle"] == "drippyUnc"

    def test_rest_feed_empty_returns_empty_quietly(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = ap.parse_kol_callouts('{"callouts": []}')
        assert result == []
        assert caplog.text == ""

    def test_rest_native_callouts_feed(self):
        rows = ap.parse_kol_callouts(NATIVE_CALLOUTS_FEED_REST)
        # of 3 input rows: 1 valid, 1 empty callerHandle, 1 kind="manifesto"
        assert len(rows) == 1
        assert rows[0]["kol_handle"] == "chesterer"
        assert rows[0]["mint"] == "7YEX6FHygjyPvLbWPJpBeVUburdCUhwWSUVZ8MxKR8sB"
        assert rows[0]["chain"] == "solana"
        assert rows[0]["called_mcap"] == 7921.5985189933
        assert rows[0]["text"] == "truno where u at"

    def test_rest_native_callouts_feed_empty_handle_is_dropped(self):
        rows = ap.parse_kol_callouts(NATIVE_CALLOUTS_FEED_REST)
        assert not any(r["mint"] == "HpYdUftXwEaG7jekkjFvFLWeJ5dfqaFvtDHoRC6jd99s" for r in rows)

    def test_rest_native_callouts_feed_empty_list_quiet(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = ap.parse_kol_callouts("[]")
        assert result == []
        assert caplog.text == ""

    def test_rest_native_callouts_feed_wrong_shape_refuses_loudly(self, caplog):
        payload = json.dumps([{"unrelated": "shape"}])
        with caplog.at_level(logging.WARNING):
            result = ap.parse_kol_callouts(payload)
        assert result == []
        assert caplog.text


class TestParseWalletTransactionsRealShape:
    def test_buy_row(self):
        rows = ap.parse_wallet_transactions(json.dumps([WALLET_TX_ROW_BUY]))
        assert len(rows) == 1
        row = rows[0]
        assert row["address"] == "4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD"
        assert row["tx_sig"] == "4P98Z8TSf9ATB9TDT4uPGXQQu3JUmDhyheqFUTgH7MPA84Jk4HxcyATCsUmb31SkbN5f5hZhMnmiHYD1eDiib1EM"
        assert row["side"] == "buy"
        # axiom_ts is element 0 (transactionTime per Axiom's own decoder),
        # not element 12 (pairCreatedAt -- an earlier version of this test
        # got that backwards).
        assert row["axiom_ts"] == "2026-09-16T07:38:47.872Z"
        # mint is element 16 (tokenAddress, Axiom's own field name), not
        # derived from the CDN image URL.
        assert row["mint"] == "4MMQY9bwkxxTtsK3W227Q5ABT6yFY8Pmn9Ze7wmAXKY8"
        assert row["chain"] == "solana"
        assert row["amount_usd"] == 111.93817499999999  # element 7, totalUsd

    def test_sell_row(self):
        rows = ap.parse_wallet_transactions(json.dumps([WALLET_TX_ROW_SELL]))
        row = rows[0]
        assert row["side"] == "sell"
        assert row["mint"] == "98kfF7rmsg1QDUEoCqNE7g7M1FdrTt92TEp2CLzypump"
        assert row["amount_usd"] == 445.6237546017

    def test_chain_from_source_url_evm_host(self):
        evm_row = list(WALLET_TX_ROW_BUY)
        evm_row[ap._WALLET_TX_ADDRESS_IDX] = "0x1111111111111111111111111111111111111111"
        evm_row[ap._WALLET_TX_MINT_IDX] = "0x2222222222222222222222222222222222222222"
        rows = ap.parse_wallet_transactions(
            json.dumps([evm_row]), source_url="https://bnb-api2.axiom.trade/tracked-wallet-transactions"
        )
        assert rows[0]["chain"] == "56"

    def test_evm_address_without_source_url_chain_is_none(self):
        evm_row = list(WALLET_TX_ROW_BUY)
        evm_row[ap._WALLET_TX_ADDRESS_IDX] = "0x1111111111111111111111111111111111111111"
        evm_row[ap._WALLET_TX_MINT_IDX] = "0x2222222222222222222222222222222222222222"
        rows = ap.parse_wallet_transactions(json.dumps([evm_row]))
        assert rows[0]["chain"] is None

    def test_unrecognized_side_row_is_dropped(self):
        bad_row = list(WALLET_TX_ROW_BUY)
        bad_row[ap._WALLET_TX_SIDE_IDX] = "transfer"
        assert ap.parse_wallet_transactions(json.dumps([bad_row])) == []

    def test_short_row_is_dropped_not_crashed(self):
        assert ap.parse_wallet_transactions(json.dumps([["too", "short"]])) == []

    def test_empty_list_returns_empty_quietly(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = ap.parse_wallet_transactions("[]")
        assert result == []
        assert caplog.text == ""


class TestParseWalletEventAcceptsRestRow:
    def test_already_decoded_row(self):
        ev = ap.parse_wallet_event(WALLET_TX_ROW_BUY)
        assert ev is not None
        assert ev["side"] == "buy"
        assert ev["address"] == "4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD"

    def test_json_encoded_single_row(self):
        ev = ap.parse_wallet_event(json.dumps(WALLET_TX_ROW_BUY))
        assert ev is not None
        assert ev["tx_sig"] == WALLET_TX_ROW_BUY[2]


class TestParseTraderDirectoryRealShape:
    def test_solana_only_verified_wallet(self):
        rows = ap.parse_trader_directory(SMART_MONEY_TRADERS_REST)
        gr3g = [r for r in rows if r["x_handle"] == "gr3gor14n"]
        assert len(gr3g) == 1
        row = gr3g[0]
        assert row["display"] == "Gr3g"
        assert row["address"] == "J23qr98GjGJJqKq9CBEnyRhHbmkaVxtTJNNxKu597wsA"
        assert row["chain"] == "solana"
        assert row["pnl_24h"] == -2672.729728329089
        assert row["trades"] == 291
        assert row["volume"] == 584020.889491868
        assert row["followers"] is None

    def test_trader_with_both_verified_wallets_emits_two_rows(self):
        rows = ap.parse_trader_directory(SMART_MONEY_TRADERS_REST)
        omakase = [r for r in rows if r["x_handle"] == "OmakaseOnly"]
        assert len(omakase) == 2
        chains = {r["chain"] for r in omakase}
        assert chains == {"solana", None}  # evm chain id is unresolved
        sol_row = next(r for r in omakase if r["chain"] == "solana")
        assert sol_row["address"] == "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv"
        evm_row = next(r for r in omakase if r["chain"] is None)
        assert evm_row["address"] == "0x6802190b213325612bc709b4972f7e6f408456a9"

    def test_unverified_wallets_emit_no_row(self):
        rows = ap.parse_trader_directory(SMART_MONEY_TRADERS_REST)
        assert not any(r["x_handle"] == "nobody_verified" for r in rows)

    def test_missing_rows_key_refuses(self, caplog):
        with caplog.at_level(logging.WARNING):
            result = ap.parse_trader_directory('{"source": "kol"}')
        assert result == []
        assert caplog.text


@pytest.mark.parametrize("bad", ["not-a-mint", "0x1234", "0" * 44, "z" * 44])
def test_discovery_rejects_invalid_solana_identity(bad):
    row = list(SOL_DISCOVERY_FULL_ROW)
    row[1] = bad
    assert ap.parse_discovery([row]) == []


@pytest.mark.parametrize("bad", ["0x1234", "0x" + "g" * 40, "plain-name"])
def test_discovery_rejects_invalid_evm_identity(bad):
    rows = json.loads(BNB_DISCOVERY_REST)
    rows[0]["tokenAddress"] = bad
    assert ap.parse_discovery(rows) == []


def test_discovery_keeps_native_solana_units():
    row = ap.parse_discovery([SOL_DISCOVERY_FULL_ROW])[0]
    assert row["mcap_unit"] == row["liquidity_unit"] == "SOL"
    assert row["volume_value"] is None
    assert row["volume_unit"] is None
    assert row["volume_window_s"] is None
    assert row["parser_version"] == "axiom-discovery-v2"


def test_wallet_ws_refuses_unconfirmed_layout_even_with_rest_content():
    assert ap.parse_wallet_event({"room": "v:" + WALLET_TX_ROW_BUY[1],
                                  "content": WALLET_TX_ROW_BUY}) is None


@pytest.mark.parametrize("length", [2, 28, 56, 58])
def test_discovery_requires_supported_positional_length(length):
    row = (list(SOL_DISCOVERY_FULL_ROW) + [None])[:length]
    assert ap.parse_discovery([row]) == []


@pytest.mark.parametrize("index,value", [(1, "not-wallet"), (16, "not-mint"), (16, None)])
def test_wallet_rest_rejects_invalid_address(index, value):
    row = list(WALLET_TX_ROW_BUY)
    row[index] = value
    assert ap.parse_wallet_transactions([row]) == []


def test_wallet_rest_rejects_unknown_positional_extension():
    assert ap.parse_wallet_transactions([WALLET_TX_ROW_BUY + ["new-field"]]) == []


def test_wallet_rest_rejects_chain_address_mismatch():
    assert ap.parse_wallet_transactions([WALLET_TX_ROW_BUY],
        source_url="https://bnb-api2.axiom.trade/tracked-wallet-transactions") == []


def test_tracked_wallets_read_named_identity_only():
    identity = {"chain": "sol", "address": "7iPPqPyrqcmfenRs4xZ72ab4pyuUofXB5YaQB83WJmT9",
                "xHandle": "notanicecat69", "xDisplayName": "Wood",
                "verifiedAt": "2026-09-12T04:42:34.847Z"}
    rows = ap.parse_tracked_wallets({"trackedWallets": [["untrusted positional address", identity]], "groups": []})
    assert len(rows) == 1
    assert rows[0]["address"] == identity["address"]
    assert rows[0]["x_handle"] == "notanicecat69"
    assert rows[0]["chain"] == "solana"
    assert rows[0]["source"] == "axiom_tracked_identity"
    assert rows[0]["pnl_24h"] is None
    assert ap.parse_tracked_wallets({"trackedWallets": [[identity["address"], "display only"]]}) == []
    assert ap.parse_tracked_wallets({"trackedWallets": [[dict(identity, address="bad")]]}) == []


@pytest.mark.parametrize("native", [False, True])
def test_callout_preserves_provider_event_identity(native):
    first = json.loads(NATIVE_CALLOUTS_FEED_REST)[0] if native else json.loads(KOL_CALLOUTS_REST_FEED)["callouts"][0]
    first["createdAt"] = "2026-09-16T09:12:59.047Z"
    second = dict(first, id="second-call")
    rows = ap.parse_kol_callouts([first, second] if native else {"callouts": [first, second]})
    assert [r["source_id"] for r in rows] == [first["id"], "second-call"]
    assert all(r["called_at"] == first["createdAt"] for r in rows)
    assert all(r["identity_kind"] == "source_id" for r in rows)
    assert all(r["source_kind"] == ("axiom" if native else "gmgn") for r in rows)


def test_callout_without_source_id_or_time_preserves_distinct_payloads():
    first = json.loads(NATIVE_CALLOUTS_FEED_REST)[0]
    del first["id"]
    second = dict(first, callerKey="another-payload")
    rows = ap.parse_kol_callouts([first, second])
    assert all(r["identity_kind"] == "unique_payload" for r in rows)
    assert rows[0]["payload_sha"] != rows[1]["payload_sha"]
    assert ap.parse_kol_callouts([dict(reversed(list(first.items())))])[0]["payload_sha"] == rows[0]["payload_sha"]
