import sqlite3, json, math, csv, sys
W=sqlite3.connect('file:data/research_signal_windows_solana.db?mode=ro',uri=True,timeout=60)
L=sqlite3.connect('file:sniper_tracker.db?mode=ro',uri=True,timeout=60)
rows=list(csv.DictReader(open('data/research/2026-10-21-desk-judge-labeled-set.csv')))
sig=W.execute("select signal_key,source_sig,mint,s_slot,landing_sig,attempts_json from signals where status='read' and s_slot is not null and truncated=0").fetchall()
pos2sig={}; own=set()
for key,ssig,mint,s_slot,lsig,aj in sig:
    if lsig: own.add(lsig)
    for a in json.loads(aj or '[]'):
        r=L.execute("select tx_sig,position_id from memecoin_entry_attempts where attempt_id=?",(a,)).fetchone()
        if not r: continue
        if r[0]: own.add(r[0])
        if r[1]:
            pos2sig.setdefault(r[1],(ssig,mint,s_slot))
            for (t,) in L.execute("select tx_sig from memecoin_fills where position_id=? and tx_sig is not null",(r[1],)): own.add(t)
def feats(ssig,mint,s_slot):
    src=W.execute("select coin_vault,price from pool_swaps where signature=? and mint=? and price>0 limit 1",(ssig,mint)).fetchone()
    if not src: return None
    vault,p0=src
    sw=[((s-s_slot)*0.4,p) for s,ix,p,g in W.execute("select slot,tx_index,price,signature from pool_swaps where mint=? and coin_vault=? and slot between ? and ? and price>0 order by slot,tx_index",(mint,vault,s_slot,s_slot+50)) if g not in own and g!=ssig]
    n2=sum(1 for t,_ in sw if 0<t<=2); pre=[(t,p) for t,p in sw if 0<t<=5]
    return n2,len(pre),(math.log(pre[-1][1]/p0) if pre else 0.0)
out=[]
for r in rows:
    k=pos2sig.get(int(r['position_id']))
    f=feats(*k) if k and k[1]==r['mint'] else None
    r['f_swaps_since_source_2s'],r['f_swaps_since_source_5s'],r['f_r_since_source_5s']=(f[0],f[1],round(f[2],6)) if f else ('','','')
    out.append(r)
w=csv.DictWriter(open(sys.argv[1],'w',newline=''),fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
have=[r for r in out if r['f_swaps_since_source_5s']!='']
print('rows',len(out),'with value',len(have))
ret=sorted(out,key=lambda r:float(r['y_return_pct']))
bot,top=ret[:20],ret[-20:]
for lab,g in (('bottom20',bot),('top20',top)):
    hv=[r for r in g if r['f_swaps_since_source_5s']!='']
    print(lab,'with value',len(hv),'gate SKIP (5s<=1)',sum(int(r['f_swaps_since_source_5s'])<=1 for r in hv), 'pids', [ (r['position_id'],r['f_swaps_since_source_5s']) for r in hv])
