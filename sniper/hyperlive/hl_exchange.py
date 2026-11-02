"""Thin adapter over hyperliquid-python-sdk for the real copy leg.

Hard contract (tested): every market call passes EXPLICIT slippage — the SDK default is
5% and must never be relied on. Closes use the SDK market_close (reduce-only IOC).
Signing uses the dedicated API wallet; the funded account is only referenced by address.
Real SDK objects are built lazily so tests inject fakes and never touch the network.
"""
from __future__ import annotations

import json


SDK_TIMEOUT_SECONDS = 10.0


class HlExchange:
    def __init__(self, *, api_wallet_file: str, account_address: str, base_url: str,
                 entry_slippage: float, exit_slippage: float,
                 sdk_exchange=None, sdk_info=None):
        self._api_wallet_file = api_wallet_file
        self._account_address = account_address
        self._base_url = base_url
        self._entry_slippage = float(entry_slippage)
        self._exit_slippage = float(exit_slippage)
        self._ex = sdk_exchange
        self._info = sdk_info
        self._szdec: dict[str, int] | None = None
        self._lev_done: set[str] = set()

    # ── lazy SDK construction (skipped when fakes are injected) ──────────────
    def _exchange(self):
        if self._ex is None:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            with open(self._api_wallet_file) as f:
                key = json.load(f)["private_key"]
            self._ex = Exchange(Account.from_key(key), self._base_url,
                                account_address=self._account_address,
                                timeout=SDK_TIMEOUT_SECONDS)
        return self._ex

    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info
            self._info = Info(self._base_url, skip_ws=True, timeout=SDK_TIMEOUT_SECONDS)
        return self._info

    @staticmethod
    def _cloid(cloid_hex: str):
        from hyperliquid.utils.types import Cloid
        return Cloid.from_str(cloid_hex)

    # ── orders ───────────────────────────────────────────────────────────────
    def market_open(self, coin: str, is_buy: bool, sz: float, cloid_hex: str):
        return self._exchange().market_open(coin, is_buy, sz, None,
                                            self._entry_slippage, self._cloid(cloid_hex))

    def market_close(self, coin: str, cloid_hex: str):
        # sz=None closes the whole position; SDK issues a reduce-only IOC.
        return self._exchange().market_close(coin, None, None,
                                             self._exit_slippage, self._cloid(cloid_hex))

    def set_isolated_1x(self, coin: str) -> None:
        if coin in self._lev_done:
            return
        self._exchange().update_leverage(1, coin, False)
        self._lev_done.add(coin)

    # ── reads ────────────────────────────────────────────────────────────────
    def order_status(self, cloid_hex: str) -> dict | None:
        resp = self._get_info().query_order_by_cloid(self._account_address,
                                                     self._cloid(cloid_hex))
        if not isinstance(resp, dict) or resp.get("status") != "order":
            return None
        return resp.get("order")

    def positions(self) -> dict[str, dict]:
        st = self._get_info().user_state(self._account_address) or {}
        out: dict[str, dict] = {}
        for ap in st.get("assetPositions", []):
            p = ap.get("position") or {}
            coin = str(p.get("coin", "")).upper()
            try:
                szi = float(p.get("szi", 0))
            except (TypeError, ValueError):
                continue
            if coin and szi != 0.0:
                try:
                    entry_px = float(p.get("entryPx") or 0)
                except (TypeError, ValueError):
                    entry_px = 0.0
                out[coin] = {"szi": szi, "entry_px": entry_px}
        return out

    def sz_decimals(self, coin: str) -> int:
        if self._szdec is None:
            meta = self._get_info().meta()
            self._szdec = {str(u["name"]).upper(): int(u["szDecimals"])
                           for u in meta.get("universe", [])}
        return self._szdec[coin.upper()]

    def fills_for_cloid(self, cloid_hex: str) -> dict | None:
        """Aggregate this cloid's fills: size-weighted avg px, total sz, summed fee.
        None when no fill carries the cloid (order never landed / not yet visible)."""
        fills = [f for f in (self._get_info().user_fills(self._account_address) or [])
                 if f.get("cloid") == cloid_hex]
        if not fills:
            return None
        tot_sz = sum(float(f["sz"]) for f in fills)
        if tot_sz <= 0:
            return None
        px = sum(float(f["px"]) * float(f["sz"]) for f in fills) / tot_sz
        fee = sum(float(f.get("fee") or 0) for f in fills)
        return {"px": px, "sz": tot_sz, "fee": fee}
