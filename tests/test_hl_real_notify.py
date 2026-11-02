"""Notifier for the HL real leg — prefixes [hl-real], delegates to the shared Telegram
sender, never raises."""
from sniper.hyperlive import real_notify as RN


def test_prefix_and_passthrough(monkeypatch):
    sent = []

    def fake_send(text, **kw):
        sent.append(text)
        return (True, 12.0)

    monkeypatch.setattr(RN, "send_telegram", fake_send)
    assert RN.notify("filled ZEC long 0.15@100") is True
    assert sent == ["[hl-real] filled ZEC long 0.15@100"]


def test_never_raises(monkeypatch):
    def boom(text, **kw):
        raise RuntimeError("tg down")

    monkeypatch.setattr(RN, "send_telegram", boom)
    assert RN.notify("x") is False
