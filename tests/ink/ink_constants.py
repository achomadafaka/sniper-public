"""Ink test constants under a name no other test directory uses (2026-10-15).

Test modules used `from conftest import INK_TOKEN`. In one pytest process every conftest.py is
imported under the bare module name `conftest`, so in a mixed run the last one loaded wins:
collecting tests/ink/test_accounting.py, then any tests/wallet_protocol test, then
tests/ink/test_op_fees.py raised ImportError against tests/wallet_protocol/conftest.py. The
constants live here; tests/ink/conftest.py re-exports them for its own fixtures."""
INK = "57073"
INK_WETH = "0x4200000000000000000000000000000000000006"
INK_ROUTER = "0x177778F19E89dD1012BdBe603F144088A95C4B53"
INK_POOL = "0x" + "70" * 20
INK_TOKEN = "0x" + "b" * 40
