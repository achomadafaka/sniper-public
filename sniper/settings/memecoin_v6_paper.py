"""Isolated, inert settings for the V6 live-paper process."""
import math
import os


SUPPORTED_NETWORKS = ("solana", "4663")


def evm_rpc_for_chain(chain_id, env=None):
    source = os.environ if env is None else env
    return (source.get(f"MEMECOIN_EVM_RPC_URL_{chain_id}")
            or source.get("MEMECOIN_EVM_RPC_URL", ""))


def load_settings(env=None):
    env = os.environ if env is None else env
    values = {
        "enabled": env.get("MEMECOIN_V6_PAPER_ENABLED", "0").strip().lower()
        in {"1", "true", "yes"},
        "db": (env.get("MEMECOIN_V6_PAPER_DB", "") or "").strip() or None,
        "notional_usd": float(env.get("MEMECOIN_V6_PAPER_NOTIONAL_USD", "25")),
        "max_open": int(env.get("MEMECOIN_V6_PAPER_MAX_OPEN", "4")),
        "quote_max_age_s": float(env.get("MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S", "10")),
        "sweep_s": float(env.get("MEMECOIN_V6_PAPER_SWEEP_S", "5")),
        "event_coalesce_ms": int(env.get("MEMECOIN_V6_EVENT_COALESCE_MS", "50")),
        "heartbeat_s": float(env.get("MEMECOIN_V6_HEARTBEAT_S", "1")),
        "evm_fallback_poll_ms": int(env.get("MEMECOIN_V6_EVM_FALLBACK_POLL_MS", "500")),
        "solana_binding_refresh_s": float(
            env.get("MEMECOIN_V6_SOLANA_BINDING_REFRESH_S", "0.5")),
        "quote_refresh_s": float(env.get("MEMECOIN_V6_QUOTE_REFRESH_S", "5")),
        # Jupiter keyless access is 0.5 RPS. One shared lane prevents a burst of paper positions
        # from turning valid executable quotes into synthetic provider_unavailable observations.
        "jupiter_min_call_s": float(env.get("MEMECOIN_V6_JUPITER_MIN_CALL_S", "2.1")),
        "event_catchup_max": int(env.get("MEMECOIN_V6_EVENT_CATCHUP_MAX", "400")),
    }
    values["networks"] = tuple(
        item.strip().lower()
        for item in env.get("MEMECOIN_V6_PAPER_NETWORKS", ",".join(SUPPORTED_NETWORKS)).split(",")
        if item.strip())
    values["admission_networks"] = tuple(
        item.strip().lower()
        for item in env.get("MEMECOIN_V6_PAPER_ADMISSION_NETWORKS", "4663").split(",")
        if item.strip())
    env_names = {
        "notional_usd": "MEMECOIN_V6_PAPER_NOTIONAL_USD",
        "max_open": "MEMECOIN_V6_PAPER_MAX_OPEN",
        "quote_max_age_s": "MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S",
        "sweep_s": "MEMECOIN_V6_PAPER_SWEEP_S",
        "event_coalesce_ms": "MEMECOIN_V6_EVENT_COALESCE_MS",
        "heartbeat_s": "MEMECOIN_V6_HEARTBEAT_S",
        "evm_fallback_poll_ms": "MEMECOIN_V6_EVM_FALLBACK_POLL_MS",
        "solana_binding_refresh_s": "MEMECOIN_V6_SOLANA_BINDING_REFRESH_S",
        "quote_refresh_s": "MEMECOIN_V6_QUOTE_REFRESH_S",
        "jupiter_min_call_s": "MEMECOIN_V6_JUPITER_MIN_CALL_S",
        "event_catchup_max": "MEMECOIN_V6_EVENT_CATCHUP_MAX",
    }
    for key, name in env_names.items():
        value = values[key]
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and > 0 (got {value!r})")
    unsupported = [item for item in values["networks"] if item not in SUPPORTED_NETWORKS]
    if not values["networks"] or unsupported:
        raise ValueError(
            f"MEMECOIN_V6_PAPER_NETWORKS must be a non-empty subset of "
            f"{SUPPORTED_NETWORKS} (got {values['networks']})")
    unsupported_admission = [
        item for item in values["admission_networks"] if item not in values["networks"]
    ]
    if not values["admission_networks"] or unsupported_admission:
        raise ValueError(
            "MEMECOIN_V6_PAPER_ADMISSION_NETWORKS must be a non-empty subset of "
            f"MEMECOIN_V6_PAPER_NETWORKS (got {values['admission_networks']})")
    return values


_VALUES = load_settings()
MEMECOIN_V6_PAPER_ENABLED = _VALUES["enabled"]
MEMECOIN_V6_PAPER_DB = _VALUES["db"]
MEMECOIN_V6_PAPER_NOTIONAL_USD = _VALUES["notional_usd"]
MEMECOIN_V6_PAPER_MAX_OPEN = _VALUES["max_open"]
MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S = _VALUES["quote_max_age_s"]
MEMECOIN_V6_PAPER_SWEEP_S = _VALUES["sweep_s"]
MEMECOIN_V6_PAPER_NETWORKS = _VALUES["networks"]
MEMECOIN_V6_PAPER_ADMISSION_NETWORKS = _VALUES["admission_networks"]
MEMECOIN_V6_EVENT_COALESCE_MS = _VALUES["event_coalesce_ms"]
MEMECOIN_V6_HEARTBEAT_S = _VALUES["heartbeat_s"]
MEMECOIN_V6_EVM_FALLBACK_POLL_MS = _VALUES["evm_fallback_poll_ms"]
MEMECOIN_V6_SOLANA_BINDING_REFRESH_S = _VALUES["solana_binding_refresh_s"]
MEMECOIN_V6_QUOTE_REFRESH_S = _VALUES["quote_refresh_s"]
MEMECOIN_V6_JUPITER_MIN_CALL_S = _VALUES["jupiter_min_call_s"]
MEMECOIN_V6_EVENT_CATCHUP_MAX = _VALUES["event_catchup_max"]

# Quote-only chain-4663 route metadata. These are public contracts, never credentials.
MEMECOIN_V6_EVM_V3_ADDRESSES_4663 = {
    "quoter": os.getenv(
        "MEMECOIN_EVM_V3_QUOTER_4663", "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"),
    "router": os.getenv(
        "MEMECOIN_EVM_V3_ROUTER_4663", "0xcaf681a66d020601342297493863e78c959e5cb2"),
}
MEMECOIN_V6_EVM_FACTORIES_4663 = {
    "v3_factory": os.getenv(
        "MEMECOIN_EVM_V3_FACTORY_4663", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"),
    "v2_factory": os.getenv(
        "MEMECOIN_EVM_V2_FACTORY_4663", "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"),
}
MEMECOIN_V6_EVM_V3_FEE_TIERS = tuple(int(item) for item in os.getenv(
    "MEMECOIN_EVM_V3_FEE_TIERS", "100,500,3000,10000").split(",") if item.strip())
MEMECOIN_V6_EVM_V2_ROUTER_4663 = os.getenv(
    "MEMECOIN_EVM_V2_ROUTER_4663", "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba")
MEMECOIN_V6_EVM_V2_CONNECTORS_4663 = tuple(item.strip() for item in os.getenv(
    "MEMECOIN_EVM_V2_CONNECTORS_4663",
    "0xc6911796042b15d7fa4f6cde69e245ddcd3d9c31,"
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168").split(",") if item.strip())

# The same quote-only route metadata for Base. Every Base address below was PROVED ON CHAIN on
# 2026-09-06 against https://mainnet.base.org, not copied from a page:
#   SwapRouter02 0x2626664c… .factory() -> 0x33128a8f…   (24,535 bytes of code)
#   QuoterV2     0x3d4e44Eb… .factory() -> 0x33128a8f…   (the same factory, cross-checked)
#   V2Router02   0x4752ba5d… .factory() -> 0x8909dc15…   (13,859 bytes)
#   V2Router02   0x4752ba5d… .WETH()    -> 0x42000000…06 (confirms the quote token independently)
MEMECOIN_V6_EVM_V3_ADDRESSES_8453 = {
    "quoter": os.getenv(
        "MEMECOIN_EVM_V3_QUOTER_8453", "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"),
    "router": os.getenv(
        "MEMECOIN_EVM_V3_ROUTER_8453", "0x2626664c2603336E57B271c5C0b26F421741e481"),
}
MEMECOIN_V6_EVM_FACTORIES_8453 = {
    "v3_factory": os.getenv(
        "MEMECOIN_EVM_V3_FACTORY_8453", "0x33128a8fc17869897dce68ed026d694621f6fdfd"),
    "v2_factory": os.getenv(
        "MEMECOIN_EVM_V2_FACTORY_8453", "0x8909dc15e40173ff4699343b6eb8132c65e18ec6"),
}
MEMECOIN_V6_EVM_V2_ROUTER_8453 = os.getenv(
    "MEMECOIN_EVM_V2_ROUTER_8453", "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24")
MEMECOIN_V6_EVM_V2_CONNECTORS_8453 = tuple(item.strip() for item in os.getenv(
    "MEMECOIN_EVM_V2_CONNECTORS_8453",
    "0x4200000000000000000000000000000000000006,"
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913").split(",") if item.strip())

# PancakeSwap on 56. Every address cross-confirmed by a non-empty `eth_getCode` on
# https://bsc-dataseed.bnbchain.org (V2 factory 19,084 B, V2 router 21,936 B, V3 factory 5,151 B,
# V3 router 12,154 B, V3 quoter 8,331 B) and every factory DERIVED on chain via `factory()`:
# V2Router02 -> 0xca143ce3…, V3SwapRouter -> 0x0bfbcf9f…, V3QuoterV2 -> 0x0bfbcf9f…
MEMECOIN_V6_EVM_V3_ADDRESSES_56 = {
    "quoter": os.getenv(
        "MEMECOIN_EVM_V3_QUOTER_56", "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"),
    "router": os.getenv(
        "MEMECOIN_EVM_V3_ROUTER_56", "0x1b81D678ffb9C0263b24A97847620C99d213eB14"),
}
MEMECOIN_V6_EVM_FACTORIES_56 = {
    "v3_factory": os.getenv(
        "MEMECOIN_EVM_V3_FACTORY_56", "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"),
    "v2_factory": os.getenv(
        "MEMECOIN_EVM_V2_FACTORY_56", "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"),
}
MEMECOIN_V6_EVM_V2_ROUTER_56 = os.getenv(
    "MEMECOIN_EVM_V2_ROUTER_56", "0x10ED43C718714eb63d5aA57B78B54704E256024E")
MEMECOIN_V6_EVM_V2_CONNECTORS_56 = tuple(item.strip() for item in os.getenv(
    "MEMECOIN_EVM_V2_CONNECTORS_56",
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c,"
    "0x55d398326f99059ff775485246999027b3197955").split(",") if item.strip())

# ONE LOOKUP PER PURPOSE, keyed by chain. The `_4663` names stay because other modules import them.
# Ethereum mainnet (2026-09-30, eth-mainnet spec). Proved on chain against publicnode:
#   SwapRouter02 0x68b34658... .factory() -> 0x1F98431c... and .WETH9() -> 0xC02aaA39...
#   QuoterV2     0x61fFE014... .factory() -> 0x1F98431c... (the same factory)
#   V2Router02   0x7a250d56... .factory() -> 0x5C69bEe7... and .WETH() -> 0xC02aaA39...
MEMECOIN_V6_EVM_V3_ADDRESSES_1 = {
    "quoter": os.getenv("MEMECOIN_EVM_V3_QUOTER_1", "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    "router": os.getenv("MEMECOIN_EVM_V3_ROUTER_1", "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"),
}
MEMECOIN_V6_EVM_FACTORIES_1 = {
    "v3_factory": os.getenv("MEMECOIN_EVM_V3_FACTORY_1", "0x1F98431c8aD98523631AE4a59f267346ea31F984"),
    "v2_factory": os.getenv("MEMECOIN_EVM_V2_FACTORY_1", "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"),
}
MEMECOIN_V6_EVM_V2_ROUTER_1 = os.getenv(
    "MEMECOIN_EVM_V2_ROUTER_1", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
MEMECOIN_V6_EVM_V2_CONNECTORS_1 = tuple(item.strip() for item in os.getenv(
    "MEMECOIN_EVM_V2_CONNECTORS_1",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2,"
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48").split(",") if item.strip())

MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN = {
    "4663": MEMECOIN_V6_EVM_V3_ADDRESSES_4663, "8453": MEMECOIN_V6_EVM_V3_ADDRESSES_8453,
    "56": MEMECOIN_V6_EVM_V3_ADDRESSES_56, "1": MEMECOIN_V6_EVM_V3_ADDRESSES_1}
MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN = {
    "4663": MEMECOIN_V6_EVM_FACTORIES_4663, "8453": MEMECOIN_V6_EVM_FACTORIES_8453,
    "56": MEMECOIN_V6_EVM_FACTORIES_56, "1": MEMECOIN_V6_EVM_FACTORIES_1}
MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN = {
    "4663": MEMECOIN_V6_EVM_V2_ROUTER_4663, "8453": MEMECOIN_V6_EVM_V2_ROUTER_8453,
    "56": MEMECOIN_V6_EVM_V2_ROUTER_56, "1": MEMECOIN_V6_EVM_V2_ROUTER_1}
MEMECOIN_V6_EVM_V2_CONNECTORS_BY_CHAIN = {
    "4663": MEMECOIN_V6_EVM_V2_CONNECTORS_4663, "8453": MEMECOIN_V6_EVM_V2_CONNECTORS_8453,
    "56": MEMECOIN_V6_EVM_V2_CONNECTORS_56, "1": MEMECOIN_V6_EVM_V2_CONNECTORS_1}
MEMECOIN_V6_EVM_V3_SLIPPAGE_BPS = int(round(float(os.getenv(
    "MEMECOIN_ENTRY_TX_SLIPPAGE_PCT", os.getenv("MEMECOIN_ENTRY_SLIPPAGE_PCT", "3"))) * 100))
MEMECOIN_V6_EVM_V2_SLIPPAGE_BPS = int(round(float(
    (os.getenv("MEMECOIN_EXIT_SLIPPAGE_LADDER", "3,10,25").split(",")[-1])) * 100))
MEMECOIN_V6_EVM_V2_TAX_BUFFER_BPS = int(round(float(os.getenv(
    "MEMECOIN_EVM_MAX_TAX_PCT", "10")) * 100))
MEMECOIN_V6_V3_DEPTH_VERIFIED_CHAINS = frozenset({"4663"}) | frozenset(
    item.strip().lower() for item in os.getenv(
        "MEMECOIN_V3_DEPTH_VERIFIED_CHAINS", "").split(",") if item.strip())
