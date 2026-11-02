"""Preserve exact event quote fields; never infer a missing net input or instruction budget."""
import base64,json,sqlite3,time
from sniper.services import memecoin_desk_pumpswap as P
from test_desk_pumpswap_virtual_quote import source_buy,append
from test_memecoin_desk_pumpswap import logs
from test_memecoin_desk_pumpswap_worker import worker
from test_desk_ps_sell_fee_capture import checker_module,witness_module

QUOTE_KEYS=('quote_amount_in','quote_amount_in_with_lp_fee','user_quote_amount_in','max_quote_amount_in','min_base_amount_out')
FEES=('lp_fee_basis_points','lp_fee','protocol_fee_basis_points','protocol_fee','coin_creator_fee_basis_points','coin_creator_fee',
      'cashback_fee_basis_points','cashback','buyback_fee_basis_points','buyback_fee')

def event():return P.events(logs(append(source_buy('buy_exact_quote_in'),bytes(16))))[0][1]

def test_quote_fields_are_literal_event_values_and_tagged():
    e=event();raw_payload=P.parse_buy_event('Program data: '+e['payload_b64'])
    for k in(*QUOTE_KEYS,*FEES):assert e['payload'].get(k)==raw_payload[k]
    assert e['payload']['buy_quote_capture_version']=='buy-exact-event-quotes-fees-v1'
    assert e['payload']['buy_quote_idl_sha256']==P.PUMP_AMM_IDL_SHA256
    assert e['payload']['buy_quote_capture_reason']is None
    assert e['virtual_quote_atomic']=='0' # pre-existing legacy-buy proof, unchanged


def test_compacted_buy_exact_row_keeps_quotes_fees_and_third_witness(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);at=time.time();e=event()
    try:
        w.on_pumpswap(e,42,'sig',0,1,None,at,at);w.commit_files();c=w.files.conn('swaps_ps',at)
        p=json.loads(c.execute('SELECT payload_json FROM swaps_ps').fetchone()[0])
        for k in(*QUOTE_KEYS,*FEES):assert k in p and p[k]==e['payload'][k]
        assert p['buy_quote_capture_version']=='buy-exact-event-quotes-fees-v1'
        row=c.execute('SELECT signature,ordinal,kind,raw,raw_sha256,published_json FROM ps_sell_fee_witness').fetchone()
        assert row[2]=='buy_exact_quote_in'
        report=checker_module().check_rows([row]);assert report['valid']==1 and report['mismatches']==[]
    finally:
        for c in w.files._open.values():c.close()


def test_independent_buy_parser_rejects_dynamic_name_and_quote_tampering():
    S=witness_module();C=checker_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    S.record(c,event(),signature='sig',ordinal=1,slot=42,tx_index=1,received_at=101.,known_at=102.)
    row=c.execute('SELECT signature,ordinal,kind,raw,raw_sha256,published_json FROM ps_sell_fee_witness').fetchone()
    assert row is not None and C.check_rows([row])['valid']==1
    p=json.loads(row[-1]);p['quote_amount_in']+=1
    report=C.check_rows([(*row[:-1],json.dumps(p))]);assert report['mismatches'][0]['field']=='quote_amount_in'


def test_buy_independent_parser_rejects_spoofed_name_and_float_field():
    import hashlib
    S=witness_module();C=checker_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    S.record(c,event(),signature='sig',ordinal=1,slot=42,tx_index=1,received_at=101.,known_at=102.)
    row=c.execute('SELECT signature,ordinal,kind,raw,raw_sha256,published_json FROM ps_sell_fee_witness').fetchone()
    raw=bytearray(row[3]);raw[405]=ord('X')
    bad=(*row[:3],bytes(raw),hashlib.sha256(raw).hexdigest(),row[-1])
    assert C.check_rows([bad])['mismatches'][0]['error']=='buy_instruction_name'
    p=json.loads(row[-1]);p['user_quote_amount_in']=float(p['user_quote_amount_in'])
    assert C.check_rows([(*row[:-1],json.dumps(p))])['mismatches'][0]['field']=='user_quote_amount_in'


def test_third_kind_has_independent_budget_and_readiness_requires_it():
    from test_desk_ps_sell_fee_capture import event as sell_event
    S=witness_module();C=checker_module();c=sqlite3.connect(':memory:')
    for sql in S.SCHEMAS.values():c.execute(sql)
    for kind in('sell','sell_v2','buy_exact_quote_in'):
        e=event()if kind=='buy_exact_quote_in'else sell_event(kind)
        for n in range(105):S.record(c,e,signature=kind+str(n),ordinal=n,slot=42,tx_index=1,received_at=101.,known_at=102.)
    rows=c.execute('SELECT signature,ordinal,kind,raw,raw_sha256,published_json FROM ps_sell_fee_witness').fetchall()
    assert len(rows)==300
    report=C.check_rows(rows);assert report['ready'] and report['valid']==300 and all(n==100 for n in report['by_kind'].values())
    assert not C.check_rows([r for r in rows if r[2]!='buy_exact_quote_in'])['ready']
