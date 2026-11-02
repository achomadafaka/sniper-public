//! Simple nonblocking client that connects to a given UDP port with the QUIC protocol
//! and provides an interface for sending data which is restricted by the
//! server's flow control.
use {
    async_lock::Mutex,
    async_trait::async_trait,
    futures::future::TryFutureExt,
    log::*,
    quinn::{
        ClientConfig, ClosedStream, ConnectError, Connection, ConnectionError, Endpoint,
        EndpointConfig, IdleTimeout, TokioRuntime, TransportConfig, WriteError,
        crypto::rustls::QuicClientConfig,
    },
    solana_connection_cache::{
        client_connection::ClientStats, connection_cache_stats::ConnectionCacheStats,
        nonblocking::client_connection::ClientConnection,
    },
    solana_keypair::Keypair,
    solana_measure::measure::Measure,
    solana_net_utils::sockets,
    solana_rpc_client_api::client_error::ErrorKind as ClientErrorKind,
    solana_streamer::{nonblocking::quic::ALPN_TPU_PROTOCOL_ID, quic::QUIC_MAX_TIMEOUT},
    solana_tls_utils::{
        QuicClientCertificate, new_dummy_x509_certificate, socket_addr_to_quic_server_name,
        tls_client_config_builder,
    },
    solana_transaction_error::TransportResult,
    std::{
        net::{SocketAddr, UdpSocket},
        sync::{Arc, atomic::{AtomicU64, Ordering}},
        thread,
        time::Duration,
    },
    thiserror::Error,
    tokio::{sync::OnceCell, time::timeout},
};

const QUIC_KEEP_ALIVE: Duration = Duration::from_secs(1);

// Based on commonly-used handshake timeouts for various TCP
// applications. Different applications vary, but most seem to
// be in the 30-60 second range
pub const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(60);

/// A lazy-initialized Quic Endpoint
pub struct QuicLazyInitializedEndpoint {
    endpoint: OnceCell<Arc<Endpoint>>,
    client_certificate: Arc<QuicClientCertificate>,
    client_endpoint: Option<Endpoint>,
}

#[derive(Error, Debug)]
pub enum QuicError {
    #[error(transparent)]
    WriteError(#[from] WriteError),
    #[error(transparent)]
    ConnectionError(#[from] ConnectionError),
    #[error(transparent)]
    ConnectError(#[from] ConnectError),
    #[error(transparent)]
    ClosedStream(#[from] ClosedStream),
}

impl From<QuicError> for ClientErrorKind {
    fn from(quic_error: QuicError) -> Self {
        Self::Custom(format!("{quic_error:?}"))
    }
}

impl QuicLazyInitializedEndpoint {
    pub fn new(
        client_certificate: Arc<QuicClientCertificate>,
        client_endpoint: Option<Endpoint>,
    ) -> Self {
        Self {
            endpoint: OnceCell::<Arc<Endpoint>>::new(),
            client_certificate,
            client_endpoint,
        }
    }

    fn create_endpoint(&self) -> Endpoint {
        let mut endpoint = if let Some(endpoint) = &self.client_endpoint {
            endpoint.clone()
        } else {
            // This will bind to random ports, but VALIDATOR_PORT_RANGE is outside
            // of the range for CI tests when this is running in CI
            let client_socket = sockets::bind_in_range_with_config(
                std::net::IpAddr::V4(std::net::Ipv4Addr::UNSPECIFIED),
                solana_net_utils::VALIDATOR_PORT_RANGE,
                sockets::SocketConfiguration::default(),
            )
            .expect("QuicLazyInitializedEndpoint::create_endpoint bind_in_range")
            .1;
            info!("Local endpoint is : {client_socket:?}");

            QuicNewConnection::create_endpoint(EndpointConfig::default(), client_socket)
        };

        let mut crypto = tls_client_config_builder()
            .with_client_auth_cert(
                vec![self.client_certificate.certificate.clone()],
                self.client_certificate.key.clone_key(),
            )
            .expect("Failed to set QUIC client certificates");
        crypto.enable_early_data = true;
        crypto.alpn_protocols = vec![ALPN_TPU_PROTOCOL_ID.to_vec()];

        let mut config = ClientConfig::new(Arc::new(QuicClientConfig::try_from(crypto).unwrap()));
        let mut transport_config = TransportConfig::default();

        let timeout = IdleTimeout::try_from(QUIC_MAX_TIMEOUT).unwrap();
        transport_config.max_idle_timeout(Some(timeout));
        transport_config.keep_alive_interval(Some(QUIC_KEEP_ALIVE));
        transport_config.send_fairness(false);
        config.transport_config(Arc::new(transport_config));

        endpoint.set_default_client_config(config);

        endpoint
    }

    async fn get_endpoint(&self) -> Arc<Endpoint> {
        self.endpoint
            .get_or_init(|| async { Arc::new(self.create_endpoint()) })
            .await
            .clone()
    }
}

impl Default for QuicLazyInitializedEndpoint {
    fn default() -> Self {
        let (cert, priv_key) = new_dummy_x509_certificate(&Keypair::new());
        Self::new(
            Arc::new(QuicClientCertificate {
                certificate: cert,
                key: priv_key,
            }),
            None,
        )
    }
}

/// A wrapper over NewConnection with additional capability to create the endpoint as part
/// of creating a new connection.
#[derive(Clone)]
struct QuicNewConnection {
    endpoint: Arc<Endpoint>,
    connection: Arc<Connection>,
}

impl QuicNewConnection {
    /// Create a QuicNewConnection given the remote address 'addr'.
    async fn make_connection(
        endpoint: Arc<QuicLazyInitializedEndpoint>,
        addr: SocketAddr,
        stats: &ClientStats,
    ) -> Result<Self, QuicError> {
        let mut make_connection_measure = Measure::start("make_connection_measure");
        let endpoint = endpoint.get_endpoint().await;
        let server_name = socket_addr_to_quic_server_name(addr);
        let connecting = endpoint.connect(addr, &server_name)?;
        stats.total_connections.fetch_add(1, Ordering::Relaxed);
        if let Ok(connecting_result) = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await
        {
            if connecting_result.is_err() {
                stats.connection_errors.fetch_add(1, Ordering::Relaxed);
            }
            make_connection_measure.stop();
            stats
                .make_connection_ms
                .fetch_add(make_connection_measure.as_ms(), Ordering::Relaxed);

            let connection = connecting_result?;

            Ok(Self {
                endpoint,
                connection: Arc::new(connection),
            })
        } else {
            Err(ConnectionError::TimedOut.into())
        }
    }

    fn create_endpoint(config: EndpointConfig, client_socket: UdpSocket) -> Endpoint {
        quinn::Endpoint::new(config, None, client_socket, Arc::new(TokioRuntime))
            .expect("QuicNewConnection::create_endpoint quinn::Endpoint::new")
    }

    // Attempts to make a faster connection by taking advantage of pre-existing key material.
    // Only works if connection to this endpoint was previously established.
    async fn make_connection_0rtt(
        &mut self,
        addr: SocketAddr,
        stats: &ClientStats,
    ) -> Result<Arc<Connection>, QuicError> {
        let server_name = socket_addr_to_quic_server_name(addr);
        let connecting = self.endpoint.connect(addr, &server_name)?;
        stats.total_connections.fetch_add(1, Ordering::Relaxed);
        let connection = match connecting.into_0rtt() {
            Ok((connection, zero_rtt)) => {
                if let Ok(zero_rtt) = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, zero_rtt).await {
                    if zero_rtt {
                        stats.zero_rtt_accepts.fetch_add(1, Ordering::Relaxed);
                    } else {
                        stats.zero_rtt_rejects.fetch_add(1, Ordering::Relaxed);
                    }
                    connection
                } else {
                    return Err(ConnectionError::TimedOut.into());
                }
            }
            Err(connecting) => {
                stats.connection_errors.fetch_add(1, Ordering::Relaxed);

                if let Ok(connecting_result) =
                    timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await
                {
                    connecting_result?
                } else {
                    return Err(ConnectionError::TimedOut.into());
                }
            }
        };
        self.connection = Arc::new(connection);
        Ok(self.connection.clone())
    }
}

pub struct QuicClient {
    endpoint: Arc<QuicLazyInitializedEndpoint>,
    connection: Arc<Mutex<Option<QuicNewConnection>>>,
    addr: SocketAddr,
    stats: Arc<ClientStats>,
    refresh_epoch: AtomicU64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ConnectionRefresh {
    Ready,
    Refreshed,
    Busy,
}

const CONNECTION_CLOSE_CODE_APPLICATION_CLOSE: u32 = 0u32;
const CONNECTION_CLOSE_REASON_APPLICATION_CLOSE: &[u8] = b"dropped";

impl QuicClient {
    /// Explicitly close the connection. Must be called manually if cleanup is needed.
    pub async fn close(&self) {
        let mut conn_guard = self.connection.lock().await;
        // Retiring even an empty cache invalidates a handshake still running off-path.
        self.refresh_epoch.fetch_add(1, Ordering::Relaxed);
        if let Some(conn) = conn_guard.take() {
            debug!(
                "Closing connection to {} connection_id: {:?}",
                self.addr, conn.connection
            );
            conn.connection.close(
                CONNECTION_CLOSE_CODE_APPLICATION_CLOSE.into(),
                CONNECTION_CLOSE_REASON_APPLICATION_CLOSE,
            );
        }
    }
}

impl QuicClient {
    pub fn new(endpoint: Arc<QuicLazyInitializedEndpoint>, addr: SocketAddr) -> Self {
        Self {
            endpoint,
            connection: Arc::new(Mutex::new(None)),
            addr,
            stats: Arc::new(ClientStats::default()),
            refresh_epoch: AtomicU64::new(0),
        }
    }

    async fn refresh_connection(&self, stats: &ClientStats) -> Result<ConnectionRefresh, QuicError> {
        let (original, epoch) = {
            let Some(guard) = self.connection.try_lock() else {
                return Ok(ConnectionRefresh::Busy);
            };
            let epoch = self.refresh_epoch.load(Ordering::Relaxed);
            if guard.as_ref().is_some_and(|c| c.connection.close_reason().is_none()) {
                return Ok(ConnectionRefresh::Ready);
            }
            (guard.clone(), epoch)
        };
        let expected = original.as_ref().map(|c| c.connection.stable_id());
        // A handshake must never hold the mutex used by a funded send.
        let candidate = match original {
            Some(mut candidate) => {
                candidate.make_connection_0rtt(self.addr, stats).await?;
                candidate
            }
            None => QuicNewConnection::make_connection(self.endpoint.clone(), self.addr, stats).await?,
        };
        if let Some(reason) = candidate.connection.close_reason() {
            return Err(reason.into());
        }
        if let Some(mut guard) = self.connection.try_lock() {
            if self.refresh_epoch.load(Ordering::Relaxed) == epoch
                && guard.as_ref().map(|c| c.connection.stable_id()) == expected
            {
                *guard = Some(candidate);
                return Ok(ConnectionRefresh::Refreshed);
            }
        }
        // A send or retirement won the race. Close only this unpublished candidate.
        candidate.connection.close(0u32.into(), b"warm superseded");
        Ok(ConnectionRefresh::Busy)
    }

    async fn _send_buffer_using_conn(
        data: &[u8],
        connection: &Connection,
    ) -> Result<(), QuicError> {
        let mut send_stream = connection.open_uni().await?;
        send_stream.write_all(data).await?;
        Ok(())
    }

    // Attempts to send data, connecting/reconnecting as necessary
    // On success, returns the connection used to successfully send the data
    async fn _send_buffer(
        &self,
        data: &[u8],
        stats: &ClientStats,
        connection_stats: Arc<ConnectionCacheStats>,
    ) -> Result<Arc<Connection>, QuicError> {
        let mut measure_send_packet = Measure::start("send_packet_us");
        let mut measure_prepare_connection = Measure::start("prepare_connection");
        let mut connection_try_count = 0;
        let mut last_connection_id = 0;
        let mut last_error = None;
        while connection_try_count < 2 {
            let connection = {
                let mut conn_guard = self.connection.lock().await;

                let maybe_conn = conn_guard.as_mut();
                match maybe_conn {
                    Some(conn) => {
                        if conn.connection.stable_id() == last_connection_id {
                            // this is the problematic connection we had used before, create a new one
                            let conn = conn.make_connection_0rtt(self.addr, stats).await;
                            match conn {
                                Ok(conn) => {
                                    info!(
                                        "Made 0rtt connection to {} with id {} try_count {}, \
                                         last_connection_id: {}, last_error: {:?}",
                                        self.addr,
                                        conn.stable_id(),
                                        connection_try_count,
                                        last_connection_id,
                                        last_error,
                                    );
                                    connection_try_count += 1;
                                    conn
                                }
                                Err(err) => {
                                    info!(
                                        "Cannot make 0rtt connection to {}, error {:}",
                                        self.addr, err
                                    );
                                    return Err(err);
                                }
                            }
                        } else {
                            stats.connection_reuse.fetch_add(1, Ordering::Relaxed);
                            conn.connection.clone()
                        }
                    }
                    None => {
                        let conn = QuicNewConnection::make_connection(
                            self.endpoint.clone(),
                            self.addr,
                            stats,
                        )
                        .await;
                        match conn {
                            Ok(conn) => {
                                *conn_guard = Some(conn.clone());
                                info!(
                                    "Made connection to {} id {} try_count {}, from connection \
                                     cache warming?: {}",
                                    self.addr,
                                    conn.connection.stable_id(),
                                    connection_try_count,
                                    data.is_empty(),
                                );
                                connection_try_count += 1;
                                conn.connection.clone()
                            }
                            Err(err) => {
                                info!(
                                    "Cannot make connection to {}, error {:}, from connection \
                                     cache warming?: {}",
                                    self.addr,
                                    err,
                                    data.is_empty()
                                );
                                return Err(err);
                            }
                        }
                    }
                }
            };

            let new_stats = connection.stats();

            connection_stats
                .total_client_stats
                .congestion_events
                .update_stat(
                    &self.stats.congestion_events,
                    new_stats.path.congestion_events,
                );

            connection_stats
                .total_client_stats
                .streams_blocked_uni
                .update_stat(
                    &self.stats.streams_blocked_uni,
                    new_stats.frame_tx.streams_blocked_uni,
                );

            connection_stats
                .total_client_stats
                .data_blocked
                .update_stat(&self.stats.data_blocked, new_stats.frame_tx.data_blocked);

            connection_stats
                .total_client_stats
                .acks
                .update_stat(&self.stats.acks, new_stats.frame_tx.acks);

            if data.is_empty() {
                // no need to send packet as it is only for warming connections
                return Ok(connection);
            }

            last_connection_id = connection.stable_id();
            measure_prepare_connection.stop();

            match Self::_send_buffer_using_conn(data, &connection).await {
                Ok(()) => {
                    measure_send_packet.stop();
                    stats.successful_packets.fetch_add(1, Ordering::Relaxed);
                    stats
                        .send_packets_us
                        .fetch_add(measure_send_packet.as_us(), Ordering::Relaxed);
                    stats
                        .prepare_connection_us
                        .fetch_add(measure_prepare_connection.as_us(), Ordering::Relaxed);
                    trace!(
                        "Successfully sent to {} with id {}, thread: {:?}, data len: {}, \
                         send_packet_us: {} prepare_connection_us: {}",
                        self.addr,
                        connection.stable_id(),
                        thread::current().id(),
                        data.len(),
                        measure_send_packet.as_us(),
                        measure_prepare_connection.as_us(),
                    );

                    return Ok(connection);
                }
                Err(err) => match err {
                    QuicError::ConnectionError(_) => {
                        last_error = Some(err);
                    }
                    _ => {
                        info!(
                            "Error sending to {} with id {}, error {:?} thread: {:?}",
                            self.addr,
                            connection.stable_id(),
                            err,
                            thread::current().id(),
                        );
                        return Err(err);
                    }
                },
            }
        }

        // if we come here, that means we have exhausted maximum retries, return the error
        info!(
            "Ran into an error sending data {:?}, exhausted retries to {}",
            last_error, self.addr
        );
        // If we get here but last_error is None, then we have a logic error
        // in this function, so panic here with an expect to help debugging
        Err(last_error.expect("QuicClient::_send_buffer last_error.expect"))
    }

    pub async fn send_buffer<T>(
        &self,
        data: T,
        stats: &ClientStats,
        connection_stats: Arc<ConnectionCacheStats>,
    ) -> Result<(), ClientErrorKind>
    where
        T: AsRef<[u8]>,
    {
        self._send_buffer(data.as_ref(), stats, connection_stats)
            .await
            .map_err(Into::<ClientErrorKind>::into)?;
        Ok(())
    }

    pub async fn send_batch<T>(
        &self,
        buffers: &[T],
        stats: &ClientStats,
        connection_stats: Arc<ConnectionCacheStats>,
    ) -> Result<(), ClientErrorKind>
    where
        T: AsRef<[u8]>,
    {
        // Start off by "testing" the connection by sending the first buffer
        // This will also connect to the server if not already connected
        // and reconnect and retry if the first send attempt failed
        // (for example due to a timed out connection), returning an error
        // or the connection that was used to successfully send the buffer.
        // We will use the returned connection to send the rest of the buffers in the batch
        // to avoid touching the mutex in self, and not bother reconnecting if we fail along the way
        // since testing even in the ideal GCE environment has found no cases
        // where reconnecting and retrying in the middle of a batch send
        // (i.e. we encounter a connection error in the middle of a batch send, which presumably cannot
        // be due to a timed out connection) has succeeded
        if buffers.is_empty() {
            return Ok(());
        }
        let connection = self
            ._send_buffer(buffers[0].as_ref(), stats, connection_stats)
            .await
            .map_err(Into::<ClientErrorKind>::into)?;

        for data in buffers[1..buffers.len()].iter() {
            Self::_send_buffer_using_conn(data.as_ref(), &connection).await?;
        }
        Ok(())
    }

    pub fn server_addr(&self) -> &SocketAddr {
        &self.addr
    }

    pub fn stats(&self) -> Arc<ClientStats> {
        self.stats.clone()
    }
}

pub struct QuicClientConnection {
    pub client: Arc<QuicClient>,
    pub connection_stats: Arc<ConnectionCacheStats>,
}

impl QuicClientConnection {
    /// Off-path cache refresh. Busy means skipped; Ready is local transport state, not execution proof.
    pub async fn refresh_connection(&self) -> Result<ConnectionRefresh, ClientErrorKind> {
        let stats = ClientStats::default();
        let result = self.client.refresh_connection(&stats).await;
        self.connection_stats.add_client_stats(&stats, 0, result.is_ok());
        result.map_err(Into::into)
    }

    pub fn base_stats(&self) -> Arc<ClientStats> {
        self.client.stats()
    }

    pub fn connection_stats(&self) -> Arc<ConnectionCacheStats> {
        self.connection_stats.clone()
    }

    pub fn new(
        endpoint: Arc<QuicLazyInitializedEndpoint>,
        addr: SocketAddr,
        connection_stats: Arc<ConnectionCacheStats>,
    ) -> Self {
        let client = Arc::new(QuicClient::new(endpoint, addr));
        Self::new_with_client(client, connection_stats)
    }

    pub fn new_with_client(
        client: Arc<QuicClient>,
        connection_stats: Arc<ConnectionCacheStats>,
    ) -> Self {
        Self {
            client,
            connection_stats,
        }
    }
}

#[async_trait]
impl ClientConnection for QuicClientConnection {
    fn server_addr(&self) -> &SocketAddr {
        self.client.server_addr()
    }

    async fn send_data_batch(&self, buffers: &[Vec<u8>]) -> TransportResult<()> {
        let stats = ClientStats::default();
        let len = buffers.len();
        let res = self
            .client
            .send_batch(buffers, &stats, self.connection_stats.clone())
            .await;
        self.connection_stats
            .add_client_stats(&stats, len, res.is_ok());
        res?;
        Ok(())
    }

    async fn send_data(&self, data: &[u8]) -> TransportResult<()> {
        if data.is_empty() {
            // Legacy cache warming is best effort; callers needing state use refresh_connection.
            return self.refresh_connection().await.map(|_| ()).map_err(Into::into);
        }
        let stats = Arc::new(ClientStats::default());
        // When data is empty which is from cache warmer, we are not sending packets actually, do not count it in
        let num_packets = if data.is_empty() { 0 } else { 1 };
        self.client
            .send_buffer(data, &stats, self.connection_stats.clone())
            .map_ok(|v| {
                self.connection_stats
                    .add_client_stats(&stats, num_packets, true);
                v
            })
            .map_err(|e| {
                warn!(
                    "Failed to send data async to {}, error: {:?} ",
                    self.server_addr(),
                    e
                );
                datapoint_warn!("send-wire-async", ("failure", 1, i64),);
                self.connection_stats
                    .add_client_stats(&stats, num_packets, false);
                e.into()
            })
            .await
    }
}

#[cfg(test)]
mod stale_warm_tests {
    use super::*;

    fn peer() -> Endpoint {
        let (cert, key) = new_dummy_x509_certificate(&Keypair::new());
        let mut tls = solana_tls_utils::tls_server_config_builder()
            .with_single_cert(vec![cert], key).unwrap();
        tls.alpn_protocols = vec![ALPN_TPU_PROTOCOL_ID.to_vec()];
        let crypto = quinn::crypto::rustls::QuicServerConfig::try_from(tls).unwrap();
        Endpoint::server(quinn::ServerConfig::with_crypto(Arc::new(crypto)),
                         "127.0.0.1:0".parse().unwrap()).unwrap()
    }

    async fn connected(server: &Endpoint) -> (Arc<QuicClientConnection>, Connection) {
        let client = Arc::new(QuicClientConnection::new(
            Arc::new(QuicLazyInitializedEndpoint::default()), server.local_addr().unwrap(),
            Arc::new(ConnectionCacheStats::default())));
        let warming = {
            let client = client.clone();
            tokio::spawn(async move { client.send_data(&[]).await.unwrap() })
        };
        let remote = server.accept().await.unwrap().await.unwrap();
        warming.await.unwrap();
        (client, remote)
    }

    async fn cached(client: &QuicClientConnection) -> Connection {
        client.client.connection.lock().await.as_ref().unwrap().connection.as_ref().clone()
    }

    async fn remote_close(client: &QuicClientConnection, remote: &Connection) {
        let old = cached(client).await;
        remote.close(1u32.into(), b"dropped");
        old.closed().await;
    }

    async fn paused_refresh(client: &Arc<QuicClientConnection>, server: &Endpoint)
        -> (tokio::task::JoinHandle<Result<ConnectionRefresh, ClientErrorKind>>, quinn::Incoming)
    {
        let task = {
            let client = client.clone();
            tokio::spawn(async move { client.refresh_connection().await })
        };
        let incoming = timeout(Duration::from_secs(1), server.accept()).await.unwrap().unwrap();
        (task, incoming)
    }

    #[tokio::test]
    async fn stale_warm_recovers_remote_close_without_a_transaction() {
        tokio::time::timeout(Duration::from_secs(3), async {
            let server = peer();
            let (client, remote) = connected(&server).await;
            let original = cached(&client).await.stable_id();
            remote_close(&client, &remote).await;
            let accepting = {
                let server = server.clone();
                tokio::spawn(async move { server.accept().await.unwrap().await.unwrap() })
            };
            client.send_data(&[]).await.unwrap();
            let repaired = cached(&client).await;
            assert!(repaired.close_reason().is_none(), "warm returned a remotely closed connection");
            assert_ne!(repaired.stable_id(), original);
            let remote = accepting.await.unwrap();
            let first_repair = repaired.stable_id();
            client.send_data(&[]).await.unwrap();
            assert_eq!(cached(&client).await.stable_id(), first_repair);
            assert_eq!(client.refresh_connection().await.unwrap(), ConnectionRefresh::Ready);
            remote.close(0u32.into(), b"done");
        }).await.unwrap();
    }

    #[tokio::test]
    async fn stale_warm_handshake_never_holds_the_send_lock_or_replaces_a_send_generation() {
        let server = peer();
        let (client, remote) = connected(&server).await;
        let (replacement, replacement_remote) = connected(&server).await;
        remote_close(&client, &remote).await;
        let (refresh, incoming) = paused_refresh(&client, &server).await;
        let generation = replacement.client.connection.lock().await.take().unwrap();
        let wanted = generation.connection.stable_id();
        *client.client.connection.try_lock().expect("refresh held send lock during handshake") = Some(generation);
        let discarded_peer = incoming.await.unwrap();
        assert_eq!(refresh.await.unwrap().unwrap(), ConnectionRefresh::Busy);
        assert_eq!(cached(&client).await.stable_id(), wanted);
        assert!(replacement_remote.close_reason().is_none());
        timeout(Duration::from_secs(1), discarded_peer.closed()).await.unwrap();
    }

    #[tokio::test]
    async fn stale_warm_late_completion_cannot_revive_a_retired_connection_or_empty_cache() {
        for empty in [false, true] {
            let server = peer();
            let (client, remote) = connected(&server).await;
            remote_close(&client, &remote).await;
            if empty {
                client.client.close().await;
            }
            let (refresh, incoming) = paused_refresh(&client, &server).await;
            client.client.close().await;
            let discarded_peer = incoming.await.unwrap();
            assert_eq!(refresh.await.unwrap().unwrap(), ConnectionRefresh::Busy);
            assert!(client.client.connection.lock().await.is_none());
            timeout(Duration::from_secs(1), discarded_peer.closed()).await.unwrap();
        }
    }

    #[tokio::test]
    async fn stale_warm_contention_returns_without_waiting() {
        let server = peer();
        let (client, _remote) = connected(&server).await;
        let _guard = client.client.connection.lock().await;
        assert!(timeout(Duration::from_millis(10), client.send_data(&[]))
                .await.expect("legacy warm waited on send lock").is_ok());
        let mut samples = Vec::new();
        for _ in 0..1000 {
            let started = std::time::Instant::now();
            assert_eq!(timeout(Duration::from_millis(10), client.refresh_connection())
                       .await.expect("refresh waited on send lock").unwrap(), ConnectionRefresh::Busy);
            samples.push(started.elapsed().as_nanos());
        }
        samples.sort_unstable();
        println!("busy refresh overhead ns p50={} p90={}", samples[500], samples[900]);
    }

    #[tokio::test]
    async fn stale_warm_rejection_and_cancellation_leave_the_cached_generation_alone() {
        let server = peer();
        let (client, remote) = connected(&server).await;
        remote_close(&client, &remote).await;
        let original = cached(&client).await.stable_id();
        let (refresh, incoming) = paused_refresh(&client, &server).await;
        incoming.refuse();
        assert!(refresh.await.unwrap().is_err());
        assert_eq!(cached(&client).await.stable_id(), original);
        let (refresh, incoming) = paused_refresh(&client, &server).await;
        refresh.abort();
        assert!(refresh.await.unwrap_err().is_cancelled());
        drop(incoming);
        assert_eq!(cached(&client).await.stable_id(), original);
        assert!(client.client.connection.try_lock().is_some());
    }

    #[tokio::test]
    async fn stale_warm_cached_send_benchmark() {
        let server = peer();
        let (client, remote) = connected(&server).await;
        let draining = tokio::spawn(async move {
            while let Ok(mut stream) = remote.accept_uni().await {
                let _ = stream.read_to_end(32).await;
            }
        });
        let mut samples = Vec::new();
        for _ in 0..1000 {
            let started = std::time::Instant::now();
            client.send_data(&[0]).await.unwrap(); // loopback bytes, never a signed transaction
            samples.push(started.elapsed().as_nanos());
        }
        samples.sort_unstable();
        println!("cached QUIC write overhead ns p50={} p90={}", samples[500], samples[900]);
        client.client.close().await;
        timeout(Duration::from_secs(1), draining).await.unwrap().unwrap();
    }
}
