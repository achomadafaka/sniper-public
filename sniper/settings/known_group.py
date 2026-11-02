"""Versioned operator-approved experiment data; authority lives in the funded ledger."""
from types import MappingProxyType

# Dependency-free to avoid settings.web -> sync -> db -> policy import cycles.
KNOWN_GROUP_LAUNCH_V1 = MappingProxyType(dict(
    version='known-group-launch-v1',
    registry_hash='eb81b2da4c235e8b8396ef5abb985d0c647d40776ba21c52d175dc546f899ba4',
    network_id='solana', venue='pumpswap', clip_usd=25.0, stop_mode='manual',
    entry_ttl_s=5.0, funding_min_lamports=10_000_000, funding_window_s=60.0,
    sell_freshness_s=1.0, tp_pct=40.0, sl_pct=30.0, trail_pct=0.0,
    time_stop_s=30.0, membership='entry_frozen', history_basis='observed_since_tail',
))
KNOWN_GROUP_TAIL_PAGE = 64
KNOWN_GROUP_TAIL_PENDING_MAX = 128
KNOWN_GROUP_TAIL_INTERVAL_S = 0.05
KNOWN_GROUP_PROOF_MAX_BYTES = 65_536

# Admission evidence identity, separate from the unchanged funded strategy policy.
KNOWN_GROUP_SERVICE_CATALOGUE = MappingProxyType(dict(
    version='known-group-services-v1',
    sha256='67c789b1242bc56b1470a8fbb8bea28403fb39d1c59833772e79bb4dcc18dd51',
    known_funders_version='cluster-known-funders-v1',
    known_funders_sha256='6835e4fd5f69cc7e40c67296559be44f3836e46b8fbb62f2c05f6371b8917bf5',
))
