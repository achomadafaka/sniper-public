#!/usr/bin/env bash
# Build the pinned, keyless Solana TPU sidecar in Docker and export ONLY the stripped release binary
# to a SHA-keyed artifact directory with a deployment manifest fragment. Build-time only — the funded
# runtime never needs Docker or Cargo.
#
#   build_memecoin_tpu_sidecar.sh <release-root> <artifact-root> <full-sha>
#
# Refuses a non-40-hex source SHA. Idempotent: an existing artifact for the same SHA is never
# overwritten. Writes metadata.json { path, sha256, source_sha, cargo_lock_sha256, target,
# build_image_digest }. The DOCKER executable is overridable via $DOCKER (tests inject a fake).
set -euo pipefail

DOCKER="${DOCKER:-docker}"
# Immutable base image digest — must match rust/solana-tpu-sidecar/Dockerfile FROM.
BASE_IMAGE_DIGEST="sha256:c1e5f19e773b7878c3f7a805dd00a495e747acbdc76fb2337a4ebf0418896b33"
TARGET_TRIPLE="x86_64-unknown-linux-gnu"

die() { echo "build_memecoin_tpu_sidecar: $*" >&2; exit "${2:-1}"; }

[ "$#" -eq 3 ] || die "usage: $0 <release-root> <artifact-root> <full-sha>" 2
release_root="$1"; artifact_root="$2"; full_sha="$3"

[[ "$full_sha" =~ ^[0-9a-f]{40}$ ]] || die "refusing non-40-hex source sha: '$full_sha'" 3
ctx="$release_root/rust/solana-tpu-sidecar"
[ -f "$ctx/Cargo.lock" ] || die "missing Cargo.lock at $ctx" 4
[ -f "$ctx/Dockerfile" ] || die "missing Dockerfile at $ctx" 4

out_dir="$artifact_root/$full_sha"
bin="$out_dir/memecoin-tpu-sidecar"
meta="$out_dir/metadata.json"

if [ -f "$bin" ]; then
    # Idempotent: never overwrite an existing artifact for this SHA (a re-run must not clobber a
    # deployed binary; a mismatch is investigated by hand, not silently replaced).
    echo "artifact already present for $full_sha at $bin; not overwriting" >&2
    exit 0
fi

mkdir -p "$out_dir"
cargo_lock_sha="$(sha256sum "$ctx/Cargo.lock" | cut -d' ' -f1)"

DOCKER_BUILDKIT=1 "$DOCKER" build --build-arg CARGO_BUILD_JOBS=2 --target artifact \
    -o "type=local,dest=$out_dir" "$ctx"

[ -f "$bin" ] || die "docker build produced no binary at $bin" 5
chmod 0555 "$bin"
bin_sha="$(sha256sum "$bin" | cut -d' ' -f1)"

cat > "$meta" <<EOF
{
  "path": "$bin",
  "sha256": "$bin_sha",
  "source_sha": "$full_sha",
  "cargo_lock_sha256": "$cargo_lock_sha",
  "target": "$TARGET_TRIPLE",
  "build_image_digest": "$BASE_IMAGE_DIGEST"
}
EOF

echo "built $bin (sha256=$bin_sha)"
