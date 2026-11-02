import unittest

from sniper.utils import (
    _format_unix,
    _normalize_tx_hash,
    _normalize_wallet,
    _optional_float,
    _parse_any_ts,
    _safe_float,
    _safe_int,
    _short_wallet,
)


class UtilsTests(unittest.TestCase):
    def test_safe_float_and_safe_int(self) -> None:
        self.assertEqual(_safe_float("1.25"), 1.25)
        self.assertEqual(_safe_float(None, 7.0), 7.0)
        self.assertEqual(_safe_int("4.9"), 4)
        self.assertEqual(_safe_int("bad", 11), 11)

    def test_normalizers(self) -> None:
        self.assertEqual(_normalize_wallet(" 0xABC "), "0xabc")
        self.assertEqual(_normalize_tx_hash(" 0xDEAD "), "0xdead")

    def test_optional_float(self) -> None:
        self.assertIsNone(_optional_float(None))
        self.assertIsNone(_optional_float(" "))
        self.assertEqual(_optional_float("2.5"), 2.5)

    def test_parse_any_ts(self) -> None:
        self.assertEqual(_parse_any_ts(1700000000), 1700000000)
        self.assertEqual(_parse_any_ts("1700000001"), 1700000001)
        self.assertEqual(_parse_any_ts("2026-02-11"), 1770768000)
        self.assertIsNone(_parse_any_ts("bad-date"))

    def test_format_unix(self) -> None:
        self.assertEqual(_format_unix(0), "")
        self.assertEqual(_format_unix(1770768000), "2026-02-11 00:00:00 UTC")

    def test_short_wallet(self) -> None:
        self.assertEqual(_short_wallet(""), "")
        self.assertEqual(_short_wallet("0x1234"), "0x1234")
        self.assertEqual(
            _short_wallet("0x1234567890abcdef1234567890abcdef12345678"),
            "0x123456...5678",
        )


if __name__ == "__main__":
    unittest.main()
