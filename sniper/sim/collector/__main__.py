"""Entry point: python -m sim.collector

Starts the Polymarket WebSocket collector, loading config from environment
variables and registering SIGTERM/SIGINT for graceful shutdown.
"""
import asyncio
import logging
import signal

from sniper.sim.collector.config import CollectorConfig
from sniper.sim.collector.ingest import Collector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("sniper.sim.collector")


async def main() -> None:
    config = CollectorConfig.from_env()
    collector = Collector(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, collector.stop)

    LOG.info(
        "Starting collector (raw_dir=%s, max_markets=%d)",
        config.raw_dir,
        config.max_markets,
    )
    await collector.run()


if __name__ == "__main__":
    asyncio.run(main())
