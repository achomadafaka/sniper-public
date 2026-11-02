"""J7 is a hint; confirmed watcher evidence owns the funded backup."""
import re
from datetime import datetime, timezone

from sniper.services.memecoin_source_initiation import DIRECT_PROOF_VERSION, source_initiation_refusal


def is_j7_buy(ev):
    return (getattr(ev, 'side', None) == 'buy'
            and getattr(ev, 'signal_source', None) == 'follow_activity'
            and getattr(ev, 'delivery_lane', None) in {'j7_wallets', 'j7_pump'}
            and getattr(ev, 'network_id', None) == 'solana')


def prepare(conn, ev):
    """Resolve only an exact cached receipt; no provider reads, waiting or state writes."""
    from sniper.services.memecoin_onchain_lane import OnchainEv
    from sniper.settings import memecoin as cfg
    parts = str(getattr(ev, 'fomo_event_id', '')).split(':')
    pump = getattr(ev, 'delivery_lane', None) == 'j7_pump'
    if (not pump and len(parts) == 2 and parts[0] == 'swap' and re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', parts[1])):
        return None, 'j7_opaque_hint'
    if len(parts) != 5:
        return None, 'j7_exact_source_unavailable'
    if (pump and (parts[0] != 'pump' or parts[3] != 'buy')
            or not pump and (parts[0] != 'swap' or not parts[1])):
        return None, 'j7_exact_source_unavailable'
    signature, wallet = (parts[1], parts[2]) if pump else (parts[2], parts[3])
    if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{64,88}', signature):
        return None, 'j7_exact_source_unavailable'
    try:
        row = conn.execute('SELECT * FROM onchain_wallet_detections WHERE signature=?', (signature,)).fetchone()
        if row is None:
            return None, 'j7_receipt_not_observed'
        proven = OnchainEv(row)
        if (proven.network_id != 'solana' or proven.mint != ev.mint
                or proven.source_user_id != ev.source_user_id
                or wallet != (proven.source_wallet if pump else str(proven.source_wallet).lower())
                or parts[4] != (proven.mint if pump else proven.mint.lower())
                or row['source_observation_state'] == 'retracted'):
            return None, 'receipt_identity_mismatch'
        if isinstance(proven.source_initiation, dict) and proven.source_initiation.get('proof_version') != DIRECT_PROOF_VERSION:
            return None, 'j7_confirmed_receipt_required'
        why = source_initiation_refusal(proven, 'solana')
        if why:
            return None, why
        proven.j7_native_failure_backup = str(cfg.MEMECOIN_SOLANA_INFRA_PROVIDER).lower() == 'shyft'
        if proven.j7_native_failure_backup:
            try:
                block_time = row['block_time']
                if type(block_time) is not int or block_time <= 0:
                    return None, 'j7_source_time_unavailable'
                source_at = datetime.fromtimestamp(block_time, timezone.utc)
                received_at = datetime.fromisoformat(str(row['detected_at']).replace('Z', '+00:00'))
                if received_at.utcoffset() is None or source_at > received_at:
                    return None, 'j7_source_time_unavailable'
            except (ValueError, TypeError, OverflowError, OSError):
                return None, 'j7_source_time_unavailable'
            # A later confirmed-receipt arrival cannot renew source age or funded authority.
            proven.created_at = source_at.isoformat()
        why = native_refusal(conn, proven)
        if why:
            return None, why
        proven.j7_verified_backup = True
        proven.delivery_lane = 'j7_verified_backup'
        return proven, None
    except Exception:
        return None, 'j7_receipt_state_unavailable'


def native_refusal(conn, ev):
    """Recheck native ownership; exact Shyft hints need a configured mechanical release."""
    from sniper.services.memecoin_onchain_lane import slot_lane_verdict, SHAPE_UNSUPPORTED_CLASS, MULTI_OWN_BUY_PREFIX
    from sniper.services.memecoin_slot_skips import decision_for
    from sniper.settings import memecoin as cfg
    signature = ev.fomo_event_id.removeprefix('onchain:')
    state, klass = slot_lane_verdict(conn, signature, ev.created_at)
    strict = getattr(ev, 'j7_native_failure_backup', False)
    decided = decision_for(signature) if strict or state is None else None
    if state is None and decided and decided[0] == 'rejected' and klass != SHAPE_UNSUPPORTED_CLASS:
        state = 'rejected'
    if state not in (None, 'failed', 'graduated'):
        return 'j7_native_' + state
    if strict:
        # A later policy refusal supersedes an older failed build or released reservation.
        if (decided and decided[0] == 'rejected'
                and not (klass == SHAPE_UNSUPPORTED_CLASS
                         and str(decided[1] or '').startswith(MULTI_OWN_BUY_PREFIX))):
            return 'j7_native_rejected'
        if klass not in cfg.MEMECOIN_SOLANA_FALLBACK_CLASSES:
            return 'j7_native_no_mechanical_failure'
        ev.slot_fallback_class = klass
        ev.native_failure_fallback = True
    return None


def has_exact_hint(conn, ev):
    """A bounded exact source match; opaque IDs cannot authorise a fallback."""
    from datetime import datetime, timedelta
    try:
        since = (datetime.fromisoformat(ev.created_at.replace('Z', '+00:00')) - timedelta(seconds=60)).isoformat()
        signature = ev.fomo_event_id.removeprefix('onchain:')
        pattern = f'swap:*:{signature}:{ev.source_wallet.lower()}:{ev.mint.lower()}'
        return conn.execute('SELECT 1 FROM memecoin_entry_attempts WHERE decision_started_at>=? '
                            "AND source_user_id=? AND network_id='solana' AND mint=? "
                            "AND signal_source='follow_activity' AND ((delivery_lane='j7_wallets' "
                            "AND origin_event_id GLOB ?) OR (delivery_lane='j7_pump' AND origin_event_id=?)) LIMIT 1",
                            (since, ev.source_user_id, ev.mint, pattern,
                             f'pump:{signature}:{ev.source_wallet}:buy:{ev.mint}')).fetchone() is not None
    except Exception:
        return False
