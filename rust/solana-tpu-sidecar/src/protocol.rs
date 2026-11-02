//! Framed local protocol + independent payload verification. No network, no keys, no payload logging.
//!
//! `verify_submit` re-derives the payload SHA-256 over the EXACT base64 field the shared Python
//! transport hashed (`signed_payload_sha256` == sha256 of the base64 string — the transport's
//! transit-integrity checksum), independently base64-decodes and wincode-decodes the COMPLETE
//! `VersionedTransaction`, and requires the decoded transaction's first signature (base58) to equal
//! the caller's `tx_identity`. Any deterministic failure is an EXPLICIT_REJECTED reason established
//! BEFORE any delivery. (The design's "SHA-256 over decoded wire bytes" is satisfied by the
//! decode-and-first-signature check; the checksum itself matches the shared transport field so the
//! money path needs no change.)

use base64::prelude::{Engine as _, BASE64_STANDARD};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use solana_transaction::versioned::VersionedTransaction;

pub const MAX_FRAME_BYTES: usize = 2 * 1024 * 1024;

#[derive(Debug, Deserialize)]
pub struct IncomingRequest {
    pub request_id: String,
    #[serde(default)]
    pub health: bool,
    /// Independent processed head from the runner, never a price or signed-byte mutation.
    #[serde(default)]
    pub min_context_slot: Option<u64>,
    /// Inclusive entry deadline; absence preserves exit/manual delivery.
    #[serde(default, deserialize_with = "deserialize_entry_max_slot")]
    pub entry_max_slot: Option<u64>,
    #[serde(default)]
    pub tx_identity: Option<String>,
    #[serde(default)]
    pub signed_payload_base64: Option<String>,
    #[serde(default)]
    pub signed_payload_sha256: Option<String>,
    // Received per the shared protocol; the sidecar does not act on it (transaction validity is
    // intrinsic to the signed bytes it delivers), so it is intentionally not read here.
    #[allow(dead_code)]
    #[serde(default)]
    pub last_valid_block_height: Option<u64>,
}

/// The verified, ready-to-deliver wire transaction.
#[derive(Debug)]
pub struct VerifiedTx {
    pub wire: Vec<u8>,
    pub identity: String,
}

/// A deterministic verification failure carrying a short reason_code (never a payload).
#[derive(Debug, PartialEq, Eq)]
pub struct RejectReason(pub &'static str);

fn deserialize_entry_max_slot<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<Option<u64>, D::Error> {
    u64::deserialize(deserializer).map(Some)
}

fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// Big-endian 32-bit length prefix + body. Framing is symmetric with the Python provider.
pub fn frame_bytes(body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(4 + body.len());
    out.extend_from_slice(&(body.len() as u32).to_be_bytes());
    out.extend_from_slice(body);
    out
}

pub fn parse_len_prefix(header: &[u8; 4]) -> usize {
    u32::from_be_bytes(*header) as usize
}

pub fn verify_submit(req: &IncomingRequest) -> Result<VerifiedTx, RejectReason> {
    if req.entry_max_slot == Some(0) {
        return Err(RejectReason("invalid_entry_max_slot"));
    }
    let identity = req.tx_identity.as_deref().unwrap_or("");
    if identity.is_empty() {
        return Err(RejectReason("missing_identity"));
    }
    let payload_b64 = req
        .signed_payload_base64
        .as_deref()
        .ok_or(RejectReason("missing_payload"))?;
    let expected_hash = req
        .signed_payload_sha256
        .as_deref()
        .ok_or(RejectReason("missing_hash"))?;
    // 1. transit integrity over the exact base64 field the shared transport hashed
    let mut hasher = Sha256::new();
    hasher.update(payload_b64.as_bytes());
    if to_hex(&hasher.finalize()) != expected_hash.to_lowercase() {
        return Err(RejectReason("hash_mismatch"));
    }
    // 2. independently decode base64 -> wire bytes
    let wire = BASE64_STANDARD
        .decode(payload_b64)
        .map_err(|_| RejectReason("bad_base64"))?;
    if wire.len() > MAX_FRAME_BYTES {
        return Err(RejectReason("payload_too_large"));
    }
    // 3. independently decode the COMPLETE VersionedTransaction (rejects trailing bytes)
    let tx: VersionedTransaction =
        wincode::deserialize_exact(&wire).map_err(|_| RejectReason("bad_transaction"))?;
    // 4. first signature (base58) must equal the caller's identity
    let first = tx.signatures.first().ok_or(RejectReason("no_signature"))?;
    let sig_b58 = bs58::encode(first.as_ref()).into_string();
    if sig_b58 != identity {
        return Err(RejectReason("signature_mismatch"));
    }
    Ok(VerifiedTx {
        wire,
        identity: identity.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use solana_signature::Signature;
    use solana_transaction::versioned::VersionedTransaction;

    fn sha256_hex(bytes: &[u8]) -> String {
        let mut h = Sha256::new();
        h.update(bytes);
        to_hex(&h.finalize())
    }

    // Build a wincode-serialized VersionedTransaction whose first signature is `sig`, and the matching
    // base64 payload + hash + identity — no signing tooling required (Signature is raw 64 bytes).
    fn fixture(sig: Signature) -> (String, String, String) {
        let tx = VersionedTransaction {
            signatures: vec![sig],
            message: Default::default(),
        };
        let wire = wincode::serialize(&tx).unwrap();
        let payload_b64 = BASE64_STANDARD.encode(&wire);
        let hash = sha256_hex(payload_b64.as_bytes());
        let identity = bs58::encode(sig.as_ref()).into_string();
        (payload_b64, hash, identity)
    }

    fn req(id: &str, payload: Option<&str>, hash: Option<&str>) -> IncomingRequest {
        IncomingRequest {
            request_id: "r".into(),
            health: false,
            min_context_slot: None,
            entry_max_slot: None,
            tx_identity: Some(id.into()),
            signed_payload_base64: payload.map(|s| s.to_string()),
            signed_payload_sha256: hash.map(|s| s.to_string()),
            last_valid_block_height: Some(1),
        }
    }

    #[test]
    fn framing_roundtrip() {
        let body = b"{\"x\":1}";
        let framed = frame_bytes(body);
        assert_eq!(&framed[..4], &(body.len() as u32).to_be_bytes());
        let mut hdr = [0u8; 4];
        hdr.copy_from_slice(&framed[..4]);
        assert_eq!(parse_len_prefix(&hdr), body.len());
    }

    #[test]
    fn accepts_matching_signature_identity() {
        let sig = Signature::from([7u8; 64]);
        let (payload, hash, identity) = fixture(sig);
        let v = verify_submit(&req(&identity, Some(&payload), Some(&hash))).unwrap();
        assert_eq!(v.identity, identity);
        assert!(!v.wire.is_empty());
    }

    #[test]
    fn missing_fields_are_rejected() {
        assert_eq!(
            verify_submit(&req("", None, None)).unwrap_err(),
            RejectReason("missing_identity")
        );
        assert_eq!(
            verify_submit(&req("id", None, Some("h"))).unwrap_err(),
            RejectReason("missing_payload")
        );
        assert_eq!(
            verify_submit(&req("id", Some("QQ=="), None)).unwrap_err(),
            RejectReason("missing_hash")
        );
    }

    #[test]
    fn malformed_entry_max_slot_is_rejected() {
        for value in [serde_json::json!("101"), serde_json::json!(-1), serde_json::json!(1.5), serde_json::json!(true), serde_json::Value::Null] {
            assert!(serde_json::from_value::<IncomingRequest>(serde_json::json!({
                "request_id": "cap", "entry_max_slot": value,
            })).is_err(), "accepted malformed entry cap: {value}");
        }
    }

    #[test]
    fn wrong_sha_is_rejected() {
        let (payload, _hash, identity) = fixture(Signature::from([1u8; 64]));
        let err = verify_submit(&req(&identity, Some(&payload), Some("deadbeef"))).unwrap_err();
        assert_eq!(err, RejectReason("hash_mismatch"));
    }

    #[test]
    fn malformed_base64_is_rejected() {
        // valid hash over the (invalid-base64) string, so we reach the decode step
        let payload = "!!!not-base64!!!";
        let hash = sha256_hex(payload.as_bytes());
        let err = verify_submit(&req("id", Some(payload), Some(&hash))).unwrap_err();
        assert_eq!(err, RejectReason("bad_base64"));
    }

    #[test]
    fn non_transaction_bytes_are_rejected() {
        let payload = BASE64_STANDARD.encode([0xffu8; 8]); // not a VersionedTransaction
        let hash = sha256_hex(payload.as_bytes());
        let err = verify_submit(&req("id", Some(&payload), Some(&hash))).unwrap_err();
        assert_eq!(err, RejectReason("bad_transaction"));
    }

    #[test]
    fn no_signature_is_rejected() {
        let tx = VersionedTransaction::default(); // empty signatures
        let wire = wincode::serialize(&tx).unwrap();
        let payload = BASE64_STANDARD.encode(&wire);
        let hash = sha256_hex(payload.as_bytes());
        let err = verify_submit(&req("id", Some(&payload), Some(&hash))).unwrap_err();
        assert_eq!(err, RejectReason("no_signature"));
    }

    #[test]
    fn first_signature_mismatch_is_rejected() {
        let (payload, hash, _identity) = fixture(Signature::from([2u8; 64]));
        // claim a DIFFERENT identity than the tx's first signature
        let wrong = bs58::encode([9u8; 64]).into_string();
        let err = verify_submit(&req(&wrong, Some(&payload), Some(&hash))).unwrap_err();
        assert_eq!(err, RejectReason("signature_mismatch"));
    }
}
