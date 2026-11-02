"""Read-only inputs for account identity research; never opens a writable ledger."""
import json
from datetime import datetime

WINDOW_LIMIT = 10_000
BUY_LIMIT = 100_000
FUNDER_LIMIT = 10_000


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def read_account_claims(ledger):
    """Retain separate current claims so conflicting UIDs cannot overwrite one another."""
    from sniper.services.axiom_address_verify import _current_proofs, LEGACY_CONFIRMED
    from sniper.services.memecoin_chains import canonical_chain_id
    tables = _tables(ledger)
    rows = []
    if 'memecoin_wallet_addresses' in tables:
        for net, address, uid, handle in ledger.execute(
                'SELECT network_id,address,source_user_id,handle FROM memecoin_wallet_addresses WHERE verify_status=?',
                (LEGACY_CONFIRMED,)):
            rows.append(dict(chain=net, address=address, uid=uid, handle=handle, evidence_kind='legacy_confirmed'))
    if {'axiom_verified_addresses','memecoin_wallet_addresses'} <= tables:
        for net, address, uid, handle, detail in _current_proofs(ledger):
            info = json.loads(detail or '{}')
            rows.append(dict(chain=net, address=address, uid=uid, handle=handle,
                             evidence_kind=info.get('proof_version','current_verified_registry')))
    if 'memecoin_address_registry' in tables:
        for r in ledger.execute("SELECT source_user_id,network_id,address,evidence_kind,handle_seen,conflict_at "
                                "FROM memecoin_address_registry WHERE status='verified' AND network_id='solana'"):
            rows.append(dict(uid=r[0],chain=r[1],address=r[2],evidence_kind=r[3],handle=r[4],conflict=bool(r[5])))
    refuted=set()
    if 'memecoin_address_registry' in tables:
        refuted={(uid,canonical_chain_id(net),address.lower() if canonical_chain_id(net)!='solana' else address)
                 for uid,net,address in ledger.execute("SELECT source_user_id,network_id,address FROM memecoin_address_registry WHERE status='refuted'")}
    # Keep Pump UID claims separate; an address-keyed owner map loses competing UIDs.
    from sniper.services.memecoin_address_registry import _is_solana_address
    uids = {r[0] for r in ledger.execute('SELECT source_user_id FROM memecoin_wallets')}
    for uid in uids:
        if uid.startswith('pump:') and _is_solana_address(uid[5:]):
            rows.append(dict(chain='solana',address=uid[5:],uid=uid,evidence_kind='pump_profile_identity'))
        if str(uid).startswith('evm:'):
            from sniper.services.axiom_address_verify import evm_roster_address
            from sniper.settings.memecoin import MEMECOIN_EVM_ALLOWLIST
            address = evm_roster_address(uid)
            if address:
                rows.extend(dict(chain=c,address=address,uid=uid,evidence_kind='evm_profile_identity')
                            for c in MEMECOIN_EVM_ALLOWLIST)
    return [dict(r,chain=canonical_chain_id(r['chain'])) for r in rows if canonical_chain_id(r['chain']) and
            (r['uid'],canonical_chain_id(r['chain']),r['address'].lower() if canonical_chain_id(r['chain'])!='solana' else r['address']) not in refuted]


def load_identity_inputs(ledger, graph, *, since_iso, now_iso):
    claims = read_account_claims(ledger)
    coverage = dict(state='ok',window_from=since_iso,pre_window_s=60,capped=[],funding_state='unavailable')
    result = dict(claims=claims, windows=[],early_buys=[],funders=[],coverage=coverage)
    if graph is None:
        coverage.update(state='unavailable',reason='graph_absent')
        return result
    tables = _tables(graph)
    if not {'memecoin_wallet_upstream_reads','memecoin_wallet_early_buys'} <= tables:
        coverage.update(state='unavailable',reason='early_buyer_tables_absent')
        return result
    since, now = (datetime.fromisoformat(v.replace('Z','+00:00')).timestamp() for v in (since_iso,now_iso))
    read_cols={r[1] for r in graph.execute('PRAGMA table_info(memecoin_wallet_upstream_reads)')}
    ordering_col='n_order_unknown' if 'n_order_unknown' in read_cols else 'NULL'
    windows = graph.execute(
        'SELECT signature,mint,source_user_id,origin,t0,status,control_named,method_version,read_at,'+ordering_col+' '
        'FROM memecoin_wallet_upstream_reads WHERE (t0>=? AND t0<=?) OR '
        '(t0 IS NULL AND read_at>=? AND read_at<=?) ORDER BY t0,signature LIMIT ?',
        (since,now,since_iso,now_iso,WINDOW_LIMIT+1)).fetchall()
    if len(windows)>WINDOW_LIMIT:
        coverage['capped'].append('source_windows')
    result['windows'] = [dict(zip(('signature','mint','source_user_id','origin','t0','status','control_named',
                                   'method_version','read_at','n_order_unknown'),r)) for r in windows[:WINDOW_LIMIT]]
    cols={r[1] for r in graph.execute('PRAGMA table_info(memecoin_wallet_early_buys)')}
    signature_col='b.buy_signature' if 'buy_signature' in cols else 'NULL'
    order_col='b.order_proven' if 'order_proven' in cols else 'NULL'
    buys=graph.execute(
        'SELECT b.signature,b.trader,b.lead_s,b.slot,b.tx_index,'+signature_col+','+order_col+' '
        'FROM memecoin_wallet_early_buys b JOIN memecoin_wallet_upstream_reads r ON r.signature=b.signature '
        'WHERE r.t0>=? AND r.t0<=? ORDER BY r.t0,b.signature,b.trader LIMIT ?',
        (since,now,BUY_LIMIT+1)).fetchall()
    if len(buys)>BUY_LIMIT:
        coverage['capped'].append('early_buys')
    result['early_buys']=[dict(zip(('signature','trader','lead_s','slot','tx_index','buy_signature','order_proven'),r))
                          for r in buys[:BUY_LIMIT]]
    if 'memecoin_wallet_funders' in tables:
        funders=graph.execute('SELECT wallet,status,funder,payer,funding_sig,funded_at '
                             'FROM memecoin_wallet_funders ORDER BY wallet LIMIT ?', (FUNDER_LIMIT+1,)).fetchall()
        if len(funders)>FUNDER_LIMIT:
            coverage['capped'].append('funders')
        else:
            coverage['funding_state']='cached_heuristic'
        result['funders']=[dict(zip(('wallet','status','funder','payer','funding_sig','funded_at'),r))
                           for r in funders[:FUNDER_LIMIT]]
    if coverage['capped']:
        coverage['state']='partial'
    return result
