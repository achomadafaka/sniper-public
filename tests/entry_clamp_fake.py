"""For tests that sign FAKE payload strings (not parseable transactions): the unconditional entry fee clamp
(option B, 2026-10-22) refuses an unreadable entry before signing, so these tests, which are about something
else, pass the clamp through exactly as the old cache-less path did. Real-message tests never use this."""
import pytest


@pytest.fixture
def fake_payload_clamp(monkeypatch):
    from sniper.execution import solana_sender_ab as ab
    monkeypatch.setattr(ab, "payload_tip", lambda *a, **k: 0)
    monkeypatch.setattr(ab, "cap_unsigned_entry_priority", lambda unsigned, **k: (unsigned, {}))
    monkeypatch.setattr(ab, "cap_entry_priority_fee", lambda message, **k: (message, {}))
