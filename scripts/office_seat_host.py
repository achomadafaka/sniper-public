#!/usr/bin/env python3
"""Runs one Office v2 seat's host side: proxies, account poller, wake loop. Paper and research only; no funded import."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sniper.office import claude_seat_auth as A,chain_data as C,model_proxy as MP,seat_retention as R,egress_proxy as P,quota_measurement as Q,seat_events as E,seat_host as H,seat_sandbox as B,work as W

LOG=logging.getLogger('office_seat_host')
MODEL_HOSTS=frozenset({'anthropic.com','claude.ai','claude.com'})
POLL_S=5
RETENTION_EVERY_S=3600


def stop_seat_units(cfg):
    """Every wake and tool unit of this seat. They live under the office user's manager, so stopping the host does not reach them."""
    stopped=subprocess.run(['sudo','-n','-u','office','env','XDG_RUNTIME_DIR=/run/user/%d'%B.office_uid(),'systemctl','--user','stop','office-seat-%s-*'%cfg['actor']],capture_output=True,cwd='/',timeout=60)
    if stopped.returncode:raise SystemExit('seat_units_not_stopped: '+stopped.stderr.decode()[:200])


def prepare(cfg):
    """Everything a bind needs must exist before bwrap runs. Office-owned paths are made as office."""
    seat=Path(cfg.get('bridge',B.BRIDGE))/'seats'/cfg['actor'];seat.mkdir(parents=True,exist_ok=True)
    made=subprocess.run(['sudo','-n','-u','office','mkdir','-p',cfg['workspace']+'/tmp',cfg['state_dir']+'/.claude',cfg['state_dir']+'/.codex/sessions',cfg['state_dir']+'/tmp'],capture_output=True,cwd='/')
    if made.returncode:raise SystemExit('seat_dirs_failed: '+made.stderr.decode()[:200])
    # A host crash frees the lifecycle lock while its wake unit can still run: end any leftover before this host can start another.
    stop_seat_units(cfg)
    if cfg.get('family')=='codex':
        import shutil
        # Latest-version order: a CLI bump changes these pins at publish.
        for source,name,pin in ((cfg['codex_binary'],'codex',cfg['codex_sha256']),(cfg['codex_host_binary'],'codex-code-mode-host',cfg['codex_host_sha256'])):
            shutil.copyfile(source,seat/(name+'.tmp'))
            if hashlib.sha256((seat/(name+'.tmp')).read_bytes()).hexdigest()!=pin:raise SystemExit('codex_binary_not_pinned')
            os.chmod(seat/(name+'.tmp'),0o755);os.replace(seat/(name+'.tmp'),seat/name)
    return seat


def runner_environ(pid_file):
    """.env is not ground truth: the watchdog overrides it, so the running process's environment is read too. Only hostnames are kept from it."""
    pid=Path(pid_file).read_text().strip()
    if not pid.isdigit() or b'memecoin_runner' not in Path('/proc',pid,'cmdline').read_bytes():raise ValueError('runner_pid_mismatch')
    return Path('/proc',pid,'environ').read_bytes().replace(b'\0',b'\n').decode('utf-8','replace')


def live_hosts(cfg):
    return P.effective_hosts(cfg['funded_config_files'],environ_texts=[runner_environ(cfg['runner_pid_file'])],keep=MODEL_HOSTS)


class DenyRefresher:
    """Keeps the deny set current during wakes too: the proxies read it per connection. A funded deploy can change hosts or make the
    environment briefly unreadable, so failure is tolerated for GRACE_S, then parks wakes and stops a running one. The set only grows."""
    INTERVAL_S=30
    GRACE_S=75   # with 30 s refresh and 10 s watcher sampling, a running wake stops within the accepted 120 s, plus stop latency

    def __init__(self,cfg,proxies,clock=time.time):
        self.cfg,self.proxies,self.clock=cfg,proxies,clock
        self.failing_since=None

    def refresh(self):
        try:added=live_hosts(self.cfg)
        except (OSError,ValueError) as exc:
            LOG.warning('funded_hosts_refresh_failed %s',type(exc).__name__)
            if self.failing_since is None:self.failing_since=self.clock()
            return
        for proxy in self.proxies:proxy.deny=frozenset(proxy.deny|added)
        self.failing_since=None

    def park_reason(self):
        return 'funded_hosts_unknown' if self.failing_since is not None and self.clock()-self.failing_since>=self.GRACE_S else None

    def run(self):
        while True:
            self.refresh()
            time.sleep(self.INTERVAL_S)


def start_proxies(cfg,seat):
    deny,version=P.load_denylist(cfg['denylist'])
    # Fails closed if the funded config cannot be read. Only the count is logged: hostnames can carry keys.
    live=live_hosts(cfg)
    if not live:raise SystemExit('funded_hosts_empty')
    deny=deny|live
    LOG.warning('funded_hosts_loaded count=%d',len(live))
    ca=P.load_or_create_ca(cfg['ca_dir'],cfg['actor'])
    (seat/'ca.pem').write_bytes(Path(cfg['ca_dir'],'ca.pem').read_bytes())
    uid=B.office_uid();log=Path(cfg['proxy_log_dir']);log.mkdir(parents=True,exist_ok=True)
    tool=P.Proxy(str(seat/'tool-proxy.sock'),peer_uid=uid,deny=deny,ca=ca,log=str(log/'tool-%Y%m%d.jsonl'),search_broker=P.S.from_config(cfg),read_only_hosts=P.load_read_only_hosts(cfg['denylist']));tool.start()
    if cfg.get('family')=='codex':model=MP.ModelProxy(str(seat/'model-proxy.sock'),peer_uid=uid,deny=deny,ca=ca,actor=cfg['actor'],account_alias=cfg['account_alias'],quota_db=cfg['quota_db'],auth_file=cfg['codex_auth'],log=str(log/'model-%Y%m%d.jsonl'))
    else:model=P.Proxy(str(seat/'model-proxy.sock'),peer_uid=uid,deny=deny,allow=MODEL_HOSTS,log=str(log/'model-%Y%m%d.jsonl'))
    model.start()
    LOG.warning('proxies_up denylist_sha=%s',version)
    return tool,model


def ticket_source(desk_db):
    def latest(after=None):
        try:
            conn=sqlite3.connect('file:%s?mode=ro'%desk_db,uri=True,timeout=.2)
            try:
                deadline=time.monotonic()+.05
                conn.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
                rows=conn.execute("SELECT rowid,ticket_id,stage,verdict,substr(reason,1,160) FROM desk_tickets WHERE rowid>? AND stage='hard_filter' AND verdict='PASS' ORDER BY rowid DESC LIMIT 33",(after if after is not None else 0,)).fetchall()
                if not rows:return None
                tickets=[dict(zip(('rowid','ticket_id','stage','verdict','reason'),row)) for row in reversed(rows[:32])]
                return dict(cursor=rows[0][0],tickets=tickets,observed_count=len(tickets),
                            count=len(tickets) if after is not None and len(rows)<=32 else None,baseline=after is None,truncated=len(rows)>32)
            finally:conn.close()
        except sqlite3.Error:return None   # unreadable is no signal, never a cursor advance
    return latest


FILL_RETRY_CHANGE="Alpha 2026-10-30: commit-lag retry on entry per operator's ASAP order; fills later instead of censoring when tape commit lags; nothing booked in the past. measurement_definition_changed. entry_timeout_s 60 bounds total age."


RESULTS_CHANGE="Alpha 2026-10-30: closed paper results open to seats by results_view, per Gamma's learning-rule proposal 395179646. NET before and after is not comparable. Alpha accepts entry 2 before entry 1; outcome_table_asof absent_by_manifest. results_view opened to Head, Judge, Scout; not to Researcher, Forensics."
ROTATE_CHANGE="session rotation by context per model request: rotate_read_per_request 300000 on all seats, Alpha 2026-10-31; rotate_cache_read 800000 only when no request count exists; changes what a seat remembers; not a change to fill model, rails, equity or null-arm definitions. Alpha 2026-10-30: all seats, 800k knob. One-time catch-up at publish: Head, Judge, Scout rotate at once, one swap wake each at about 0.5M to 1.5M cache read."
EXIT_ESTIMATE_CHANGE="Alpha 2026-10-30, operator order: after one full 60 s failed hold, an exit closes at the first labelled estimate observed from expiry on, closed_at that tick; none: keeps trying. orders.exit_fill_kind=estimated_terminal; same fee path, proceeds are an upper-biased model. Its cash counts in kill equity: the kill reading is no longer strictly proven-only for those closes. Kill OFFICE_EXIT_ESTIMATE_FILL=0. 3e55d750 opened under the older definition. measurement_definition_changed."
SEAT_STRATEGY_CHANGE="Alpha 2026-10-30, operator order: a Judge may set sl_pct, tp_pct, trail_pct inside rail_bounds, frozen, labelled rail_override, and request an exit (paper_exit, exit_by judge). Limits unchanged. Rail choice follows seeing the coin: discretion, not skill; null-arm comparisons are confounded by it. Request ping to Alpha: first use proves the bridge self-send; failure only logs. Kill OFFICE_SEAT_STRATEGY=0, OFFICE_REQUEST_NOTIFY=0. measurement_definition_changed."
BLOCK_CUT_CHANGE="Alpha 2026-10-30: an exit refused pool_block_cut_unproven takes a fresh cut at once, since that cut can never heal. Terminal policy change: past 120 s from the exit request, strict is tried first unless the cut is over 60 s old (no past exit), then the terminal estimate. orders.entry_refusals counts entry refusals by reason and branch; delayed entries are struck at the later cut. Entry path otherwise unchanged. measurement_definition_changed."
DOCS_EGRESS_CHANGE="Alpha 2026-10-30: seat tool proxy allows docs.pump.fun only, exact host, GET and HEAD, no body, no Authorization or Cookie. pump.fun, its other subdomains and all other denied domains stay denied; each redirect is a new request and is rechecked. Model proxy unchanged."
JUDGE_GPT_CHANGE="Alpha 2026-10-31, operator order: second Judge on gpt-6.1-sol, own ledger epoch and paper book, same clip, concurrency, daily halt and $200 kill, per book. One entry per coin per book. No null arm is wired for this book yet. Head halt stops both. New stratum from first wake; first 10 wakes are a canary. No pooling or comparison: same market, correlated. measurement_definition_changed."
HEAD_GPT_CHANGE="Alpha 2026-10-30: Head moves from Opus to GPT gpt-6.1-sol. Different judgement: results before and after are not comparable. Rotation stays the 800k cache-read knob, counted by CLI cached input; the CLI also compacts on its own. Counters may read unknown, never zero; every wake is labelled tainted. Kill: restore the Claude config. measurement_definition_changed."
NARRATIVE_CHANGE="Alpha 2026-10-30, operator GO: Narrative seat added, no order tools. Operator order: denied_tools empty on every seat. It mails Judge and Scout NARRATIVE lines with sources, retrieved_at and verdict; those lines are the claim log, counted forward against outcomes. Seat mail may now shape entries: label reads, no signal claim without counts. Search budget 500/day stays shared. Runs on GPT; model in each wake; counters may read unknown. Forensics not started. measurement_definition_changed."
CAPTURE_CLOCK_CHANGE="Alpha 2026-10-30: strict PumpSwap quotes, entries and exits, check heartbeat liveness on a clock sampled at the heartbeat read, not the frozen tick. A heartbeat ahead of it still refuses. Off when the tick is over 30 s old (live_clock_off). Commit slack is still 5 s but read on a later clock; commit_slack refusals can rise. marks.reason_branches: all branches, not a share of null_n. Kill OFFICE_CAPTURE_LIVE_CLOCK=0. measurement_definition_changed."
EXIT_CUT_CHANGE="Alpha 2026-10-30, operator order, stuck exit 3e55d7509eda: an exit keeps its requested cut through tape, capture and FX lag while the watcher attends, up to 60 s, then a fresh cut. Before: 5 s frontier grace, any other refusal moved the cut. Live replay: held cuts proved at 26-42 s, 3 of 7 not by 45 s. A loss inside the span never heals, so the bound still applies. Entries unchanged. measurement_definition_changed."
ENTRY_CHANGE="Alpha 2026-10-30, entry sell-tip selection: when the newest swap at the entry cut is a sell, the fill uses newest reserves with the quote carried from the exact proving buy, age at observation up to 120 s, else entry_carry_stale. Other checks stay strict. A hole in the carried span refuses, entry_carry_span_gap: entries stricter than exits. No estimated entries; exit fills strict. Kill OFFICE_ENTRY_CARRIED=0. See operator-changes record. measurement_definition_changed."
MARK_AGE_CHANGE="Alpha 2026-10-30: MARK_MAX_AGE_S 15 to 30 s per Gamma's prior rule; one-hour commit lag p90 11.7 s, n=1800, hour included publishes. Observation bounds unchanged: swap 60 s, carry 300 s, entries 120 s. May return to 15 s if a quiet hour shows p90 under 10 s. measurement_definition_changed."
PARTIAL_SHEET_CHANGE="Alpha 2026-10-31, terminal policy change partial-sheet-orders-v1: a paper order may ride a partial receipt when the full sheet failed for an ageing or proof class. Sells from the tape 60 s before the ticket cut; a proven zero vetoes; unknown passes only on a stored passing sell probe. orders.sheet_state and sells_basis stratify; never pool. Rollback OFFICE_PARTIAL_SHEET_ORDERS=0."
FEE_BASIS_CHANGE="Alpha 2026-10-31: sheet caps and paper fills no longer take a 0/0/0 fee from one exempt tip swap. They use the pool's newest non-zero fees within the reserve age bound, labelled fee_basis newest_nonzero; none found keeps zero, labelled tip_zero_unresolved. Real zero rows were 0.23% of the tape. Fills in this segment can cost more than before. Paper only."
TAPE_GAP_RETRY_CHANGE="Alpha 2026-10-31: a sheet read that fails tape_gap_since_cut while heartbeat ps_pending_gaps is non-empty retries on the Office thread, 3 tries at 1.5 s. A settled gap refuses at once. Sheet reads and paper orders can take up to 4.5 s longer. The pending map is kept on the failure event. Inferred race, checked by those events."
SAFETY_EVIDENCE_CHANGE="Operator 2026-10-31 via Alpha: sheets report sheet-safety-evidence-v1. safety.cluster_hit is the host check evidence.checks.cluster; the newest store decision is diagnostic, disagreement labelled. lp_locked_or_burned is true only on LP mint supply 0 at a dated slot, else null. Holder and cluster shares carry denominators. Not a gate: risk reads none of it. Null is not known, never safe. Seats may trade differently on it from this segment."
PRICE_BASIS_CHANGE="Operator 2026-10-31 via Alpha: sheets label price_basis virtual_inclusive and show the real-reserve price and the ratio. Display only. Fills and exits are unchanged and already price on real plus virtual quote; a fill above the real-reserve price is not a premium. price_basis.unpriced_after_last counts tape swaps after structure's last priced one: a tip sell leaves last at the price before it."
WALLET_NETWORK_CHANGE="Operator 2026-10-31 via Alpha: Forensics gets wallet_add, edge_add, network_expand and network_read on data/office/wallet_network.db. Seat-sourced and labelled, separate from funded arming. Verified needs a matching chain fetch. Caps: 1 GB, 100000 rows a day, 33333 estimated credits a day (10 percent of the 10M plan over 30 days); expiry 90 and 365 days."
FEED_LIQUIDITY_CHANGE="Seat request 22511, Alpha 2026-10-31: new tool feed_liquidity: real-quote liquidity at emission against the newest tape row, with reserves, slots and ages, material on a 50 percent fall. Reported only, no gate and no wake. Missing either side is unknown, never no change."
CANDIDATE_LISTS_CHANGE="Alpha 2026-10-31: new tool candidate_lists: graduated_liquid (real quote at least 85 SOL) and buyer_growth, from stored tape. Reported, no gate. Seats may order differently from this segment. One window showed no edge over all pools."
WALLET_WATCH_CHANGE="Operator 2026-10-31 via Alpha: watch list in wallet_network.db, tools watch_add, watch_remove, wallet_episodes for Forensics, watch_events for all paper seats. Every swap by a listed wallet is recorded by capture. Claims, not findings; no gate, no funded path. Seats may trade on it from this segment."
GATE_CHANGE="Alpha 2026-10-30: entry gate accepts labelled carried and estimated marks (30 s); reversible refusals no longer burn the coin. Kill and 24 h inputs stay proven-only (60 s). While any open mark is an estimate the drawdown kill is blind: loss is bounded only by book capital, _submit concurrency and the 24 h halt, not the $200 line. A known proven read skips the 30 s check, so a 31-60 s proven mark admits. Kill OFFICE_GATE_ESTIMATES=0. measurement_definition_changed."
ESTIMATE_CHANGE="Alpha 2026-10-30, P1 FINDING: exit marks fall to a labelled estimate when strict proof refuses: newest swap, age at observation up to 60 s; quote proved by it or carried up to 300 s. Rails may fire; fills, entries strict. Liquidity removal cannot be detected. See operator-changes section 9, d44574da3. measurement_definition_changed."
MARK_CHANGE="Alpha 2026-10-30, P1 FINDING: marks are almost always null: marks price at the newest committed cut when the requested cut is past the frontier, age recorded, max age 15 s, older is null; exits requested at observation time; exit commit-lag retry at a later cut, paged once at 300 s and once on recovery. measurement_definition_changed."


def set_funded_db(cfg):
    """The sheet's buyer ledger opens the default database path, relative to the working directory; this host runs from the release. Set once before any thread starts."""
    if cfg.get('funded_db'):os.environ['DB_PATH']=cfg['funded_db']


def logged_settings(cfg):
    """What the config change entry hashes: anything that changes when a seat wakes or parks."""
    from sniper.office.tools import sheet
    return {'ps_trending_budget_s':sheet.TRENDING_BUDGET_S,'heat_budget_s':0,**{k:cfg.get(k) for k in ('rotate_cache_read','rotate_read_per_request','ticket_wakes','ticket_wake_min_s','default_sleep_s','min_sleep_s','max_sleep_s','uncapped','family','effort','wakes_per_hour','absolute_ceiling','usage_scope','sdk_version','wake_deadline_s','max_turns',
                                   'account_poller','desk_watcher','account_max_age_s','account_poll_s','research_env_file')}}


WINDOW_RESET_GRACE_S=5


def _window_reset_since(quota,alias,latest,now):
    """A window the newest reading names has reset since that reading, so seats would park on an expired window until the next poll. A failed poll after the reset is itself the latest, so this can't hammer a rate limit."""
    row=quota.execute("SELECT payload_json FROM quota_account_snapshots WHERE account_alias=? AND observed_at=? LIMIT 1",(alias,latest)).fetchone()
    try:windows=json.loads(row[0])['windows'] if row else {}
    except (ValueError,KeyError,TypeError):return False
    return any(isinstance(w,dict) and isinstance(w.get('resets_at'),(int,float)) and latest<w['resets_at']<=now-WINDOW_RESET_GRACE_S for w in windows.values())


def poll_codex_sandboxed(cfg,quota):
    """Never the CLI on the real home: its rate-limit read can refresh and rotate the real credential. Here it holds the fake token only. A failed read records as unknown and the seat parks."""
    raw,error=None,'transport'
    try:
        done=subprocess.run(['sudo','-n','-u','office']+B.codex_args(cfg['actor'],cfg['state_dir'],bridge=cfg.get('bridge',B.BRIDGE),host_sock=False)+['/opt/runtime/bin/python3','/opt/release/sniper/office/codex_child.py','--poll'],
                            input=json.dumps(dict(account_id=MP.read_token(cfg['codex_auth'])[1]))+'\n',capture_output=True,text=True,timeout=60,cwd='/')
        doc=json.loads(done.stdout.splitlines()[-1])
        raw,error=doc['raw'],doc['error']
    except (OSError,ValueError,KeyError,TypeError,IndexError,subprocess.SubprocessError):LOG.warning('account_poll_failed codex')
    Q.record_account_snapshot(quota,snapshot_id=uuid.uuid4().hex,provider='codex',account_alias=cfg['account_alias'],observed_at=time.time(),raw=raw,error=error)
    Q.prune(quota,now=time.time())


def poll_account(cfg,quota=None):
    """Seats share one account and one rate-limited usage endpoint, so exactly one seat polls (config account_poller). The others only read; if the poller is down their reading ages out and they park."""
    if not cfg.get('account_poller'):return
    if quota is not None:
        latest=quota.execute('SELECT max(observed_at) FROM quota_account_snapshots WHERE account_alias=?',(cfg['account_alias'],)).fetchone()[0]
        now=time.time()
        if latest is not None and 0<=now-latest<cfg['account_poll_s'] and not _window_reset_since(quota,cfg['account_alias'],latest,now):return
    if cfg.get('family')=='codex':return poll_codex_sandboxed(cfg,quota)
    done=subprocess.run([sys.executable,str(Path(__file__).with_name('office_quota_snapshot.py')),'--db',cfg['quota_db'],'--provider','claude','--claude-credentials',cfg['claude_credentials'],
                         '--account-alias',cfg['account_alias']],capture_output=True,text=True,timeout=60)
    if done.returncode:LOG.warning('account_poll_failed rc=%s',done.returncode)
    else:
        try:
            doc=json.loads(done.stdout.splitlines()[-1])
            if cfg.get('research_env_file'):
                try:A.quota_page(cfg,doc,page=C.operator_page(cfg['research_env_file'],prefix=''))
                except Exception:LOG.warning('quota_auth_page_failed')
        except (OSError,ValueError,TypeError,IndexError):LOG.warning('quota_auth_page_or_receipt_failed')


EXIT_PAGE_S=300


def page_stuck_exits(cfg,paged,*,now=None,page=None):
    """An exit can't censor, so one still open past the wait reaches the operator, once per order per host run. The watcher's sandbox has no Telegram."""
    from sniper.office.chain_data import operator_page
    conn=sqlite3.connect('file:%s?mode=ro'%cfg['ledger_db'],uri=True,timeout=2)
    try:rows=conn.execute("SELECT order_id,exit_requested_at,exit_frontier_retries FROM orders WHERE state='open' AND exit_requested_at IS NOT NULL AND exit_requested_at<?",((now or time.time())-EXIT_PAGE_S,)).fetchall()
    finally:conn.close()
    for order_id,at,retries in rows:
        if order_id in paged:continue
        # Per order: one failed send retries next pass and costs the other orders nothing.
        try:(page or operator_page(cfg['research_env_file'],prefix='Office paper exit stuck: '))('order %s exit requested at %s, %s commit-lag retries, not filled. Paper only.'%(order_id[:12],at,retries))
        except Exception:LOG.exception('exit_page_failed order=%s',order_id[:12])
        else:paged.add(order_id)
    stuck={r[0] for r in rows}
    for order_id in sorted(paged-stuck):   # no longer open past the wait: it closed
        (page or operator_page(cfg['research_env_file'],prefix='Office paper exit recovered: '))('order %s is no longer stuck. Paper only.'%order_id[:12])
        paged.discard(order_id)


def poll_loop(cfg,*,wait=time.sleep,paged=None):
    """Its own thread and connection: a wake can run an hour, and the other seats' reading must not age out meanwhile."""
    paged=set() if paged is None else paged
    while True:
        try:
            quota=sqlite3.connect(cfg['quota_db'],timeout=5,isolation_level=None)
            try:poll_account(cfg,quota)
            finally:quota.close()
            if (cfg.get('desk_watcher') or cfg.get('exit_pages')) and cfg.get('ledger_db'):
                # A page about an exit must never cost the seats their account reading.
                try:page_stuck_exits(cfg,paged)
                except Exception:LOG.exception('exit_page_failed')
        except (OSError,ValueError,sqlite3.Error,subprocess.SubprocessError):LOG.exception('account_poll_failed')
        wait(5)


def eval_cohort(office):
    """The feed stays eval-1's frozen cohort (epoch record: feed and hard filter do not change inside the epoch). Missing means a seat lists nothing, loudly."""
    row=office.execute("SELECT payload_json FROM epochs WHERE id='office-paper-eval-1'").fetchone()
    return json.loads(row[0])['record']['near_pass_cohort']['id'] if row else None


def policy_problem(office,epoch,policy):
    """None, or why this book policy can't run. Rails and the fill model define what the epoch's numbers mean: the file must match the epoch, unless an override with a written reference entered exactly this policy."""
    from sniper.office import eval_transition as X
    row=office.execute('SELECT payload_json FROM epochs WHERE id=?',(epoch,)).fetchone()
    stored=json.loads(row[0]).get('book_policy') if row else None
    if stored is None:return 'book_policy_unknown'
    identity=X.book_policy_identity(policy)
    if identity==stored:return None
    entered=office.execute("SELECT 1 FROM office_v2_changes WHERE epoch_id=? AND kind='override' AND content_hash=? AND note<>'' LIMIT 1",(epoch,identity['sha256'])).fetchone()
    return None if entered else 'book_policy_changed'


def X_identity(policy):
    from sniper.office import eval_transition as X
    return X.book_policy_identity(policy)['sha256']


def check_policy(office,epoch,policy):
    why=policy_problem(office,epoch,policy)
    if why:raise SystemExit('explore_'+why)


def policy_watch(cfg,office):
    """Every wake re-reads the file. A file that differs from what the running service loaded parks the seat until a rebuild: an override only takes effect at restart, so orders never run on one policy while the record says another."""
    from sniper.office import eval_transition as X
    def reason():
        try:
            policy=json.loads(Path(cfg['book_policy']).read_text())['policy']
            # The wake watcher calls this off the main thread; a sqlite connection may not cross threads.
            conn=sqlite3.connect(cfg['office_db'],timeout=5) if cfg.get('office_db') else office
            try:why=policy_problem(conn,cfg['epoch'],policy)
            finally:
                if conn is not office:conn.close()
            if why:return why
            loaded=cfg['paper'].policy_sha256 if cfg.get('paper') else None
            return 'book_policy_restart_needed' if loaded and X.book_policy_identity(policy)['sha256']!=loaded else None
        except (OSError,ValueError,KeyError,sqlite3.Error):return 'book_policy_unknown'
    return reason


def log_seat(cfg,office,release):
    """Every seat's tool set, prompt, model and settings enter the change log before they take effect, with or without a book."""
    from sniper.office import seat_tools as T,v2_explore as X
    epoch=cfg['epoch']
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(release.encode()).hexdigest(),kind='release')
    for kind,doc in (('tool_set',sorted(set(T.TOOLS)-set(cfg.get('denied_tools',())))),('prompt',Path(cfg['prompt_file']).read_text()),('model',cfg['model']),('config',logged_settings(cfg))):
        X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(json.dumps(doc).encode()).hexdigest(),kind=kind,note=cfg['actor'])


def build_paper(cfg,office):
    """Refuses to start unless the epoch is really open and this seat's tool set and prompt are entered in the change log first."""
    from sniper.office import ledger as L,runtime as RT,seat_tools as T,v2_explore as X,v2_paper as V
    epoch=cfg['epoch']
    Path(cfg['ledger_db']).parent.mkdir(parents=True,exist_ok=True)
    ledger=L.open_ledger(Path(cfg['ledger_db']))
    try:
        # The GPT Judge's book is its own ledger epoch: the store's epoch must be active, the main book is not its business.
        why=(X.office_ready(office) or X.provision_gpt_ledger(ledger,now=time.time())) if cfg.get('ledger_epoch') else X.ready(office,ledger)
    finally:ledger.close()
    if why:raise SystemExit('explore_not_ready:'+why)
    release=Path('PUBLISHED_SHA').read_text().strip()
    book=json.loads(Path(cfg['book_policy']).read_text())
    check_policy(office,epoch,book['policy'])
    log_seat(cfg,office,release)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(FILL_RETRY_CHANGE.encode()).hexdigest(),kind='config',note=FILL_RETRY_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(MARK_CHANGE.encode()).hexdigest(),kind='config',note=MARK_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(ROTATE_CHANGE.encode()).hexdigest(),kind='config',note=ROTATE_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(ESTIMATE_CHANGE.encode()).hexdigest(),kind='config',note=ESTIMATE_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(GATE_CHANGE.encode()).hexdigest(),kind='config',note=GATE_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(MARK_AGE_CHANGE.encode()).hexdigest(),kind='config',note=MARK_AGE_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(ENTRY_CHANGE.encode()).hexdigest(),kind='config',note=ENTRY_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(EXIT_CUT_CHANGE.encode()).hexdigest(),kind='config',note=EXIT_CUT_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(CAPTURE_CLOCK_CHANGE.encode()).hexdigest(),kind='config',note=CAPTURE_CLOCK_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(NARRATIVE_CHANGE.encode()).hexdigest(),kind='config',note=NARRATIVE_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(BLOCK_CUT_CHANGE.encode()).hexdigest(),kind='config',note=BLOCK_CUT_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(DOCS_EGRESS_CHANGE.encode()).hexdigest(),kind='config',note=DOCS_EGRESS_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(SEAT_STRATEGY_CHANGE.encode()).hexdigest(),kind='config',note=SEAT_STRATEGY_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(EXIT_ESTIMATE_CHANGE.encode()).hexdigest(),kind='config',note=EXIT_ESTIMATE_CHANGE)
    if os.environ.get('OFFICE_PARTIAL_SHEET_ORDERS','1').strip()!='0':X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(PARTIAL_SHEET_CHANGE.encode()).hexdigest(),kind='config',note=PARTIAL_SHEET_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(FEE_BASIS_CHANGE.encode()).hexdigest(),kind='config',note=FEE_BASIS_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(TAPE_GAP_RETRY_CHANGE.encode()).hexdigest(),kind='config',note=TAPE_GAP_RETRY_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(SAFETY_EVIDENCE_CHANGE.encode()).hexdigest(),kind='config',note=SAFETY_EVIDENCE_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(PRICE_BASIS_CHANGE.encode()).hexdigest(),kind='config',note=PRICE_BASIS_CHANGE)
    if cfg.get('role')=='forensics':X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(WALLET_NETWORK_CHANGE.encode()).hexdigest(),kind='config',note=WALLET_NETWORK_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(FEED_LIQUIDITY_CHANGE.encode()).hexdigest(),kind='config',note=FEED_LIQUIDITY_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(CANDIDATE_LISTS_CHANGE.encode()).hexdigest(),kind='config',note=CANDIDATE_LISTS_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(WALLET_WATCH_CHANGE.encode()).hexdigest(),kind='config',note=WALLET_WATCH_CHANGE)
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(RESULTS_CHANGE.encode()).hexdigest(),kind='learning_rule',note=RESULTS_CHANGE)
    if cfg.get('ledger_epoch'):X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(JUDGE_GPT_CHANGE.encode()).hexdigest(),kind='config',note=JUDGE_GPT_CHANGE)
    if cfg.get('family')=='codex' and cfg.get('role')=='head':X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=hashlib.sha256(HEAD_GPT_CHANGE.encode()).hexdigest(),kind='config',note=HEAD_GPT_CHANGE)
    from sniper.office import self_sheet as SS
    X.record_change(office,epoch_id=epoch,at=time.time(),release_sha=release,content_hash=SS.definition_hash(),kind='ticketless_sheets',note=cfg['actor'])
    policy=L.OfficeBookPolicy(**{**book['policy'],'chains':('solana',)})
    cohort=eval_cohort(office)
    cfg['paper']=V.PaperService(epoch_id=epoch,book_id=cfg.get('ledger_epoch') or epoch,store_path=cfg['office_db'],ledger_path=cfg['ledger_db'],desk_path=cfg['desk_db'],desk_dir=cfg['desk_dir'],clock_file=cfg['clock_file'],policy=policy,
                          segment_of=lambda store:X.segment_id(store,epoch),pool_state_reader=RT.pool_reader(Path(cfg['office_dir'])),cohort_of=lambda:cohort,policy_sha256=X_identity(book['policy']),policy_file=cfg['book_policy'],ledger_epoch_id=cfg.get('ledger_epoch'))
    cfg['paper'].release_sha=release;cfg['paper'].funded_db=cfg.get('funded_db');cfg['paper'].results_ledgers=[p for p in (str(Path(cfg['office_db']).parent/'v2/explore-ledger/ledger.db'),str(Path(cfg['office_db']).parent/'v2/explore-gpt-ledger/ledger.db')) if Path(p).exists()]
    return cfg['paper']


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--force-swap',action='store_true');parser.add_argument('--stop-children',action='store_true',help='end this seat\'s wake and tool units, then exit; the service runs this on every stop')
    args=parser.parse_args()
    cfg=json.loads(Path(args.config).read_text())
    if args.stop_children:return stop_seat_units(cfg)
    set_funded_db(cfg)
    seat=prepare(cfg);proxies=start_proxies(cfg,seat)
    office=sqlite3.connect(cfg['office_db'],timeout=5,isolation_level=None);W.ensure_schema(office);E.ensure_schema(office)
    from sniper.office import v2_orders as VO
    VO.ensure_schema(office)   # idempotent: tables added after an epoch opened exist before any tool reads them
    quota=sqlite3.connect(cfg['quota_db'],timeout=5,isolation_level=None);Q.ensure_schema(quota);H.ensure_schema(quota)
    if cfg.get('desk_db') and cfg.get('ticket_wakes'):cfg['ticket_source']=ticket_source(cfg['desk_db'])
    state=Path(cfg['host_state']);state.parent.mkdir(parents=True,exist_ok=True)
    refresher=DenyRefresher(cfg,proxies);threading.Thread(target=refresher.run,daemon=True).start()
    if cfg.get('ledger_db'):cfg['paper']=build_paper(cfg,office)
    else:log_seat(cfg,office,Path('PUBLISHED_SHA').read_text().strip())
    seat_host=H.Seat(cfg=cfg,office_conn=office,quota_conn=quota,clock=time.time,extra_park=(lambda watch=policy_watch(cfg,office) if cfg.get('ledger_db') else (lambda:None):lambda:refresher.park_reason() or watch())())
    if args.force_swap:
        with H.lifecycle(cfg.get('bridge',B.BRIDGE)):seat_host.force_swap()
        return
    threading.Thread(target=poll_loop,args=(cfg,),daemon=True).start()
    if cfg.get('desk_watcher') and cfg.get('ledger_db') and cfg.get('research_env_file'):   # one watcher, one seat's host
        from sniper.office import chain_data as C,watch_alert as WA
        chain=C.ChainData(Path(cfg['office_db']).parent/'v2/chain.db',C.research_url_of(cfg['research_env_file']),page=C.operator_page(cfg['research_env_file']))
        threading.Thread(target=WA.loop,args=(chain,cfg['office_db'],cfg['ledger_db']),kwargs=dict(log=LOG),daemon=True).start()
    last=None;pruned=0
    while True:
        if time.time()-pruned>=RETENTION_EVERY_S:
            for line in R.run(cfg,office,quota,now=time.time(),current=seat_host.state.get('session_id')):LOG.warning('retention %s',line)
            pruned=time.time()
        try:outcome=seat_host.tick()
        except ValueError as exc:outcome='refused:'+str(exc)   # lifecycle_busy: a publish holds the lock
        if outcome!=last and outcome!='idle':LOG.warning('seat actor=%s outcome=%s',cfg['actor'],outcome)
        last=outcome
        time.sleep(POLL_S)


if __name__=='__main__':main()
