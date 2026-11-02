#!/usr/bin/env bash
# Fire ONCE when DexScreener's data plane comes back, and stamp the confounded cohort closed.
#
# 2026-08-24: DexScreener's data routes stopped answering globally (verified from a second egress —
# r.jina.ai hangs on the same routes while its control passes, so it is their outage, not our IP).
# While it is dark, five coin-quality gates are blind (mcap ceiling, coin band, revival guard,
# liquidity/cluster, execution premium) and MEMECOIN_MAX_CONCURRENT is temporarily 4. Both revert
# when the provider returns. Relying on a human noticing is how a temporary setting becomes
# permanent, so this watches for it.
#
# HEALTH IS NOT "THE HOST RESPONDS". Throughout the outage the static routes answered instantly —
# `api.dexscreener.com/` returned 404 in 65 ms — while every data route hung for 75 s with zero
# bytes. So a 200 alone proves nothing: this requires a 200 whose body parses AND carries a pairs
# array with a real priceUsd. Anything less is treated as still-down.
set -u

REPO=/home/bigpapi/Desktop/Sniper
cd "$REPO" || exit 1

STATE=$REPO/data/dexscreener_recovery.json
LOG=$REPO/logs/dexscreener_recovery.log
# QUORUM, not one mint. During the 2026-08-24 partial recovery the index repopulated PER TOKEN and
# churned over minutes: wrapped SOL read empty 3x then returned data, while a coin that had 4 pairs
# minutes earlier went to 0/8. A single-mint probe is therefore a coin flip, and one lucky response
# fired this watcher at 14:35:01 and stamped the blind-gate cohort closed while half the index was
# still empty. All three must carry data before we call it recovered — a false negative only delays
# the alert, a false positive corrupts the cohort.
PROBE_MINTS="So11111111111111111111111111111111111111112 EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
COHORT_OPENED_AT=2026-08-24T11:40:00Z
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"

mkdir -p "$REPO/logs" "$REPO/data"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# Already fired: never probe again. Delete the state file to re-arm.
[ -f "$STATE" ] && exit 0

# fd 9 closed for any child so nothing can inherit the lock and wedge this permanently.
exec 9>"$REPO/data/dexscreener_recovery.lock"
flock -n 9 || exit 0

for probe_mint in $PROBE_MINTS; do
    body=$(curl -s --max-time 20 -H "User-Agent: $UA" -w $'\n%{http_code}' \
           "https://api.dexscreener.com/latest/dex/tokens/$probe_mint" 2>/dev/null)
    code=$(printf '%s' "$body" | tail -n 1)
    payload=$(printf '%s' "$body" | sed '$d')
    [ "$code" = "200" ] || exit 0             # still dark; stay quiet, try again next interval
    printf '%s' "$payload" | "$REPO/scripts/dexscreener_health_check.py" || exit 0
done

TS=$(date -u +%FT%TZ)
printf '{"recovered_at":"%s","cohort_opened_at":"%s","cohort_closed_at":"%s","probe_mint":"%s"}\n' \
    "$TS" "$COHORT_OPENED_AT" "$TS" "$PROBE_MINTS" > "$STATE"
log "RECOVERED $TS — confounded cohort closed ($COHORT_OPENED_AT .. $TS)"

tok=$(grep -m1 '^TELEGRAM_BOT_TOKEN_AI=' "$REPO/.env" | cut -d= -f2-)
chat=$(grep -m1 '^ALLOWED_TELEGRAM_USER_IDS=' "$REPO/.env" | cut -d= -f2- | cut -d, -f1)
if [ -n "$tok" ] && [ -n "$chat" ]; then
    curl -sf --max-time 10 "https://api.telegram.org/bot$tok/sendMessage" \
        --data-urlencode "chat_id=$chat" \
        --data-urlencode "text=DexScreener data plane is BACK ($TS).
Blind-gate cohort closed: $COHORT_OPENED_AT .. $TS
TODO: set MEMECOIN_MAX_CONCURRENT back to 2 and restart the runner on an empty book." \
        > /dev/null || log "telegram send failed"
fi
exit 0
