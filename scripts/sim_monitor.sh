#!/bin/bash
# Sim collector 24h burn-in monitor
# Run hourly via: watch -n 3600 bash scripts/sim_monitor.sh
# Or add to crontab: 0 * * * * bash ~/sniper/scripts/sim_monitor.sh >> ~/sniper/logs/sim_monitor.log

RAW_DIR=~/sniper/raw/polymarket
SNAPSHOT=~/sniper/state/markets_snapshot.json
LOG_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Raw data size
RAW_SIZE=$(du -sb $RAW_DIR 2>/dev/null | awk '{print $1}')
RAW_MB=$(echo "scale=1; $RAW_SIZE / 1048576" | bc)

# File count
FILE_COUNT=$(find $RAW_DIR -name '*.jsonl.gz' | wc -l)

# Snapshot market count
MARKET_COUNT=$(python3 -c "import json; print(len(json.load(open('$SNAPSHOT'))))" 2>/dev/null || echo 0)

# Collector status
COLLECTOR=$(systemctl --user is-active sniper-sim-collector 2>/dev/null)
META=$(systemctl --user is-active sniper-sim-meta 2>/dev/null)

# Disk free
DISK_FREE=$(df -h / | tail -1 | awk '{print $4}')

echo "$LOG_TS | raw=${RAW_MB}MB files=$FILE_COUNT markets=$MARKET_COUNT collector=$COLLECTOR meta=$META disk_free=$DISK_FREE"
