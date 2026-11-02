import importlib
import sys
from unittest.mock import patch


def test_import_does_not_load_dotenv():
    sys.modules.pop("sniper.services.real_trader", None)
    with patch("dotenv.load_dotenv") as load_dotenv:
        module = importlib.import_module("sniper.services.real_trader")
    assert module is not None
    load_dotenv.assert_not_called()
