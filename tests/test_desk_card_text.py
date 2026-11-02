"""Desk cards carried copy-lane lines (2026-10-23, pos 74142 / attempt 16469fe1): 'not armed on
Solana' (a desk ticket arms no wallet) and 'source (unverified)' / 'Landed S+? (source slot resolving)'
(there is no source buy)."""
from sniper.services.memecoin_fill_timing import _format, classify
from sniper.services.memecoin_telegram import render_entry_notification

VIEW = {"status": "filled", "signal_source": "desk", "network_id": "solana", "ticker": "A3Wq",
        "origin_event_id": "desk:desk:U:990f581866b58e41c6b4eb8c", "attempt_id": "44d4fff5",
        "current_authority": {"state": "not_armed"}, "source_created_at": "2026-10-23T13:18:21+00:00",
        "source_timestamp_trust": "claimed_untrusted"}


def test_desk_card_names_the_ticket_and_drops_the_arming_and_source_words():
    text = render_entry_notification(dict(VIEW))
    assert "Desk ticket desk:U:990f581866b58e41c6b4eb8c" in text
    assert "armed" not in text and "source (unverified)" not in text
    assert "Times (UTC): ticket" in text


def test_copy_card_keeps_its_arming_and_source_words():
    text = render_entry_notification({**VIEW, "signal_source": "global_feed", "source_handle": "w"})
    assert "not armed on Solana" in text and "source (unverified)" in text


def test_desk_landing_has_no_source_slot_to_resolve():
    cause, detail = classify({"signal_source": "desk", "network_id": "solana",
                              "terminal_reason": "chain_error:slippage tolerance exceeded (Jupiter 0x1771)"})
    assert cause == "desk" and "0x1771" in detail
    line = _format("solana", None, None, 460460531, cause, detail, source_available=False)
    assert line.startswith("Landed slot 460460531 · desk entry") and "S+?" not in line
