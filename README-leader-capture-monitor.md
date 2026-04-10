# Leader Capture Monitor

Automatically captures bundle transaction DEBUG logs around leader slots. The monitor watches the leader schedule, enables `bundle_stage` debug logging before each leader rotation, and extracts bundle transaction signatures after the rotation completes.

## How It Works

1. **Poll** — Queries the leader schedule via RPC to find upcoming leader slots for your validator
2. **Merge** — Groups nearby leader rotations into a single capture window (avoids toggling debug logging on/off rapidly when rotations are close together)
3. **Wait** — Sleeps with adaptive polling, re-checking slot timing frequently to handle drift
4. **Capture** — Enables `solana_core::bundle_stage=debug` logging ~60s before the first slot
5. **Extract** — After the last slot + post-buffer, restores default logging and runs `bundle-txn-signatures.py` to extract signatures
6. **Report** — Sends a summary to Discord and saves results to `captures/`

This avoids running DEBUG logging 24/7 (which bloats logs significantly) while still capturing every bundle transaction during your leader slots.

## Prerequisites

- Solana CLI tools (`solana`, `agave-validator`)
- `jq`, `bc`, `curl`, `python3`
- `bundle-txn-signatures.py` (included in this repo)
- Discord embed helper (see [Discord Notifications](README.md#discord-notifications))
- Validator config file at `~/.config/validator/rpc.conf` (see [Configuration](#configuration))

## Usage

```bash
./leader-capture-monitor.sh [--once] [--verbose] [--dry-run]
```

### Options

| Option | Description |
|--------|-------------|
| `--once` | Run one capture cycle and exit (useful for testing) |
| `--verbose` | Print debug output (drift checks, slot calculations, sleep intervals) |
| `--dry-run` | Show what would happen without enabling/disabling log filters |
| `-h`, `--help` | Show help message |

### Examples

```bash
# Run continuously as a long-lived process (typical usage)
./leader-capture-monitor.sh

# Test with verbose output, no actual log filter changes
./leader-capture-monitor.sh --once --verbose --dry-run

# Single capture cycle with debug output
./leader-capture-monitor.sh --once --verbose
```

## Configuration

### Validator config

The script loads RPC and identity settings from `~/.config/validator/rpc.conf`. Create this file with:

```bash
mkdir -p ~/.config/validator
cat > ~/.config/validator/rpc.conf << 'EOF'
MAINNET_RPC_URL="https://api.mainnet-beta.solana.com"
VALIDATOR_IDENTITY="YourValidatorIdentityPubkey"
EOF
```

Replace the RPC URL with your preferred endpoint and the identity with your validator's public key.

### Script variables

Edit the variables at the top of the script to customize behavior:

```bash
LEDGER_DIR="/mnt/ledger"            # Validator ledger directory (for agave-validator CLI)
LOG_FILE="$HOME/logs/validator.log"  # Validator log file path

# Timing
BUFFER_SECONDS=60                    # Enable debug logging this far before first slot
BUFFER_AFTER_SECONDS=60              # Keep debug logging this long after last slot
MERGE_GAP_SECONDS=180                # Merge leader groups closer than 3 minutes
POLL_INTERVAL_FAR=60                 # Poll every 60s when next slot is >5 min away
POLL_INTERVAL_NEAR=30                # Poll every 30s when next slot is <5 min away

# Log filters
DEBUG_FILTER="solana=info,solana_core::bundle_stage=debug"
DEFAULT_FILTER="solana=info,agave=info"
```

### Timing diagram

```
        BUFFER_SECONDS     Leader Slots      BUFFER_AFTER_SECONDS
       |<-- 60s -->|<-- leader rotation -->|<-- 60s -->|
       |           |                       |           |
  debug logging ON                              debug logging OFF
       |           |                       |           |
       |<------------- capture window -------------------->|
```

When multiple leader rotations are within `MERGE_GAP_SECONDS` (3 min) of each other, they are merged into a single capture window to avoid repeatedly toggling the log filter:

```
  Group 1         Gap < 3min        Group 2
  |-- slots --|   ...............   |-- slots --|
  |<------------- single capture window -------->|
```

## Output

Capture results are saved to the `captures/` directory:

- `bundle_txns_YYYYMMDD_HHMMSS.txt` — Human-readable text format
- `bundle_txns_YYYYMMDD_HHMMSS.json` — JSON format for programmatic use

Discord notifications include:
- Slot range and number of leader rotations
- Capture window duration
- Bundle and transaction counts
- Success rate (when bundles are found)

## Running as a systemd Service

For production use, run the monitor as a systemd service so it starts automatically and restarts on failure.

### Service file

Create `/etc/systemd/system/leader-capture-monitor.service`:

```ini
[Unit]
Description=Leader Slot Bundle Capture Monitor
After=network-online.target sol.service
Wants=network-online.target
# Optional: only run when the validator is running
# BindsTo=sol.service

[Service]
Type=simple
User=sol
Group=sol
ExecStart=/home/sol/bam-leader-activity/leader-capture-monitor.sh
Restart=on-failure
RestartSec=30

# Logging
StandardOutput=append:/home/sol/logs/leader-capture-monitor.log
StandardError=append:/home/sol/logs/leader-capture-monitor.log

# Environment (PATH for solana, agave-validator, jq, etc.)
Environment="PATH=/home/sol/.local/share/solana/install/active_release/bin:/usr/local/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable leader-capture-monitor
sudo systemctl start leader-capture-monitor
```

### Manage

```bash
# Check status
sudo systemctl status leader-capture-monitor

# View logs
tail -f ~/logs/leader-capture-monitor.log

# Restart after config changes
sudo systemctl restart leader-capture-monitor
```

### Log rotation

Add a logrotate config at `/etc/logrotate.d/leader-capture-monitor`:

```
/home/sol/logs/leader-capture-monitor.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
```

## Troubleshooting

### "Validator config not found"

Create `~/.config/validator/rpc.conf` with `MAINNET_RPC_URL` and `VALIDATOR_IDENTITY` (see [Configuration](#configuration)).

### "Could not get current slot"

- Verify the RPC URL is reachable: `solana -u $RPC_URL slot`
- Check that the Solana CLI is in your PATH

### "No upcoming leader slots found in this epoch"

- Normal near the end of an epoch — the monitor will retry automatically
- Verify your identity is correct: `solana -u $RPC_URL leader-schedule | grep $VALIDATOR_IDENTITY`

### "Failed to set log filter"

- The `agave-validator` CLI must be able to reach the validator's admin RPC socket
- Verify ledger directory is correct: `ls /mnt/ledger/admin.rpc`
- Test manually: `agave-validator -l /mnt/ledger set-log-filter "solana=info"`

### No bundles captured during leader slot

- Confirm BAM is connected and sending bundles (check `bam-log-monitor.sh` output)
- The capture window may have missed the slot due to timing drift — try increasing `BUFFER_SECONDS`
- Check that `bundle-txn-signatures.py` can parse the log: `./bundle-txn-signatures.py ~/logs/validator.log --summary`

## Related Scripts

- `bundle-txn-signatures.py` — Extraction script used by the monitor (see [README](README-bundle-txn-signatures.md))
- `capture-bundle-txns.sh` — Simpler manual capture for a fixed duration (no leader schedule awareness)
- `bam-log-monitor.sh` — Real-time BAM error/anomaly monitoring
- `bam-leader-activity.py` — Post-hoc analysis of bundle activity across leader slots
