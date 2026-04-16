#!/usr/bin/env bash
set -u

###############################################################################
# Daily revenue summary for leader captures.
#
# Intended to be run by cron at 18:15 America/Chicago. Sums all capture
# entries in daily_totals.jsonl whose central_day label matches the day
# that just closed (yesterday in America/Chicago at fire time) and posts a
# Discord summary embed.
#
# Usage:
#   ./daily-summary.sh [--day YYYY-MM-DD] [--dry-run]
###############################################################################

LOG_FILE="${HOME}/logs/daily-summary.log"
exec >> "$LOG_FILE" 2>&1

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
LEDGER="$SCRIPT_DIR/daily_totals.jsonl"
DAY_TZ="America/Chicago"

# Read commission from validator.sh (--commission-bps value, in basis points)
COMMISSION_BPS=$(grep -oP '(?<=--commission-bps )\d+' "$HOME/validator.sh" 2>/dev/null || echo "0")
COMMISSION_PCT=$(echo "scale=4; $COMMISSION_BPS / 100" | bc -l)

DISCORD_WEBHOOK="$(cat "$HOME/.config/discord/webhook" 2>/dev/null | tr -d '[:space:]')"
DISCORD_EMBED_SCRIPT="$HOME/999_discord_embed.sh"
BOT_USERNAME="Leader Daily Summary"
SCRIPT_PATH="$(hostname):$(readlink -f "${BASH_SOURCE[0]}")"

DRY_RUN=false
DAY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --day) DAY="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

if [[ -f "$DISCORD_EMBED_SCRIPT" ]]; then
    # shellcheck source=/home/sol/999_discord_embed.sh
    source "$DISCORD_EMBED_SCRIPT"
else
    log "WARNING: Discord embed script not found at $DISCORD_EMBED_SCRIPT"
fi

if [[ -z "$DAY" ]]; then
    # The day that just closed = yesterday's date in DAY_TZ at fire time.
    DAY=$(TZ="$DAY_TZ" date -d "yesterday" +%Y-%m-%d)
fi

if [[ ! -s "$LEDGER" ]]; then
    log "Ledger $LEDGER missing or empty; nothing to summarize for $DAY"
    exit 0
fi

summary=$(python3 - "$LEDGER" "$DAY" "$COMMISSION_PCT" <<'PY'
import json, sys
path, day = sys.argv[1], sys.argv[2]
fees = tips = rev = 0.0
rotations = 0
slots = 0
first_ts = None
last_ts = None
with open(path) as fh:
    for line in fh:
        try: d = json.loads(line)
        except Exception: continue
        if d.get("central_day") != day: continue
        fees += float(d.get("fees_sol", 0))
        tips += float(d.get("tips_sol", 0))
        rev  += float(d.get("revenue_sol", 0))
        slots += int(d.get("slots", 0))
        rotations += 1
        ts = int(d.get("ts", 0))
        first_ts = ts if first_ts is None else min(first_ts, ts)
        last_ts  = ts if last_ts  is None else max(last_ts, ts)
comm_pct = float(sys.argv[3])
tips_to_val = tips * comm_pct / 100
total_to_val = fees + tips_to_val
print(f"{rotations} {slots} {fees:.6f} {tips:.6f} {rev:.6f} {first_ts or 0} {last_ts or 0} {tips_to_val:.6f} {total_to_val:.6f}")
PY
)

read -r rotations slots fees tips rev first_ts last_ts tips_to_val total_to_val <<< "$summary"

if (( rotations == 0 )); then
    log "No captures for central_day=$DAY; skipping Discord post"
    exit 0
fi

window_start=$(TZ="$DAY_TZ" date -d "$DAY 18:15" +"%Y-%m-%d %H:%M %Z")
window_end=$(TZ="$DAY_TZ" date -d "$DAY 18:14 next day" +"%Y-%m-%d %H:%M %Z")

desc="**Window:** ${window_start} → ${window_end}"
desc+=$'\n'"**Rotations:** ${rotations} (${slots} leader slots)"
desc+=$'\n'"**Fees earned:** ${fees} SOL"
desc+=$'\n'"**Jito tips earned:** ${tips} SOL"
desc+=$'\n'"**Jito to Validator:** ${tips_to_val} SOL (${COMMISSION_PCT}% commission)"
desc+=$'\n'"**Total to Validator:** ${total_to_val} SOL"

title="Daily Leader Revenue — ${DAY}"

log "Summary for $DAY: rotations=$rotations slots=$slots fees=$fees tips=$tips tips_to_val=$tips_to_val total_to_val=$total_to_val"

if $DRY_RUN; then
    log "[DRY-RUN] Would post to Discord:"
    log "$title"
    log "$desc"
    exit 0
fi

if [[ -z "$DISCORD_WEBHOOK" ]]; then
    log "WARNING: No Discord webhook configured; skipping post"
    exit 0
fi

desc="${desc//$'\n'/\\n}"
send_discord_embed "$DISCORD_WEBHOOK" "info" \
    "$title" "$desc" \
    username="$BOT_USERNAME" \
    script_path="$SCRIPT_PATH" \
    pagerduty=false

log "Daily summary posted."
