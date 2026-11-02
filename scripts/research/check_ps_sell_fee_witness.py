#!/usr/bin/env python3
"""Independent raw PumpSwap fee/quote offset audit. No production decoder import, RPC or writes."""
import argparse,hashlib,json,sqlite3,struct
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone

IDL='b7d8c57a4d9c4dd0109a9ab893052352333252d4eedced10ac091d4d66cab89b'
VERSION='sell-fees-head360-ext392-v1'
PROGRAM='pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
DISC=bytes.fromhex('3e2f370aa503dc2a')
BUY_DISC=bytes.fromhex('67f4521f2cf57777')
BUY_VERSION='buy-exact-event-quotes-fees-v1'
KINDS=('sell','sell_v2','buy_exact_quote_in')
# Pinned IDL: unsigned u64 fees, little-endian; virtual quote is signed i128 at byte392.
OFFSETS={'lp_fee_basis_points':72,'lp_fee':80,'protocol_fee_basis_points':88,'protocol_fee':96,
         'coin_creator_fee_basis_points':344,'coin_creator_fee':352,'cashback_fee_basis_points':360,
         'cashback':368,'buyback_fee_basis_points':376,'buyback_fee':384}


def check_rows(rows):
    counts=Counter();days=Counter();errors=[];unverified=0
    for signature,ordinal,kind,raw,digest,encoded in rows:
        identity={'signature':signature,'ordinal':ordinal}
        try:
            p=json.loads(encoded)
            if raw is None:
                unverified+=1;continue
            if hashlib.sha256(raw).hexdigest()!=digest or len(raw)<360 or kind not in KINDS:
                raise ValueError('raw_hash_or_layout')
            if p.get('kind')!=kind or p.get('program')!=PROGRAM:raise ValueError('program_or_kind')
            if kind=='buy_exact_quote_in':
                if raw[:8]!=BUY_DISC or len(raw)<405 or raw[360]not in(0,1):raise ValueError('buy_layout')
                n=struct.unpack_from('<I',raw,401)[0]
                if n>64 or len(raw)<437+n or raw[405:405+n]!=b'buy_exact_quote_in':raise ValueError('buy_instruction_name')
                if p.get('buy_quote_idl_sha256')!=IDL or p.get('buy_quote_capture_version')!=BUY_VERSION:raise ValueError('buy_abi')
                offsets={k:off for k,off in OFFSETS.items()if off<360}
                offsets.update(quote_amount_in=64,quote_amount_in_with_lp_fee=104,user_quote_amount_in=112,
                               max_quote_amount_in=24,min_base_amount_out=393)
                offsets.update({k:405+n+i*8 for i,k in enumerate(('cashback_fee_basis_points','cashback','buyback_fee_basis_points','buyback_fee'))})
                expected={k:struct.unpack_from('<Q',raw,off)[0]for k,off in offsets.items()}
                tail=437+n
                expected.update(buy_quote_event_bytes=len(raw),buy_quote_capture_reason=None)
            else:
                if raw[:8]!=DISC or p.get('sell_fee_idl_sha256')!=IDL or p.get('sell_fee_capture_version')!=VERSION:
                    raise ValueError('sell_abi_or_layout')
                expected={k:struct.unpack_from('<Q',raw,off)[0]if len(raw)>=off+8 else None for k,off in OFFSETS.items()}
                tail=392
                expected.update(sell_fee_event_bytes=len(raw),sell_fee_capture_reason=(
                    None if len(raw)>=392 else'extension_absent'if len(raw)==360 else'extension_partial'))
            if tail<len(raw)<tail+16:raise ValueError('partial_virtual_quote')
            expected['virtual_quote_reserves']=int.from_bytes(raw[tail:tail+16],'little',signed=True)if len(raw)>=tail+16 else None
            bad=[k for k,v in expected.items()if k not in p or type(p[k])is not type(v)or p[k]!=v]
            if bad:
                errors.extend(dict(identity,field=k,expected=expected[k],actual=p.get(k))for k in bad);continue
            cashback=expected['cashback_fee_basis_points']
            days[(p['witness_day'],kind)]+=1
            counts[(kind,'cashback_unknown'if cashback is None else'cashback_zero'if cashback==0 else'cashback_nonzero')]+=1
        except (ValueError,TypeError,KeyError,struct.error) as exc:errors.append(dict(identity,field='identity_or_layout',error=str(exc)))
    kinds={kind:sum(v for(k,_),v in counts.items()if k==kind)for kind in KINDS}
    return dict(checked=len(rows),valid=sum(counts.values()),by_kind=kinds,
                strata=[dict(kind=k,cashback=s,n=v)for(k,s),v in counts.items()],by_day=[dict(day=d,kind=k,n=v)for(d,k),v in days.items()],mismatches=errors,
                unverified=unverified,ready=not errors and sum(counts.values())>=200 and all(n>=50 for n in kinds.values()))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('database',type=Path);args=p.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2) as c:
        c.execute('PRAGMA query_only=ON');c.execute('PRAGMA trusted_schema=OFF')
        rows=c.execute('SELECT signature,ordinal,kind,raw,raw_sha256,published_json FROM ps_sell_fee_witness WHERE day<? LIMIT 901',(datetime.now(timezone.utc).date().isoformat(),)).fetchall()
    if len(rows)>900:raise SystemExit('witness_row_budget')
    report=check_rows(rows);print(json.dumps(report,sort_keys=True))
    raise SystemExit(1 if report['mismatches']else 0 if report['ready']else 2)


if __name__=='__main__':main()
