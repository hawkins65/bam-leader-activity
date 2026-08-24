#!/usr/bin/env bash
set -u

# Redirect all output to log file (avoids systemd StandardOutput=append: FD
# inheritance issues that cause bash wait/pipefail to deadlock in subshells)
LOG_FILE="${HOME}/logs/leader-capture-monitor.log"
exec >> "$LOG_FILE" 2>&1

# Wait for the staked identity rather than exiting.
# role-gate.sh: 0 = staked/active, 1 = standby, 2 = error (validator still
# starting, admin RPC unreachable, ...). The previous
#     role-gate.sh || exit 0
# treated BOTH 1 and 2 as "quit", and this unit is Restart=always with
# RestartSec=10 -- so every standby host, and every validator startup, became a
# restart every 10s forever: ~8,205 restarts on ogden on 2026-07-28, and 199 in
# the 34 minutes between boot and promotion on 2026-08-05. It never latched
# FAILED (a 10s cycle is slower than the 2s needed to trip systemd's 10s/5
# burst limit), so no alert ever fired and `systemctl is-active` read healthy.
# Blocking here keeps exactly ONE idle process and starts work on promotion.
if ! /home/sol/bam-leader-activity/role-gate.sh >/dev/null 2>&1; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Not the staked identity - waiting for promotion (recheck every 60s)"
    while ! /home/sol/bam-leader-activity/role-gate.sh >/dev/null 2>&1; do
        sleep 60
    done
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Staked identity acquired - starting capture"
fi

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

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
OUTPUT_DIR="$SCRIPT_DIR/captures"
DAILY_LEDGER="$SCRIPT_DIR/daily_totals.jsonl"
# Day boundary: 18:15 America/Chicago. A capture's "central_day" is the
# label of the day-window it falls into (window runs 18:15 → next 18:14).
DAY_ROLLOVER_HHMM="1815"
DAY_TZ="America/Chicago"

# Detect mainnet vs testnet from ~/validator.sh (or $NETWORK override).
# Exported so child processes (slot-transactions.py) skip re-parsing.
# shellcheck source=detect-network.sh
source "$SCRIPT_DIR/detect-network.sh"
NETWORK="$(detect_network)" || exit 1
export NETWORK

case "$NETWORK" in
    mainnet)
        RPC_URL="${MAINNET_RPC_URL:?MAINNET_RPC_URL not set in $VALIDATOR_CONFIG}"
        ;;
    testnet)
        RPC_URL="${TESTNET_RPC_URL:?TESTNET_RPC_URL not set in $VALIDATOR_CONFIG}"
        ;;
esac
VALIDATOR_IDENTITY="${VALIDATOR_IDENTITY:?VALIDATOR_IDENTITY not set in $VALIDATOR_CONFIG}"

# Timing configuration
BUFFER_AFTER_SECONDS=60     # Wait this long after last slot before querying RPC
MERGE_GAP_SECONDS=180       # Merge groups closer than this (3 minutes)
MAX_ROTATIONS_PER_WINDOW=6  # Hard cap: never merge more than this many rotations
MAX_WINDOW_SECONDS=600      # Hard cap: refuse capture windows longer than this
POLL_INTERVAL_FAR=60        # Poll interval when next slot is far away (>5 min)
POLL_INTERVAL_NEAR=30       # Poll interval when next slot is near (<5 min)
NEAR_THRESHOLD=300          # "Near" means within this many seconds (5 min)
MIN_SLEEP=5                 # Never sleep less than this
WAIT_PROGRESS_INTERVAL=300  # Emit a progress log every N sec in the completion-wait loop

# RPC-based extraction (BAM bundles don't produce debug logs)
SLOT_TRANSACTIONS_SCRIPT="$SCRIPT_DIR/slot-transactions.py"
# BAM telemetry for the same window. slot-transactions.py is pure getBlock: it
# reports WHAT landed but nothing about whether BAM itself was healthy while we
# were leader, so a low-tip rotation is ambiguous - degraded BAM and a quiet
# market look identical. bam-leader-activity.py reads bam_connection-metrics,
# which is info-level and always emitted (NOT the dead bundle_stage debug path).
BAM_ACTIVITY_SCRIPT="$SCRIPT_DIR/bam-leader-activity.py"
BAM_MIN_SEND_RATE=95          # alert below this % of bundles forwarded
# How far BEFORE capture_start_time to let bam-leader-activity.py read. Our
# window is stamped when an RPC poll first sees current_slot >= first_slot,
# which trails the real leader period by poll granularity + RPC lag, while the
# rotation itself is only ~1.6s long - so capture_start_time is routinely a few
# seconds PAST the entire bundle burst. This is only a read bound: the script is
# given --leader-slots and pins the actual counting window to where our slots
# appear in the log, so widening this cannot pull in a neighbour's rotation.
BAM_LOOKBACK_SECONDS=300

# Vote cost. Our validator submits exactly one vote transaction per slot and a
# vote txn pays a flat 5000-lamport base fee (one signature), charged whether it
# succeeds or errors. Counting landed signatures for the identity is therefore
# an exact spend figure, not a model - and it drops when we miss votes, which is
# precisely when the number matters.
VOTE_LAMPORTS=5000            # base fee per vote txn
VOTE_MAX_PAGES=20             # 1000 sigs/page; 20 pages ~ 20k slots ~ 2.2h
VOTE_MAX_INTERVAL_SLOTS=20000 # clamp: a monitoring gap must not dump hours of
                              # vote cost onto one rotation

# Discord
DISCORD_WEBHOOK="$(cat "$HOME/.config/discord/webhook" 2>/dev/null | tr -d '[:space:]')"
DISCORD_EMBED_SCRIPT="$HOME/999_discord_embed.sh"
BOT_USERNAME="Leader Capture Monitor"
SCRIPT_PATH="$(hostname):$(readlink -f "${BASH_SOURCE[0]}")"
# Short host label for the Discord embed title. The same file is deployed to all
# three hosts that run this monitor, so the title has to say which one is
# talking. Anything unmapped falls back to the bare hostname rather than an
# abbreviation nobody would recognise.
case "$(hostname)" in
    new-amsterdam) HOST_LABEL="AMS" ;;
    ogden)         HOST_LABEL="OGDEN" ;;
    testnet-ogden) HOST_LABEL="TESTNET" ;;
    *)             HOST_LABEL="$(hostname)" ;;
esac

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

# Read the MEV commission from validator.sh (--commission-bps, basis points).
# This used to run once at process start, so a commission change was reported at
# the stale rate until the unit was restarted -- testnet-ogden went 1000 -> 10000
# bps on 2026-08-09 and kept printing "10.0000%" for hours. run_capture_cycle
# calls this on every rotation, so an edit to validator.sh now takes effect on
# the next report with no restart.
refresh_commission() {
    local bps pct
    bps=$(grep -oP '(?<=--commission-bps )\d+' "$HOME/validator.sh" 2>/dev/null | head -1)
    [[ "$bps" =~ ^[0-9]+$ ]] || bps=0
    pct=$(echo "scale=4; $bps / 100" | bc -l)
    if [[ -n "${COMMISSION_BPS:-}" && "$bps" != "$COMMISSION_BPS" ]]; then
        log "Commission changed: ${COMMISSION_BPS} -> ${bps} bps (${COMMISSION_PCT}% -> ${pct}%)"
    fi
    COMMISSION_BPS=$bps
    COMMISSION_PCT=$pct
}
refresh_commission

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
    # A phone's Discord push notification shows the webhook USERNAME and almost
    # nothing else — the embed title is not in it. A fixed "Leader Capture
    # Monitor" therefore made every rotation look identical on a phone, so
    # callers pass the per-rotation headline here and it becomes the sender
    # name. Falls back to the static name for any caller that does not.
    local username="${4:-$BOT_USERNAME}"

    # Testnet alerts must not ping a person. The shared embed helper tags the
    # Discord user from the SEVERITY alone (warning/error/critical), so the
    # pagerduty=false below does not suppress it. A local shadow of the helper's
    # global turns the mention off for this host only, which keeps the file safe
    # to deploy to AMS/ogden where the ping is still wanted.
    local _DISCORD_TAG_USER_ID="${_DISCORD_TAG_USER_ID:-}"
    [[ "$HOST_LABEL" == "TESTNET" ]] && _DISCORD_TAG_USER_ID=""

    if [[ -z "$DISCORD_WEBHOOK" ]]; then
        log "WARNING: No Discord webhook configured"
        return 1
    fi

    # Convert newlines to \n literals for the embed script
    description="${description//$'\n'/\\n}"

    send_discord_embed "$DISCORD_WEBHOOK" "$severity" \
        "$title" "$description" \
        username="$username" \
        script_path="$SCRIPT_PATH" \
        pagerduty=false
}

# Return the central_day label (YYYY-MM-DD) for a given epoch timestamp.
# A day starts at DAY_ROLLOVER_HHMM in DAY_TZ. If local time is before the
# rollover, the label is the previous calendar date.
central_day_label() {
    local ts="$1"
    TZ="$DAY_TZ" date -d "@$ts" +"%Y-%m-%d %H%M" | awk -v r="$DAY_ROLLOVER_HHMM" '
        { if ($2 >= r) print $1;
          else { cmd = "TZ=\"'"$DAY_TZ"'\" date -d \"" $1 " -1 day\" +%Y-%m-%d"; cmd | getline y; close(cmd); print y } }'
}

# Append a capture to the JSONL ledger and echo today's running totals
# (fees_sol tips_sol revenue_sol rotation_count) to stdout.
update_daily_ledger() {
    local ts="$1" fees="$2" tips="$3" revenue="$4" slots="$5" first="$6" last="$7"
    local cu="$8" produced="$9" vote_txns="${10}"
    local day
    day=$(central_day_label "$ts")
    printf '{"ts":%d,"central_day":"%s","first_slot":%d,"last_slot":%d,"slots":%d,"produced_slots":%d,"fees_sol":%s,"tips_sol":%s,"revenue_sol":%s,"compute_units":%d,"vote_txns":%d}\n' \
        "$ts" "$day" "$first" "$last" "$slots" "$produced" "$fees" "$tips" "$revenue" "$cu" "$vote_txns" >> "$DAILY_LEDGER"
    python3 - "$DAILY_LEDGER" "$day" "$COMMISSION_PCT" "$VOTE_LAMPORTS" <<'PY'
import json, sys
path, day = sys.argv[1], sys.argv[2]
comm_pct = float(sys.argv[3])
vote_lamports = int(sys.argv[4])
f = t = r = 0.0; n = 0
cu_sum = 0; produced_sum = 0; vote_sum = 0
with open(path) as fh:
    for line in fh:
        try: d = json.loads(line)
        except Exception: continue
        if d.get("central_day") != day: continue
        f += float(d.get("fees_sol", 0))
        t += float(d.get("tips_sol", 0))
        r += float(d.get("revenue_sol", 0))
        n += 1
        # CU/produced_slots only count for entries that recorded them.
        if "compute_units" in d and "produced_slots" in d:
            cu_sum += int(d.get("compute_units", 0) or 0)
            produced_sum += int(d.get("produced_slots", 0) or 0)
        # Rows written before vote-cost tracking simply contribute 0.
        vote_sum += int(d.get("vote_txns", 0) or 0)
tips_to_val = t * comm_pct / 100
total_to_val = f + tips_to_val
vote_cost = vote_sum * vote_lamports / 1e9
net_to_val = total_to_val - vote_cost
avg_cu = (cu_sum // produced_sum) if produced_sum > 0 else 0
print(f"{f:.6f} {t:.6f} {r:.6f} {n} {tips_to_val:.6f} {total_to_val:.6f} {avg_cu} {produced_sum}"
      f" {vote_cost:.6f} {net_to_val:.6f} {vote_sum}")
PY
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

# Count our identity's landed transactions in (from_slot, to_slot] - in practice
# exactly our vote txns, one per slot. Pages getSignaturesForAddress backwards
# from the chain head, so it also skips the slots after to_slot that belong to
# the NEXT rotation's interval.
#
# Echoes "<count> exact" or "<count> est". "est" is the slot delta used as a
# stand-in when the RPC fails or the range needs more than VOTE_MAX_PAGES pages;
# it is right to within our vote-landing rate and keeps the report sending.
count_vote_txns() {
    local from_slot="$1" to_slot="$2"
    local delta=$(( to_slot - from_slot ))
    (( delta <= 0 )) && { echo "0 exact"; return; }

    local before="" count=0 page=0 params resp n oldest
    while (( page < VOTE_MAX_PAGES )); do
        params="\"$VALIDATOR_IDENTITY\",{\"limit\":1000"
        [[ -n "$before" ]] && params+=",\"before\":\"$before\""
        params+="}"
        resp=$(rpc_call "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"getSignaturesForAddress\",\"params\":[$params]}")
        n=$(jq -r '.result | length' <<< "$resp" 2>/dev/null)
        [[ "$n" =~ ^[0-9]+$ ]] || { echo "$delta est"; return; }
        (( n == 0 )) && { echo "$count exact"; return; }
        count=$(( count + $(jq -r --argjson f "$from_slot" --argjson t "$to_slot" \
            '[.result[] | select(.slot > $f and .slot <= $t)] | length' <<< "$resp") ))
        oldest=$(jq -r '.result[-1].slot' <<< "$resp")
        before=$(jq -r '.result[-1].signature' <<< "$resp")
        (( oldest <= from_slot )) && { echo "$count exact"; return; }
        (( n < 1000 )) && { echo "$count exact"; return; }
        page=$(( page + 1 ))
    done
    echo "$delta est"
}

get_slot_duration() {
    # Returns seconds per slot. Mainnet runs ~0.4s; clamp anything outside
    # [0.3, 0.8] to the default — a bad value here propagates into the merge
    # logic and can collapse all leader rotations into one giant window.
    local result
    result=$(rpc_call '{"jsonrpc":"2.0","id":1,"method":"getRecentPerformanceSamples","params":[1]}')
    local dur
    dur=$(echo "$result" | jq -r '
        if .result[0] and .result[0].numSlots > 0
        then (.result[0].samplePeriodSecs / .result[0].numSlots | tostring)
        else empty
        end' 2>/dev/null)
    # Sanity-check: reject NaN, empty, or out-of-band values
    if [[ -z "$dur" ]] || ! awk -v d="$dur" 'BEGIN{exit !(d >= 0.3 && d <= 0.8)}'; then
        dur="0.420"
    fi
    echo "$dur"
}

get_current_slot() {
    local result
    result=$(rpc_call '{"jsonrpc":"2.0","id":1,"method":"getSlot","params":[{"commitment":"confirmed"}]}')
    echo "$result" | jq -r '.result // empty' 2>/dev/null
}

# Get upcoming leader slot groups as merged capture windows.
# Uses getLeaderSchedule + getEpochInfo via RPC, processes with jq.
# Output: one line per window: "first_slot last_slot num_rotations leader_slots_csv"
# where leader_slots_csv lists the actual leader slots in the window (not the
# inter-rotation gap slots that belong to other validators).
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
        --argjson maxg "$MAX_ROTATIONS_PER_WINDOW" \
        --arg id "$VALIDATOR_IDENTITY" '
        .result[$id] // empty
        | map(. + $es)
        | map(select(. > $cs))
        | sort
        | if length == 0 then empty else
            # Phase 1: group consecutive leader slots into runs [first, last]
            reduce .[] as $s ([];
                if length == 0 then [[$s, $s]]
                elif (.[-1][1] + 1) == $s then (.[0:-1] + [[.[-1][0], $s]])
                else . + [[$s, $s]]
                end
            )
            # Phase 2: merge runs closer than mg seconds into windows; each
            # window keeps its member runs so we can emit ONLY the actual
            # leader slots (not the other-validator slots in the merge gap).
            # Bounded by $maxg so a degenerate sd cannot collapse everything.
            | reduce .[] as $r ([];
                if length == 0 then [[$r]]
                elif (($r[0] - .[-1][-1][1]) * $sd) < $mg
                     and ((.[-1] | length) < $maxg) then
                    (.[0:-1] + [.[-1] + [$r]])
                else . + [[$r]]
                end
            )
            | .[]
            | "\(.[0][0]) \(.[-1][1]) \(length) \([.[] | range(.[0]; .[1] + 1)] | join(","))"
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
    local leader_slots_csv="$6"

    local timestamp
    timestamp=$(date -u +"%Y%m%d_%H%M%S")
    local text_file="$OUTPUT_DIR/slot_txns_${timestamp}.txt"
    local json_file="$OUTPUT_DIR/slot_txns_${timestamp}.json"

    log "Querying RPC for leader slot transactions..."

    if $DRY_RUN; then
        log "[DRY-RUN] Would query leader slots $leader_slots_csv and report to Discord"
        return 0
    fi

    # Query RPC ONLY for our actual leader slots (not the inter-rotation gap
    # slots owned by other validators). slot-transactions.py also filters
    # block.rewards[] by our pubkey as a defensive second layer.
    "$SLOT_TRANSACTIONS_SCRIPT" --leader-slots "$leader_slots_csv" > "$text_file" 2>/dev/null
    "$SLOT_TRANSACTIONS_SCRIPT" --leader-slots "$leader_slots_csv" --json > "$json_file" 2>/dev/null

    # Parse summary from JSON output (one python invocation, not five)
    local summary_line
    summary_line=$(python3 -c "
import json
d = json.load(open('$json_file'))['summary']
print(
    d.get('total_non_vote_transactions', 0),
    d.get('successful', 0),
    d.get('failed', 0),
    d.get('skipped_slots', 0),
    f\"{d.get('total_fees_sol', 0):.6f}\",
    f\"{d.get('total_tips_sol', 0):.6f}\",
    f\"{d.get('total_revenue_sol', 0):.6f}\",
    d.get('tip_withdrawal_count', 0),
    f\"{d.get('tip_withdrawal_sol', 0):.6f}\",
    d.get('total_compute_units', 0),
)
" 2>/dev/null)

    local total_txns success_count failed_count skipped_slots
    local total_fees_sol total_tips_sol total_revenue_sol
    local withdrawal_count withdrawal_sol total_compute_units
    read -r total_txns success_count failed_count skipped_slots \
            total_fees_sol total_tips_sol total_revenue_sol \
            withdrawal_count withdrawal_sol total_compute_units <<< "$summary_line"

    total_txns="${total_txns:-0}"
    success_count="${success_count:-0}"
    failed_count="${failed_count:-0}"
    skipped_slots="${skipped_slots:-0}"
    total_fees_sol="${total_fees_sol:-0}"
    total_tips_sol="${total_tips_sol:-0}"
    total_revenue_sol="${total_revenue_sol:-0}"
    withdrawal_count="${withdrawal_count:-0}"
    withdrawal_sol="${withdrawal_sol:-0}"
    total_compute_units="${total_compute_units:-0}"

    # ── BAM telemetry for exactly this rotation window ───────────────────────
    # Windowed on purpose: without --since/--until this would re-scan the whole
    # ~500MB validator.log every rotation (~30x/day) and re-report every prior
    # rotation's bundles.
    #
    # --since is deliberately BAM_LOOKBACK_SECONDS earlier than our own window,
    # and --leader-slots then pins the bundle count to the log region where our
    # slots actually appear. Before that pinning, this reported 0 bundles for a
    # healthy BAM on 8 of 15 rotations (2026-08-09) because capture_start_time
    # landed after the ~1.6s burst had already finished.
    local bam_json_file="$OUTPUT_DIR/bam_activity_${timestamp}.json"
    local bam_line=""
    if [[ -x "$BAM_ACTIVITY_SCRIPT" ]]; then
        if "$BAM_ACTIVITY_SCRIPT" --since "$(( capture_start_time - BAM_LOOKBACK_SECONDS ))" \
                --until "$capture_end_time" --leader-slots "$leader_slots_csv" \
                --json > "$bam_json_file" 2>/dev/null; then
            bam_line=$(python3 -c "
import json
j = json.load(open('$bam_json_file'))
d = j['bam']
rate = d.get('send_rate_pct')
print(
    d.get('bundles_received', 0),
    d.get('results_sent', 0),
    'na' if rate is None else f'{rate:.1f}',
    d.get('scheduler_fail', 0),
    d.get('outbound_fail', 0),
    d.get('heartbeats_total', 0),
    d.get('unhealthy_connection_events', 0),
    1 if j.get('bam_window', {}).get('leader_window_found') else 0,
)
" 2>/dev/null)
        else
            log "WARNING: bam-leader-activity.py failed for this window"
        fi
    fi

    local bam_bundles bam_sent bam_rate bam_sched_fail bam_out_fail
    local bam_heartbeats bam_unhealthy bam_anchored
    read -r bam_bundles bam_sent bam_rate bam_sched_fail bam_out_fail \
            bam_heartbeats bam_unhealthy bam_anchored <<< "$bam_line"
    bam_bundles="${bam_bundles:-0}"
    bam_sent="${bam_sent:-0}"
    bam_rate="${bam_rate:-na}"
    bam_sched_fail="${bam_sched_fail:-0}"
    bam_out_fail="${bam_out_fail:-0}"
    bam_heartbeats="${bam_heartbeats:-0}"
    bam_unhealthy="${bam_unhealthy:-0}"
    # 0 = we could not find our leader slots in the log, so the bundle count is
    # a fallback over the wall-clock window and must not raise a BAM alert.
    bam_anchored="${bam_anchored:-0}"

    local capture_duration=$(( capture_end_time - capture_start_time ))
    local slot_range="${first_slot}–${last_slot}"
    # Count of OUR leader slots in the window (not the range span, which
    # would include other validators' slots between merged rotations).
    local total_slots
    if [[ -n "$leader_slots_csv" ]]; then
        total_slots=$(awk -F, '{print NF}' <<< "$leader_slots_csv")
    else
        total_slots=$(( last_slot - first_slot + 1 ))
    fi
    local produced_slots=$(( total_slots - skipped_slots ))

    local group_label="rotation"
    if (( num_groups > 1 )); then
        group_label="${num_groups} rotations"
    fi

    # Compute validator's share: commission % of Jito tips
    local jito_to_validator total_to_validator
    jito_to_validator=$(printf '%.6f' "$(echo "$total_tips_sol * $COMMISSION_PCT / 100" | bc -l)")
    total_to_validator=$(printf '%.6f' "$(echo "$total_fees_sol + $jito_to_validator" | bc -l)")

    # ── Vote cost for the interval this rotation closes ──────────────────────
    # Earnings arrive in bursts (our leader slots); vote fees are paid every
    # slot, continuously. Charging each rotation for the votes since the PREVIOUS
    # rotation makes the intervals tile the day, so the daily vote cost below is
    # the real daily spend rather than a sample of the capture windows.
    #
    # Read the previous last_slot BEFORE update_daily_ledger appends this
    # rotation's row, or we would read our own.
    local prev_last_slot vote_txns=0 vote_mode="na" vote_cost_sol="0.000000"
    local net_to_validator="$total_to_validator"
    prev_last_slot=$(tail -n 1 "$DAILY_LEDGER" 2>/dev/null | jq -r '.last_slot // empty' 2>/dev/null)
    if [[ "$prev_last_slot" =~ ^[0-9]+$ ]] && (( prev_last_slot < last_slot )); then
        # A monitoring gap (restart, standby stint) must not dump hours of vote
        # cost onto one rotation.
        if (( last_slot - prev_last_slot > VOTE_MAX_INTERVAL_SLOTS )); then
            prev_last_slot=$(( last_slot - VOTE_MAX_INTERVAL_SLOTS ))
        fi
        read -r vote_txns vote_mode <<< "$(count_vote_txns "$prev_last_slot" "$last_slot")"
        vote_txns="${vote_txns:-0}"
        vote_mode="${vote_mode:-est}"
        vote_cost_sol=$(printf '%.6f' "$(echo "$vote_txns * $VOTE_LAMPORTS / 1000000000" | bc -l)")
        net_to_validator=$(printf '%.6f' "$(echo "$total_to_validator - $vote_cost_sol" | bc -l)")
    fi
    local vote_txns_fmt
    vote_txns_fmt=$(LC_NUMERIC=en_US.UTF-8 printf "%'d" "$vote_txns" 2>/dev/null || echo "$vote_txns")

    # Build Discord message
    local severity="info"
    if (( total_txns == 0 )); then
        severity="warning"
    fi
    if (( withdrawal_count > 0 )); then
        severity="warning"
    fi

    local desc=""
    desc+="**Slots:** ${slot_range} (${total_slots} slots across ${group_label})"
    if (( skipped_slots > 0 )); then
        desc+=", ${skipped_slots} skipped"
    fi
    desc+=$'\n'"**Capture window:** $(duration_fmt $capture_duration)"
    desc+=$'\n'"**Transactions:** ${total_txns} (${success_count} success, ${failed_count} failed)"
    local avg_cu_per_block=0
    if (( produced_slots > 0 )); then
        avg_cu_per_block=$(( total_compute_units / produced_slots ))
    fi
    local avg_cu_fmt
    avg_cu_fmt=$(LC_NUMERIC=en_US.UTF-8 printf "%'d" "$avg_cu_per_block" 2>/dev/null || echo "$avg_cu_per_block")
    desc+=$'\n'"**Avg CU/block:** ${avg_cu_fmt} CU (over ${produced_slots} produced)"
    desc+=$'\n'"**Fees earned:** ${total_fees_sol} SOL"
    desc+=$'\n'"**Jito tips earned:** ${total_tips_sol} SOL (tip-PDA inflow during our slots)"
    desc+=$'\n'"**Jito to Validator:** ${jito_to_validator} SOL (${COMMISSION_PCT}% commission)"
    desc+=$'\n'"**Total to Validator:** ${total_to_validator} SOL"
    if [[ "$vote_mode" != "na" ]]; then
        if [[ "$vote_mode" == "est" ]]; then
            desc+=$'\n'"**Vote cost:** ~${vote_cost_sol} SOL (est, ${vote_txns_fmt} slots since last rotation)"
        else
            desc+=$'\n'"**Vote cost:** ${vote_cost_sol} SOL (${vote_txns_fmt} votes since last rotation)"
        fi
        # Negative is normal and not an alert: a quiet rotation can earn less
        # than the votes paid while waiting for it.
        desc+=$'\n'"**Net to Validator:** ${net_to_validator} SOL"
    fi

    # Update daily ledger and append rolling subtotal (since 18:15 CT)
    local day_line day_fees day_tips day_rev day_n day_tips_to_val day_total_to_val
    local day_avg_cu day_produced day_vote_cost day_net day_votes
    day_line=$(update_daily_ledger "$capture_end_time" \
        "$total_fees_sol" "$total_tips_sol" "$total_revenue_sol" \
        "$total_slots" "$first_slot" "$last_slot" \
        "$total_compute_units" "$produced_slots" "$vote_txns")
    read -r day_fees day_tips day_rev day_n day_tips_to_val day_total_to_val \
            day_avg_cu day_produced day_vote_cost day_net day_votes <<< "$day_line"
    local day_label
    day_label=$(central_day_label "$capture_end_time")
    desc+=$'\n'"**Today (${day_label}, since 18:15 CT):** ${day_fees} fees + ${day_tips_to_val} tips (${COMMISSION_PCT}%) = ${day_total_to_val} SOL to validator across ${day_n} rotation(s)"
    local day_votes_fmt
    day_votes_fmt=$(LC_NUMERIC=en_US.UTF-8 printf "%'d" "${day_votes:-0}" 2>/dev/null || echo "${day_votes:-0}")
    desc+=$'\n'"**Today net:** ${day_net} SOL — ${day_total_to_val} − ${day_vote_cost} vote cost (${day_votes_fmt} votes)"
    if (( day_produced > 0 )); then
        local day_avg_cu_fmt
        day_avg_cu_fmt=$(LC_NUMERIC=en_US.UTF-8 printf "%'d" "$day_avg_cu" 2>/dev/null || echo "$day_avg_cu")
        desc+=$'\n'"**Today avg CU/block:** ${day_avg_cu_fmt} CU (over ${day_produced} produced blocks)"
    fi

    if (( withdrawal_count > 0 )); then
        desc+=$'\n'"⚠️ **Tip account withdrawals:** ${withdrawal_count} event(s), ${withdrawal_sol} SOL out — see ${text_file}"
    fi
    # BAM health for this window - this is what makes the revenue figure above
    # diagnosable rather than just observed.
    #
    # Everything BAM-related below is gated on there being BAM telemetry at all.
    # Heartbeats flow continuously whenever a BAM connection exists, independent
    # of whether we are leader, so "no heartbeats" means this validator is not
    # running BAM rather than that BAM is broken. Without this gate a non-BAM
    # validator (e.g. testnet) would fire the zero-bundles alert on every single
    # rotation.
    # Declared before the bam_present gate on purpose: it is read further down
    # in the title and the log summary, both OUTSIDE the gate, and set -u would
    # abort the entire rotation report on a validator with no BAM telemetry.
    local bam_alert=""
    local bam_present=0
    if (( bam_heartbeats > 0 || bam_bundles > 0 )); then
        bam_present=1
    fi

    if (( bam_present == 1 )); then
    # Bundles are counted over the leader-anchored sub-window; heartbeats are
    # counted over the whole scan window (they stop during leader slots, so a
    # leader-only heartbeat count would be near zero and useless as a liveness
    # signal). Different spans, so the line says which is which.
    local bam_scan_span=$(( BAM_LOOKBACK_SECONDS + capture_duration ))
    desc+=$'\n'"**BAM:** ${bam_bundles} bundles → ${bam_sent} sent (${bam_rate}%) during our slots, ${bam_heartbeats} heartbeats in the last $(duration_fmt $bam_scan_span)"
    if (( bam_anchored == 0 )); then
        desc+=$'\n'"ℹ️ BAM bundle count unanchored — our leader slots were not found in the validator log for this window, so the count above may be incomplete."
    fi
    if (( bam_sched_fail > 0 || bam_out_fail > 0 )); then
        desc+=$'\n'"⚠️ **BAM failures:** ${bam_sched_fail} scheduler, ${bam_out_fail} outbound"
    fi
    if (( bam_unhealthy > 0 )); then
        desc+=$'\n'"⚠️ **BAM unhealthy connection events:** ${bam_unhealthy}"
    fi

    # ── Exception alerting (threshold, not schedule) ─────────────────────────
    # Evaluated PER ROTATION on purpose: zero bundles outside a leader window is
    # normal (bundles only arrive while we lead), so "0 bundles" is only
    # meaningful when set against slots we actually produced.
    #
    # bam_anchored is the second half of that: a zero count is only evidence
    # about BAM if we located our leader period in the log. If we did not, the
    # count describes the wrong seconds and says nothing about BAM - never page
    # on our own instrumentation gap. The note added above still surfaces it.
    # The anchoring guard covers only the two bundle-derived checks. The
    # unhealthy-connection count is taken over the whole window and stays live
    # either way.
    if (( bam_present == 0 )); then
        : # no BAM on this validator - nothing to assert about it
    elif (( bam_anchored == 1 && produced_slots > 0 && bam_bundles == 0 )); then
        bam_alert="BAM delivered 0 bundles across ${produced_slots} produced slot(s)"
    elif (( bam_anchored == 1 )) && [[ "$bam_rate" != "na" ]] \
            && (( $(echo "$bam_rate < $BAM_MIN_SEND_RATE" | bc -l) )); then
        bam_alert="BAM send rate ${bam_rate}% is below ${BAM_MIN_SEND_RATE}%"
    elif (( bam_unhealthy > 0 )); then
        bam_alert="${bam_unhealthy} unhealthy BAM connection event(s)"
    fi
    if [[ -n "$bam_alert" ]]; then
        severity="warning"
        desc+=$'\n'"🚨 **BAM ALERT:** ${bam_alert}"
        log "BAM ALERT: $bam_alert"
    fi
    fi   # bam_present

    desc+=$'\n'"**Output:** ${text_file}"

    # The headline carries the DAY's running totals, not this rotation's, so the
    # channel reads as a running tally without opening any embed:
    #   "AMS 1.40 35.95CU 9 Rotations"
    # Same three numbers as the two "Today" lines in the description above.
    # It is sent as the webhook USERNAME rather than the embed title, because a
    # phone notification shows only the sender name — see send_discord().
    local head_sol head_cu head_rot
    head_sol=$(printf '%.2f' "$day_total_to_val" 2>/dev/null || echo "$day_total_to_val")
    local headline="${HOST_LABEL} ${head_sol}"
    # Gated exactly like the "Today avg CU/block" line: with no produced blocks
    # the average is 0 and printing "0.00CU" would read as a real measurement.
    if (( day_produced > 0 )); then
        head_cu=$(awk -v cu="$day_avg_cu" 'BEGIN{printf "%.2f", cu/1000000}')
        headline+=" ${head_cu}CU"
    fi
    head_rot="Rotations"
    (( day_n == 1 )) && head_rot="Rotation"
    headline+=" ${day_n} ${head_rot}"

    # Exception markers ride in the headline too: they are worthless in the
    # embed title if the phone never renders it.
    if (( total_txns == 0 )); then
        headline+=" — No Transactions"
    elif (( withdrawal_count > 0 )); then
        headline+=" — ⚠️ Tip Withdrawal Detected"
    fi
    if [[ -n "$bam_alert" ]]; then
        headline="${headline} — 🚨 BAM"
    fi

    # Discord rejects a webhook username over 80 chars and drops the whole
    # message; the exception suffixes above can push a long hostname fallback
    # past that, so clamp rather than lose the report.
    # iconv -c drops a trailing partial character: this unit runs with no locale
    # set, so ${#headline} and the slice are BYTES, and cutting mid-emoji would
    # hand jq invalid UTF-8 and lose the message that way instead.
    if (( ${#headline} > 80 )); then
        headline=$(printf '%s' "${headline:0:80}" | iconv -c -f UTF-8 -t UTF-8)
    fi

    # Static name to the embed title, running totals to the sender name.
    send_discord "$BOT_USERNAME" "$desc" "$severity" "$headline"
    log "Discord notification sent (headline: ${headline})"

    # Log summary locally
    log "Capture summary:"
    log "  Slots: $slot_range ($produced_slots produced, $skipped_slots skipped)"
    log "  Duration: $(duration_fmt $capture_duration)"
    log "  Transactions: $total_txns ($success_count success, $failed_count failed)"
    log "  Avg CU/block: ${avg_cu_fmt} CU (over ${produced_slots} produced)"
    log "  Fees: $total_fees_sol SOL"
    log "  Tips: $total_tips_sol SOL"
    log "  Jito to Validator: $jito_to_validator SOL (${COMMISSION_PCT}%)"
    log "  Total to Validator: $total_to_validator SOL"
    if [[ "$vote_mode" != "na" ]]; then
        log "  Vote cost: $vote_cost_sol SOL ($vote_txns_fmt, $vote_mode)"
        log "  Net to Validator: $net_to_validator SOL"
    fi
    if (( withdrawal_count > 0 )); then
        log "  ⚠️  Tip withdrawals: $withdrawal_count event(s), $withdrawal_sol SOL out"
    fi
    log "  BAM: $bam_bundles bundles, $bam_sent sent (${bam_rate}%), $bam_heartbeats heartbeats, $bam_unhealthy unhealthy, anchored=$bam_anchored"
    if [[ -n "$bam_alert" ]]; then
        log "  BAM ALERT: $bam_alert"
    fi
    log "  Output: $text_file"
}

# ── Main loop ─────────────────────────────────────────────────────────────────

run_capture_cycle() {
    log "Checking leader schedule..."

    refresh_commission

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

    local first_slot last_slot num_groups leader_slots_csv
    read -r first_slot last_slot num_groups leader_slots_csv <<< "$(echo "$windows" | head -1)"
    debug "Next capture window: slots $first_slot-$last_slot ($num_groups rotation(s), leader slots: $leader_slots_csv)"

    # Calculate time until the leader window
    local slots_until_start=$(( first_slot - current_slot ))
    local seconds_until_start
    seconds_until_start=$(printf '%.0f' "$(echo "$slots_until_start * $slot_duration" | bc)")

    local total_leader_slots=$(( last_slot - first_slot + 1 ))
    local leader_duration_seconds
    leader_duration_seconds=$(printf '%.0f' "$(echo "$total_leader_slots * $slot_duration" | bc)")

    log "Next leader window: slots $first_slot-$last_slot ($num_groups group(s), $(duration_fmt $leader_duration_seconds))"
    log "Leader slots start in ~$(duration_fmt $seconds_until_start)"

    # Defense in depth: even with the jq-side cap, refuse to enter a
    # capture window that would block us for too long. Sleep briefly and
    # let the next iteration re-derive a sane window.
    if (( num_groups > MAX_ROTATIONS_PER_WINDOW )) || (( leader_duration_seconds > MAX_WINDOW_SECONDS )); then
        log "ERROR: window exceeds safety caps (groups=$num_groups max=$MAX_ROTATIONS_PER_WINDOW," \
            "duration=${leader_duration_seconds}s max=${MAX_WINDOW_SECONDS}s)."
        log "       Slot duration at scan: ${slot_duration}s. Skipping; will recheck in 60s."
        sleep 60
        return 1
    fi

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

    # Wait through the leader slots + post-buffer for blocks to finalize.
    # Emit a periodic log line so a stuck wait is visible (see prior incident
    # where slot_duration=0.00042 collapsed 42 rotations and the script sat
    # silent in this loop for nearly 48h).
    local last_progress_log
    last_progress_log=$(date +%s)
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

        # Periodic visible progress so the script's state is observable
        local now
        now=$(date +%s)
        if (( now - last_progress_log >= WAIT_PROGRESS_INTERVAL )); then
            local remaining=$(( last_slot - current_slot ))
            slot_duration=$(get_slot_duration)
            local secs_remaining
            secs_remaining=$(printf '%.0f' "$(echo "$remaining * $slot_duration" | bc)")
            log "  ...still waiting: current=$current_slot, target=$last_slot ($remaining slots / ~$(duration_fmt $secs_remaining))"
            last_progress_log=$now
        fi

        sleep "$POLL_INTERVAL_NEAR"
    done

    local capture_end_time
    capture_end_time=$(date +%s)

    # ── Extract and report ────────────────────────────────────────────────

    extract_and_report "$capture_start_time" "$capture_end_time" \
        "$first_slot" "$last_slot" "$num_groups" "$leader_slots_csv"
}

# ── Entry point ───────────────────────────────────────────────────────────────

log "Leader Capture Monitor starting (RPC mode)"
log "  Network: $NETWORK"
log "  Validator: $VALIDATOR_IDENTITY"
log "  RPC: ${RPC_URL%%://*}://***${RPC_URL##*/}"
log "  Post-slot buffer: ${BUFFER_AFTER_SECONDS}s (wait for block finalization)"
log "  Merge gap: ${MERGE_GAP_SECONDS}s (groups closer than this are merged)"
log "  Dry-run: $DRY_RUN"
log "  MEV commission: ${COMMISSION_PCT}% (${COMMISSION_BPS} bps, from validator.sh)"

if $ONCE; then
    run_capture_cycle
    log "Single capture cycle complete."
else
    _last_role_check=$SECONDS
    while true; do
        # The gate used to be evaluated only at startup, so a demoted host kept
        # capturing indefinitely: on 2026-08-08 ogden was still running this
        # while new-amsterdam held the staked identity. Re-check periodically and
        # exit cleanly on demotion -- systemd restarts us and the wait loop above
        # parks the process until this host is promoted again.
        if (( SECONDS - _last_role_check >= 300 )); then
            _last_role_check=$SECONDS
            if ! /home/sol/bam-leader-activity/role-gate.sh >/dev/null 2>&1; then
                log "Lost the staked identity - exiting; will wait for promotion."
                exit 0
            fi
        fi
        if run_capture_cycle; then
            log "Capture cycle complete. Checking for next window..."
        else
            log "No capture window available. Rechecking in ${POLL_INTERVAL_FAR}s..."
            sleep "$POLL_INTERVAL_FAR"
        fi
    done
fi
