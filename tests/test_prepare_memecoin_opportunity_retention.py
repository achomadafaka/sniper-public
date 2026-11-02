import hashlib
import subprocess
import sys

from scripts.prepare_memecoin_opportunity_retention import prepare
from sniper.research.memecoin_opportunity.insights import connect_insights
from sniper.research.memecoin_opportunity.store import (
    LaunchIdentity, append_canonical_event, append_raw_event, connect,
    upsert_launch_identity,
)
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent, ChainOrder, ConfirmationState, EventKind, LifecycleState,
    NetworkId, RawEvent, VenueId,
)


def _seed(path):
    conn = connect(path)
    payload = '{}'
    raw = RawEvent(
        raw_event_id="r", network_id=NetworkId.SOLANA,
        venue_id=VenueId.PUMPFUN_SOLANA, source_key="pumpfun_solana:ws",
        source_provider="test", source_cursor="1", token_id="mint",
        transaction_id="tx", chain_order=ChainOrder(1, 0, 0, "block"),
        chain_timestamp="2026-07-20T00:00:00Z", received_at="2026-07-20T00:00:00Z",
        confirmation=ConfirmationState.FINALIZED, raw_payload_json=payload,
        raw_payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )
    event = CanonicalEvent(
        canonical_event_id="e", raw_event_id="r", launch_id="l",
        network_id=NetworkId.SOLANA, venue_id=VenueId.PUMPFUN_SOLANA,
        token_id="mint", lifecycle=LifecycleState.PRE_MIGRATION,
        event_kind=EventKind.LAUNCH, transaction_id="tx", chain_order=raw.chain_order,
        chain_timestamp=raw.chain_timestamp, received_at=raw.received_at,
        decoded_at=raw.received_at, state_applied_at=raw.received_at,
        confirmation=ConfirmationState.FINALIZED, actor_id=None,
        quote_delta_atomic=None, token_delta_atomic=None, progress_bps=0,
        creator_linked=None, decoder_version="test-v1",
    )
    append_raw_event(conn, raw)
    append_canonical_event(conn, event)
    upsert_launch_identity(conn, LaunchIdentity(
        launch_id="l", network_id=event.network_id, venue_id=event.venue_id,
        token_id="mint", creator_id=None, launch_event_id="e",
        launched_at=raw.chain_timestamp,
    ))
    conn.close()


def test_prepare_projects_before_explicit_fresh_hot_removal(tmp_path):
    hot = tmp_path / "hot.db"
    insight = tmp_path / "insight.db"
    _seed(hot)

    result = prepare(
        hot_db=hot, insight_db=insight, now_iso="2026-07-26T12:00:00Z",
        retention_s=1800, fresh_hot=True,
    )

    assert result["projected_launches"] == 1
    assert not hot.exists()
    compact = connect_insights(insight)
    assert compact.execute("SELECT launch_id FROM op_projected_launches").fetchone()[0] == "l"


def test_prepare_script_imports_from_direct_launch():
    result = subprocess.run(
        [sys.executable, "scripts/prepare_memecoin_opportunity_retention.py", "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
