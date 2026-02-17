#!/usr/bin/env bash
#
# bam-log-monitor.sh — BAM connection & bundle error monitor with Discord alerting
# Monitors validator.log for BAM-specific errors: connection failures, scheduler
# issues, auth problems, unhealthy connections, and outbound failures.
# Uses byte offset tracking to only process new log content each run.
#
# Usage:
#   ./bam-log-monitor.sh --once        # Single scan (for cron)
#   ./bam-log-monitor.sh --verbose     # Single scan with debug output
#   ./bam-log-monitor.sh --reset       # Clear state and exit
#

set -uo pipefail
trap '' PIPE

# ── Configuration ──────────────────────────────────────────────────────────────
VALIDATOR_LOG="$HOME/logs/validator.log"
STATE_DIR="$HOME/.log_monitor/bam"
OFFSETS_DIR="$STATE_DIR/offsets"
HASHES_FILE="$STATE_DIR/alerted_errors.txt"
STATE_DATE_FILE="$STATE_DIR/state_date.txt"
DISCORD_WEBHOOK="$(cat "$HOME/.config/discord/webhook" 2>/dev/null | tr -d '[:space:]')"
if [[ -z "$DISCORD_WEBHOOK" ]]; then
    echo "ERROR: Discord webhook not found at ~/.config/discord/webhook" >&2
    exit 1
fi
BOT_USERNAME="Validator Log Monitor"
BOT_AVATAR="https://trillium.so/images/trillium-default.png"
HOSTNAME_STR=$(hostname)

# ── Flags ──────────────────────────────────────────────────────────────────────
VERBOSE=false
RESET=false

for arg in "$@"; do
    case "$arg" in
        --verbose) VERBOSE=true ;;
        --once)    ;;
        --reset)   RESET=true ;;
        *) echo "Unknown flag: $arg"; exit 1 ;;
    esac
done

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
vlog() { $VERBOSE && log "[DEBUG] $*" || true; }

# ── State management ──────────────────────────────────────────────────────────
mkdir -p "$STATE_DIR" "$OFFSETS_DIR"

if $RESET; then
    rm -f "$HASHES_FILE" "$STATE_DATE_FILE"
    rm -f "$OFFSETS_DIR"/*
    log "BAM monitor state reset complete."
    exit 0
fi

TODAY=$(date '+%Y-%m-%d')
if [[ -f "$STATE_DATE_FILE" ]]; then
    LAST_DATE=$(cat "$STATE_DATE_FILE")
    if [[ "$LAST_DATE" != "$TODAY" ]]; then
        log "New day detected ($LAST_DATE -> $TODAY), resetting BAM monitor state."
        rm -f "$HASHES_FILE"
        rm -f "$OFFSETS_DIR"/*
    fi
fi
echo "$TODAY" > "$STATE_DATE_FILE"
touch "$HASHES_FILE"

# ── BAM-specific error patterns ───────────────────────────────────────────────
# Connection state errors
BAM_ERROR_PATTERN='BAM connection lost|BAM connection not healthy|Failed to connect to BAM|Failed to start scheduler stream|Failed to prepare auth response|Failed to send initial auth proof|Failed to get auth challenge|Inbound stream closed|Failed to receive message from inbound stream|Failed to get config|Received unsupported versioned message|BAM Manager: timed out waiting for new identity'

# Metric anomalies: unhealthy_connection_count > 0 or outbound_fail > 0 or scheduler_fail > 0
# We handle these separately via the Python dedup helper
BAM_METRIC_ANOMALY_PATTERN='unhealthy_connection_count=[1-9]|outbound_fail=[1-9]|bundle_forward_to_scheduler_fail=[1-9]'

# Combined pattern
COMBINED_PATTERN="$BAM_ERROR_PATTERN|$BAM_METRIC_ANOMALY_PATTERN"

# ── Functions ──────────────────────────────────────────────────────────────────

get_offset() {
    local file_key="$1"
    local offset_file="$OFFSETS_DIR/$file_key"
    if [[ -f "$offset_file" ]]; then
        cat "$offset_file"
    else
        echo 0
    fi
}

save_offset() {
    echo "$2" > "$OFFSETS_DIR/$1"
}

normalize_and_hash() {
    python3 -c "
import sys, hashlib, re

seen = set()
try:
    with open('$HASHES_FILE', 'r') as f:
        seen = set(line.strip() for line in f)
except FileNotFoundError:
    pass

new_hashes = []
new_lines = []

for line in sys.stdin:
    line = line.rstrip('\n')
    # Normalize: strip timestamps, IPs, slot numbers, large numbers, hash values
    norm = re.sub(r'\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^ ]*\]?', '', line)
    norm = re.sub(r'\b\d+\.\d+\.\d+\.\d+(:\d+)?', 'ADDR', norm)
    norm = re.sub(r'slot=\d+', 'slot=N', norm)
    norm = re.sub(r'url: [^:]+:', 'url: HOST:', norm)
    norm = re.sub(r'\d{5,}', 'N', norm)
    norm = ' '.join(norm.split())

    h = hashlib.md5(norm.encode()).hexdigest()
    if h not in seen:
        seen.add(h)
        new_hashes.append(h)
        new_lines.append(line)

if new_hashes:
    with open('$HASHES_FILE', 'a') as f:
        for h in new_hashes:
            f.write(h + '\n')

for line in new_lines:
    print(line)
"
}

send_discord_embed() {
    local error_count="$1"
    local sample_lines="$2"
    local category="$3"

    # Color by category
    local color
    case "$category" in
        connection) color=16711680 ;;  # red
        metrics)    color=16744192 ;;  # orange
        *)          color=16776960 ;;  # yellow
    esac

    local escaped_lines
    escaped_lines=$(printf '%s' "$sample_lines" | python3 -c "
import sys, json
text = sys.stdin.read()
if len(text) > 4000:
    text = text[:3997] + '...'
print(json.dumps(text)[1:-1])
" 2>/dev/null)

    local title="BAM Errors: ${category^} ($error_count new)"

    local payload
    payload=$(cat <<ENDJSON
{
  "username": "${BOT_USERNAME}",
  "avatar_url": "${BOT_AVATAR}",
  "embeds": [{
    "title": "${title}",
    "description": "\`\`\`\\n${escaped_lines}\\n\`\`\`",
    "color": ${color},
    "fields": [
      {"name": "New Errors", "value": "${error_count}", "inline": true},
      {"name": "Category", "value": "${category^}", "inline": true},
      {"name": "Host", "value": "${HOSTNAME_STR}", "inline": true}
    ],
    "footer": {"text": "BAM Log Monitor"},
    "timestamp": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  }]
}
ENDJSON
)

    vlog "Sending Discord notification: $title"

    local response
    response=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$DISCORD_WEBHOOK" 2>&1)

    if [[ "$response" == "204" || "$response" == "200" ]]; then
        vlog "Discord notification sent (HTTP $response)"
    else
        log "WARNING: Discord webhook returned HTTP $response"
    fi
}

# ── Main scan ──────────────────────────────────────────────────────────────────

main() {
    log "Starting BAM log error scan"

    if [[ ! -f "$VALIDATOR_LOG" ]]; then
        log "ERROR: Validator log not found at $VALIDATOR_LOG"
        exit 1
    fi

    local current_size
    current_size=$(stat -c%s "$VALIDATOR_LOG" 2>/dev/null || echo 0)
    local last_offset
    last_offset=$(get_offset "validator.log")

    # Handle rotation
    if [[ "$current_size" -lt "$last_offset" ]]; then
        vlog "Validator log appears rotated, resetting offset"
        last_offset=0
    fi

    if [[ "$current_size" -eq "$last_offset" ]]; then
        vlog "No new data in validator.log"
        save_offset "validator.log" "$current_size"
        log "Scan complete — no new data."
        return
    fi

    local bytes_to_read=$((current_size - last_offset))
    vlog "Reading $bytes_to_read new bytes (offset $last_offset -> $current_size)"

    # Extract new content and filter for BAM-related lines only
    local new_content
    new_content=$(tail -c +"$((last_offset + 1))" "$VALIDATOR_LOG" | head -c "$bytes_to_read")

    save_offset "validator.log" "$current_size"

    # ── Connection errors ──
    local conn_errors
    conn_errors=$(echo "$new_content" \
        | grep -E "$BAM_ERROR_PATTERN" 2>/dev/null \
        | normalize_and_hash || true)

    if [[ -n "$conn_errors" ]]; then
        local conn_count
        conn_count=$(echo "$conn_errors" | wc -l)
        log "Found $conn_count new BAM connection error(s)"

        local conn_sample
        conn_sample=$(echo "$conn_errors" | head -5 | while IFS= read -r line; do echo "${line:0:300}"; done)
        if [[ "$conn_count" -gt 5 ]]; then
            conn_sample+=$'\n'"... and $((conn_count - 5)) more"
        fi

        send_discord_embed "$conn_count" "$conn_sample" "connection"
        sleep 2
    fi

    # ── Metric anomalies (unhealthy/failures) ──
    local metric_errors
    metric_errors=$(echo "$new_content" \
        | grep -E "$BAM_METRIC_ANOMALY_PATTERN" 2>/dev/null \
        | normalize_and_hash || true)

    if [[ -n "$metric_errors" ]]; then
        local metric_count
        metric_count=$(echo "$metric_errors" | wc -l)
        log "Found $metric_count new BAM metric anomaly line(s)"

        local metric_sample
        metric_sample=$(echo "$metric_errors" | head -5 | while IFS= read -r line; do echo "${line:0:300}"; done)
        if [[ "$metric_count" -gt 5 ]]; then
            metric_sample+=$'\n'"... and $((metric_count - 5)) more"
        fi

        send_discord_embed "$metric_count" "$metric_sample" "metrics"
        sleep 2
    fi

    if [[ -z "$conn_errors" && -z "$metric_errors" ]]; then
        vlog "No new BAM errors found"
    fi

    log "Scan complete."
}

main
