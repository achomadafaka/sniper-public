"""Bounded authenticated PumpSwap fee/quote decode sample; never pricing or loss proof."""
import base64,hashlib,json,math
from datetime import datetime,timedelta,timezone
from sniper.services.memecoin_desk_pumpswap import SELL_FEE_CAPTURE_KEYS,SELL_EVENT_DISCRIMINATOR,AMM_PROGRAM,BUY_EXACT_CAPTURE_KEYS,BUY_EVENT_DISCRIMINATOR

RULE='bottom100-per-kind-sha256-signature-nul-u64be-ordinal-utc-day-v1'
MAX_RAW=4096
SCHEMAS={'ps_sell_fee_witness':'CREATE TABLE IF NOT EXISTS ps_sell_fee_witness('
    'signature TEXT,ordinal INTEGER,kind TEXT,day TEXT,rank TEXT,raw BLOB,raw_sha256 TEXT,published_json TEXT,'
    'received_at REAL,slot INTEGER,tx_index INTEGER,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID'}
INDEXES={'ps_sell_fee_witness':('CREATE INDEX IF NOT EXISTS ps_sell_fee_witness_rank ON ps_sell_fee_witness(day,kind,rank)',)}


def expire(c,day):
    cutoff=(datetime.fromisoformat(day).date()-timedelta(days=2)).isoformat()
    return c.execute('DELETE FROM ps_sell_fee_witness WHERE (signature,ordinal) IN '
                     '(SELECT signature,ordinal FROM ps_sell_fee_witness WHERE day<? LIMIT 8)',(cutoff,)).rowcount


def record(c,event,*,signature,ordinal,slot,tx_index,received_at,known_at):
    kind=event['payload'].get('ix_name')
    if kind not in('sell','sell_v2','buy_exact_quote_in'):return 'unsupported'
    is_exact=kind=='buy_exact_quote_in'
    if event['kind']!=('buy' if is_exact else 'sell'):return 'unsupported'
    if (type(ordinal)is not int or not 0<=ordinal<2**63 or not signature
            or not all(type(t)in(int,float)and math.isfinite(t)for t in(received_at,known_at))
            or not 0<=received_at<=known_at):raise ValueError('sell_witness_identity')
    raw=base64.b64decode(event['payload_b64'],validate=True)
    oversize=len(raw)>MAX_RAW
    if len(raw)<360 or raw[:8]!=(BUY_EVENT_DISCRIMINATOR if is_exact else SELL_EVENT_DISCRIMINATOR):raise ValueError('sell_witness_event')
    day=datetime.fromtimestamp(known_at,timezone.utc).date().isoformat()
    rank=hashlib.sha256(signature.encode()+b'\0'+ordinal.to_bytes(8,'big')).hexdigest()
    capture_keys=BUY_EXACT_CAPTURE_KEYS if is_exact else SELL_FEE_CAPTURE_KEYS
    published={k:event['payload'].get(k)for k in(*capture_keys,'virtual_quote_reserves')}
    published.update(kind=kind,sample_population='decoded_successful_pumpswap_buy_exact_quote_in' if is_exact else 'decoded_successful_pumpswap_sells',program=AMM_PROGRAM,witness_rule=RULE,witness_day=day,
                     witness_raw_status='oversize' if oversize else 'captured')
    c.execute('SAVEPOINT sell_fee_witness')
    try:
        expire(c,day)
        if c.execute('SELECT 1 FROM ps_sell_fee_witness WHERE signature=? AND ordinal=?',(signature,ordinal)).fetchone():
            c.execute('RELEASE sell_fee_witness');return 'duplicate'
        kept=c.execute('SELECT signature,ordinal,rank FROM ps_sell_fee_witness WHERE day=? AND kind=? ORDER BY rank DESC LIMIT 100',(day,kind)).fetchall()
        if len(kept)==100:
            if rank>=kept[0][2]:c.execute('RELEASE sell_fee_witness');return 'sampled_out'
            c.execute('DELETE FROM ps_sell_fee_witness WHERE signature=? AND ordinal=?',kept[0][:2])
        c.execute('INSERT INTO ps_sell_fee_witness VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                  (signature,ordinal,kind,day,rank,None if oversize else raw,None if oversize else hashlib.sha256(raw).hexdigest(),json.dumps(published,sort_keys=True),received_at,slot,tx_index))
        c.execute('RELEASE sell_fee_witness');return 'oversize' if oversize else 'captured'
    except BaseException:
        c.execute('ROLLBACK TO sell_fee_witness');c.execute('RELEASE sell_fee_witness');raise
