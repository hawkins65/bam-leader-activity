#!/usr/bin/env bash
set -uo pipefail
trap '' PIPE

###############################################################################
# Leader Slot Capture Monitor
#
# Monitors the leader schedule for upcoming leader slots and automatically
# enables bundle transaction DEBUG capture around each leader rotation.
#
# Features:
#   - Polls leader schedule frequently to handle slot timing drift
#   - Merges leader groups that are close together into one capture window
#   - Enables debug logging ~60s before first slot, disables ~60s after last
#   - Extracts bundle transaction signatures after each capture
#   - Reports results to Discord
#
# Usage:
#   ./leader-capture-monitor.sh [--once] [--verbose] [--dry-run]
#
#   --once      Run one capture cycle and exit
#   --verbose   Print debug output
#   --dry-run   Show what would happen without enabling/disabling logging
###############################################################################

# ── Configuration ─────────────────────────────────────────────────────────────

# Load RPC and identity from shared config
VALIDATOR_CONFIG="$HOME/.config/validator/rpc.conf"
if [[ ! -f "$VALIDATOR_CONFIG" ]]; then
    echo "ERROR: Validator config not found at $VALIDATOR_CONFIG" >&2
    exit 1
fi
# shellcheck source=/home/sol/.config/validator/rpc.conf
source "$VALIDATOR_CONFIG"

RPC_URL="${MAINNET_RPC_URL:?MAINNET_RPC_URL not set in $VALIDATOR_CONFIG}"
VALIDATOR_IDENTITY="${VALIDATOR_IDENTITY:?VALIDATOR_IDENTITY not set in $VALIDATOR_CONFIG}"

LEDGER_DIR="/mnt/ledger"
LOG_FILE="$HOME/logs/validator.log"
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
OUTPUT_DIR="$SCRIPT_DIR/captures"
STATE_DIR="$HOME/.log_monitor/leader_capture"

# Timing configuration
BUFFER_SECONDS=60           # Start capture this many seconds before first slot
BUFFER_AFTER_SECONDS=60     # Keep capturing this many seconds after last slot
MERGE_GAP_SECONDS=180       # Merge groups closer than this (3 minutes)
POLL_INTERVAL_FAR=60        # Poll interval when next slot is far away (>5 min)
POLL_INTERVAL_NEAR=30       # Poll interval when next slot is near (<5 min)
NEAR_THRESHOLD=300          # "Near" means within this many seconds (5 min)
MIN_SLEEP=5                 # Never sleep less than this

# Log filter settings
DEBUG_FILTER="solana=info,solana_core::bundle_stage=debug"
DEFAULT_FILTER="solana=info,agave=info"

# Discord
DISCORD_WEBHOOK="$(cat "$HOME/.config/discord/webhook" 2>/dev/null | tr -d '[:space:]')"
DISCORD_EMBED_SCRIPT="$HOME/999_discord_embed.sh"
BOT_USERNAME="Leader Capture Monitor"
SCRIPT_PATH="$(hostname):$(readlink -f "${BASH_SOURCE[0]}")"

# ── CLI flags ─────────────────────────────────────────────────────────────────

ONCE=false
VERBOSE=false
DRY_RUN=false

usage() {
    echo "Usage: $0 [--once] [--verbose] [--dry-run]"
    echo "  --once     Run one capture cycle and exit"
    echo "  --verbose  Print debug output"
    echo "  --dry-run  Show what would happen without changing log levels"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --once)    ONCE=true; shift ;;
        --verbose) VERBOSE=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --help|-h) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }
debug() { $VERBOSE && log "DEBUG: $*"; }

mkdir -p "$OUTPUT_DIR" "$STATE_DIR"

# Source Discord embed helper
if [[ -f "$DISCORD_EMBED_SCRIPT" ]]; then
    # shellcheck source=/home/sol/999_discord_embed.sh
    source "$DISCORD_EMBED_SCRIPT"
else
    log "WARNING: Discord embed script not found at $DISCORD_EMBED_SCRIPT"
fi

send_discord() {
    local title="$1"
    local description="$2"
    local severity="${3:-info}"

    if [[ -z "$DISCORD_WEBHOOK" ]]; then
        log "WARNING: No Discord webhook configured"
        return 1
    fi

    # Convert newlines to \n literals for the embed script
    description="${description//$'\n'/\\n}"

    send_discord_embed "$DISCORD_WEBHOOK" "$severity" \
        "$title" "$description" \
        username="$BOT_USERNAME" \
        script_path="$SCRIPT_PATH" \
        pagerduty=false
}

duration_fmt() {
    local total_seconds=$1
    local hours=$(( total_seconds / 3600 ))
    local minutes=$(( (total_seconds % 3600) / 60 ))
    local seconds=$(( total_seconds % 60 ))

    if (( hours > 0 )); then
        printf '%dh %dm %ds' $hours $minutes $seconds
    elif (( minutes > 0 )); then
        printf '%dm %ds' $minutes $seconds
    else
        printf '%ds' $seconds
    fi
}

# ── Slot timing functions ────────────────────────────────────────────────────

get_slot_duration() {
    local samples
    samples=$(curl -s "$RPC_URL" -X POST -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","id":1,"method":"getRecentPerformanceSamples","params":[1]}')
    local num_slots sample_period
    num_slots=$(echo "$samples" | jq -r '.result[0].numSlots')
    sample_period=$(echo "$samples" | jq -r '.result[0].samplePeriodSecs')

    if [[ -z "$num_slots" || "$num_slots" == "null" || -z "$sample_period" || "$sample_period" == "null" ]]; then
        echo "0.000420"  # fallback ~420ms
        return 1
    fi

    echo "scale=6; $sample_period / $num_slots" | bc -l
}

get_current_slot() {
    solana -u "$RPC_URL" slot 2>/dev/null
}

# Get upcoming leader slot groups as merged capture windows.
# Output: one line per capture window with format:
#   first_slot last_slot num_groups
# where groups within MERGE_GAP_SECONDS are merged.
get_capture_windows() {
    local current_slot="$1"
    local slot_duration="$2"

    # Get upcoming leader slots
    local leader_slots
    leader_slots=$(solana -u "$RPC_URL" leader-schedule 2>/dev/null \
        | grep "$VALIDATOR_IDENTITY" \
        | awk '{print $1}' \
        | sort -n \
        | awk -v cs="$current_slot" '$1 > cs')

    if [[ -z "$leader_slots" ]]; then
        debug "No upcoming leader slots found"
        return 1
    fi

    # Group consecutive slots into leader groups, then merge close groups
    python3 -c "
import sys

slot_duration = float('$slot_duration')
merge_gap = int('$MERGE_GAP_SECONDS')
current_slot = int('$current_slot')

slots = [int(s) for s in '''$leader_slots'''.strip().split('\n') if s.strip()]
if not slots:
    sys.exit(1)

# Build consecutive groups
groups = []
group_start = slots[0]
group_end = slots[0]
for s in slots[1:]:
    if s == group_end + 1:
        group_end = s
    else:
        groups.append((group_start, group_end))
        group_start = s
        group_end = s
groups.append((group_start, group_end))

# Merge groups that are close together
merged = [groups[0]]
merge_count = [1]
for g_start, g_end in groups[1:]:
    prev_start, prev_end = merged[-1]
    gap_slots = g_start - prev_end
    gap_seconds = gap_slots * slot_duration
    if gap_seconds < merge_gap:
        merged[-1] = (prev_start, g_end)
        merge_count[-1] += 1
    else:
        merged.append((g_start, g_end))
        merge_count.append(1)

# Output: first_slot last_slot num_groups_merged
for i, (first, last) in enumerate(merged):
    print(f'{first} {last} {merge_count[i]}')
" 2>/dev/null
}

# ── Capture logic ─────────────────────────────────────────────────────────────

enable_debug_logging() {
    if $DRY_RUN; then
        log "[DRY-RUN] Would enable debug logging: $DEBUG_FILTER"
        return 0
    fi
    log "Enabling DEBUG logging for bundle_stage..."
    if agave-validator -l "$LEDGER_DIR" set-log-filter "$DEBUG_FILTER" 2>/dev/null; then
        log "Log filter set to: $DEBUG_FILTER"
        return 0
    else
        log "ERROR: Failed to set log filter"
        return 1
    fi
}

disable_debug_logging() {
    if $DRY_RUN; then
        log "[DRY-RUN] Would restore default logging: $DEFAULT_FILTER"
        return 0
    fi
    log "Restoring default log filter..."
    if agave-validator -l "$LEDGER_DIR" set-log-filter "$DEFAULT_FILTER" 2>/dev/null; then
        log "Log filter restored to: $DEFAULT_FILTER"
        return 0
    else
        log "WARNING: Failed to restore default log filter"
        return 1
    fi
}

extract_and_report() {
    local capture_start_time="$1"
    local capture_end_time="$2"
    local first_slot="$3"
    local last_slot="$4"
    local num_groups="$5"

    local timestamp
    timestamp=$(date -u +"%Y%m%d_%H%M%S")
    local text_file="$OUTPUT_DIR/bundle_txns_${timestamp}.txt"
    local json_file="$OUTPUT_DIR/bundle_txns_${timestamp}.json"

    log "Extracting bundle transaction signatures..."

    if $DRY_RUN; then
        log "[DRY-RUN] Would extract signatures and report to Discord"
        return 0
    fi

    # Run the extraction script
    "$SCRIPT_DIR/bundle-txn-signatures.py" "$LOG_FILE" > "$text_file" 2>&1
    "$SCRIPT_DIR/bundle-txn-signatures.py" "$LOG_FILE" --json > "$json_file" 2>&1

    # Parse summary from output
    local total_bundles total_txns avg_txns
    total_bundles=$(grep -oP 'Total bundles processed: \K[\d,]+' "$text_file" 2>/dev/null | tr -d ',')
    total_txns=$(grep -oP 'Total transactions: \K[\d,]+' "$text_file" 2>/dev/null | tr -d ',')
    avg_txns=$(grep -oP 'Avg transactions per bundle: \K[\d.]+' "$text_file" 2>/dev/null)

    # Parse results breakdown
    local success_count
    success_count=$(grep -oP 'success: \K[\d,]+' "$text_file" 2>/dev/null | tr -d ',')

    total_bundles="${total_bundles:-0}"
    total_txns="${total_txns:-0}"
    avg_txns="${avg_txns:-0}"
    success_count="${success_count:-0}"

    local capture_duration=$(( capture_end_time - capture_start_time ))
    local slot_range="${first_slot}–${last_slot}"
    local total_slots=$(( last_slot - first_slot + 1 ))

    # Build Discord message
    local severity="info"
    if (( total_bundles == 0 )); then
        severity="warning"
    fi

    local group_label="rotation"
    if (( num_groups > 1 )); then
        group_label="${num_groups} rotations"
    fi

    local desc=""
    desc+="**Slots:** ${slot_range} (${total_slots} slots across ${group_label})"
    desc+=$'\n'"**Capture window:** $(duration_fmt $capture_duration)"
    desc+=$'\n'"**Bundles:** ${total_bundles} | **Transactions:** ${total_txns}"
    if (( total_bundles > 0 )); then
        desc+=$'\n'"**Avg txns/bundle:** ${avg_txns} | **Successful:** ${success_count}"
    fi
    desc+=$'\n'"**Output:** ${text_file}"

    local title="Bundle Capture Complete"
    if (( total_bundles == 0 )); then
        title="Bundle Capture — No Bundles Found"
    fi

    send_discord "$title" "$desc" "$severity"
    log "Discord notification sent"

    # Log summary locally
    log "Capture summary:"
    log "  Slots: $slot_range ($total_slots slots, $group_label)"
    log "  Duration: $(duration_fmt $capture_duration)"
    log "  Bundles: $total_bundles | Transactions: $total_txns"
    log "  Output: $text_file"
}

# ── Main loop ─────────────────────────────────────────────────────────────────

run_capture_cycle() {
    log "Checking leader schedule..."

    local slot_duration
    slot_duration=$(get_slot_duration)
    debug "Slot duration: ${slot_duration}s"

    local current_slot
    current_slot=$(get_current_slot)
    if [[ -z "$current_slot" ]]; then
        log "ERROR: Could not get current slot"
        return 1
    fi
    debug "Current slot: $current_slot"

    # Get the next capture window (first line = nearest)
    local windows
    windows=$(get_capture_windows "$current_slot" "$slot_duration")
    if [[ -z "$windows" ]]; then
        log "No upcoming leader slots found in this epoch"
        return 1
    fi

    local first_slot last_slot num_groups
    read -r first_slot last_slot num_groups <<< "$(echo "$windows" | head -1)"
    debug "Next capture window: slots $first_slot-$last_slot ($num_groups group(s) merged)"

    # Calculate time until the capture window
    local slots_until_start=$(( first_slot - current_slot ))
    local seconds_until_start
    seconds_until_start=$(echo "$slots_until_start * $slot_duration" | bc | cut -d. -f1)

    local capture_start_offset=$(( seconds_until_start - BUFFER_SECONDS ))

    local total_leader_slots=$(( last_slot - first_slot + 1 ))
    local leader_duration_seconds
    leader_duration_seconds=$(echo "$total_leader_slots * $slot_duration" | bc | cut -d. -f1)

    local capture_total_seconds=$(( BUFFER_SECONDS + leader_duration_seconds + BUFFER_AFTER_SECONDS ))

    log "Next leader window: slots $first_slot-$last_slot ($num_groups group(s), $(duration_fmt $leader_duration_seconds))"
    log "Capture starts in ~$(duration_fmt $capture_start_offset) (with ${BUFFER_SECONDS}s pre-buffer)"
    log "Total capture duration: ~$(duration_fmt $capture_total_seconds)"

    if (( num_groups > 1 )); then
        log "Merged $num_groups nearby leader rotations into single capture window"
    fi

    # ── Wait phase: re-check timing frequently to handle drift ────────────
    while true; do
        current_slot=$(get_current_slot)
        if [[ -z "$current_slot" ]]; then
            log "WARNING: Could not get current slot, retrying..."
            sleep "$MIN_SLEEP"
            continue
        fi

        # Recalculate slot duration periodically for accuracy
        slot_duration=$(get_slot_duration)

        # Recalculate windows to handle drift and possible schedule changes
        windows=$(get_capture_windows "$current_slot" "$slot_duration")
        if [[ -z "$windows" ]]; then
            log "Leader slots no longer found (epoch boundary?). Restarting cycle."
            return 1
        fi

        read -r first_slot last_slot num_groups <<< "$(echo "$windows" | head -1)"

        slots_until_start=$(( first_slot - current_slot ))
        seconds_until_start=$(echo "$slots_until_start * $slot_duration" | bc | cut -d. -f1)
        capture_start_offset=$(( seconds_until_start - BUFFER_SECONDS ))

        debug "Drift check: $slots_until_start slots away (~$(duration_fmt $seconds_until_start)), capture in ~$(duration_fmt $capture_start_offset)"

        # If it's time to start the capture
        if (( capture_start_offset <= 0 )); then
            log "Capture window reached! Starting capture."
            break
        fi

        # Adaptive sleep: shorter when close, longer when far
        local sleep_time
        if (( capture_start_offset < NEAR_THRESHOLD )); then
            sleep_time=$POLL_INTERVAL_NEAR
        else
            sleep_time=$POLL_INTERVAL_FAR
        fi

        # Don't sleep longer than the time until capture
        if (( sleep_time > capture_start_offset )); then
            sleep_time=$capture_start_offset
        fi
        if (( sleep_time < MIN_SLEEP )); then
            sleep_time=$MIN_SLEEP
        fi

        debug "Sleeping ${sleep_time}s before next drift check"
        sleep "$sleep_time"
    done

    # ── Capture phase ─────────────────────────────────────────────────────

    local slots_str="${first_slot}–${last_slot}"
    local group_label="rotation"
    (( num_groups > 1 )) && group_label="${num_groups} rotations"

    local capture_start_time
    capture_start_time=$(date +%s)

    # Enable debug logging
    if ! enable_debug_logging; then
        send_discord "Bundle Capture Failed" \
            "Could not enable DEBUG logging for bundle_stage.\n**Slots:** ${slots_str}" \
            "error"
        return 1
    fi

    # Record the log file size at capture start (for targeted extraction later)
    local log_offset_start
    log_offset_start=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    echo "$log_offset_start" > "$STATE_DIR/capture_log_offset"

    # Wait through the leader slots + post-buffer, re-checking for drift
    # to extend the window if the group's end drifts later
    while true; do
        current_slot=$(get_current_slot)
        if [[ -z "$current_slot" ]]; then
            sleep "$MIN_SLEEP"
            continue
        fi

        # Check if we've passed the last slot + buffer
        local slots_past_end=$(( current_slot - last_slot ))
        if (( slots_past_end > 0 )); then
            slot_duration=$(get_slot_duration)
            local seconds_past
            seconds_past=$(echo "$slots_past_end * $slot_duration" | bc | cut -d. -f1)

            if (( seconds_past >= BUFFER_AFTER_SECONDS )); then
                log "Post-buffer complete (${seconds_past}s past last slot)"
                break
            fi

            debug "Past last slot by ${seconds_past}s, waiting for ${BUFFER_AFTER_SECONDS}s post-buffer"
        else
            debug "Still in leader window, $(( -slots_past_end )) slots remaining"
        fi

        sleep "$POLL_INTERVAL_NEAR"
    done

    # Disable debug logging
    disable_debug_logging

    local capture_end_time
    capture_end_time=$(date +%s)

    # ── Extract and report ────────────────────────────────────────────────

    extract_and_report "$capture_start_time" "$capture_end_time" \
        "$first_slot" "$last_slot" "$num_groups"
}

# ── Entry point ───────────────────────────────────────────────────────────────

log "Leader Capture Monitor starting"
log "  Validator: $VALIDATOR_IDENTITY"
log "  RPC: ${RPC_URL%%://*}://***${RPC_URL##*/}"
log "  Buffer: ${BUFFER_SECONDS}s before / ${BUFFER_AFTER_SECONDS}s after"
log "  Merge gap: ${MERGE_GAP_SECONDS}s (groups closer than this are merged)"
log "  Dry-run: $DRY_RUN"

if $ONCE; then
    run_capture_cycle
    log "Single capture cycle complete."
else
    while true; do
        if run_capture_cycle; then
            log "Capture cycle complete. Checking for next window..."
        else
            log "No capture window available. Rechecking in ${POLL_INTERVAL_FAR}s..."
            sleep "$POLL_INTERVAL_FAR"
        fi
    done
fi
