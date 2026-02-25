#!/usr/bin/env bash
set -uo pipefail
trap '' PIPE

###############################################################################
# BAM Error Monitor for Solana Validator
# Monitors validator.log for BAM-specific connection errors and metric anomalies.
# Auto-detects network (testnet/mainnet) from validator.sh --bam-url.
###############################################################################

DISCORD_WEBHOOK="$(cat "$HOME/.config/discord/webhook" 2>/dev/null | tr -d '[:space:]')"
if [[ -z "$DISCORD_WEBHOOK" ]]; then
    echo "ERROR: Discord webhook not found at ~/.config/discord/webhook" >&2
    exit 1
fi
BOT_USERNAME="Validator Log Monitor"
BOT_AVATAR="https://trillium.so/images/trillium-default.png"
VALIDATOR_LOG="$HOME/logs/validator.log"
STATE_DIR="$HOME/.log_monitor/bam"
OFFSET_DIR="$STATE_DIR/offsets"
HASH_DIR="$STATE_DIR/hashes"
DATE_FILE="$STATE_DIR/last_reset_date"

ONCE=false
VERBOSE=false
RESET=false
LOOP_INTERVAL=60

# Failover configuration
FAILOVER_DIR="$STATE_DIR/failover"
VALIDATOR_SH="/home/sol/validator.sh"
ADMIN_RPC="/mnt/ledger/admin.rpc"
# Auto-detect network and regions from validator.sh --bam-url
NETWORK=""
REGIONS=""
_bam_url=$(grep -oP '(?<=--bam-url )\S+' "$VALIDATOR_SH" 2>/dev/null | tail -1)
if [[ -z "$_bam_url" ]]; then
    _bam_url=$(ps aux 2>/dev/null | grep -oP '\-\-bam-url\s+\S+' | head -1 | awk '{print $2}')
fi
if [[ -n "$_bam_url" ]]; then
    NETWORK=$(echo "$_bam_url" | sed -E 's|https?://[^.]+\.([^.]+)\..*|\1|')
fi
case "$NETWORK" in
    mainnet) REGIONS="amsterdam dublin dallas frankfurt london lax ny pittsburgh slc singapore tokyo" ;;
    testnet) REGIONS="dallas ny slc" ;;
esac
FAIL_THRESHOLD=2
RECOVERY_THRESHOLD=2
FO_PING_COUNT=3
FO_PING_TIMEOUT=2

# BAM connection error patterns
BAM_CONNECTION_PATTERN='BAM connection lost|BAM connection not healthy|Failed to connect to BAM|Failed to start scheduler stream|auth.*fail|Inbound stream closed|Failed to get config'

# BAM metric anomaly patterns (non-zero counts for failure metrics)
BAM_METRIC_PATTERN='unhealthy_connection_count=[1-9]|outbound_fail=[1-9]|bundle_forward_to_scheduler_fail=[1-9]'

usage() {
    echo "Usage: $0 [--once] [--verbose] [--reset]"
    echo "  --once     Run once and exit"
    echo "  --verbose  Print debug output"
    echo "  --reset    Reset all BAM monitor state and exit"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --once)    ONCE=true; shift ;;
        --verbose) VERBOSE=true; shift ;;
        --reset)   RESET=true; shift ;;
        --help|-h) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
debug() { $VERBOSE && log "DEBUG: $*"; }

mkdir -p "$OFFSET_DIR" "$HASH_DIR" "$FAILOVER_DIR"

if $RESET; then
    log "Resetting BAM monitor state..."
    rm -f "$OFFSET_DIR"/* "$HASH_DIR"/* "$DATE_FILE"
    rm -rf "$FAILOVER_DIR"
    log "State reset complete."
    exit 0
fi

check_daily_reset() {
    local today
    today=$(date '+%Y-%m-%d')
    if [[ -f "$DATE_FILE" ]]; then
        local last_date
        last_date=$(cat "$DATE_FILE")
        if [[ "$last_date" != "$today" ]]; then
            log "New day ($last_date -> $today). Resetting BAM state."
            rm -f "$OFFSET_DIR"/* "$HASH_DIR"/*
            # Reset transient failover counters (but not on_fallback/fallback_region)
            rm -f "$FAILOVER_DIR/fail_count" "$FAILOVER_DIR/preferred_healthy_count"
        fi
    fi
    echo "$today" > "$DATE_FILE"
}

init_offset() {
    local log_file="$1"
    local offset_file="$2"
    if [[ ! -f "$offset_file" ]]; then
        local size
        size=$(stat -c%s "$log_file" 2>/dev/null || echo 0)
        echo "$size" > "$offset_file"
        debug "Initialized BAM offset for $log_file to $size"
    fi
}

read_new_content() {
    local log_file="$1"
    local offset_file="$2"
    local last_offset
    last_offset=$(cat "$offset_file" 2>/dev/null || echo 0)
    local current_size
    current_size=$(stat -c%s "$log_file" 2>/dev/null || echo 0)

    if (( current_size < last_offset )); then
        debug "Log rotation detected (was $last_offset, now $current_size)"
        last_offset=0
    fi

    if (( current_size > last_offset )); then
        local bytes_to_read=$(( current_size - last_offset ))
        debug "Reading $bytes_to_read new bytes (offset $last_offset -> $current_size)"
        tail -c +"$(( last_offset + 1 ))" "$log_file" | head -c "$bytes_to_read"
        echo "$current_size" > "$offset_file"
    fi
}

dedup_errors() {
    local hash_file="$1"
    python3 -c "
import sys, hashlib, re

hash_file = sys.argv[1]
seen = set()
try:
    with open(hash_file, 'r') as f:
        for line in f:
            seen.add(line.strip())
except FileNotFoundError:
    pass

new_hashes = []
for line in sys.stdin:
    line = line.rstrip('\n')
    if not line:
        continue
    normalized = line
    normalized = re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*Z?', 'TS', normalized)
    normalized = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?', 'IP', normalized)
    normalized = re.sub(r'\bslot[=: ]\d+', 'slot=S', normalized)
    normalized = re.sub(r'\b\d{6,}\b', 'N', normalized)
    h = hashlib.md5(normalized.encode()).hexdigest()
    if h not in seen:
        seen.add(h)
        new_hashes.append(h)
        print(line)

if new_hashes:
    with open(hash_file, 'a') as f:
        for h in new_hashes:
            f.write(h + '\n')
" "$hash_file"
}

send_discord() {
    local title="$1"
    local description="$2"
    local color="${3:-16711680}"

    if (( ${#description} > 4000 )); then
        description="${description:0:3990}..."
    fi

    local escaped_desc
    escaped_desc=$(python3 -c "
import json, sys
print(json.dumps(sys.stdin.read()))" <<< "$description")

    local escaped_title
    escaped_title=$(python3 -c "
import json, sys
print(json.dumps(sys.stdin.read().strip()))" <<< "$title")

    local payload
    payload=$(cat <<EOJSON
{
  "username": "$BOT_USERNAME",
  "avatar_url": "$BOT_AVATAR",
  "embeds": [{
    "title": $escaped_title,
    "description": $escaped_desc,
    "color": $color,
    "timestamp": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  }]
}
EOJSON
)

    local response
    response=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$DISCORD_WEBHOOK")

    if [[ "$response" == "204" || "$response" == "200" ]]; then
        debug "Discord message sent (HTTP $response)"
    else
        log "WARNING: Discord returned HTTP $response"
    fi
}

###############################################################################
# Failover helper functions
###############################################################################

read_state_file() {
    local name="$1"
    local default="${2:-}"
    local file="$FAILOVER_DIR/$name"
    if [[ -f "$file" ]]; then
        cat "$file"
    else
        echo "$default"
    fi
}

write_state_file() {
    local name="$1"
    local value="$2"
    echo "$value" > "$FAILOVER_DIR/$name"
}

get_preferred_region() {
    local url
    url=$(grep -oP '(?<=--bam-url )\S+' "$VALIDATOR_SH" 2>/dev/null | tail -1)
    if [[ -z "$url" ]]; then
        return 1
    fi
    # http://dallas.testnet.bam.jito.wtf -> dallas
    echo "$url" | sed -E 's|https?://([^.]+)\..*|\1|'
}

get_current_region() {
    local url
    url=$(ps aux 2>/dev/null | grep -oP '\-\-bam-url\s+\S+' | head -1 | awk '{print $2}')
    if [[ -z "$url" ]]; then
        return 1
    fi
    echo "$url" | sed -E 's|https?://([^.]+)\..*|\1|'
}

make_bam_url() {
    local region="$1"
    echo "http://${region}.${NETWORK}.bam.jito.wtf"
}

ping_bam_region() {
    local host="${1}.${NETWORK}.bam.jito.wtf"
    local avg
    avg=$(ping -c "$FO_PING_COUNT" -W "$FO_PING_TIMEOUT" "$host" 2>/dev/null \
        | awk -F'/' '/^rtt|^round-trip/ {printf "%.0f", $5}')
    if [[ -z "$avg" ]]; then
        echo "timeout"
    else
        echo "$avg"
    fi
}

select_best_region() {
    local exclude="${1:-}"
    local best_region=""
    local best_ms=999999
    for r in $REGIONS; do
        [[ "$r" == "$exclude" ]] && continue
        local ms
        ms=$(ping_bam_region "$r")
        debug "Ping $r: ${ms}" >&2
        if [[ "$ms" != "timeout" ]] && (( ms < best_ms )); then
            best_ms=$ms
            best_region=$r
        fi
    done
    if [[ -n "$best_region" ]]; then
        echo "$best_region"
    else
        return 1
    fi
}

apply_bam_switch() {
    local url="$1"
    if [[ ! -S "$ADMIN_RPC" ]]; then
        log "ERROR: Admin RPC socket not found at $ADMIN_RPC"
        return 1
    fi
    local resp
    resp=$(echo "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"setBamUrl\",\"params\":[\"${url}\"]}" \
        | socat - UNIX-CONNECT:"$ADMIN_RPC" 2>&1)
    local rc=$?
    if (( rc != 0 )); then
        log "ERROR: socat failed (rc=$rc): $resp"
        return 1
    fi
    if echo "$resp" | grep -q '"error"'; then
        log "ERROR: setBamUrl returned error: $resp"
        return 1
    fi
    debug "setBamUrl response: $resp"
    return 0
}

do_failover() {
    local current_region="$1"
    log "Initiating failover from $current_region..."

    local best_region
    best_region=$(select_best_region "$current_region") || {
        log "ERROR: No reachable alternative BAM regions"
        send_discord "BAM Failover Failed" \
            "No reachable alternative BAM regions.\nCurrent: **${current_region}**\nAll regions unreachable." \
            16711680  # Red
        return 1
    }

    local new_url
    new_url=$(make_bam_url "$best_region")
    log "Best alternative: $best_region — switching to $new_url"

    if apply_bam_switch "$new_url"; then
        write_state_file "on_fallback" "true"
        write_state_file "fallback_region" "$best_region"
        write_state_file "fail_count" "0"
        write_state_file "preferred_healthy_count" "0"
        write_state_file "last_failover_time" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        log "Failover complete: $current_region -> $best_region"
        send_discord "BAM Failover: ${current_region} -> ${best_region}" \
            "Switched BAM node due to repeated errors.\n**From:** ${current_region}\n**To:** ${best_region}\n**Time:** $(date -u '+%Y-%m-%d %H:%M:%S UTC')" \
            16744448  # Orange
        return 0
    else
        log "ERROR: Failed to apply BAM URL switch"
        send_discord "BAM Failover Failed" \
            "Could not switch BAM node via admin RPC.\n**From:** ${current_region}\n**To:** ${best_region} (attempted)" \
            16711680  # Red
        return 1
    fi
}

check_recovery() {
    local preferred_region="$1"
    local ms
    ms=$(ping_bam_region "$preferred_region")
    debug "Recovery ping to $preferred_region: ${ms}"

    if [[ "$ms" == "timeout" ]]; then
        debug "Preferred region $preferred_region still unreachable"
        write_state_file "preferred_healthy_count" "0"
        return 1
    fi

    local count
    count=$(read_state_file "preferred_healthy_count" "0")
    count=$(( count + 1 ))
    write_state_file "preferred_healthy_count" "$count"
    debug "Preferred region $preferred_region healthy ($count/$RECOVERY_THRESHOLD)"

    if (( count >= RECOVERY_THRESHOLD )); then
        local new_url
        new_url=$(make_bam_url "$preferred_region")
        log "Recovery: switching back to preferred region $preferred_region"

        local fallback_region
        fallback_region=$(read_state_file "fallback_region" "unknown")

        if apply_bam_switch "$new_url"; then
            write_state_file "on_fallback" "false"
            rm -f "$FAILOVER_DIR/fallback_region" "$FAILOVER_DIR/preferred_healthy_count" "$FAILOVER_DIR/fail_count"
            log "Recovery complete: back to $preferred_region"
            send_discord "BAM Recovery: Back to ${preferred_region}" \
                "Preferred BAM node is healthy again.\n**Restored:** ${preferred_region}\n**Was on:** ${fallback_region}\n**Time:** $(date -u '+%Y-%m-%d %H:%M:%S UTC')" \
                5763719  # Green
            return 0
        else
            log "ERROR: Failed to switch back to preferred region"
            return 1
        fi
    fi
    return 1
}

scan_bam() {
    check_daily_reset

    if [[ ! -f "$VALIDATOR_LOG" ]]; then
        log "WARNING: Validator log not found at $VALIDATOR_LOG"
        return
    fi

    local offset_file="$OFFSET_DIR/validator_log"
    local hash_conn="$HASH_DIR/bam_connection"
    local hash_metric="$HASH_DIR/bam_metric"

    init_offset "$VALIDATOR_LOG" "$offset_file"

    local new_content
    new_content=$(read_new_content "$VALIDATOR_LOG" "$offset_file")

    if [[ -z "$new_content" ]]; then
        debug "No new content in validator.log"
        return
    fi

    # BAM connection errors
    local conn_errors
    conn_errors=$(echo "$new_content" | grep -E "$BAM_CONNECTION_PATTERN" || true)

    if [[ -n "$conn_errors" ]]; then
        local unique_conn
        unique_conn=$(echo "$conn_errors" | dedup_errors "$hash_conn")

        if [[ -n "$unique_conn" ]]; then
            local count
            count=$(echo "$unique_conn" | wc -l)
            log "Found $count unique BAM connection error(s)"

            local samples
            samples=$(echo "$unique_conn" | head -5 | while IFS= read -r line; do
                if (( ${#line} > 200 )); then
                    echo "${line:0:200}..."
                else
                    echo "$line"
                fi
            done)

            local title="BAM Connection Errors ($count new)"
            local desc
            desc=$(printf '```\n%s\n```' "$samples")
            if (( count > 5 )); then
                desc+=$'\n'"_(showing 5 of $count errors)_"
            fi

            send_discord "$title" "$desc" 16711680  # Red
            sleep 2
        fi
    fi

    # BAM metric anomalies
    local metric_errors
    metric_errors=$(echo "$new_content" | grep -E "$BAM_METRIC_PATTERN" || true)

    if [[ -n "$metric_errors" ]]; then
        local unique_metrics
        unique_metrics=$(echo "$metric_errors" | dedup_errors "$hash_metric")

        if [[ -n "$unique_metrics" ]]; then
            local count
            count=$(echo "$unique_metrics" | wc -l)
            log "Found $count unique BAM metric anomalie(s)"

            local samples
            samples=$(echo "$unique_metrics" | head -5 | while IFS= read -r line; do
                if (( ${#line} > 200 )); then
                    echo "${line:0:200}..."
                else
                    echo "$line"
                fi
            done)

            local title="BAM Metric Anomalies ($count new)"
            local desc
            desc=$(printf '```\n%s\n```' "$samples")
            if (( count > 5 )); then
                desc+=$'\n'"_(showing 5 of $count anomalies)_"
            fi

            send_discord "$title" "$desc" 16744448  # Orange (0xFF9900)
            sleep 2
        fi
    fi

    # ── Failover logic ──────────────────────────────────────────────────
    local has_errors=false
    [[ -n "$conn_errors" || -n "$metric_errors" ]] && has_errors=true

    local preferred_region current_region
    preferred_region=$(get_preferred_region) || {
        debug "Could not determine preferred region from $VALIDATOR_SH"
        return
    }
    current_region=$(get_current_region) || {
        debug "Validator not running or --bam-url not set, skipping failover logic"
        return
    }

    local on_fallback
    on_fallback=$(read_state_file "on_fallback" "false")

    # When on_fallback, use our state file as source of truth (ps shows stale argv)
    local effective_current="$current_region"
    if [[ "$on_fallback" == "true" ]]; then
        local fb_region
        fb_region=$(read_state_file "fallback_region" "")
        [[ -n "$fb_region" ]] && effective_current="$fb_region"
    fi

    debug "Failover state: preferred=$preferred_region current=$effective_current on_fallback=$on_fallback has_errors=$has_errors"

    # Detect manual switch-back (operator restarted validator, ps now shows preferred)
    if [[ "$on_fallback" == "true" && "$current_region" == "$preferred_region" ]]; then
        log "Manual switch-back detected: now on preferred region $preferred_region"
        write_state_file "on_fallback" "false"
        rm -f "$FAILOVER_DIR/fallback_region" "$FAILOVER_DIR/preferred_healthy_count" "$FAILOVER_DIR/fail_count"
        return
    fi

    if [[ "$on_fallback" == "true" ]]; then
        # We're on a fallback node
        if $has_errors; then
            # Fallback node is also having issues
            local fail_count
            fail_count=$(read_state_file "fail_count" "0")
            fail_count=$(( fail_count + 1 ))
            write_state_file "fail_count" "$fail_count"
            write_state_file "preferred_healthy_count" "0"
            debug "Fallback node errors: fail_count=$fail_count/$FAIL_THRESHOLD"

            if (( fail_count >= FAIL_THRESHOLD )); then
                log "Fallback node $effective_current also failing, attempting secondary failover"
                do_failover "$effective_current"
            fi
        else
            # Fallback node is healthy — check if preferred has recovered
            check_recovery "$preferred_region"
        fi
    else
        # We're on the preferred node
        if $has_errors; then
            local fail_count
            fail_count=$(read_state_file "fail_count" "0")
            fail_count=$(( fail_count + 1 ))
            write_state_file "fail_count" "$fail_count"
            debug "Preferred node errors: fail_count=$fail_count/$FAIL_THRESHOLD"

            if (( fail_count >= FAIL_THRESHOLD )); then
                do_failover "$effective_current"
            fi
        else
            # All good — reset counter
            local prev_count
            prev_count=$(read_state_file "fail_count" "0")
            if (( prev_count > 0 )); then
                debug "Resetting fail_count (was $prev_count)"
                write_state_file "fail_count" "0"
            fi
        fi
    fi
}

if $ONCE; then
    log "Running single BAM scan..."
    scan_bam
    log "BAM scan complete."
else
    log "Starting continuous BAM monitoring (interval: ${LOOP_INTERVAL}s)..."
    while true; do
        scan_bam
        sleep "$LOOP_INTERVAL"
    done
fi
