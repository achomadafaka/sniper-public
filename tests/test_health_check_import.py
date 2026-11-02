import importlib
import sys
import unittest
from unittest.mock import patch


class HealthCheckImportTests(unittest.TestCase):
    def test_import_does_not_load_dotenv(self) -> None:
        sys.modules.pop("health_check", None)
        with patch("dotenv.load_dotenv") as mock_load_dotenv:
            module = importlib.import_module("scripts.ops.health_check")
        self.assertIsNotNone(module)
        mock_load_dotenv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
