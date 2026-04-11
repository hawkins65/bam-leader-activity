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

The authoritative unit file is tracked in this repo at [`leader-capture-monitor.service`](leader-capture-monitor.service). Install it with:

```bash
sudo cp /home/sol/bam-leader-activity/leader-capture-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Key settings and the reasoning behind the non-obvious ones:

| Setting | Value | Why |
|---|---|---|
| `After=network.target sol.service` | | Wait for network and the validator unit before launching. |
| `Wants=sol.service` | | Soft dependency — if the validator unit isn't present the capture monitor still starts; if it restarts we don't get dragged down with it. |
| `Restart=always` + `RestartSec=10` | | The monitor is a long-lived poll loop. Any exit is unexpected, so restart unconditionally with a short backoff. |
| `User=sol` | | Runs as the validator user so it can reach `~/.config/validator/rpc.conf`, `~/validator.sh`, and the capture output directory. |
| `Environment=PATH=...` | | Cron/systemd start with a minimal PATH; the script needs Solana CLI tools (`agave-validator`, `solana`, etc.) so we prepend the install dir. |
| `ExecStart=/home/sol/bam-leader-activity/leader-capture-monitor.sh` | | Absolute path to the tracked script. |
| `StandardOutput=null`, `StandardError=null` | | **Intentional, not a bug.** The script self-redirects to `~/logs/leader-capture-monitor.log` as its first action. Using `StandardOutput=append:` here causes a `wait`/`pipefail` FD-inheritance deadlock in bash subshells (see commit `98c7199`), so we route logging through the script instead. |

### Enable and start

```bash
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

## Hourly Log Summary (cron)

A separate cron entry drives [`hourly_log_error_summary.py`](hourly_log_error_summary.py), which runs at the top of every hour and produces a combined Discord report that includes a leader-slot earnings roll-up over the past hour.

The script:

- scans every `~/logs/*.log` file for genuine errors vs. tracked low-severity noise in the window
- extracts BAM connectivity metrics and bundle/heartbeat counts from `validator.log`
- iterates `captures/slot_txns_*.json` produced by `leader-capture-monitor.sh` in the window and rolls up leader fees, Jito tip revenue, total revenue, and tip anomaly counts
- sends the merged report to Discord with an AI-generated severity assessment via the Claude API

### Crontab entry

```
0 * * * * /home/sol/python/venv/bin/python3 /home/sol/bam-leader-activity/hourly_log_error_summary.py >> /home/sol/logs/hourly_log_summary.log 2>&1
```

Install with:

```bash
crontab -e
# append the line above, save, exit
```

Notes on the specific invocation:

- **Explicit interpreter path.** `/home/sol/python/venv/bin/python3` pins the Python interpreter to a venv that has `anthropic` and any other dependencies installed. The script's shebang points at the same venv, so executing the script directly would also work, but the explicit path makes the dependency visible in `crontab -l`.
- **Absolute script path.** Cron always uses absolute paths — the script lives in this repo, so update the path if you clone to a different location.
- **Stdout/stderr appended to a log.** Cron emails output by default; appending to `hourly_log_summary.log` keeps everything on disk and avoids noisy mail.

### Prerequisites

- `/home/sol/python/venv/bin/python3` — Python venv containing `anthropic` (or set up via `python3 -m venv /home/sol/python/venv && /home/sol/python/venv/bin/pip install anthropic`).
- `~/.config/anthropic/api_key` or `ANTHROPIC_API_KEY` in the environment — required for AI summaries. If absent the script falls back to a raw error dump but still runs.
- `~/.config/discord/webhook` — Discord webhook URL (same file the capture monitor uses).
- `~/999_discord_embed.sh` — shared embed helper (see [Discord Notifications](README.md#discord-notifications)).
- `leader-capture-monitor.service` running — without it, `captures/slot_txns_*.json` never gets written and the leader roll-up section is silently omitted from the hourly report.

### Testing without waiting for cron

```bash
# Dry run — collects, analyzes, prints the embed to stdout, does NOT post to Discord
/home/sol/python/venv/bin/python3 /home/sol/bam-leader-activity/hourly_log_error_summary.py --dry-run --verbose
```

If captures exist in the last hour, the log should include a line like:

```
Leader: N rotation(s), M txns, X.XXXXXX SOL fees + Y.YYYYYY SOL tips = Z.ZZZZZZ SOL revenue
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
