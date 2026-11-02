# solana-quic-client 4.1.2

Source: the crates.io package for `solana-quic-client` 4.1.2, from Agave commit
`182084b82aae88b1b0731540f63bf7416f27773a`, directory `quic-client`.
Original package SHA-256: `5f94e5546a008014b89b20eef8524069b8b6bf23333210e764edb4162360ce87`.
License: Apache-2.0; see LICENSE.

Only `src/nonblocking/quic_client.rs` differs from the published source: retained
connections can be refreshed off the send path without waiting on the client lock
or replacing a connection created by a concurrent send. Local regression tests live
in that file. The other copied source, test and manifest files are unchanged.
