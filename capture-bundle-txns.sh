#!/bin/bash
#
# Capture Bundle Transaction Signatures
#
# This script temporarily enables DEBUG logging for bundle_stage,
# waits for a specified duration, then restores default logging
# and runs the bundle-txn-signatures.py script to extract transactions.
#
# Schedule to run at 2026-01-20 22:08 UTC:
#   echo "/home/sol/bam-leader-activity/capture-bundle-txns.sh" | at 22:08 UTC 2026-01-20
#
# Or use cron (crontab -e):
#   8 22 20 1 * /home/sol/bam-leader-activity/capture-bundle-txns.sh
#

set -e

# Configuration
LEDGER_DIR="/mnt/ledger"
LOG_FILE="$HOME/logs/validator.log"
CAPTURE_DURATION=3  # 5 minutes in seconds
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
OUTPUT_DIR="$SCRIPT_DIR/captures"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%S")
OUTPUT_FILE="$OUTPUT_DIR/bundle_txns_$TIMESTAMP.txt"
JSON_FILE="$OUTPUT_DIR/bundle_txns_$TIMESTAMP.json"

# Log filter settings
DEBUG_FILTER="solana=info,solana_core::bundle_stage=debug"
DEFAULT_FILTER="solana=info,agave=info"

# Create output directory
mkdir -p "$OUTPUT_DIR"

log() {
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $1"
}

log "Starting bundle transaction capture"
log "Ledger directory: $LEDGER_DIR"
log "Validator log: $LOG_FILE"
log "Capture duration: $CAPTURE_DURATION seconds"
log "Output file: $OUTPUT_FILE"

# Enable DEBUG logging for bundle_stage
log "Enabling DEBUG logging for bundle_stage..."
if agave-validator -l "$LEDGER_DIR" set-log-filter "$DEBUG_FILTER"; then
    log "Log filter set to: $DEBUG_FILTER"
else
    log "ERROR: Failed to set log filter. Is the validator running?"
    exit 1
fi

# Wait for the capture duration
log "Capturing for $CAPTURE_DURATION seconds (until $(date -u -d "+$CAPTURE_DURATION seconds" '+%Y-%m-%d %H:%M:%S UTC'))..."
sleep "$CAPTURE_DURATION"

# Restore default logging
log "Restoring default log filter..."
if agave-validator -l "$LEDGER_DIR" set-log-filter "$DEFAULT_FILTER"; then
    log "Log filter restored to: $DEFAULT_FILTER"
else
    log "WARNING: Failed to restore default log filter"
fi

# Run the extraction script
log "Extracting bundle transaction signatures from $LOG_FILE..."

"$SCRIPT_DIR/bundle-txn-signatures.py" "$LOG_FILE" > "$OUTPUT_FILE" 2>&1

# Also save JSON format
"$SCRIPT_DIR/bundle-txn-signatures.py" "$LOG_FILE" --json > "$JSON_FILE" 2>&1

log "Capture complete!"
log "Results saved to:"
log "  Text: $OUTPUT_FILE"
log "  JSON: $JSON_FILE"

# Print summary
echo ""
echo "========== SUMMARY =========="
grep -E "^(Summary:|  Total|  Avg|Results breakdown:)" "$OUTPUT_FILE" || true
echo "============================="
