# BAM Leader Activity Analyzer

Analyzes Solana validator logs to correlate BAM (Block Assembly Marketplace) bundle activity with leader slots.

## Usage

```bash
./bam-leader-activity.py [OPTIONS]
```

## Options

| Option | Description |
|--------|-------------|
| (none) | Use default log file (set via `VALIDATOR_LOG` in script) |
| `/path/to/file.log` | Read from specified log file |
| `-j [service]` | Read from journalctl using specified service (default: `sol.service`) |
| `--journal [service]` | Same as `-j` |
| `-h` | Show help message |
| `--help` | Same as `-h` |

## Examples

```bash
# Use default log file
./bam-leader-activity.py

# Use a specific log file
./bam-leader-activity.py /home/sol/logs/validator.log

# Read from journalctl with default service (sol.service)
./bam-leader-activity.py -j

# Read from journalctl with a specific service
./bam-leader-activity.py -j myvalidator
./bam-leader-activity.py --journal agave

# Show help
./bam-leader-activity.py -h
./bam-leader-activity.py --help
```

## Configuration

Edit the variables at the top of the script to set your defaults:

```python
VALIDATOR_LOG = "/home/sol/logs/validator.log"  # Default log file path
SERVICE_NAME = "sol.service"                     # Default systemd service name
VOTE_CU_COST = 3428                              # Vote transaction CU cost (from Solana source)
```

## Output

### Bundle Activity Table

| Column | Description |
|--------|-------------|
| Time (UTC) | Minute window when bundle activity occurred |
| Slot Range | The slots being processed during that period |
| Bundles | Number of bundles received from block builders |
| Results Sent | Number of bundle results sent back |
| % Sent | Percentage of bundles that received results |

### Failures Table (shown only if failures detected)

| Column | Description |
|--------|-------------|
| Time (UTC) | Minute window when failures occurred |
| Slot Range | The slots being processed during that period |
| Sched Fail | Scheduler failures (bundle_forward_to_scheduler_fail) |
| Outbound Fail | Outbound message failures (outbound_fail) |
| Total | Total failures for that period |

### Leader Slot Metrics Table

| Column | Description |
|--------|-------------|
| Slot | Slot number |
| Txns | Total transactions in the block |
| Votes | Estimated vote transactions |
| User | Estimated user (non-vote) transactions |
| Block CUs | Total compute units consumed |
| Time (ms) | Block production time in milliseconds |
| Total Fee | Total transaction fees (in SOL) |
| Priority Fee | Priority fees portion (in SOL) |

Skipped slots (announced but not produced) are shown inline with "SKIPPED" in the Priority Fee column and dashes for other metrics.

### Outlier Indicators

Values that are significantly above or below average are marked with indicators:

| Indicator | Meaning |
|-----------|---------|
| ▲ | Value is more than 20% above average |
| ▼ | Value is more than 20% below average |

These indicators appear on: Bundles, Results Sent, Txns, Votes, User, Block CUs, and Time (ms) columns.

## Sample Output

```
Analyzing: /home/sol/logs/validator.log
Processing logs.............................. done (11,482,524 lines)

========================================BAM BUNDLE ACTIVITY========================================
Time (UTC)           | Slot Range                |      Bundles |   Results Sent |   % Sent
---------------------------------------------------------------------------------------------------
2026-01-16T01:20     | 393779538 - 393779692     |      2,150 ▼ |        2,150 ▼ |   100.0%
2026-01-16T01:35     | 393781801 - 393781953     |      2,198 ▼ |        2,198 ▼ |   100.0%
2026-01-16T06:25     | 393825812 - 393825962     |      6,776 ▲ |        6,776 ▲ |   100.0%
...
---------------------------------------------------------------------------------------------------
TOTAL                | 22 periods                |      102,927 |        102,927 |   100.0%
(average)            |                           |        4,678 |          4,678 |
===================================================================================================

====================================================================LEADER SLOT METRICS=====================================================================
Slot                       |     Txns |    Votes |     User |       Block CUs |    Time (ms) |      Total Fee |   Priority Fee
------------------------------------------------------------------------------------------------------------------------------------------------------------
393779592                  |  1,314   |    778   |    536   |    45,354,622   |      345.2   |       0.021618 |       0.014393
393779593                  |  1,072   |    787   |    285 ▼ |    23,140,917 ▼ |      288.7   |       0.013247 |       0.007582
...
------------------------------------------------------------------------------------------------------------------------------------------------------------
TOTAL                      |  116,108 |   72,354 |   43,754 |   3,802,340,772 |        311.0 |         3.0066 |         2.3771
(92 produced, 0 skipped)   |    (avg) |    (avg) |    (avg) |           (avg) |        (avg) |                |
============================================================================================================================================================

Time range: 2026-01-16 01:20 to 2026-01-16 15:40 UTC
Leader periods: 22
Total bundles received: 102,927
Total bundle results sent: 102,927
Overall send rate: 100.0%
Average bundles per leader period: 4,678

No failures detected.

Connection health:
  Heartbeats received (during leader periods): 660
  Heartbeats received (total): 30,656
  Unhealthy connection events: 81

Leader slot summary:
  Slots produced: 92
  Slots skipped: 0
  Total transactions: 116,108 (72,354 votes, 43,754 user)
  Total compute units: 3,802,340,772
  Total fees: 3.0066 SOL
  Total priority fees: 2.3771 SOL
  Avg transactions per slot: 1,262
  Avg block time: 311.0 ms
```

## Requirements

- Python 3.6+
- No external libraries required (uses only standard library)
- For journalctl mode: systemd-based system with journalctl available

## Log Source Options

### File Mode (default)
Reads directly from a validator log file. This is typically faster and works with rotated/archived logs.

### Journalctl Mode (-j)
Reads from systemd journal. Useful when:
- Validator logs to systemd journal instead of a file
- You want to analyze logs without knowing the exact file path
- Log files are not persisted to disk

Note: If your validator uses `--log /path/to/file` in its startup command, logs go to that file, not journalctl.

## How It Works

1. Scans validator log for `bam_connection-metrics` entries
2. Identifies periods with non-zero `bundle_received` values
3. Correlates timestamps with `bank frozen` entries to get slot numbers
4. Tracks failure metrics (`bundle_forward_to_scheduler_fail`, `outbound_fail`)
5. Tracks connection health (`heartbeat_received`, `unhealthy_connection_count`)
6. Collects per-slot leader metrics from `cost_tracker_stats` and timing logs
7. Detects skipped leader slots by comparing announced vs produced slots
8. Aggregates data and produces the report

Bundle activity only occurs during your validator's leader slots, so this effectively shows your leader slot activity with BAM.

## Metrics Tracked

### From `bam_connection-metrics` log entries:

| Field | Description |
|-------|-------------|
| `bundle_received` | Bundles received from BAM block builders |
| `bundleresult_sent` | Bundle execution results sent back to builders |
| `bundle_forward_to_scheduler_fail` | Failed attempts to forward bundle to transaction scheduler |
| `outbound_fail` | Failed outbound messages to BAM node |
| `heartbeat_received` | Heartbeats received from BAM node |
| `unhealthy_connection_count` | Connection health check failures |

### From `cost_tracker_stats,is_leader=true` log entries:

| Field | Description |
|-------|-------------|
| `bank_slot` | Slot number for the block |
| `transaction_count` | Total transactions included in the block |
| `vote_cost` | Compute units consumed by vote transactions |
| `block_cost` | Total compute units consumed by the block |
| `total_transaction_fee` | Total transaction fees collected (in lamports) |
| `total_priority_fee` | Priority fees portion of total fees (in lamports) |

### From `broadcast-process-shreds-stats` log entries:

| Field | Description |
|-------|-------------|
| `slot` | Slot number |
| `slot_broadcast_time` | Time to broadcast block shreds (microseconds) |

### From `replay_stage-my_leader_slot` log entries:

| Field | Description |
|-------|-------------|
| `slot` | Announced leader slot number |

### Derived metrics:

| Metric | Calculation |
|--------|-------------|
| Vote transactions | `vote_cost / 3428` (SIMPLE_VOTE_USAGE_COST from Solana source) |
| User transactions | `transaction_count - vote_transactions` |
| Block time (ms) | `slot_broadcast_time / 1000` |
| Skipped slots | Slots in `replay_stage-my_leader_slot` but not in `cost_tracker_stats` |
