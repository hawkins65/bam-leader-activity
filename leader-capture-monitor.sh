#!/usr/bin/env bash
set -u

# Redirect all output to log file (avoids systemd StandardOutput=append: FD
# inheritance issues that cause bash wait/pipefail to deadlock in subshells)
LOG_FILE="${HOME}/logs/leader-capture-monitor.log"
exec >> "$LOG_FILE" 2>&1

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

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
OUTPUT_DIR="$SCRIPT_DIR/captures"

# Timing configuration
BUFFER_AFTER_SECONDS=60     # Wait this long after last slot before querying RPC
MERGE_GAP_SECONDS=180       # Merge groups closer than this (3 minutes)
POLL_INTERVAL_FAR=60        # Poll interval when next slot is far away (>5 min)
POLL_INTERVAL_NEAR=30       # Poll interval when next slot is near (<5 min)
NEAR_THRESHOLD=300          # "Near" means within this many seconds (5 min)
MIN_SLEEP=5                 # Never sleep less than this

# RPC-based extraction (BAM bundles don't produce debug logs)
SLOT_TRANSACTIONS_SCRIPT="$SCRIPT_DIR/slot-transactions.py"

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

mkdir -p "$OUTPUT_DIR"

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

rpc_call() {
    curl -s --max-time 10 "$RPC_URL" -X POST -H "Content-Type: application/json" -d "$1"
}

get_slot_duration() {
    local result
    result=$(rpc_call '{"jsonrpc":"2.0","id":1,"method":"getRecentPerformanceSamples","params":[1]}')
    local dur
    dur=$(echo "$result" | jq -r 'if .result[0] then (.result[0].samplePeriodSecs / .result[0].numSlots | tostring) else empty end' 2>/dev/null)
    echo "${dur:-0.000420}"
}

get_current_slot() {
    local result
    result=$(rpc_call '{"jsonrpc":"2.0","id":1,"method":"getSlot","params":[{"commitment":"confirmed"}]}')
    echo "$result" | jq -r '.result // empty' 2>/dev/null
}

# Get upcoming leader slot groups as merged capture windows.
# Uses getLeaderSchedule + getEpochInfo via RPC, processes with jq.
# Output: one line per window: "first_slot last_slot num_groups"
get_capture_windows() {
    local current_slot="$1"
    local slot_duration="$2"

    # Get epoch start slot (leader schedule returns offsets from epoch start)
    local epoch_info epoch_start
    epoch_info=$(rpc_call '{"jsonrpc":"2.0","id":1,"method":"getEpochInfo","params":[{"commitment":"confirmed"}]}')
    epoch_start=$(echo "$epoch_info" | jq -r '.result | .absoluteSlot - .slotIndex' 2>/dev/null)

    if [[ -z "$epoch_start" || "$epoch_start" == "null" ]]; then
        debug "Could not get epoch info"
        return 1
    fi

    local result
    result=$(rpc_call "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getLeaderSchedule\",\"params\":[null,{\"identity\":\"$VALIDATOR_IDENTITY\"}]}")

    echo "$result" | jq -r --argjson cs "$current_slot" --argjson es "$epoch_start" \
        --argjson sd "$slot_duration" --argjson mg "$MERGE_GAP_SECONDS" \
        --arg id "$VALIDATOR_IDENTITY" '
        .result[$id] // empty
        | map(. + $es)
        | map(select(. > $cs))
        | sort
        | if length == 0 then empty else
            # Group consecutive slots
            reduce .[] as $s ([];
                if length == 0 then [[($s), ($s)]]
                elif (.[length-1][1] + 1) == $s then .[length-1][1] = $s
                else . + [[($s), ($s)]]
                end
            )
            # Merge groups closer than merge_gap seconds
            | reduce .[] as $g ([];
                if length == 0 then [$g]
                elif (($g[0] - .[length-1][1]) * $sd) < $mg then .[length-1] = [.[length-1][0], $g[1]]
                else . + [$g]
                end
            )
            | .[]
            | "\(.[0]) \(.[1]) 1"
          end
    ' 2>/dev/null
}

# ── Capture logic ─────────────────────────────────────────────────────────────


extract_and_report() {
    local capture_start_time="$1"
    local capture_end_time="$2"
    local first_slot="$3"
    local last_slot="$4"
    local num_groups="$5"

    local timestamp
    timestamp=$(date -u +"%Y%m%d_%H%M%S")
    local text_file="$OUTPUT_DIR/slot_txns_${timestamp}.txt"
    local json_file="$OUTPUT_DIR/slot_txns_${timestamp}.json"

    log "Querying RPC for leader slot transactions..."

    if $DRY_RUN; then
        log "[DRY-RUN] Would query slots $first_slot–$last_slot and report to Discord"
        return 0
    fi

    # Query RPC for block data from our leader slots (stderr has progress, keep it separate)
    "$SLOT_TRANSACTIONS_SCRIPT" --slots "$first_slot" "$last_slot" > "$text_file" 2>/dev/null
    "$SLOT_TRANSACTIONS_SCRIPT" --slots "$first_slot" "$last_slot" --json > "$json_file" 2>/dev/null

    # Parse summary from JSON output
    local total_txns success_count failed_count total_fees_sol skipped_slots
    total_txns=$(python3 -c "import json; d=json.load(open('$json_file')); print(d['summary']['total_non_vote_transactions'])" 2>/dev/null)
    success_count=$(python3 -c "import json; d=json.load(open('$json_file')); print(d['summary']['successful'])" 2>/dev/null)
    failed_count=$(python3 -c "import json; d=json.load(open('$json_file')); print(d['summary']['failed'])" 2>/dev/null)
    total_fees_sol=$(python3 -c "import json; d=json.load(open('$json_file')); print(f\"{d['summary']['total_fees_sol']:.6f}\")" 2>/dev/null)
    skipped_slots=$(python3 -c "import json; d=json.load(open('$json_file')); print(d['summary']['skipped_slots'])" 2>/dev/null)

    total_txns="${total_txns:-0}"
    success_count="${success_count:-0}"
    failed_count="${failed_count:-0}"
    total_fees_sol="${total_fees_sol:-0}"
    skipped_slots="${skipped_slots:-0}"

    local capture_duration=$(( capture_end_time - capture_start_time ))
    local slot_range="${first_slot}–${last_slot}"
    local total_slots=$(( last_slot - first_slot + 1 ))
    local produced_slots=$(( total_slots - skipped_slots ))

    local group_label="rotation"
    if (( num_groups > 1 )); then
        group_label="${num_groups} rotations"
    fi

    # Build Discord message
    local severity="info"
    if (( total_txns == 0 )); then
        severity="warning"
    fi

    local desc=""
    desc+="**Slots:** ${slot_range} (${total_slots} slots across ${group_label})"
    if (( skipped_slots > 0 )); then
        desc+=", ${skipped_slots} skipped"
    fi
    desc+=$'\n'"**Capture window:** $(duration_fmt $capture_duration)"
    desc+=$'\n'"**Transactions:** ${total_txns} (${success_count} success, ${failed_count} failed)"
    desc+=$'\n'"**Fees earned:** ${total_fees_sol} SOL"
    desc+=$'\n'"**Output:** ${text_file}"

    local title="Leader Slot Report"
    if (( total_txns == 0 )); then
        title="Leader Slot Report — No Transactions"
    fi

    send_discord "$title" "$desc" "$severity"
    log "Discord notification sent"

    # Log summary locally
    log "Capture summary:"
    log "  Slots: $slot_range ($produced_slots produced, $skipped_slots skipped)"
    log "  Duration: $(duration_fmt $capture_duration)"
    log "  Transactions: $total_txns ($success_count success, $failed_count failed)"
    log "  Fees: $total_fees_sol SOL"
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

    # Calculate time until the leader window
    local slots_until_start=$(( first_slot - current_slot ))
    local seconds_until_start
    seconds_until_start=$(printf '%.0f' "$(echo "$slots_until_start * $slot_duration" | bc)")

    local total_leader_slots=$(( last_slot - first_slot + 1 ))
    local leader_duration_seconds
    leader_duration_seconds=$(printf '%.0f' "$(echo "$total_leader_slots * $slot_duration" | bc)")

    log "Next leader window: slots $first_slot-$last_slot ($num_groups group(s), $(duration_fmt $leader_duration_seconds))"
    log "Leader slots start in ~$(duration_fmt $seconds_until_start)"

    if (( num_groups > 1 )); then
        log "Merged $num_groups nearby leader rotations into single capture window"
    fi

    # ── Wait phase: sleep until leader slots arrive ─────────────────────
    # Target window is locked in — don't re-query or we'll skip past it
    while true; do
        current_slot=$(get_current_slot)
        if [[ -z "$current_slot" ]]; then
            log "WARNING: Could not get current slot, retrying..."
            sleep "$MIN_SLEEP"
            continue
        fi

        slots_until_start=$(( first_slot - current_slot ))

        # If leader slots have arrived (or passed)
        if (( slots_until_start <= 0 )); then
            log "Leader slots reached! (current=$current_slot, target=$first_slot)"
            break
        fi

        slot_duration=$(get_slot_duration)
        seconds_until_start=$(printf '%.0f' "$(echo "$slots_until_start * $slot_duration" | bc)")

        debug "Drift check: $slots_until_start slots away (~$(duration_fmt $seconds_until_start))"

        # Adaptive sleep: faster when close
        local sleep_time
        if (( seconds_until_start < 30 )); then
            sleep_time=$MIN_SLEEP
        elif (( seconds_until_start < NEAR_THRESHOLD )); then
            sleep_time=$POLL_INTERVAL_NEAR
        else
            sleep_time=$POLL_INTERVAL_FAR
        fi

        # Don't sleep longer than the time until leader slots
        if (( sleep_time > seconds_until_start )); then
            sleep_time=$seconds_until_start
        fi
        if (( sleep_time < MIN_SLEEP )); then
            sleep_time=$MIN_SLEEP
        fi

        debug "Sleeping ${sleep_time}s before next drift check"
        sleep "$sleep_time"
    done

    # ── Wait for slots to pass ───────────────────────────────────────────

    local slots_str="${first_slot}–${last_slot}"
    local group_label="rotation"
    (( num_groups > 1 )) && group_label="${num_groups} rotations"

    local capture_start_time
    capture_start_time=$(date +%s)

    log "Waiting for leader slots to complete..."

    # Wait through the leader slots + post-buffer for blocks to finalize
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
            seconds_past=$(printf '%.0f' "$(echo "$slots_past_end * $slot_duration" | bc)")

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

    local capture_end_time
    capture_end_time=$(date +%s)

    # ── Extract and report ────────────────────────────────────────────────

    extract_and_report "$capture_start_time" "$capture_end_time" \
        "$first_slot" "$last_slot" "$num_groups"
}

# ── Entry point ───────────────────────────────────────────────────────────────

log "Leader Capture Monitor starting (RPC mode)"
log "  Validator: $VALIDATOR_IDENTITY"
log "  RPC: ${RPC_URL%%://*}://***${RPC_URL##*/}"
log "  Post-slot buffer: ${BUFFER_AFTER_SECONDS}s (wait for block finalization)"
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
