#!/usr/bin/env bash
#
# install-cron.sh — idempotent crontab installer for this repo.
#
# Manages a marker-delimited block in the `sol` user's crontab. Re-running
# replaces the managed block in place; entries OUTSIDE the block are
# never touched. To remove the block: `crontab -e` and delete the lines
# between the BEGIN/END markers.
#
# Usage:
#   ./install-cron.sh            # show diff, prompt before applying
#   ./install-cron.sh --yes      # apply without prompting (automation)
#   ./install-cron.sh --check    # show diff only, never apply
#                                #   exit 0 if up to date, 1 if pending
#
# CRON_TZ note: the managed block places `CRON_TZ=America/Chicago` right
# above the daily-summary entry and `CRON_TZ=UTC` immediately after, so
# any cron entries OUTSIDE the managed block stay in their previous TZ.

set -euo pipefail

BEGIN_MARK="# BEGIN bam-leader-activity (managed by install-cron.sh)"
END_MARK="# END bam-leader-activity"

ASSUME_YES=false
CHECK_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --yes|-y)   ASSUME_YES=true ;;
        --check)    CHECK_ONLY=true ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)          echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

REPO_DIR=$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")
LOG_DIR="$HOME/logs"

# ── Pre-flight: warn (don't fail) about missing target files ────────
missing=()
[[ -x /home/sol/bash/monitor_log_errors.sh ]]      || missing+=("/home/sol/bash/monitor_log_errors.sh (external; entry can be removed if unused)")
[[ -x /home/sol/python/venv/bin/python3 ]]         || missing+=("python venv at /home/sol/python/venv")
[[ -x "$REPO_DIR/bam-log-monitor.sh" ]]            || missing+=("$REPO_DIR/bam-log-monitor.sh")
[[ -x "$REPO_DIR/role-bam-sync.sh" ]]              || missing+=("$REPO_DIR/role-bam-sync.sh")
[[ -x "$REPO_DIR/daily-summary.sh" ]]              || missing+=("$REPO_DIR/daily-summary.sh")
[[ -f "$REPO_DIR/hourly_log_error_summary.py" ]]   || missing+=("$REPO_DIR/hourly_log_error_summary.py")
[[ -d "$LOG_DIR" ]]                                || missing+=("$LOG_DIR (cron output destination)")

if (( ${#missing[@]} > 0 )); then
    echo "WARNING: these targets are missing — corresponding cron entries will fail at runtime:" >&2
    for m in "${missing[@]}"; do
        echo "  - $m" >&2
    done
    echo "" >&2
fi

# ── Compose the managed block ───────────────────────────────────────
MANAGED_BLOCK="$BEGIN_MARK
# Log monitoring — error detection every 5 minutes (external script)
*/5 * * * * /home/sol/bash/monitor_log_errors.sh --once >> $LOG_DIR/log_monitor.log 2>&1

# Hourly AI-powered log error summary (gated to primary)
0 * * * * /home/sol/python/venv/bin/python3 $REPO_DIR/hourly_log_error_summary.py >> $LOG_DIR/hourly_log_summary.log 2>&1

# BAM error monitoring every 5 minutes (gated to primary)
*/5 * * * * $REPO_DIR/bam-log-monitor.sh --once >> $LOG_DIR/bam_monitor.log 2>&1

# BAM URL role sync — every 5 minutes (drives BAM on/off by current validator role)
*/5 * * * * $REPO_DIR/role-bam-sync.sh >> $LOG_DIR/role_bam_sync.log 2>&1

# Daily leader revenue summary — fires 18:15 America/Chicago (gated to primary)
CRON_TZ=America/Chicago
15 18 * * * $REPO_DIR/daily-summary.sh
CRON_TZ=UTC
$END_MARK"

current=$(crontab -l 2>/dev/null || true)

# ── Compute new crontab ─────────────────────────────────────────────
# 1. Drop everything inside any existing managed block (markers + content).
# 2. Drop orphan lines (outside the block) that reference managed commands —
#    this absorbs pre-existing manual installs into the managed block on
#    first run, so re-running always yields the same canonical state.
# 3. Append the fresh managed block at the end.
#
# Any line removed in step 2 is reflected in the diff prompt below, so
# the operator gets a chance to cancel if a strip looks wrong.

if grep -qF "$BEGIN_MARK" <<< "$current"; then
    action="refresh"
else
    action="install"
fi

MANAGED_CMD_PATTERN='monitor_log_errors[.]sh|hourly_log_error_summary[.]py|bam-log-monitor[.]sh|role-bam-sync[.]sh|daily-summary[.]sh'

# 1. Drop the existing managed block (between markers).
without_block=$(awk -v begin="$BEGIN_MARK" -v end="$END_MARK" '
    BEGIN { in_block = 0 }
    $0 == begin { in_block = 1; next }
    $0 == end   { in_block = 0; next }
    !in_block   { print }
' <<< "$current")

# 2. Drop any blank-line-separated stanza that references a managed
#    command. This absorbs not just the cron line but the comment header
#    and any adjacent CRON_TZ that travel with it.
#
#    awk RS='' = paragraph mode (records split on blank lines).
stripped=$(awk -v RS='' -v ORS='\n\n' -v pat="$MANAGED_CMD_PATTERN" '$0 !~ pat' <<< "$without_block")

# 3. Collapse trailing whitespace from awk's ORS handling.
stripped=$(printf '%s' "$stripped" | sed -E ':a; /^[[:space:]]*$/{$d;N;ba}')

if [[ -n "$stripped" ]]; then
    new="$stripped"$'\n\n'"$MANAGED_BLOCK"
else
    new="$MANAGED_BLOCK"
fi

if [[ "$new" == "$current" ]]; then
    echo "Crontab managed block already up to date — no change."
    exit 0
fi

echo "=== Crontab change (will $action managed block) ==="
diff -u <(printf '%s\n' "$current") <(printf '%s\n' "$new") | sed 's/^/  /' || true
echo ""

if $CHECK_ONLY; then
    echo "(--check: not applying)"
    exit 1
fi

if ! $ASSUME_YES; then
    read -rp "Apply this change? [y/N] " ans
    if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
        echo "Cancelled."
        exit 1
    fi
fi

printf '%s\n' "$new" | crontab -
case "$action" in
    install) echo "Crontab managed block installed." ;;
    refresh) echo "Crontab managed block refreshed." ;;
esac
