//! Keyless Solana TPU sidecar. Binds a PRIVATE 0600 Unix socket, reads length-prefixed JSON requests,
//! verifies + delivers each ONCE over QUIC, and answers no-send health probes. It never reads a key,
//! opens SQLite, signs, mutates, retries with new bytes, or logs a payload / URL / validator address.
//!
//! The RPC/WS URLs arrive ONLY through the environment (`SNIPER_TPU_RPC_URL`, `SNIPER_TPU_WS_URL`),
//! never argv or logs. Each request is bounded by a deadline shorter than the outer transport's.

mod delivery;
mod protocol;

use std::os::unix::fs::PermissionsExt;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use delivery::{SendDecision, SendOutcome, WireDelivery};
use protocol::{frame_bytes, parse_len_prefix, verify_submit, IncomingRequest, MAX_FRAME_BYTES};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::UnixListener;

const REQUEST_DEADLINE: Duration = Duration::from_millis(1500);

/// Prints the Agave QUIC records that name why a pooled connection was replaced (review DR-05);
/// see delivery::quic_reason_line. Only the solana_quic_client target is enabled, so no other
/// crate's logging starts and no other record is even formatted.
struct QuicReasonLog;
static QUIC_REASON_LOG: QuicReasonLog = QuicReasonLog;

impl log::Log for QuicReasonLog {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        metadata.level() <= log::Level::Info && metadata.target().starts_with("solana_quic_client")
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let unix_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_millis());
        if let Some(line) = delivery::quic_reason_line(&record.args().to_string(), unix_ms) {
            println!("{line}");
        }
    }

    fn flush(&self) {}
}

/// Pure request handler. Given any `WireDelivery`, returns the response JSON. A verification failure
/// is EXPLICIT_REJECTED before delivery; a health request never sends.
fn handle_request(delivery: &dyn WireDelivery, req: &IncomingRequest) -> serde_json::Value {
    let started = Instant::now();
    delivery.observe_slot(req.min_context_slot);
    if req.health {
        let leader_count = delivery.leader_count();
        return serde_json::json!({
            "request_id": req.request_id,
            "ready": leader_count > 0 && delivery.healthy(),
            "leader_count": leader_count,
            "leader_slot": delivery.leader_slot(),
        });
    }
    let identity = req.tx_identity.clone().unwrap_or_default();
    let respond = |state: &str, ident: &str, leader_count: u64, reason: Option<&str>| {
        serde_json::json!({
            "request_id": req.request_id,
            "state": state,
            "tx_identity": ident,
            "leader_count": leader_count,
            "reason_code": reason,
            "latency_ms": started.elapsed().as_secs_f64() * 1000.0,
        })
    };
    match verify_submit(req) {
        Err(reason) => respond("explicit_rejected", &identity, 0, Some(reason.0)),
        Ok(v) => {
            let decision = delivery.send_once(v.wire, &v.identity, req.entry_max_slot);
            let mut response = match decision.outcome {
            SendOutcome::Accepted { leader_count } => {
                respond("accepted", &v.identity, leader_count, None)
            }
            SendOutcome::Ambiguous { leader_count } => {
                respond("ambiguous", &v.identity, leader_count, Some("send_error"))
            }
            SendOutcome::NoLeaders => respond("not_sent", &v.identity, 0, Some("no_leader")),
            SendOutcome::StaleSlot => respond("not_sent", &v.identity, 0, Some("stale_slot")),
            SendOutcome::EntrySlotExpired => respond("not_sent", &v.identity, 0, Some("entry_slot_expired")),
            };
            if let Some(guard) = decision.guard { response["guard"] = serde_json::json!(guard); }
            response["request_min_context_slot"] = serde_json::json!(req.min_context_slot);
            response["request_entry_max_slot"] = serde_json::json!(req.entry_max_slot);
            response
        },
    }
}

/// Read one framed request, handle it, write one framed response. The WHOLE request is bounded by
/// `deadline`, including synchronous RPC/QUIC work inside the Agave client. Delivery runs on Tokio's
/// blocking pool so it cannot stall an async worker. If the deadline wins, the blocking delivery may
/// still finish in the background, but the local caller loses the reply and therefore maps the
/// attempt to AMBIGUOUS — never safe rejection or a new signed transaction.
/// A frame-length outside `(0, MAX_FRAME_BYTES]` or malformed JSON closes the connection without a
/// reply (the caller maps a lost reply to AMBIGUOUS).
async fn handle_conn<S>(
    mut stream: S,
    delivery: Arc<dyn WireDelivery>,
    deadline: Duration,
) -> anyhow::Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    tokio::time::timeout(deadline, handle_conn_inner(&mut stream, delivery))
        .await
        .map_err(|_| anyhow::anyhow!("request deadline"))??;
    Ok(())
}

async fn handle_conn_inner<S>(stream: &mut S, delivery: Arc<dyn WireDelivery>) -> anyhow::Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let mut header = [0u8; 4];
    stream.read_exact(&mut header).await?;
    let len = parse_len_prefix(&header);
    if len == 0 || len > MAX_FRAME_BYTES {
        anyhow::bail!("frame length out of bounds");
    }
    let mut body = vec![0u8; len];
    stream.read_exact(&mut body).await?;
    let req: IncomingRequest = serde_json::from_slice(&body)?;
    let resp = tokio::task::spawn_blocking(move || handle_request(delivery.as_ref(), &req)).await?;
    let out = serde_json::to_vec(&resp)?;
    stream.write_all(&frame_bytes(&out)).await?;
    stream.flush().await.ok();
    Ok(())
}

/// Bind a private Unix socket and lock it to owner-only (0600).
fn bind_private_socket(path: &str) -> anyhow::Result<UnixListener> {
    let _ = std::fs::remove_file(path);
    let listener = UnixListener::bind(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    Ok(listener)
}

fn derive_ws_url(rpc_url: &str) -> String {
    if let Some(rest) = rpc_url.strip_prefix("https://") {
        format!("wss://{rest}")
    } else if let Some(rest) = rpc_url.strip_prefix("http://") {
        format!("ws://{rest}")
    } else {
        rpc_url.to_string()
    }
}

/// A URL-free startup line — proves the sidecar logs no endpoint.
fn sanitized_startup_line() -> String {
    format!(
        "memecoin-tpu-sidecar ready fanout_slots={} pool_size={} warm_lookahead_slots={}",
        delivery::FANOUT_SLOTS,
        delivery::POOL_SIZE,
        delivery::warm_lookahead(
            std::env::var("SNIPER_TPU_WARM_LOOKAHEAD_SLOTS")
                .ok()
                .as_deref()
        )
    )
}

#[cfg(target_os = "linux")]
fn arm_parent_death_signal() -> anyhow::Result<()> {
    let parent = unsafe { libc::getppid() };
    if let Ok(raw) = std::env::var("SNIPER_TPU_PARENT_PID") {
        let expected: libc::pid_t = raw.parse()?;
        if parent != expected {
            anyhow::bail!("sidecar parent identity changed before startup");
        }
    } else if parent == 1 {
        // Without an explicit binding, PID 1 is indistinguishable from a child that was already
        // re-parented after its owner exited. The Python owner always supplies the binding; this
        // fallback protects manual launches using the old environment contract.
        anyhow::bail!("sidecar parent already exited");
    }
    let rc = unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    if unsafe { libc::getppid() } != parent {
        anyhow::bail!("sidecar parent exited during startup");
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn arm_parent_death_signal() -> anyhow::Result<()> {
    Ok(())
}

/// A startup failure exits 1 with its cause on the log, every URL reduced to scheme and host: Agave's
/// RPC errors print the full URL, and a provider key rides in it (2,976 key-bearing lines found in
/// the sidecar log on 2026-09-27).
#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() {
    if log::set_logger(&QUIC_REASON_LOG).is_ok() {
        log::set_max_level(log::LevelFilter::Info);
    }
    if let Err(e) = run().await {
        eprintln!("Error: {}", delivery::redact_urls(&format!("{e:?}")));
        std::process::exit(1);
    }
}

async fn run() -> anyhow::Result<()> {
    arm_parent_death_signal()?;
    let socket_path = std::env::var("SNIPER_TPU_SOCKET_PATH")?;
    let rpc_url = std::env::var("SNIPER_TPU_RPC_URL")?;
    let ws_url = std::env::var("SNIPER_TPU_WS_URL").unwrap_or_else(|_| derive_ws_url(&rpc_url));

    // Construct the real QUIC delivery on the runtime: its leader service runs as tasks here, and so
    // does the supervisor that rebuilds the client when its slot socket dies.
    let delivery =
        Arc::new(delivery::AgaveDelivery::new(&rpc_url, &ws_url, delivery::FANOUT_SLOTS).await?);
    tokio::spawn(delivery.clone().supervise());
    let lookahead = delivery::warm_lookahead(
        std::env::var("SNIPER_TPU_WARM_LOOKAHEAD_SLOTS")
            .ok()
            .as_deref(),
    );
    tokio::spawn(delivery.clone().warm(lookahead));
    let delivery: Arc<dyn WireDelivery> = delivery;

    let listener = bind_private_socket(&socket_path)?;
    println!("{}", sanitized_startup_line()); // URL-free
    while let Ok((stream, _addr)) = listener.accept().await {
        let d = delivery.clone();
        tokio::spawn(async move {
            let _ = handle_conn(stream, d, REQUEST_DEADLINE).await;
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::prelude::{Engine as _, BASE64_STANDARD};
    use delivery::FakeDelivery;
    use sha2::{Digest, Sha256};
    use solana_signature::Signature;
    use solana_transaction::versioned::VersionedTransaction;

    struct SlowDelivery;

    #[cfg(target_os = "linux")]
    #[test]
    fn sidecar_arms_sigterm_when_parent_dies() {
        let parent = unsafe { libc::getppid() };
        std::env::set_var("SNIPER_TPU_PARENT_PID", parent.to_string());
        arm_parent_death_signal().unwrap();
        std::env::remove_var("SNIPER_TPU_PARENT_PID");
        let mut signal = 0;
        let rc = unsafe { libc::prctl(libc::PR_GET_PDEATHSIG, &mut signal) };
        assert_eq!(rc, 0);
        assert_eq!(signal, libc::SIGTERM);
    }

    impl WireDelivery for SlowDelivery {
        fn leader_count(&self) -> u64 {
            1
        }
        fn healthy(&self) -> bool {
            true
        }
        fn send_once(&self, _wire: Vec<u8>, _identity: &str, _entry_max_slot: Option<u64>) -> SendDecision {
            std::thread::sleep(Duration::from_millis(250));
            SendOutcome::Accepted { leader_count: 1 }.into()
        }
    }

    fn sha256_hex(bytes: &[u8]) -> String {
        let mut h = Sha256::new();
        h.update(bytes);
        h.finalize().iter().map(|b| format!("{:02x}", b)).collect()
    }

    fn valid_submit() -> IncomingRequest {
        let sig = Signature::from([5u8; 64]);
        let tx = VersionedTransaction {
            signatures: vec![sig],
            message: Default::default(),
        };
        let payload = BASE64_STANDARD.encode(wincode::serialize(&tx).unwrap());
        IncomingRequest {
            request_id: "r1".into(),
            health: false,
            min_context_slot: None,
            entry_max_slot: None,
            tx_identity: Some(bs58::encode(sig.as_ref()).into_string()),
            signed_payload_sha256: Some(sha256_hex(payload.as_bytes())),
            signed_payload_base64: Some(payload),
            last_valid_block_height: Some(1),
        }
    }

    #[test]
    fn fake_one_success_maps_to_accepted_and_sends_once() {
        let fake = FakeDelivery::new(3, SendOutcome::Accepted { leader_count: 3 });
        let resp = handle_request(&fake, &valid_submit());
        assert_eq!(resp["state"], "accepted");
        assert_eq!(resp["leader_count"], 3);
        assert_eq!(fake.sends.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    fn submit_with_cap(cap: serde_json::Value) -> IncomingRequest {
        let req = valid_submit();
        serde_json::from_value(serde_json::json!({
            "request_id": req.request_id,
            "tx_identity": req.tx_identity,
            "signed_payload_base64": req.signed_payload_base64,
            "signed_payload_sha256": req.signed_payload_sha256,
            "entry_max_slot": cap,
        })).unwrap()
    }

    #[test]
    fn expired_entry_cap_is_not_sent_without_sender_calls() {
        let mut fake = FakeDelivery::new(2, SendOutcome::Accepted { leader_count: 2 });
        fake.slot = Some(102);
        let req = submit_with_cap(serde_json::json!(101));
        let response = handle_request(&fake, &req);
        assert_eq!(response["state"], "not_sent");
        assert_eq!(response["reason_code"], "entry_slot_expired");
        assert_eq!(response["tx_identity"], req.tx_identity.unwrap());
        assert_eq!(response["request_entry_max_slot"], 101);
        assert_eq!(fake.sends.load(std::sync::atomic::Ordering::SeqCst), 0);
    }

    #[test]
    fn entry_cap_is_inclusive_and_legacy_requests_still_send() {
        let mut fake = FakeDelivery::new(2, SendOutcome::Accepted { leader_count: 2 });
        fake.slot = Some(101);
        let response = handle_request(&fake, &submit_with_cap(serde_json::json!(101)));
        assert_eq!(response["state"], "accepted");
        assert_eq!(response["request_entry_max_slot"], 101);
        fake.slot = Some(102);
        assert_eq!(handle_request(&fake, &valid_submit())["state"], "accepted");
        assert_eq!(fake.sends.load(std::sync::atomic::Ordering::SeqCst), 2);
    }

    #[test]
    fn zero_entry_cap_is_rejected_without_delivery() {
        let fake = FakeDelivery::new(2, SendOutcome::Accepted { leader_count: 2 });
        let response = handle_request(&fake, &submit_with_cap(serde_json::json!(0)));
        assert_eq!(response["state"], "explicit_rejected");
        assert_eq!(response["reason_code"], "invalid_entry_max_slot");
        assert_eq!(fake.sends.load(std::sync::atomic::Ordering::SeqCst), 0);
    }

    #[test]
    fn fake_all_fail_maps_to_ambiguous() {
        let fake = FakeDelivery::new(2, SendOutcome::Ambiguous { leader_count: 2 });
        let resp = handle_request(&fake, &valid_submit());
        assert_eq!(resp["state"], "ambiguous");
    }

    #[test]
    fn no_leaders_maps_to_not_sent() {
        let fake = FakeDelivery::new(0, SendOutcome::NoLeaders);
        let resp = handle_request(&fake, &valid_submit());
        assert_eq!(resp["state"], "not_sent");
    }

    #[test]
    fn verification_failure_is_explicit_rejected_without_delivery() {
        let fake = FakeDelivery::new(3, SendOutcome::Accepted { leader_count: 3 });
        let mut req = valid_submit();
        req.signed_payload_sha256 = Some("deadbeef".into()); // wrong hash
        let resp = handle_request(&fake, &req);
        assert_eq!(resp["state"], "explicit_rejected");
        assert_eq!(resp["reason_code"], "hash_mismatch");
        assert_eq!(fake.sends.load(std::sync::atomic::Ordering::SeqCst), 0); // never delivered
    }

    #[test]
    fn health_request_reports_readiness_without_sending() {
        let fake = FakeDelivery::new(7, SendOutcome::NoLeaders);
        let req = IncomingRequest {
            request_id: "h".into(),
            health: true,
            min_context_slot: None,
            entry_max_slot: None,
            tx_identity: None,
            signed_payload_base64: None,
            signed_payload_sha256: None,
            last_valid_block_height: None,
        };
        let resp = handle_request(&fake, &req);
        assert_eq!(resp["ready"], true);
        assert_eq!(resp["leader_count"], 7);
        assert_eq!(fake.sends.load(std::sync::atomic::Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn framed_roundtrip_over_a_stream() {
        let (mut client, server) = tokio::io::duplex(64 * 1024);
        let fake: Arc<dyn WireDelivery> = Arc::new(FakeDelivery::new(
            4,
            SendOutcome::Accepted { leader_count: 4 },
        ));
        let handle = tokio::spawn(async move { handle_conn(server, fake, REQUEST_DEADLINE).await });

        let sample = valid_submit();
        let body = serde_json::to_vec(&serde_json::json!({
            "request_id": sample.request_id,
            "tx_identity": sample.tx_identity,
            "signed_payload_base64": sample.signed_payload_base64,
            "signed_payload_sha256": sample.signed_payload_sha256,
            "last_valid_block_height": 1,
        }))
        .unwrap();
        client.write_all(&frame_bytes(&body)).await.unwrap();

        let mut header = [0u8; 4];
        client.read_exact(&mut header).await.unwrap();
        let len = parse_len_prefix(&header);
        let mut resp = vec![0u8; len];
        client.read_exact(&mut resp).await.unwrap();
        let v: serde_json::Value = serde_json::from_slice(&resp).unwrap();
        assert_eq!(v["state"], "accepted");
        handle.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn read_deadline_closes_a_silent_connection() {
        let (_client, server) = tokio::io::duplex(1024); // client never sends
        let fake: Arc<dyn WireDelivery> = Arc::new(FakeDelivery::new(1, SendOutcome::NoLeaders));
        let res = handle_conn(server, fake, Duration::from_millis(50)).await;
        assert!(res.is_err()); // timed out on the header read
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn request_deadline_bounds_a_blocked_delivery_call() {
        let (mut client, server) = tokio::io::duplex(64 * 1024);
        let sample = valid_submit();
        let body = serde_json::to_vec(&serde_json::json!({
            "request_id": sample.request_id,
            "tx_identity": sample.tx_identity,
            "signed_payload_base64": sample.signed_payload_base64,
            "signed_payload_sha256": sample.signed_payload_sha256,
            "last_valid_block_height": sample.last_valid_block_height,
        }))
        .unwrap();
        client.write_all(&frame_bytes(&body)).await.unwrap();
        let started = Instant::now();
        let res = handle_conn(server, Arc::new(SlowDelivery), Duration::from_millis(50)).await;
        assert!(res.is_err());
        assert!(started.elapsed() < Duration::from_millis(180));
    }

    #[test]
    fn refused_guard_is_bound_to_response_with_independent_socket_count() {
        let mut fake = FakeDelivery::new(2, SendOutcome::StaleSlot);
        fake.healthy = false;
        fake.guard = Some(delivery::GuardSnapshot {
            version: 1, estimated_slot: 100, minimum_slot: 103,
            age_since_advance_ms: 1001.0, clock_frozen: true,
            behind_minimum: true, available_leader_count: 2,
        });
        let mut req = valid_submit();
        for minimum in [None, Some(0), Some(103)] {
            req.min_context_slot = minimum;
            let resp = handle_request(&fake, &req);
            assert_eq!(resp["state"], "not_sent");
            assert_eq!(resp["reason_code"], "stale_slot");
            assert_eq!(resp["leader_count"], 0);
            assert_eq!(resp["guard"]["available_leader_count"], 2);
            assert_eq!(resp["guard"]["clock_frozen"], true);
            assert_eq!(resp["guard"]["behind_minimum"], true);
            assert_eq!(resp["request_min_context_slot"], serde_json::json!(minimum));
        }
        assert_eq!(fake.sends.load(std::sync::atomic::Ordering::SeqCst), 0);
        fake.observe_slot(Some(102));
        assert_eq!(fake.minimum_slot.load(std::sync::atomic::Ordering::Relaxed), 103);
        req.signed_payload_sha256 = Some("wrong".into());
        assert!(handle_request(&fake, &req).get("guard").is_none());
    }

    #[test]
    fn a_frozen_slot_clock_is_not_sent_and_says_why() {
        let fake = FakeDelivery::new(3, SendOutcome::StaleSlot);
        let resp = handle_request(&fake, &valid_submit());
        assert_eq!(resp["state"], "not_sent");
        assert_eq!(resp["reason_code"], "stale_slot");
        assert_eq!(resp["leader_count"], 0);
    }

    #[test]
    fn an_unhealthy_clock_reports_not_ready() {
        let mut fake = FakeDelivery::new(3, SendOutcome::NoLeaders);
        fake.healthy = false;
        let req = IncomingRequest {
            request_id: "h1".into(),
            health: true,
            min_context_slot: None,
            entry_max_slot: None,
            tx_identity: None,
            signed_payload_base64: None,
            signed_payload_sha256: None,
            last_valid_block_height: None,
        };
        let resp = handle_request(&fake, &req);
        assert_eq!(resp["ready"], false);
    }

    #[test]
    fn health_requires_at_least_one_resolved_leader() {
        let fake = FakeDelivery::new(0, SendOutcome::NoLeaders);
        let req = IncomingRequest {
            request_id: "h0".into(),
            health: true,
            min_context_slot: None,
            entry_max_slot: None,
            tx_identity: None,
            signed_payload_base64: None,
            signed_payload_sha256: None,
            last_valid_block_height: None,
        };
        let resp = handle_request(&fake, &req);
        assert_eq!(resp["ready"], false);
        assert_eq!(resp["leader_count"], 0);
    }

    #[tokio::test]
    async fn socket_is_bound_owner_only_0600() {
        // tokio's UnixListener::bind needs a running reactor.
        let dir = std::env::temp_dir();
        let path = dir.join(format!("tpu-test-{}.sock", std::process::id()));
        let p = path.to_string_lossy().to_string();
        let _l = bind_private_socket(&p).unwrap();
        let mode = std::fs::metadata(&p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn startup_line_and_ws_derivation_leak_no_url() {
        let line = sanitized_startup_line();
        assert!(!line.contains("http") && !line.contains("://"));
        assert_eq!(
            derive_ws_url("https://custody.example/rpc"),
            "wss://custody.example/rpc"
        );
        assert_eq!(derive_ws_url("http://x/y"), "ws://x/y");
    }
}

#[cfg(test)]
mod moving_stale_tests {
    use super::*;
    use crate::delivery::{FakeDelivery, SendOutcome};
    use std::sync::atomic::Ordering;

    #[test]
    fn admeme_moving_but_twenty_slots_late_is_not_sent() {
        let mut fake = FakeDelivery::new(2, SendOutcome::Accepted { leader_count: 2 });
        fake.slot = Some(453640566);
        let req: IncomingRequest = serde_json::from_value(serde_json::json!({
            "request_id": "h", "health": true, "min_context_slot": 453640587
        })).unwrap();
        let health = handle_request(&fake, &req);
        assert_eq!(health["ready"], false);
        assert_eq!(health["leader_slot"], 453640566);
        assert!(matches!(fake.send_once(vec![], "test", None).outcome, SendOutcome::StaleSlot));
        assert_eq!(fake.sends.load(Ordering::Relaxed), 0);
        // A repaired client uses the same published floor and can send again.
        fake.slot = Some(453640587);
        assert_eq!(handle_request(&fake, &req)["ready"], true);
    }

    #[test]
    fn context_slot_is_optional_and_monotonic_under_out_of_order_requests() {
        let fake = FakeDelivery::new(2, SendOutcome::NoLeaders);
        fake.observe_slot(Some(453640587));
        fake.observe_slot(Some(453640586));
        fake.observe_slot(None);
        fake.observe_slot(Some(0));
        assert_eq!(fake.minimum_slot.load(Ordering::Relaxed), 453640587);
        assert!(crate::delivery::slot_is_current(453640585, 453640587));
        assert!(!crate::delivery::slot_is_current(453640584, 453640587));
        assert!(crate::delivery::slot_is_current(u64::MAX, u64::MAX));
    }
}
