# syntax=docker/dockerfile:1
# Pinned Agave-compatible toolchain. The base image is pinned to an immutable digest so a production
# build cannot drift. Build-time only — the funded runtime never needs Docker or Cargo.
FROM rust:1.91.1-bookworm@sha256:c1e5f19e773b7878c3f7a805dd00a495e747acbdc76fb2337a4ebf0418896b33 AS base
ARG CARGO_BUILD_JOBS=2
ENV CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS}
ENV CARGO_NET_RETRY=5
# aws-lc-rs (rustls) needs cmake + clang; some transitive crates need protoc / openssl headers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake clang libclang-dev protobuf-compiler pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY vendor ./vendor

# --- test stage: fetch + compile + RUN the tests at build time so the build itself is a gate, and
#     bake the registry sources + compiled test binaries INTO the layer so `docker run` re-runs them
#     offline (a cache mount would vanish at runtime and break --offline). ---
FROM base AS test
RUN cargo test --locked -j 2 -- --test-threads=2 \
    && cargo test --locked -j 2 -p solana-quic-client --features agave-unstable-api \
       --lib stale_warm -- --test-threads=2
CMD ["sh", "-c", "cargo test --locked --offline -j 2 -- --test-threads=2 && cargo test --locked --offline -j 2 -p solana-quic-client --features agave-unstable-api --lib stale_warm -- --test-threads=2"]

# --- release build: stripped, LTO'd binary (see [profile.release]); target stays in-layer for export ---
FROM base AS build
RUN --mount=type=cache,id=tpu-registry,target=/usr/local/cargo/registry \
    cargo build --release --locked \
    && mkdir -p /out && cp target/release/memecoin-tpu-sidecar /out/memecoin-tpu-sidecar

# --- artifact stage: export ONLY the stripped release binary (docker build -o type=local) ---
FROM scratch AS artifact
COPY --from=build /out/memecoin-tpu-sidecar /memecoin-tpu-sidecar
