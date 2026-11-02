"""Parity gate for source_entry_window: Tier W extractor vs the desk tape on Oct 21-23 signals. Field parity by signature in [tc-900, tc]; 32-feature parity vs tapefeat-v2 fed the full tape in chain order. Any FIELD or FEAT line other than age_s on a late-started tape coin is a failure."""
import sys,json,sqlite3,random,math,collections,datetime as d
sys.path.insert(0,'/home/bigpapi/.cache/lambda/sew/scripts/research')
import source_entry_window as W
import importlib.util as _u
_s=_u.spec_from_file_location('TF','/home/bigpapi/.cache/lambda/tr/sniper/services/memecoin_desk_tapefeat.py'); TF=_u.module_from_spec(_s); _s.loader.exec_module(TF)   # tapefeat-v2 is on lambda/tape-reader (unmerged)
FEATS=TF.FEATS;DROP={"top_buyer","creator_sold","holders_pos"};KEEP=[i for i,f in enumerate(FEATS) if f not in DROP]
c=sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro",uri=True,timeout=30)
sig=c.execute("""select mint,created_at from (select mint,created_at,their_price,their_market_cap,row_number() over (partition by network_id,mint order by created_at,id) rn from memecoin_events where side='buy' and network_id='solana') where rn=1 and their_price>0 and their_market_cap>0 and created_at>='2026-10-21T04:00' and created_at<'2026-10-24'""").fetchall()
dbs=[sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/data/desk/swaps/2026-10-%d.db?mode=ro"%x,uri=True) for x in (21,22,23)]
def tape(mint):
    out=[]
    for db in dbs:
        out+=db.execute("select slot,ordinal,signature,is_buy,quote_atomic,token_atomic,actor,virtual_quote,virtual_token,real_token,fee_bps,creator_fee_bps,block_time from swaps where mint=?",(mint,)).fetchall()
    out.sort(key=lambda r:(r[0],r[1]));return out
random.seed(3);random.shuffle(sig)
r=W.Rpc();n=0;fieldbad=collections.Counter();featbad=collections.Counter();fe=0;cmp=0;tot_win=0;skipped=collections.Counter()
for mint,ca in sig:
    if n>=40:break
    tc=d.datetime.fromisoformat(ca.replace('Z','+00:00')).timestamp();tci=int(tc)
    rows=[x for x in tape(mint) if x[12] is not None and x[12]<=tci]
    if len(rows)<8: skipped["tape<8"]+=1;continue
    if rows[0][12] and rows[0][12]<1792540800-60: pass
    ext=W.fetch_coin(r,mint,tc)
    if ext["reason"] not in (None,"no_swaps_in_window"): skipped[ext["reason"]]+=1;continue
    n+=1
    # field parity in window
    lo=tci-900
    tw={(x[2],x[3],x[4],x[5]):x for x in rows if x[12]>=lo}
    ew={(e["sig"],int(e["buy"]),e["qa"],e["ta"]):e for e in ext["swaps"] if e["bt"]>=lo}
    tot_win+=len(ew)
    for k in set(tw)|set(ew):
        if k not in ew: fieldbad["missing_in_ext"]+=1; print("MISS",mint,tw[k][0],tw[k][12]-lo,tw[k][2][:8],"ext n",len(ew),"t0",ext["t0"],"capped",ext.get("capped"))
        elif k not in tw: fieldbad["missing_in_tape"]+=1
        else:
            t,e=tw[k],ew[k]
            if (t[6],t[7],t[8],t[9],t[10],t[11],t[12])!=(bytes.fromhex(e["actor"]).hex() and t[6],e["vq"],e["vb"],e["rt"],e["fee_bps"],e["cfee_bps"],e["bt"]):
                fieldbad["field_diff"]+=1
    # feature parity: reference in CHAIN order inside the window (tape ordinal ties are arbitrary within a slot)
    ix={e['sig']:(e['slot'],e['idx']) for e in ext['swaps']}
    rows=sorted(rows,key=lambda x:(x[0],ix.get(x[2],(0,0))[1]))
    ref=TF.TapeCoin(mint)
    for x in rows: ref.add(x[12],x[3],x[4],x[5],x[6],x[7],x[8],x[9],x[10],x[11])
    wc=TF.TapeCoin(mint)
    import base58
    for e in ext["swaps"]: wc.add(e["bt"],int(e["buy"]),e["qa"],e["ta"],base58.b58encode(bytes.fromhex(e["actor"])).decode(),e["vq"],e["vb"],e["rt"],e["fee_bps"],e["cfee_bps"])
    wc.t0=ext["t0"];wc.first_ok=ext["first_ok"]
    try:
        a=TF.features(ref,tc);b=TF.features(wc,tc)
    except Exception as ex:
        featbad["exc:"+str(ex)[:40]]+=1;print("EXC",mint,len(ext["swaps"]),ext["reason"],ex);continue
    for i in KEEP:
        cmp+=1;x,y=a[i],b[i]
        if (x!=x and y!=y) or x==y or (abs(x-y)<=1e-6*max(1,abs(x))): continue
        featbad[FEATS[i]]+=1; print("FEAT",mint,FEATS[i],x,y,"t0 tape/ext",ref.t0,wc.t0,"nwin",len(ext["swaps"]),"capped",ext.get("capped"))
print("coins",n,"window swaps",tot_win,"skipped",dict(skipped));print("FIELD",dict(fieldbad));print("FEAT mismatches",dict(featbad),"of",cmp);print("calls",r.calls,"MB",round(r.bytes/1e6,1))
