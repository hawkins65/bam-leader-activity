# BAM Leader Activity Analyzer

Analyzes Solana validator logs to correlate BAM (Block Assembly Marketplace) bundle activity with leader slots.

## Usage

```bash
./bam-leader-activity.py [OPTIONS]
```

## Options

| Option | Description |
|--------|-------------|
| (none) | Use default log file (set via `DEFAULT_LOG_PATH` in script) |
| `/path/to/file.log` | Read from specified log file |
| `-j [service]` | Read from journalctl using specified service (default: `sol.service`) |
| `--journal [service]` | Same as `-j` |
| `--hours N` | Time span for journalctl in hours (default: 24) |
| `-h` | Show help message |
| `--help` | Same as `-h` |

## Examples

```bash
# Use default log file
./bam-leader-activity.py

# Use a specific log file
./bam-leader-activity.py /home/sol/logs/validator.log

# Read from journalctl with default service (sol.service), last 24 hours
./bam-leader-activity.py -j

# Read from journalctl with a specific service
./bam-leader-activity.py -j myvalidator
./bam-leader-activity.py --journal agave

# Read from journalctl with custom time span
./bam-leader-activity.py -j --hours 48        # Last 48 hours
./bam-leader-activity.py -j sol --hours 12    # sol.service, last 12 hours
./bam-leader-activity.py -j --hours 6         # Last 6 hours

# Show help
./bam-leader-activity.py -h
./bam-leader-activity.py --help
```

## Configuration

Edit the variables at the top of the script to set your defaults:

```python
DEFAULT_LOG_PATH = "~/logs/validator.log"  # Default log file path
DEFAULT_SERVICE = "sol.service"            # Default systemd service name
DEFAULT_HOURS = 24                         # Default time span for journalctl (hours)
VOTE_CU_COST = 3428                        # Vote transaction CU cost (from Solana source)
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

Values that are significantly above or below median are marked with indicators:

| Indicator | Meaning |
|-----------|---------|
| ▲ | Value is more than 20% above median |
| ▼ | Value is more than 20% below median |
| ◆ | Small block (user txns AND block CUs both below 25% of median) |

These indicators appear on: Bundles, Results Sent, Txns, Votes, User, Block CUs, and Time (ms) columns.

Small blocks are slots where minimal useful work was done (e.g., mostly vote transactions with very few user transactions). These are marked with ◆ after the slot number and counted separately in the summary.

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
(median)             |                           |        4,678 |          4,678 |
===================================================================================================

====================================================================LEADER SLOT METRICS=====================================================================
Slot                       |     Txns |    Votes |     User |       Block CUs |    Time (ms) |      Total Fee |   Priority Fee
------------------------------------------------------------------------------------------------------------------------------------------------------------
393779592                  |  1,314   |    778   |    536   |    45,354,622   |      345.2   |       0.021618 |       0.014393
393779593                  |  1,072   |    787   |    285 ▼ |    23,140,917 ▼ |      288.7   |       0.013247 |       0.007582
393779594 ◆                |    769 ▼ |    769   |      0 ▼ |     2,636,132 ▼ |      301.4   |       0.003860 |    0.000000000
...
------------------------------------------------------------------------------------------------------------------------------------------------------------
TOTAL                      |  116,108 |   72,354 |   43,754 |   3,802,340,772 |              |         3.0066 |         2.3771
(92 produced, 0 skipped)   |          |          |          |                 |              |                |
MEDIAN                     |    1,262 |      786 |      476 |      41,329,791 |        311.0 |       0.032681 |       0.025838
============================================================================================================================================================

Time range: 2026-01-16 01:20 to 2026-01-16 15:40 UTC
Leader periods: 22
Total bundles received: 102,927
Total bundle results sent: 102,927
Overall send rate: 100.0%
Median bundles per leader period: 4,678

No failures detected.

Connection health:
  Heartbeats received (during leader periods): 660
  Heartbeats received (total): 30,656
  Unhealthy connection events: 81

Leader slot summary:
  Slots produced: 92
  Slots skipped: 0
  Small blocks: 2 (2.2%) ◆
  Total transactions: 116,108 (72,354 votes, 43,754 user)
  Total compute units: 3,802,340,772
  Total fees: 3.0066 SOL
  Total priority fees: 2.3771 SOL
  Per-block median: 1,262 txns (786 votes, 476 user), 41,329,791 CUs
  Per-block median: 0.032681 SOL fees, 0.025838 SOL priority
  Median block time: 311.0 ms
```

## Requirements

- Python 3.6+
- No external libraries required (uses only standard library)
- For journalctl mode: systemd-based system with journalctl available

## Log Source Options

### File Mode (default)
Reads directly from a validator log file. This is typically faster and works with rotated/archived logs.

### Journalctl Mode (-j)
Reads from systemd journal. By default, analyzes the last 24 hours of logs (configurable with `--hours`). Useful when:
- Validator logs to systemd journal instead of a file
- You want to analyze logs without knowing the exact file path
- Log files are not persisted to disk
- You want to limit analysis to a specific time window

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

| Field | Description | Grep Example |
|-------|-------------|--------------|
| `bundle_received` | Bundles received from BAM block builders | `grep "bam_connection-metrics" validator.log \| grep "bundle_received"` |
| `bundleresult_sent` | Bundle execution results sent back to builders | `grep "bundleresult_sent" validator.log` |
| `bundle_forward_to_scheduler_fail` | Failed attempts to forward bundle to scheduler | `grep "bundle_forward_to_scheduler_fail" validator.log` |
| `outbound_fail` | Failed outbound messages to BAM node | `grep "outbound_fail" validator.log` |
| `heartbeat_received` | Heartbeats received from BAM node | `grep "heartbeat_received" validator.log` |
| `unhealthy_connection_count` | Connection health check failures | `grep "unhealthy_connection_count" validator.log` |

### From `cost_tracker_stats,is_leader=true` log entries:

| Field | Description | Grep Example |
|-------|-------------|--------------|
| `bank_slot` | Slot number for the block | `grep "cost_tracker_stats,is_leader=true" validator.log` |
| `transaction_count` | Total transactions included in the block | `grep "cost_tracker_stats,is_leader=true" validator.log` |
| `vote_cost` | Compute units consumed by vote transactions | `grep "cost_tracker_stats,is_leader=true" validator.log` |
| `block_cost` | Total compute units consumed by the block | `grep "cost_tracker_stats,is_leader=true" validator.log` |
| `total_transaction_fee` | Total transaction fees collected (in lamports) | `grep "cost_tracker_stats,is_leader=true" validator.log` |
| `total_priority_fee` | Priority fees portion of total fees (in lamports) | `grep "cost_tracker_stats,is_leader=true" validator.log` |

### From `broadcast-process-shreds-stats` log entries:

| Field | Description | Grep Example |
|-------|-------------|--------------|
| `slot` | Slot number | `grep "broadcast-process-shreds-stats" validator.log` |
| `slot_broadcast_time` | Time to broadcast block shreds (microseconds) | `grep "slot_broadcast_time" validator.log` |

### From `replay_stage-my_leader_slot` log entries:

| Field | Description | Grep Example |
|-------|-------------|--------------|
| `slot` | Announced leader slot number | `grep "replay_stage-my_leader_slot" validator.log` |

### Derived metrics:

| Metric | Calculation |
|--------|-------------|
| Vote transactions | `vote_cost / 3428` (SIMPLE_VOTE_USAGE_COST from Solana source) |
| User transactions | `transaction_count - vote_transactions` |
| Block time (ms) | `slot_broadcast_time / 1000` |
| Skipped slots | Slots announced in `replay_stage-my_leader_slot` but missing from `cost_tracker_stats` |
| Small blocks | Slots where user txns AND block CUs are both below 25% of median |

### Skipped Slot Detection

Skipped slots are detected by comparing two sets of data:

1. **Announced slots** - collected from `replay_stage-my_leader_slot` log entries, which indicate when the validator is scheduled to be leader
2. **Produced slots** - collected from `cost_tracker_stats,is_leader=true` log entries, which are only emitted when a block is actually produced

A slot is marked as **skipped** if it appears in the announced set but not in the produced set. This happens when the validator was scheduled to lead but failed to produce a block (due to timing issues, fork choice, etc.).
