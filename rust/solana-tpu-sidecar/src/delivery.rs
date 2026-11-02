//! WireDelivery: the boundary between the request handler and the real Agave QUIC TPU client.
//!
//! `AgaveDelivery` delivers the EXACT signed wire bytes to the current + upcoming leaders over QUIC
//! via Agave's nonblocking `TpuClient` with `fanout_slots: 5` — Agave's range already includes the
//! current slot, so 5 covers the current plus four future slots. It never signs, mutates, re-prices,
//! or retries with new bytes: `try_send_wire_transaction` is invoked EXACTLY ONCE per request. `Ok`
//! (>=1 leader write succeeded) is ACCEPTED; an error after the single delivery call is AMBIGUOUS
//! (bytes may already be on the wire); a resolved zero-leader window or a frozen slot clock is
//! NOT_SENT. The RPC/WS URLs arrive only via the process environment. No private key, SQLite,
//! signing, or payload logging exists here. Unit tests use `FakeDelivery`.
//!
//! NO RPC ON THE SEND PATH (2026-09-26). The send used to call `get_slot` and `get_slot_leaders`
//! before every delivery, two blocking round trips to count leaders the TPU client already knows:
//! its `LeaderTpuService` tracks the current slot from a `slotsUpdatesSubscribe` socket and keeps
//! the leader schedule warm on its own task. The TPU leg won 1 of 40 funded first-accept races
//! behind those round trips. The count and the health answer now read that cached service.
//!
//! A FROZEN SLOT CLOCK REFUSES. Agave's slot watcher does not reconnect: when its socket drops the
//! estimated slot stops moving, and every later send goes to the leaders of a slot that has passed.
//! `SlotClock` notices the estimate not advancing for `STALE_AFTER`; the send then answers NOT_SENT
//! (the RPC legs carry the transaction) and the health answer turns false.
//!
//! A FROZEN CLOCK IS REPAIRED IN PROCESS (2026-09-27). The socket dropped about 3.6 times an hour on
//! the public endpoint, and each drop cost a full process restart: three failed health probes, a
//! backoff, a respawn and a cold QUIC cache. `supervise` now watches the clock and, once it freezes,
//! builds a new `TpuClient` (fresh slot socket, fresh leader schedule) on the SAME QUIC connection
//! cache and swaps it in. The old client's drop stops its dead watcher. Rebuilds back off per
//! `REBUILD_BACKOFF` so a socket that keeps dying cannot hammer the RPC; the owning process's
//! unhealthy restart stays as the backstop when a rebuild cannot finish in time.
//!
//! WARM CONNECTIONS (2026-09-27). Every send opened a new QUIC connection: of 13 sends with
//! counters, 26 connections were new and 5 reused, and the handshake cost p50 62 ms. Sends are
//! minutes apart while a leader holds four slots, so the leaders at send time were never
//! connected; and the default pool of four connections per leader borrowed one at random, often one
//! still handshaking. The pool is now ONE connection per leader, and `warm` handshakes the current
//! and upcoming leaders ahead of any send. Retained peers are checked every warm tick through the
//! pinned client's off-path refresh API; handshakes never hold the funded send's client mutex.
//!
//! A CLOSE NEVER OUTLIVES ITS CHECK (review S27-09, 2026-09-27). Closes used to be spawned after one
//! look at the in-flight count, and a late warm completion closed without even that; a spawned close
//! looked the connection up by address whenever it ran, so it could land on a leader that had left,
//! come back and reconnected. Every close now goes through `retire`, on the warmer's own task: it is
//! re-checked immediately before it runs (no send in flight, the address not in the current send
//! window) and awaited for at most `CLOSE_WAIT`; a close that fails the check or the wait is deferred
//! and planned again. Agave keeps the connection itself private, so a close cannot be bound to one
//! connection generation; running it at once, re-checked, is what stands in for that. If a send still
//! meets a closed connection, Agave's send makes one fresh connection and retries.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use solana_client::connection_cache::ConnectionCache;
use solana_client::nonblocking::rpc_client::RpcClient;
use solana_client::tpu_client::TpuClientConfig;
use solana_connection_cache::connection_cache::ConnectionCache as BackendConnectionCache;
use solana_connection_cache::connection_cache_stats::ConnectionCacheStats;
use solana_connection_cache::nonblocking::client_connection::ClientConnection;
use solana_quic_client::{QuicConfig, QuicConnectionManager, QuicPool};
use solana_quic_client::nonblocking::quic_client::ConnectionRefresh;
use solana_tpu_client::nonblocking::tpu_client::TpuClient;

pub const FANOUT_SLOTS: u64 = 5; // current slot + four upcoming slots
const LEADER_RUN_SLOTS: u64 = 4; // pinned Agave NUM_CONSECUTIVE_LEADER_SLOTS
/// How long the estimated slot may stand still before the cache counts as frozen. About four slots.
pub const STALE_AFTER: Duration = Duration::from_millis(1000);
/// How often the supervisor reads the slot clock. Also keeps the clock's last-advance stamp sharp.
pub const SUPERVISE_EVERY: Duration = Duration::from_millis(250);
/// Wait after the previous rebuild attempt, by attempt number within one frozen episode. The first
/// rebuild is immediate; a socket that dies again at once is retried at most twice a minute.
pub const REBUILD_BACKOFF: [Duration; 6] = [
    Duration::from_secs(0),
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(4),
    Duration::from_secs(8),
    Duration::from_secs(30),
];
/// A clock fresh for this long after the last attempt ends the episode and resets the backoff.
pub const EPISODE_RESET: Duration = Duration::from_secs(60);
/// Upper bound on one rebuild (four RPC reads plus the websocket connect). The owning process
/// restarts the sidecar after about four seconds unhealthy, so a slower rebuild loses to it anyway.
pub const REBUILD_TIMEOUT: Duration = Duration::from_secs(5);
/// Connections per leader address. One, reused by every send to that leader: QUIC multiplexes
/// streams, and a send carries one transaction per leader.
pub const POOL_SIZE: usize = 1;
/// Slots past the send fanout whose leaders are kept warm (about three more leaders). The
/// SNIPER_TPU_WARM_LOOKAHEAD_SLOTS environment value overrides it; 0 turns warming off.
pub const WARM_LOOKAHEAD_DEFAULT: u64 = 12;
/// How often the warmer re-reads the upcoming leaders.
pub const WARM_EVERY: Duration = Duration::from_millis(200);
/// An address the window stopped wanting this long ago is closed and forgotten. About one leader's
/// four slots, so the set is the leader just past, the current one and the lookahead: ~5-8 open.
pub const CLOSE_AFTER: Duration = Duration::from_secs(2);
/// Longest a retire waits for one close. A close only waits when something holds that leader's
/// client lock, which is a handshake in progress; the close is then deferred, never waited out.
pub const CLOSE_WAIT: Duration = Duration::from_millis(50);
/// A failed handshake waits this long before the next try, doubling per failure in a row.
pub const WARM_BACKOFF_BASE: Duration = Duration::from_secs(2);
pub const WARM_BACKOFF_MAX: Duration = Duration::from_secs(60);
/// One warm summary line per this period.
pub const WARM_SUMMARY_EVERY: Duration = Duration::from_secs(60);

/// The warm lookahead from its environment value: absent or unreadable is the default.
pub fn warm_lookahead(raw: Option<&str>) -> u64 {
    raw.and_then(|v| v.trim().parse().ok())
        .unwrap_or(WARM_LOOKAHEAD_DEFAULT)
}

/// What the warmer should do this tick.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct WarmPlan {
    pub warm: Vec<SocketAddr>,
    pub close: Vec<SocketAddr>,
}

/// The warmer's bookkeeping, pure: which addresses are wanted, open, in flight or backing off.
/// BOUNDED ON PURPOSE (Alpha, 2026-09-27): every open QUIC connection keep-alives once a second,
/// and a thousand of them from this IP would be the 09-22 tape-stall load and look abusive to
/// validators. An address is closed once the leader window has not wanted it for CLOSE_AFTER,
/// whoever connected it (the send path connects its own leaders too, and those must close as well).
#[derive(Debug, Default)]
pub struct WarmBook {
    seen: HashMap<SocketAddr, Instant>,
    open: HashSet<SocketAddr>,
    pending: HashSet<SocketAddr>,
    backoff: HashMap<SocketAddr, (u32, Instant)>,
}

impl WarmBook {
    pub fn plan(&mut self, wanted: &[SocketAddr], now: Instant) -> WarmPlan {
        for addr in wanted {
            self.seen.insert(*addr, now);
        }
        let close: Vec<SocketAddr> = self
            .seen
            .iter()
            .filter(|(_, at)| now.saturating_duration_since(**at) >= CLOSE_AFTER)
            .map(|(addr, _)| *addr)
            .collect();
        for addr in &close {
            self.seen.remove(addr);
            self.open.remove(addr);
            self.backoff.remove(addr);
        }
        let warm: Vec<SocketAddr> = wanted
            .iter()
            .filter(|addr| {
                !self.pending.contains(addr)
                    && self.backoff.get(addr).is_none_or(|(_, next)| now >= *next)
            })
            .copied()
            .collect();
        self.pending.extend(warm.iter().copied());
        WarmPlan { warm, close }
    }

    /// Record a finished warm call. True when the address was dropped while it was in flight and
    /// the fresh connection must be closed at once.
    pub fn warmed(&mut self, addr: SocketAddr, ok: bool, now: Instant) -> bool {
        self.pending.remove(&addr);
        if !self.seen.contains_key(&addr) {
            return ok;
        }
        if ok {
            self.open.insert(addr);
            self.backoff.remove(&addr);
        } else {
            self.open.remove(&addr);
            let failures = self.backoff.get(&addr).map_or(0, |(n, _)| *n) + 1;
            let wait = WARM_BACKOFF_BASE
                .saturating_mul(1 << (failures - 1).min(5))
                .min(WARM_BACKOFF_MAX);
            self.backoff.insert(addr, (failures, now + wait));
        }
        false
    }

    /// A close `retire` refused: track the address as open again from `now`, so it is planned for
    /// closing once the window has not wanted it for another CLOSE_AFTER.
    pub fn defer(&mut self, addr: SocketAddr, now: Instant) {
        self.seen.insert(addr, now);
        self.open.insert(addr);
    }

    pub fn open_count(&self) -> usize {
        self.open.len()
    }

    pub fn tracked_count(&self) -> usize {
        self.seen.len()
    }
}

/// Whether a planned close may run now: never while a send is in flight, and never on an address in
/// the current send window.
pub fn retirable(addr: &SocketAddr, inflight: u64, send_window: &[SocketAddr]) -> bool {
    inflight == 0 && !send_window.contains(addr)
}

/// Close each address now, on the caller's task, or hand it back to be deferred. Each one is
/// re-checked immediately before its close, and the close is awaited for at most CLOSE_WAIT, so no
/// close is left running after the call returns. Returns (closed, deferred).
pub async fn retire<W, C, Fut>(
    addrs: Vec<SocketAddr>,
    inflight: &AtomicU64,
    send_window: W,
    close: C,
) -> (u64, Vec<SocketAddr>)
where
    W: Fn() -> Vec<SocketAddr>,
    C: Fn(SocketAddr) -> Fut,
    Fut: Future<Output = ()>,
{
    let (mut closed, mut deferred) = (0u64, Vec::new());
    for addr in addrs {
        if !retirable(&addr, inflight.load(Ordering::SeqCst), &send_window()) {
            deferred.push(addr);
        } else if tokio::time::timeout(CLOSE_WAIT, close(addr)).await.is_ok() {
            closed += 1;
        } else {
            deferred.push(addr);
        }
    }
    (closed, deferred)
}

#[derive(Debug, PartialEq, Eq)]
pub enum SendOutcome {
    Accepted { leader_count: u64 },
    Ambiguous { leader_count: u64 },
    NoLeaders,
    StaleSlot,
    EntrySlotExpired,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct GuardSnapshot {
    pub version: u8,
    pub estimated_slot: u64,
    pub minimum_slot: u64,
    pub age_since_advance_ms: f64,
    pub clock_frozen: bool,
    pub behind_minimum: bool,
    pub available_leader_count: u64,
}

#[derive(Debug)]
pub struct SendDecision {
    pub outcome: SendOutcome,
    pub guard: Option<GuardSnapshot>,
}

impl From<SendOutcome> for SendDecision {
    fn from(outcome: SendOutcome) -> Self { Self { outcome, guard: None } }
}

pub trait WireDelivery: Send + Sync {
    /// Cached independent head, monotonically published across concurrent local requests.
    fn observe_slot(&self, _slot: Option<u64>) {}
    fn leader_slot(&self) -> Option<u64> {
        None
    }

    /// Last-known count of distinct upcoming leaders over the fanout window (cached, no RPC).
    fn leader_count(&self) -> u64;
    /// A no-send readiness answer: the cached slot clock is still moving.
    fn healthy(&self) -> bool;
    /// Deliver the exact wire bytes to upcoming leaders — the underlying TPU send runs EXACTLY once.
    /// `identity` is the verified first signature, for the per-send measurement line only.
    /// `entry_max_slot` is an inclusive request-local deadline; `None` retains the full window.
    fn send_once(&self, wire: Vec<u8>, identity: &str, entry_max_slot: Option<u64>) -> SendDecision;
}

/// The QUIC connection cache's counters that say whether a send opened new connections.
///
/// WHY (2026-09-27). The TPU leg answered first on 6 of 27 sends, and its wins took 35-61 ms
/// against leaders 11-24 ms away: that fits a fresh handshake per send rather than a write on a
/// warm connection, but nothing recorded which. Agave keeps these counts but exports none of them.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct QuicCounters {
    pub lookups: u64,
    pub new_connections: u64,
    pub reused: u64,
    pub connection_errors: u64,
    pub zero_rtt_accepts: u64,
    pub zero_rtt_rejects: u64,
    pub make_connection_ms: u64,
}

impl QuicCounters {
    pub fn read(stats: &ConnectionCacheStats) -> Self {
        let client = &stats.total_client_stats;
        let get = |c: &AtomicU64| c.load(Ordering::Relaxed);
        Self {
            lookups: get(&stats.cache_hits) + get(&stats.cache_misses),
            new_connections: get(&client.total_connections),
            reused: get(&client.connection_reuse),
            connection_errors: get(&client.connection_errors),
            zero_rtt_accepts: get(&client.zero_rtt_accepts),
            zero_rtt_rejects: get(&client.zero_rtt_rejects),
            make_connection_ms: get(&client.make_connection_ms),
        }
    }

    /// This send's counts from snapshots taken before and after it, and whether the cache zeroed
    /// its counters in between. Agave's cache zeroes them (`report`) at a connection lookup once
    /// 2 s have passed since the last report, and the first lookup of a send comes before any of
    /// that send's counts are added. So a lookup count below `before + leaders` means the zeroing
    /// happened and `after` alone is this send. Counts from a concurrent send, or from the cache's
    /// own background warm-up of a leader's second pooled connection, can still ride along.
    pub fn for_send(before: Self, after: Self, leaders: u64) -> (Self, bool) {
        if after.lookups < before.lookups.saturating_add(leaders) {
            return (after, true);
        }
        let d = |a: u64, b: u64| a.saturating_sub(b);
        (
            Self {
                lookups: d(after.lookups, before.lookups),
                new_connections: d(after.new_connections, before.new_connections),
                reused: d(after.reused, before.reused),
                connection_errors: d(after.connection_errors, before.connection_errors),
                zero_rtt_accepts: d(after.zero_rtt_accepts, before.zero_rtt_accepts),
                zero_rtt_rejects: d(after.zero_rtt_rejects, before.zero_rtt_rejects),
                make_connection_ms: d(after.make_connection_ms, before.make_connection_ms),
            },
            false,
        )
    }
}

/// What delivering one transaction to its leaders did, in leader order.
#[derive(Debug, PartialEq)]
pub struct Delivery {
    pub ok: bool,
    pub leader_ms: Vec<f64>,
    pub leader_ok: Vec<bool>,
}

/// Deliver `wire` to each of `leaders` exactly once, concurrently, and report per leader.
///
/// THE SAME DELIVERY AS AGAVE'S `try_send_wire_transaction` (2026-09-27): its loop gets a
/// connection per unique leader socket, sends the same bytes to each, waits for all of them, and
/// succeeds when at least one send did, failing ("No sends attempted") on an empty list. This does
/// exactly that, through `send_one`, so that each leader's time can be measured: the TPU leg lost
/// the first-accept race on every send that had to open both leaders' connections, and whether
/// the faster leader alone would have won cannot be seen from the aggregate.
pub async fn deliver_to_leaders<F, Fut>(
    leaders: Vec<SocketAddr>,
    wire: Vec<u8>,
    send_one: F,
) -> Delivery
where
    F: Fn(SocketAddr, Vec<u8>) -> Fut,
    Fut: std::future::Future<Output = bool> + Send + 'static,
{
    let mut tasks = tokio::task::JoinSet::new();
    for (i, addr) in leaders.iter().enumerate() {
        let send = send_one(*addr, wire.clone());
        tasks.spawn(async move {
            let started = Instant::now();
            let ok = send.await;
            (i, ok, started.elapsed().as_secs_f64() * 1000.0)
        });
    }
    let mut leader_ms = vec![0.0; leaders.len()];
    let mut leader_ok = vec![false; leaders.len()];
    while let Some(joined) = tasks.join_next().await {
        // A panicked send counts as failed, the same as an Err from the connection.
        if let Ok((i, ok, ms)) = joined {
            leader_ms[i] = ms;
            leader_ok[i] = ok;
        }
    }
    Delivery {
        ok: leader_ok.iter().any(|ok| *ok),
        leader_ms,
        leader_ok,
    }
}

/// What one delivery call did, for its measurement line.
pub struct SendRecord<'a> {
    pub outcome: &'a str,
    pub identity: &'a str,
    // The sidecar's estimated slot when the delivery started: landing slot minus this is how many
    // slots the transaction took to land, for comparing legs by landing rather than by ack.
    pub slot: u64,
    pub leaders: u64,
    pub counts: QuicCounters,
    pub zeroed: bool,
    pub send_ms: f64,
    pub leader_ms: &'a [f64],
    pub leader_ok: &'a [bool],
    pub inflight: u64,
    // Warm calls in flight when the send started: its counter deltas then overlap warm work.
    pub warm_inflight: u64,
    pub unix_ms: u128,
}

impl SendRecord<'_> {
    /// One URL-free line: no address, no payload, only counts and times.
    pub fn line(&self) -> String {
        let c = &self.counts;
        format!(
            "memecoin-tpu-sidecar send outcome={} tx={} slot={} leaders={} new_connections={} reused={} \
             connection_errors={} zero_rtt={}/{} make_connection_ms={} send_ms={:.1} \
             leader_ms={} leader_ok={} counters={} inflight={} warm_inflight={} unix_ms={}",
            self.outcome,
            self.identity,
            self.slot,
            self.leaders,
            c.new_connections,
            c.reused,
            c.connection_errors,
            c.zero_rtt_accepts,
            c.zero_rtt_rejects,
            c.make_connection_ms,
            self.send_ms,
            self.leader_ms
                .iter()
                .map(|ms| format!("{ms:.1}"))
                .collect::<Vec<_>>()
                .join(","),
            self.leader_ok
                .iter()
                .map(|ok| if *ok { "1" } else { "0" })
                .collect::<Vec<_>>()
                .join(","),
            if self.zeroed { "since_report" } else { "delta" },
            self.inflight,
            self.warm_inflight,
            self.unix_ms,
        )
    }
}

/// Tracks whether an estimated slot is still advancing. Pure bookkeeping; the caller supplies the
/// slot and the instant, so the staleness rule is testable without a clock or a network.
#[derive(Default)]
pub struct SlotClock {
    last_advance: Mutex<Option<(u64, Instant)>>,
}

impl SlotClock {
    /// Record `slot` observed at `now`. True while the slot has advanced within `stale_after`;
    /// false once it has stood still longer. The first observation is fresh.
    pub fn fresh(&self, slot: u64, now: Instant, stale_after: Duration) -> bool {
        self.fresh_with_age(slot, now, stale_after).0
    }

    pub fn fresh_with_age(&self, slot: u64, now: Instant, stale_after: Duration) -> (bool, Duration) {
        let mut last = self.last_advance.lock().expect("slot clock mutex poisoned");
        match *last {
            Some((seen, since)) if slot <= seen => {
                let age = now.saturating_duration_since(since);
                (age <= stale_after, age)
            }
            _ => {
                *last = Some((slot, now));
                (true, Duration::ZERO)
            }
        }
    }

    /// Time since the slot last advanced, as of `now`; zero before the first observation.
    pub fn since_advance(&self, now: Instant) -> Duration {
        let last = self.last_advance.lock().expect("slot clock mutex poisoned");
        last.map_or(Duration::ZERO, |(_, since)| {
            now.saturating_duration_since(since)
        })
    }
}

/// When to rebuild the TPU client after its slot clock froze. Pure bookkeeping: the caller supplies
/// freshness and the instant, so the backoff is testable without a clock or a network.
#[derive(Default)]
pub struct RebuildGate {
    attempts: usize,
    last_attempt: Option<Instant>,
}

impl RebuildGate {
    /// True when a rebuild should start now. A fresh clock never rebuilds; it ends the episode once
    /// it has stayed fresh `EPISODE_RESET` past the last attempt.
    pub fn due(&mut self, fresh: bool, now: Instant) -> bool {
        match self.last_attempt {
            _ if fresh => {
                if self
                    .last_attempt
                    .is_some_and(|at| now.saturating_duration_since(at) >= EPISODE_RESET)
                {
                    *self = Self::default();
                }
                false
            }
            None => true,
            Some(at) => {
                let wait = REBUILD_BACKOFF[self.attempts.min(REBUILD_BACKOFF.len() - 1)];
                now.saturating_duration_since(at) >= wait
            }
        }
    }

    /// Record a rebuild attempt starting at `now`, whatever its outcome.
    pub fn attempted(&mut self, now: Instant) {
        self.attempts += 1;
        self.last_attempt = Some(now);
    }
}

/// Keep the scheme and host of every URL in `text` and replace the rest (path, query, userinfo)
/// with `<redacted>`. Agave's RPC errors print the full URL, and a provider key rides in the query
/// (`?api-key=`) or the path, so no error text reaches the log without passing through here.
pub fn redact_urls(text: &str) -> String {
    let stop = |c: char| c.is_whitespace() || matches!(c, '"' | '\'' | ')' | '>' | ',');
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = rest.find("://") {
        let host_start = i + 3;
        let after = &rest[host_start..];
        let authority_len = after
            .find(|c: char| stop(c) || matches!(c, '/' | '?' | '#'))
            .unwrap_or(after.len());
        let authority = &after[..authority_len];
        let tail = &after[authority_len..];
        let tail_len = tail.find(stop).unwrap_or(tail.len());
        out.push_str(&rest[..host_start]);
        match authority.rsplit_once('@') {
            Some((_, host)) => {
                out.push_str("<redacted>@");
                out.push_str(host);
            }
            None => out.push_str(authority),
        }
        if tail_len > 0 {
            out.push_str("/<redacted>");
        }
        rest = &tail[tail_len..];
    }
    out.push_str(rest);
    out
}

/// The Agave records that say WHY a pooled QUIC connection was replaced or failed (review DR-05,
/// 2026-09-27). 5 of 8 sends met a dead pooled connection (`reused=2 connection_errors=1`), and the
/// counters cannot tell one this sidecar closed from one the leader dropped; Agave states the reason
/// only in an info-level log line (`last_error: ...`). Every other record, including the warm
/// handshake chatter, is dropped. Measurement only: nothing reads this line back.
const QUIC_REASON_PREFIXES: [&str; 4] =
    ["Made 0rtt connection", "Cannot make", "Error sending", "Ran into an error"];

/// One `quic` line for an Agave record, or None when it carries no reason. Socket and IP addresses
/// are replaced by `<leader>`: this sidecar never logs a validator address.
pub fn quic_reason_line(text: &str, unix_ms: u128) -> Option<String> {
    if !QUIC_REASON_PREFIXES.iter().any(|p| text.starts_with(p)) {
        return None;
    }
    Some(format!(
        "memecoin-tpu-sidecar quic {} unix_ms={unix_ms}",
        redact_addrs(&redact_urls(text))
    ))
}

/// Replace every run of address characters that parses as a socket or IP address, wherever it sits
/// (`to 1.2.3.4:8009,`, `InvalidRemoteAddress(1.2.3.4:8009)`, `[2001:db8::1]:8009`).
pub fn redact_addrs(text: &str) -> String {
    let addr_char = |c: char| c.is_ascii_hexdigit() || matches!(c, '.' | ':' | '[' | ']');
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find(addr_char) {
        out.push_str(&rest[..start]);
        let run = &rest[start..];
        let len = run.find(|c: char| !addr_char(c)).unwrap_or(run.len());
        let token = run[..len].trim_end_matches([':', '.']);
        if token.parse::<SocketAddr>().is_ok() || token.parse::<std::net::IpAddr>().is_ok() {
            out.push_str("<leader>");
            out.push_str(&run[token.len()..len]);
        } else {
            out.push_str(&run[..len]);
        }
        rest = &run[len..];
    }
    out.push_str(rest);
    out
}

/// The decision before any byte leaves: refuse on a frozen clock or an empty leader window,
/// otherwise the leader count to report. No I/O.
pub fn pre_send(fresh: bool, leader_sockets: usize) -> Result<u64, SendOutcome> {
    if !fresh {
        return Err(SendOutcome::StaleSlot);
    }
    if leader_sockets == 0 {
        return Err(SendOutcome::NoLeaders);
    }
    Ok(leader_sockets as u64)
}

fn select_leaders(
    entry_max_slot: Option<u64>,
    fanout_slots: u64,
    slot: impl Fn() -> u64,
    sockets: impl FnOnce(u64) -> Vec<SocketAddr>,
) -> (u64, Result<Vec<SocketAddr>, SendOutcome>) {
    let Some(max_slot) = entry_max_slot else {
        let sockets = sockets(fanout_slots);
        return (slot(), Ok(sockets));
    };
    let before = slot();
    if max_slot == 0 || before > max_slot {
        return (before, Err(SendOutcome::EntrySlotExpired));
    }
    // Agave samples every four slots; keep the next leader when the cap crosses a run boundary.
    let groups = (max_slot / LEADER_RUN_SLOTS).saturating_sub(before / LEADER_RUN_SLOTS);
    let fanout = fanout_slots.min(groups.saturating_mul(LEADER_RUN_SLOTS).saturating_add(1));
    // shortcut: private Agave cache start limits this to estimated lookahead; strict slots need an SDK accessor.
    let sockets = sockets(fanout);
    let after = slot();
    if after != before {
        let reason = if after > max_slot { SendOutcome::EntrySlotExpired } else { SendOutcome::StaleSlot };
        return (after, Err(reason));
    }
    (after, Ok(sockets))
}

type QuicTpuClient = TpuClient<QuicPool, QuicConnectionManager, QuicConfig>;
type QuicCache = BackendConnectionCache<QuicPool, QuicConnectionManager, QuicConfig>;

/// One TPU client and the clock that watches ITS slot estimate. Swapped whole on a rebuild, so a
/// new client never inherits the old one's frozen stamp.
struct Live {
    tpu: QuicTpuClient,
    clock: SlotClock,
}

impl Live {
    fn fresh(&self, now: Instant, minimum_slot: u64) -> bool {
        let slot = self.tpu.get_leader_tpu_service().estimated_current_slot();
        fresh_slot(&self.clock, slot, minimum_slot, now)
    }
}

/// Permit two slots of observation skew, never a moving clock twenty slots behind the signal.
pub fn slot_is_current(slot: u64, minimum_slot: u64) -> bool {
    slot.saturating_add(2) >= minimum_slot
}

fn fresh_slot(clock: &SlotClock, slot: u64, minimum_slot: u64, now: Instant) -> bool {
    clock.fresh(slot, now, STALE_AFTER) && slot_is_current(slot, minimum_slot)
}

pub struct AgaveDelivery {
    minimum_slot: AtomicU64,
    // The runtime the TPU client's own tasks run on; `send_once` runs on the blocking pool and
    // drives the async send to completion on it.
    handle: tokio::runtime::Handle,
    // A send clones the Arc and releases the lock before any I/O; a rebuild swaps it. A send that
    // holds the old client finishes on it, and the old client stops when the last holder drops it.
    live: RwLock<Arc<Live>>,
    fanout_slots: u64,
    // Held for rebuilds only. Never logged: they can carry a provider key.
    rpc_url: String,
    ws_url: String,
    cache: Arc<QuicCache>,
    // The cache's shared counters, for the per-send line; sends in flight, to flag overlap.
    stats: Arc<ConnectionCacheStats>,
    inflight: AtomicU64,
    warm_inflight: Arc<AtomicU64>,
}

impl AgaveDelivery {
    /// Build the QUIC TPU client from the RPC/WS URLs. The default QUIC connection cache's client
    /// certificate is an EPHEMERAL networking keypair — never a wallet key. Called on the runtime.
    pub async fn new(rpc_url: &str, ws_url: &str, fanout_slots: u64) -> anyhow::Result<Self> {
        let cache = match ConnectionCache::new_quic("memecoin-tpu-sidecar", POOL_SIZE) {
            ConnectionCache::Quic(cache) => cache,
            ConnectionCache::Udp(_) => {
                anyhow::bail!("tpu client: default connection cache is not QUIC")
            }
        };
        let live = connect(rpc_url, ws_url, fanout_slots, cache.clone()).await?;
        // ponytail: the cache hands out its shared counters only through a connection object, so
        // one is made for an unroutable address. It is a lazy object: nothing connects or sends
        // until a send, and nothing ever sends to it. A public accessor upstream would replace it.
        let stats = cache
            .get_nonblocking_connection(&SocketAddr::from(([0, 0, 0, 0], 0)))
            .connection_stats();
        Ok(Self {
            handle: tokio::runtime::Handle::current(),
            live: RwLock::new(Arc::new(live)),
            fanout_slots,
            rpc_url: rpc_url.to_string(),
            ws_url: ws_url.to_string(),
            cache,
            stats,
            inflight: AtomicU64::new(0),
            minimum_slot: AtomicU64::new(0),
            warm_inflight: Arc::new(AtomicU64::new(0)),
        })
    }

    fn current(&self) -> Arc<Live> {
        self.live.read().unwrap_or_else(|p| p.into_inner()).clone()
    }

    fn fresh(&self) -> bool {
        self.current().fresh(Instant::now(), self.minimum_slot.load(Ordering::Relaxed))
    }

    fn leader_sockets(&self, live: &Live) -> usize {
        live.tpu
            .get_leader_tpu_service()
            .unique_leader_tpu_sockets(self.fanout_slots)
            .len()
    }

    /// Watch the slot clock and rebuild the client when it freezes. Runs for the process lifetime
    /// on the runtime, off every request path. One line per attempt, URL-free.
    pub async fn supervise(self: Arc<Self>) {
        let mut gate = RebuildGate::default();
        let mut tick = tokio::time::interval(SUPERVISE_EVERY);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            let now = Instant::now();
            let frozen_ms = {
                let live = self.current();
                if !gate.due(live.fresh(now, self.minimum_slot.load(Ordering::Relaxed)), now) {
                    continue;
                }
                live.clock.since_advance(now).as_millis()
            };
            gate.attempted(now);
            let built = tokio::time::timeout(
                REBUILD_TIMEOUT,
                connect(
                    &self.rpc_url,
                    &self.ws_url,
                    self.fanout_slots,
                    self.cache.clone(),
                ),
            )
            .await;
            let build_ms = now.elapsed().as_millis();
            let error = match built {
                Ok(Ok(live)) => {
                    *self.live.write().unwrap_or_else(|p| p.into_inner()) = Arc::new(live);
                    None
                }
                Ok(Err(e)) => Some(redact_urls(&e.to_string())),
                Err(_) => Some("timeout".to_string()),
            };
            let unix_ms = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |d| d.as_millis());
            println!(
                "memecoin-tpu-sidecar slot_watch_rebuild ok={} attempt={} frozen_ms={frozen_ms} \
                 build_ms={build_ms} unix_ms={unix_ms}{}",
                error.is_none(),
                gate.attempts,
                error.map_or(String::new(), |e| format!(" error={e}")),
            );
        }
    }
}

impl AgaveDelivery {
    /// Keep the current and upcoming leaders connected, off every send path, and close the rest.
    /// Runs for the process lifetime on the runtime. A tick plans nothing while a send is in flight,
    /// and every close goes through `retire`, so nothing is closed under a send.
    pub async fn warm(self: Arc<Self>, lookahead: u64) {
        if lookahead == 0 {
            return;
        }
        let (done_tx, mut done_rx) = tokio::sync::mpsc::unbounded_channel::<(
            SocketAddr, Result<ConnectionRefresh, ()>)>();
        let mut book = WarmBook::default();
        let (mut ok, mut failed, mut closed) = (0u64, 0u64, 0u64);
        let mut summary_at = Instant::now() + WARM_SUMMARY_EVERY;
        let mut tick = tokio::time::interval(WARM_EVERY);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            let now = Instant::now();
            let live = self.current();
            let cache = live.tpu.get_connection_cache().clone();
            let mut retiring = Vec::new();
            while let Ok((addr, result)) = done_rx.try_recv() {
                match result {
                    Ok(ConnectionRefresh::Busy) => {
                        book.pending.remove(&addr);
                        continue;
                    }
                    Ok(ConnectionRefresh::Refreshed) => ok += 1,
                    Ok(ConnectionRefresh::Ready) => (),
                    Err(()) => failed += 1,
                }
                if book.warmed(addr, result.is_ok(), now) {
                    retiring.push(addr); // connected after its leader left
                }
            }
            if now >= summary_at {
                summary_at = now + WARM_SUMMARY_EVERY;
                let unix_ms = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_or(0, |d| d.as_millis());
                println!(
                    "memecoin-tpu-sidecar warm lookahead_slots={lookahead} open={} tracked={} \
                     handshakes={ok} failures={failed} closed={closed} unix_ms={unix_ms}",
                    book.open_count(),
                    book.tracked_count(),
                );
                (ok, failed, closed) = (0, 0, 0);
            }
            if self.inflight.load(Ordering::SeqCst) > 0 || !live.fresh(now, self.minimum_slot.load(Ordering::Relaxed)) {
                for addr in retiring {
                    book.defer(addr, now);
                }
                continue;
            }
            let wanted = live
                .tpu
                .get_leader_tpu_service()
                .unique_leader_tpu_sockets(self.fanout_slots + lookahead);
            let plan = book.plan(&wanted, now);
            retiring.extend(plan.close);
            let (n, deferred) = retire(
                retiring,
                &self.inflight,
                || {
                    live.tpu
                        .get_leader_tpu_service()
                        .unique_leader_tpu_sockets(live.tpu.get_fanout_slots())
                },
                |addr| {
                    let cache = cache.clone();
                    async move { cache.get_nonblocking_connection(&addr).client.close().await }
                },
            )
            .await;
            closed += n;
            for addr in deferred {
                book.defer(addr, now);
            }
            for addr in plan.warm {
                let (cache, done_tx) = (cache.clone(), done_tx.clone());
                let warm_inflight = self.warm_inflight.clone();
                warm_inflight.fetch_add(1, Ordering::Relaxed);
                tokio::spawn(async move {
                    let result = cache.get_nonblocking_connection(&addr)
                        .refresh_connection().await.map_err(|_| ());
                    warm_inflight.fetch_sub(1, Ordering::Relaxed);
                    let _ = done_tx.send((addr, result));
                });
            }
        }
    }
}

/// One TPU client with its own slot socket and leader schedule, on the shared QUIC cache.
async fn connect(
    rpc_url: &str,
    ws_url: &str,
    fanout_slots: u64,
    cache: Arc<QuicCache>,
) -> anyhow::Result<Live> {
    let rpc = Arc::new(RpcClient::new(rpc_url.to_string()));
    let tpu =
        TpuClient::new_with_connection_cache(rpc, ws_url, TpuClientConfig { fanout_slots }, cache)
            .await
            .map_err(|e| anyhow::anyhow!("tpu client: {e}"))?;
    Ok(Live {
        tpu,
        clock: SlotClock::default(),
    })
}

impl WireDelivery for AgaveDelivery {
    fn observe_slot(&self, slot: Option<u64>) {
        if let Some(slot) = slot {
            self.minimum_slot.fetch_max(slot, Ordering::Relaxed);
        }
    }

    fn leader_slot(&self) -> Option<u64> {
        Some(self.current().tpu.get_leader_tpu_service().estimated_current_slot())
    }

    fn leader_count(&self) -> u64 {
        let live = self.current();
        pre_send(live.fresh(Instant::now(), self.minimum_slot.load(Ordering::Relaxed)), self.leader_sockets(&live)).unwrap_or(0)
    }

    fn healthy(&self) -> bool {
        self.fresh()
    }

    fn send_once(&self, wire: Vec<u8>, identity: &str, entry_max_slot: Option<u64>) -> SendDecision {
        // One client snapshot for the check AND the send, so a rebuild in between cannot split them;
        // one leader list too: the count checked is the list delivered to.
        let live = self.current();
        let service = live.tpu.get_leader_tpu_service();
        let (slot, selection) = select_leaders(
            entry_max_slot, live.tpu.get_fanout_slots(),
            || service.estimated_current_slot(),
            |fanout| service.unique_leader_tpu_sockets(fanout),
        );
        let minimum_slot = self.minimum_slot.load(Ordering::Relaxed);
        let (fresh, age) = live.clock.fresh_with_age(slot, Instant::now(), STALE_AFTER);
        let behind_minimum = !slot_is_current(slot, minimum_slot);
        let guard = Some(GuardSnapshot {
            version: 1, estimated_slot: slot, minimum_slot,
            age_since_advance_ms: age.as_secs_f64() * 1000.0,
            clock_frozen: !fresh, behind_minimum,
            available_leader_count: selection.as_ref().map_or(0, |sockets| sockets.len() as u64),
        });
        let sockets = match selection {
            Ok(sockets) => sockets,
            Err(outcome) => return SendDecision { outcome, guard },
        };
        let leaders = match pre_send(fresh && !behind_minimum, sockets.len()) {
            Ok(n) => n,
            Err(outcome) => return SendDecision { outcome, guard },
        };
        // SeqCst: `retire` reads this count immediately before each close.
        let inflight = self.inflight.fetch_add(1, Ordering::SeqCst) + 1;
        let warm_inflight = self.warm_inflight.load(Ordering::Relaxed);
        let before = QuicCounters::read(&self.stats);
        let started = Instant::now();
        // EXACTLY ONE delivery per leader. Ok -> >=1 leader accepted; Err -> a write may be on
        // the wire. The same calls try_send_wire_transaction makes, timed per leader.
        let cache = live.tpu.get_connection_cache().clone();
        let delivery =
            self.handle
                .block_on(deliver_to_leaders(sockets, wire, move |addr, wire| {
                    let cache = cache.clone();
                    async move {
                        cache
                            .get_nonblocking_connection(&addr)
                            .send_data(&wire)
                            .await
                            .is_ok()
                    }
                }));
        let send_ms = started.elapsed().as_secs_f64() * 1000.0;
        self.inflight.fetch_sub(1, Ordering::SeqCst);
        // Measurement only, after the bytes are out: the line never changes the outcome.
        let (counts, zeroed) =
            QuicCounters::for_send(before, QuicCounters::read(&self.stats), leaders);
        let unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_millis());
        let record = SendRecord {
            outcome: if delivery.ok { "accepted" } else { "ambiguous" },
            identity,
            slot,
            leaders,
            counts,
            zeroed,
            send_ms,
            leader_ms: &delivery.leader_ms,
            leader_ok: &delivery.leader_ok,
            inflight,
            warm_inflight,
            unix_ms,
        };
        println!("{}", record.line());
        let outcome = if delivery.ok {
            SendOutcome::Accepted {
                leader_count: leaders,
            }
        } else {
            SendOutcome::Ambiguous {
                leader_count: leaders,
            }
        };
        SendDecision { outcome, guard }
    }
}

#[cfg(test)]
pub struct FakeDelivery {
    pub slot: Option<u64>,
    pub minimum_slot: AtomicU64,
    pub leaders: u64,
    pub healthy: bool,
    pub outcome: SendOutcome,
    pub guard: Option<GuardSnapshot>,
    pub sends: std::sync::atomic::AtomicU64,
}

#[cfg(test)]
impl FakeDelivery {
    pub fn new(leaders: u64, outcome: SendOutcome) -> Self {
        Self {
            slot: None,
            minimum_slot: AtomicU64::new(0),
            leaders,
            healthy: true,
            outcome,
            guard: None,
            sends: std::sync::atomic::AtomicU64::new(0),
        }
    }
}

#[cfg(test)]
impl WireDelivery for FakeDelivery {
    fn observe_slot(&self, slot: Option<u64>) {
        if let Some(slot) = slot {
            self.minimum_slot.fetch_max(slot, Ordering::Relaxed);
        }
    }
    fn leader_slot(&self) -> Option<u64> {
        self.slot
    }

    fn leader_count(&self) -> u64 {
        self.leaders
    }
    fn healthy(&self) -> bool {
        self.healthy && self.slot.map_or(true, |slot| slot_is_current(slot, self.minimum_slot.load(Ordering::Relaxed)))
    }
    fn send_once(&self, _wire: Vec<u8>, _identity: &str, entry_max_slot: Option<u64>) -> SendDecision {
        if entry_max_slot.is_some_and(|max| max == 0 || self.slot.is_some_and(|slot| slot > max)) {
            return SendDecision { outcome: SendOutcome::EntrySlotExpired, guard: self.guard.clone() };
        }
        if !self.healthy() {
            return SendDecision { outcome: SendOutcome::StaleSlot, guard: self.guard.clone() };
        }
        self.sends.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let outcome = match self.outcome {
            SendOutcome::Accepted { leader_count } => SendOutcome::Accepted { leader_count },
            SendOutcome::Ambiguous { leader_count } => SendOutcome::Ambiguous { leader_count },
            SendOutcome::NoLeaders => SendOutcome::NoLeaders,
            SendOutcome::StaleSlot => SendOutcome::StaleSlot,
            SendOutcome::EntrySlotExpired => SendOutcome::EntrySlotExpired,
        };
        SendDecision { outcome, guard: self.guard.clone() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const WINDOW: Duration = Duration::from_millis(1000);

    #[test]
    fn entry_cap_bounds_leader_fanout_and_is_inclusive() {
        for (slot, cap, expected) in [(100, 101, 1), (101, 101, 1), (100, 200, FANOUT_SLOTS), (u64::MAX, u64::MAX, 1), (u64::MAX - 4, u64::MAX, 5)] {
            let selected = select_leaders(Some(cap), FANOUT_SLOTS, || slot, |fanout| {
                assert_eq!(fanout, expected);
                vec![leader(1)]
            });
            assert_eq!(selected, (slot, Ok(vec![leader(1)])));
        }
    }

    #[test]
    fn entry_cap_preserves_the_next_leader_at_a_four_slot_boundary() {
        for (slot, cap, groups) in [(103, 104, vec![25, 26]), (102, 103, vec![25]), (104, 105, vec![26])] {
            let selected = select_leaders(Some(cap), FANOUT_SLOTS, || slot, |fanout| {
                // Match pinned Agave get_leader_sockets: sample every four slots from the head.
                (slot..slot + fanout).step_by(4).map(|sample| leader((sample / 4) as u16)).collect()
            });
            assert_eq!(selected, (slot, Ok(groups.into_iter().map(leader).collect())));
        }
    }

    #[test]
    fn expired_entry_cap_does_not_select_or_send() {
        for cap in [0, 101] {
            let selected = select_leaders(Some(cap), FANOUT_SLOTS, || 102, |_| {
                panic!("expired entry selected a delivery target");
            });
            assert_eq!(selected.1, Err(SendOutcome::EntrySlotExpired));
        }
    }

    #[test]
    fn head_advance_during_entry_selection_refuses_the_window() {
        for (after, reason) in [(99, SendOutcome::StaleSlot), (101, SendOutcome::StaleSlot), (102, SendOutcome::EntrySlotExpired)] {
            let head = AtomicU64::new(100);
            let selected = select_leaders(Some(101), FANOUT_SLOTS, || head.load(Ordering::Relaxed), |_| {
                head.store(after, Ordering::Relaxed);
                vec![leader(1), leader(2)]
            });
            assert_eq!(selected.0, after);
            assert_eq!(selected.1, Err(reason));
        }
    }

    #[test]
    fn uncapped_delivery_preserves_the_full_leader_window() {
        let selected = select_leaders(None, FANOUT_SLOTS, || 102, |fanout| {
            assert_eq!(fanout, FANOUT_SLOTS);
            vec![leader(1), leader(2)]
        });
        assert_eq!(selected, (102, Ok(vec![leader(1), leader(2)])));
    }

    #[test]
    fn guard_age_uses_the_same_original_advance() {
        let clock = SlotClock::default();
        let start = Instant::now();
        let limit = Duration::from_millis(1000);
        assert_eq!(clock.fresh_with_age(100, start, limit), (true, Duration::ZERO));
        assert_eq!(clock.fresh_with_age(100, start + limit, limit), (true, limit));
        let later = limit + Duration::from_nanos(1);
        assert_eq!(clock.fresh_with_age(99, start + later, limit), (false, later));
        assert_eq!(clock.fresh_with_age(101, start + later, limit), (true, Duration::ZERO));
        assert!(slot_is_current(100, 102));
        assert!(!slot_is_current(100, 103));
        assert!(slot_is_current(u64::MAX, u64::MAX));
    }

    #[test]
    fn moving_stale_clock_does_not_pass_the_real_live_freshness_predicate() {
        let clock = SlotClock::default();
        let start = Instant::now();
        assert!(fresh_slot(&clock, 453640566, 0, start));
        let later = start + Duration::from_millis(250);
        // Movement alone passes the old test, but it still names past leaders.
        assert!(clock.fresh(453640567, later, STALE_AFTER));
        assert!(!fresh_slot(&clock, 453640567, 453640588, later));
        assert!(fresh_slot(&clock, 453640588, 453640588, later));
    }

    fn leader(port: u16) -> SocketAddr {
        SocketAddr::from(([10, 0, 0, 1], port))
    }

    // Every send opened a new connection on 2026-09-27 (26 new, 5 reused over 13 sends): the
    // Retained peers are rechecked; the SDK skips handshakes while the connection stays open.
    #[test]
    fn a_wanted_leader_is_rechecked_and_kept_open() {
        let t0 = Instant::now();
        let mut book = WarmBook::default();
        let plan = book.plan(&[leader(1), leader(2)], t0);
        assert_eq!(plan.warm, vec![leader(1), leader(2)]);
        assert!(book.plan(&[leader(1), leader(2)], t0).warm.is_empty()); // in flight
        assert!(!book.warmed(leader(1), true, t0));
        assert!(!book.warmed(leader(2), true, t0));
        let later = t0 + Duration::from_millis(1500);
        assert_eq!(
            book.plan(&[leader(1), leader(2)], later),
            WarmPlan { warm: vec![leader(1), leader(2)], close: vec![] }
        );
        assert_eq!(book.open_count(), 2);
    }

    // Bounded: a leader the window stopped wanting is closed after CLOSE_AFTER, so walking the
    // whole schedule never leaves more open than the window plus the leader just past.
    #[test]
    fn the_open_set_follows_the_window_and_stays_small() {
        let mut book = WarmBook::default();
        let mut now = Instant::now();
        for step in 0..200u16 {
            let window: Vec<SocketAddr> = (step..step + 5).map(leader).collect();
            let plan = book.plan(&window, now);
            for addr in plan.warm {
                book.warmed(addr, true, now);
            }
            assert!(
                book.open_count() <= 5 + 2,
                "open {} at step {step}",
                book.open_count()
            );
            now += Duration::from_millis(1200); // one leader per ~1.2 s
        }
        let plan = book.plan(&[leader(500)], now + CLOSE_AFTER);
        assert!(plan.close.len() >= 5 && book.open_count() == 0);
    }

    #[test]
    fn a_failed_handshake_backs_off_and_doubles() {
        let t0 = Instant::now();
        let mut book = WarmBook::default();
        book.plan(&[leader(1)], t0);
        book.warmed(leader(1), false, t0);
        assert!(book
            .plan(&[leader(1)], t0 + Duration::from_millis(1900))
            .warm
            .is_empty());
        let t1 = t0 + WARM_BACKOFF_BASE;
        assert_eq!(book.plan(&[leader(1)], t1).warm, vec![leader(1)]);
        book.warmed(leader(1), false, t1);
        let t2 = t1 + WARM_BACKOFF_BASE * 2;
        assert!(book
            .plan(&[leader(1)], t2 - Duration::from_millis(1))
            .warm
            .is_empty());
        assert_eq!(book.plan(&[leader(1)], t2).warm, vec![leader(1)]);
        book.warmed(leader(1), true, t2); // success resets
        assert_eq!(book.open_count(), 1);
    }

    #[test]
    fn a_warm_that_lands_after_its_leader_left_is_closed_at_once() {
        let t0 = Instant::now();
        let mut book = WarmBook::default();
        book.plan(&[leader(1)], t0);
        let gone = book.plan(&[leader(2)], t0 + CLOSE_AFTER);
        assert_eq!(gone.close, vec![leader(1)]);
        assert!(book.warmed(leader(1), true, t0 + CLOSE_AFTER));
        assert_eq!(book.open_count(), 0);
    }

    #[test]
    fn stale_warm_retained_peers_are_rechecked_without_duplicate_inflight_work() {
        let now = Instant::now();
        let mut book = WarmBook::default();
        assert_eq!(book.plan(&[leader(1)], now).warm, vec![leader(1)]);
        assert!(!book.warmed(leader(1), true, now));
        assert_eq!(book.plan(&[leader(1)], now + WARM_EVERY).warm, vec![leader(1)],
                   "a retained warm flag cannot prove a cached connection is still open");
        assert!(book.plan(&[leader(1)], now + WARM_EVERY).warm.is_empty());
    }

    #[test]
    fn stale_warm_failed_refresh_clears_the_last_open_observation() {
        let now = Instant::now();
        let mut book = WarmBook::default();
        book.plan(&[leader(1)], now);
        book.warmed(leader(1), true, now);
        book.plan(&[leader(1)], now + WARM_EVERY);
        book.warmed(leader(1), false, now + WARM_EVERY);
        assert_eq!(book.open_count(), 0, "failed refresh must not retain an open observation");
    }

    // Review S27-09: a close planned, then a send starting before the close runs. The close must
    // wait out the send, not land under it, and still happen once the send is done.
    #[tokio::test]
    async fn a_send_that_starts_after_a_close_is_planned_keeps_its_connection() {
        let t0 = Instant::now();
        let mut book = WarmBook::default();
        book.plan(&[leader(1)], t0);
        book.warmed(leader(1), true, t0);
        let plan = book.plan(&[], t0 + CLOSE_AFTER);
        assert_eq!(plan.close, vec![leader(1)]);
        let inflight = Arc::new(AtomicU64::new(0));
        let barrier = Arc::new(tokio::sync::Barrier::new(2));
        let send = {
            let (inflight, barrier) = (inflight.clone(), barrier.clone());
            tokio::spawn(async move {
                inflight.fetch_add(1, Ordering::SeqCst);
                barrier.wait().await; // in flight
                barrier.wait().await; // done
                inflight.fetch_sub(1, Ordering::SeqCst);
            })
        };
        barrier.wait().await;
        let closed = Arc::new(Mutex::new(Vec::new()));
        let closer = |addr| {
            let closed = closed.clone();
            async move { closed.lock().unwrap().push(addr) }
        };
        let (n, deferred) = retire(plan.close, &inflight, Vec::new, closer).await;
        assert_eq!((n, deferred.clone()), (0, vec![leader(1)]));
        let later = t0 + CLOSE_AFTER;
        for addr in deferred {
            book.defer(addr, later);
        }
        barrier.wait().await;
        send.await.unwrap();
        assert!(book.plan(&[], later + CLOSE_AFTER / 2).close.is_empty());
        let again = book.plan(&[], later + CLOSE_AFTER);
        let (n, deferred) = retire(again.close, &inflight, Vec::new, closer).await;
        assert_eq!((n, deferred), (1, vec![]));
        assert_eq!(*closed.lock().unwrap(), vec![leader(1)]);
    }

    // The leader left, so its close was planned, and came back before the close ran: it is in the
    // send window again, so it stays open for as long as it is wanted.
    #[tokio::test]
    async fn a_leader_that_came_back_before_its_close_ran_is_never_closed() {
        let t0 = Instant::now();
        let mut book = WarmBook::default();
        book.plan(&[leader(1)], t0);
        book.warmed(leader(1), true, t0);
        let plan = book.plan(&[], t0 + CLOSE_AFTER);
        let closed = Arc::new(Mutex::new(Vec::new()));
        let closer = |addr| {
            let closed = closed.clone();
            async move { closed.lock().unwrap().push(addr) }
        };
        let idle = AtomicU64::new(0);
        let (n, deferred) = retire(plan.close, &idle, || vec![leader(1)], closer).await;
        assert_eq!((n, deferred.clone()), (0, vec![leader(1)]));
        for addr in deferred {
            book.defer(addr, t0 + CLOSE_AFTER);
        }
        for tick in 1..=20 {
            let now = t0 + CLOSE_AFTER + WARM_EVERY * tick;
            let plan = book.plan(&[leader(1)], now);
            assert!(plan.close.is_empty());
            for addr in plan.warm {
                book.warmed(addr, true, now);
            }
        }
        assert!(closed.lock().unwrap().is_empty());
        assert_eq!(book.open_count(), 1);
    }

    // A close that cannot get the leader's client (a handshake holds it) is deferred, and the
    // warmer is back within about CLOSE_WAIT per address instead of hanging on it.
    #[tokio::test]
    async fn a_close_that_hangs_is_deferred_and_the_warmer_moves_on() {
        let started = Instant::now();
        let idle = AtomicU64::new(0);
        let (n, deferred) = retire(vec![leader(1), leader(2)], &idle, Vec::new, |_| {
            std::future::pending::<()>()
        })
        .await;
        assert_eq!((n, deferred), (0, vec![leader(1), leader(2)]));
        assert!(started.elapsed() < CLOSE_WAIT * 6);
    }

    #[test]
    fn a_close_runs_only_with_no_send_in_flight_and_outside_the_send_window() {
        assert!(retirable(&leader(1), 0, &[leader(2)]));
        assert!(!retirable(&leader(1), 1, &[leader(2)]));
        assert!(!retirable(&leader(1), 0, &[leader(1)]));
    }

    #[test]
    fn the_lookahead_defaults_on_and_zero_turns_it_off() {
        assert_eq!(warm_lookahead(None), WARM_LOOKAHEAD_DEFAULT);
        assert_eq!(warm_lookahead(Some("not a number")), WARM_LOOKAHEAD_DEFAULT);
        assert_eq!(warm_lookahead(Some(" 20 ")), 20);
        assert_eq!(warm_lookahead(Some("0")), 0);
    }

    #[test]
    fn one_connection_per_leader() {
        assert_eq!(POOL_SIZE, 1);
    }

    #[test]
    fn an_advancing_slot_is_fresh() {
        let clock = SlotClock::default();
        let t0 = Instant::now();
        assert!(clock.fresh(100, t0, WINDOW));
        assert!(clock.fresh(101, t0 + Duration::from_millis(400), WINDOW));
        assert!(clock.fresh(105, t0 + Duration::from_secs(5), WINDOW)); // advanced across a gap
    }

    #[test]
    fn a_slot_standing_still_past_the_window_is_stale() {
        let clock = SlotClock::default();
        let t0 = Instant::now();
        assert!(clock.fresh(100, t0, WINDOW));
        assert!(clock.fresh(100, t0 + Duration::from_millis(900), WINDOW)); // inside the window
        assert!(!clock.fresh(100, t0 + Duration::from_millis(1100), WINDOW)); // frozen
        // A frozen clock stays frozen: repeated reads of the same slot must not reset the stamp.
        assert!(!clock.fresh(100, t0 + Duration::from_millis(1300), WINDOW));
    }

    #[test]
    fn a_frozen_clock_recovers_when_the_slot_moves_again() {
        let clock = SlotClock::default();
        let t0 = Instant::now();
        assert!(clock.fresh(100, t0, WINDOW));
        assert!(!clock.fresh(100, t0 + Duration::from_secs(3), WINDOW));
        assert!(clock.fresh(112, t0 + Duration::from_millis(3100), WINDOW));
    }

    #[test]
    fn a_slot_going_backwards_is_not_an_advance() {
        let clock = SlotClock::default();
        let t0 = Instant::now();
        assert!(clock.fresh(100, t0, WINDOW));
        assert!(!clock.fresh(99, t0 + Duration::from_millis(1100), WINDOW));
    }

    #[test]
    fn since_advance_counts_from_the_last_move() {
        let clock = SlotClock::default();
        let t0 = Instant::now();
        assert_eq!(clock.since_advance(t0), Duration::ZERO);
        clock.fresh(100, t0, WINDOW);
        clock.fresh(100, t0 + Duration::from_millis(700), WINDOW); // standing still: no new stamp
        assert_eq!(
            clock.since_advance(t0 + Duration::from_millis(1500)),
            Duration::from_millis(1500)
        );
    }

    // The slot socket died about 3.6 times an hour on 2026-09-26 and each death cost a full process
    // restart. A frozen clock must start a rebuild on the first supervisor read, not wait for the
    // owner's three failed probes.
    #[test]
    fn a_frozen_clock_rebuilds_at_once_and_a_fresh_one_never_does() {
        let mut gate = RebuildGate::default();
        let t0 = Instant::now();
        assert!(!gate.due(true, t0));
        assert!(gate.due(false, t0));
    }

    #[test]
    fn a_socket_that_keeps_dying_backs_off_to_twice_a_minute() {
        let mut gate = RebuildGate::default();
        let mut at = Instant::now();
        assert!(gate.due(false, at));
        gate.attempted(at);
        for wait in [1, 2, 4, 8, 30, 30, 30] {
            let wait = Duration::from_secs(wait);
            assert!(
                !gate.due(false, at + wait - Duration::from_millis(1)),
                "too early at {wait:?}"
            );
            // The new client came up fresh and froze again inside the episode: still backing off.
            assert!(!gate.due(true, at + wait / 2));
            at += wait;
            assert!(gate.due(false, at), "not due after {wait:?}");
            gate.attempted(at);
        }
    }

    #[test]
    fn a_minute_fresh_after_the_last_attempt_ends_the_episode() {
        let mut gate = RebuildGate::default();
        let t0 = Instant::now();
        gate.attempted(t0);
        gate.attempted(t0 + Duration::from_secs(1));
        gate.attempted(t0 + Duration::from_secs(3));
        let later = t0 + Duration::from_secs(3) + EPISODE_RESET;
        assert!(!gate.due(true, later));
        assert!(gate.due(false, later + Duration::from_millis(1))); // next death: immediate again
    }

    #[test]
    fn urls_keep_scheme_and_host_and_lose_every_key() {
        let cases = [
            (
                r#"kind: Status(429, None), url: "https://mainnet.helius-rpc.com/?api-key=0123abcd" })"#,
                r#"kind: Status(429, None), url: "https://mainnet.helius-rpc.com/<redacted>" })"#,
            ),
            (
                "connect wss://example.solana-mainnet.quiknode.pro/0123abcd/ failed",
                "connect wss://example.solana-mainnet.quiknode.pro/<redacted> failed",
            ),
            (
                "https://user:secret@rpc.example.com:8899/path",
                "https://<redacted>@rpc.example.com:8899/<redacted>",
            ),
            (
                "url: \"https://api.mainnet-beta.solana.com/\"",
                "url: \"https://api.mainnet-beta.solana.com/<redacted>\"",
            ),
            (
                "https://api.mainnet-beta.solana.com",
                "https://api.mainnet-beta.solana.com",
            ),
            ("no url here: dns error", "no url here: dns error"),
            (
                "a http://h1/k1 b ws://h2?k2",
                "a http://h1/<redacted> b ws://h2/<redacted>",
            ),
        ];
        for (raw, want) in cases {
            let got = redact_urls(raw);
            assert_eq!(got, want);
            for secret in ["0123abcd", "secret", "k1", "k2"] {
                assert!(!got.contains(secret), "{secret} leaked from {raw}");
            }
        }
    }

    fn counters(lookups: u64, new_connections: u64, make_connection_ms: u64) -> QuicCounters {
        QuicCounters {
            lookups,
            new_connections,
            make_connection_ms,
            ..QuicCounters::default()
        }
    }

    #[test]
    fn a_send_between_reports_is_the_difference_of_its_snapshots() {
        let before = counters(10, 4, 90);
        let after = counters(12, 6, 150);
        let (send, zeroed) = QuicCounters::for_send(before, after, 2);
        assert!(!zeroed);
        assert_eq!(send, counters(2, 2, 60));
    }

    #[test]
    fn a_send_that_triggered_the_cache_report_is_its_after_snapshot_alone() {
        // Agave zeroed every counter at this send's first lookup, before adding its own counts:
        // a plain difference would saturate to zero and report a cold send as warm.
        let before = counters(10, 4, 90);
        let after = counters(2, 2, 61);
        let (send, zeroed) = QuicCounters::for_send(before, after, 2);
        assert!(zeroed);
        assert_eq!(send, after);
    }

    #[test]
    fn the_send_line_names_counts_and_times_and_no_address() {
        let line = SendRecord {
            outcome: "accepted",
            identity: "Sig111",
            slot: 450_860_000,
            leaders: 2,
            counts: counters(2, 2, 61),
            zeroed: true,
            send_ms: 41.25,
            leader_ms: &[40.95, 22.0],
            leader_ok: &[true, false],
            inflight: 1,
            warm_inflight: 2,
            unix_ms: 1790470214865,
        }
        .line();
        assert_eq!(
            line,
            "memecoin-tpu-sidecar send outcome=accepted tx=Sig111 slot=450860000 leaders=2 \
             new_connections=2 \
             reused=0 connection_errors=0 zero_rtt=0/0 make_connection_ms=61 send_ms=41.2 \
             leader_ms=41.0,22.0 leader_ok=1,0 counters=since_report inflight=1 \
             warm_inflight=2 unix_ms=1790470214865"
        );
        assert!(!line.contains("://") && !line.contains(':'));
    }

    #[test]
    fn a_reconnect_reason_is_kept_and_every_address_is_redacted() {
        let text = "Made 0rtt connection to 145.40.93.84:8009 with id 3 try_count 1, \
                    last_connection_id: 2, last_error: Some(ConnectionError(ApplicationClosed(\
                    ApplicationClose { error_code: 0, reason: b\"dropped\" })))";
        let line = quic_reason_line(text, 7).unwrap();
        assert!(line.starts_with("memecoin-tpu-sidecar quic Made 0rtt connection to <leader> with"));
        assert!(line.contains("ApplicationClosed") && line.ends_with(" unix_ms=7"));
        assert!(!line.contains("145.40"));
        let v6 = quic_reason_line(
            "Cannot make 0rtt connection to [2001:db8::1]:8009, error InvalidRemoteAddress(10.0.0.1:8009)",
            1,
        )
        .unwrap();
        assert!(!v6.contains("2001") && !v6.contains("10.0.0.1"), "{v6}");
        assert!(v6.contains("to <leader>, error InvalidRemoteAddress(<leader>)"), "{v6}");
    }

    #[test]
    fn warm_handshake_chatter_is_not_logged() {
        assert_eq!(
            quic_reason_line(
                "Made connection to 1.2.3.4:8009 id 3 try_count 0, from connection cache warming?: true",
                1
            ),
            None
        );
        assert_eq!(redact_addrs("stable id 12 try_count 1 deadbeef"), "stable id 12 try_count 1 deadbeef");
    }

    fn addr(port: u16) -> SocketAddr {
        SocketAddr::from(([10, 0, 0, 1], port))
    }

    // S1 re-implements Agave's delivery loop to time each leader. It must deliver the SAME bytes
    // to the SAME unique leaders, once each, and aggregate exactly as try_send_wire_transaction.
    #[tokio::test]
    async fn every_leader_gets_the_same_bytes_once_and_any_success_is_accepted() {
        let sent = Arc::new(Mutex::new(Vec::new()));
        let log = sent.clone();
        let wire = vec![1u8, 2, 3, 250];
        let delivery = deliver_to_leaders(vec![addr(1), addr(2)], wire.clone(), move |a, w| {
            log.lock().unwrap().push((a, w));
            async move { a.port() == 2 }
        })
        .await;
        let mut got = sent.lock().unwrap().clone();
        got.sort();
        assert_eq!(got, vec![(addr(1), wire.clone()), (addr(2), wire.clone())]);
        assert!(delivery.ok);
        assert_eq!(delivery.leader_ok, vec![false, true]);
        assert_eq!(delivery.leader_ms.len(), 2);
    }

    #[tokio::test]
    async fn all_failed_or_no_leaders_is_not_a_success() {
        let failed = deliver_to_leaders(vec![addr(1), addr(2)], vec![9], |_a, _w| async { false });
        assert!(!failed.await.ok);
        let none = deliver_to_leaders(Vec::new(), vec![9], |_a, _w| async { true }).await;
        assert_eq!(
            none,
            Delivery {
                ok: false,
                leader_ms: vec![],
                leader_ok: vec![]
            }
        );
    }

    #[tokio::test]
    async fn leaders_are_sent_concurrently_and_each_is_timed() {
        let started = Instant::now();
        let delivery = deliver_to_leaders(vec![addr(1), addr(2)], vec![7], |a, _w| async move {
            tokio::time::sleep(Duration::from_millis(if a.port() == 1 { 120 } else { 20 })).await;
            true
        })
        .await;
        assert!(started.elapsed() < Duration::from_millis(200)); // concurrent, not 140 ms serial
        assert!(delivery.leader_ms[0] >= 115.0 && delivery.leader_ms[1] < 100.0);
    }

    #[test]
    fn pre_send_refuses_a_frozen_clock_before_counting_leaders() {
        assert_eq!(pre_send(false, 3), Err(SendOutcome::StaleSlot));
        assert_eq!(pre_send(true, 0), Err(SendOutcome::NoLeaders));
        assert_eq!(pre_send(true, 2), Ok(2));
    }
}
