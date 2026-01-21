# Bundle Transaction Signature Extractor

Extract transaction signatures from Jito-Solana bundle execution logs. This tool parses DEBUG-level logs to show which transactions were included in bundles during validator leader slots.

## Prerequisites

DEBUG logging must be enabled for the `bundle_stage` module to capture transaction signatures.

### Option 1: Environment Variable (requires validator restart)

```bash
RUST_LOG="solana=info,solana_core::bundle_stage=debug"
```

### Option 2: Runtime Change (no restart required)

```bash
agave-validator -l /path/to/ledger set-log-filter "solana=info,solana_core::bundle_stage=debug"
```

The default log level if `RUST_LOG` is not set is: `solana=info,agave=info`

## Scripts

### bundle-txn-signatures.py

Main script to extract and display bundle transaction signatures.

**Usage:**

```bash
# From log file
./bundle-txn-signatures.py /path/to/validator.log

# From journalctl
./bundle-txn-signatures.py -j [service] --hours N

# Output formats
./bundle-txn-signatures.py -j --json      # JSON output
./bundle-txn-signatures.py -j --csv       # CSV output
./bundle-txn-signatures.py -j --summary   # Summary only
```

**Options:**

| Option | Description |
|--------|-------------|
| `-j, --journal [service]` | Read from journalctl (default: sol.service) |
| `--hours N` | Time span for journalctl (default: 24) |
| `--summary` | Show summary only, no individual signatures |
| `--csv` | Output in CSV format |
| `--json` | Output in JSON format |
| `--explorer-url URL` | Base URL for explorer links (default: https://solscan.io/tx) |
| `--cluster CLUSTER` | Solana cluster: mainnet, testnet, devnet (default: mainnet) |
| `--no-links` | Don't generate explorer links |

**Examples:**

```bash
# Mainnet (default)
./bundle-txn-signatures.py ~/logs/validator.log

# Testnet
./bundle-txn-signatures.py ~/logs/validator.log --cluster testnet

# Devnet with JSON output
./bundle-txn-signatures.py -j --cluster devnet --json

# Custom explorer
./bundle-txn-signatures.py -j --explorer-url https://explorer.solana.com/tx
```

### capture-bundle-txns.sh

Automated script to temporarily enable DEBUG logging, capture for a specified duration, then restore default logging and extract transactions.

**Configuration (edit script to customize):**

```bash
LEDGER_DIR="/mnt/ledger"           # Validator ledger directory
LOG_FILE="$HOME/logs/validator.log" # Validator log file
CAPTURE_DURATION=300                # Capture duration in seconds (5 min)
```

**Usage:**

```bash
# Run immediately
./capture-bundle-txns.sh

# Schedule for a specific time using 'at'
echo "/home/sol/bam-leader-activity/capture-bundle-txns.sh" | at 22:08 UTC

# Schedule via cron (crontab -e)
8 22 20 1 * /home/sol/bam-leader-activity/capture-bundle-txns.sh
```

**Output:**

Results are saved to the `captures/` directory:
- `bundle_txns_YYYYMMDD_HHMMSS.txt` - Human-readable text format
- `bundle_txns_YYYYMMDD_HHMMSS.json` - JSON format for programmatic use

## Output Format

### Text Output

```
==============================BUNDLE TRANSACTION SIGNATURES==============================

Summary:
  Total bundles processed: 47
  Total transactions: 142
  Avg transactions per bundle: 3.0
  Explorer URL: https://solscan.io/tx

Results breakdown:
  success: 42 (89.4%)
  lock_error: 3 (6.4%)
  tx_failure: 2 (4.3%)

-----------------------------------------------------------------------------------------
Timestamp                | Slot         | Txns  | Result          | Transaction Signatures
-----------------------------------------------------------------------------------------
2026-01-20T14:32:15      | 312847562    |     3 | success         | 5Kj8...
                         |              |       |                 | 4Rt2...
                         |              |       |                 | 3Ym9...

=============================ALL TRANSACTION SIGNATURES==================================
(With explorer links to https://solscan.io/tx)

https://solscan.io/tx/5Kj8abc123...
https://solscan.io/tx/4Rt2def456...
```

### JSON Output

```json
{
  "summary": {
    "total_bundles": 47,
    "total_transactions": 142,
    "avg_txns_per_bundle": 3.02,
    "results": {
      "success": 42,
      "lock_error": 3,
      "tx_failure": 2
    }
  },
  "explorer_url": "https://solscan.io/tx",
  "cluster": "mainnet",
  "bundles": [
    {
      "timestamp": "2026-01-20T14:32:15",
      "slot": 312847562,
      "signatures": ["5Kj8...", "4Rt2...", "3Ym9..."],
      "signature_links": [
        "https://solscan.io/tx/5Kj8...",
        "https://solscan.io/tx/4Rt2...",
        "https://solscan.io/tx/3Ym9..."
      ],
      "txn_count": 3,
      "result": "success"
    }
  ]
}
```

## Bundle Execution Results

| Result | Description |
|--------|-------------|
| `success` | Bundle executed successfully |
| `lock_error` | Failed to acquire account locks |
| `tx_failure` | Transaction execution failed |
| `cost_limit` | Exceeded block cost limit |
| `bundle_cost_limit` | Exceeded bundle cost limit |
| `tip_error` | Tip program error |
| `error` | Other error |

## Troubleshooting

### No bundle execution logs found

Make sure DEBUG logging is enabled:

```bash
agave-validator -l /mnt/ledger set-log-filter "solana=info,solana_core::bundle_stage=debug"
```

### Empty results during leader slot

1. Verify the validator was actually leader during the capture period
2. Check that bundles were being sent to the validator
3. Ensure the log file contains the capture time window

### Log file too large

Use the `--hours` option with journalctl to limit the time window:

```bash
./bundle-txn-signatures.py -j --hours 1
```
