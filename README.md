# BAM Leader Activity Analyzer

Analyzes Solana validator logs to correlate BAM (Block Auction Module) bundle activity with leader slots.

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

## Sample Output

```
Analyzing: /home/sol/logs/validator.log
Please wait, processing logs...

===============================================================================================
Time (UTC)           | Slot Range                |    Bundles | Results Sent |   % Sent
-----------------------------------------------------------------------------------------------
2026-01-16T01:20     | 393779538 - 393779692     |      2,150 |        2,150 |   100.0%
2026-01-16T01:35     | 393781801 - 393781953     |      2,198 |        2,198 |   100.0%
2026-01-16T02:34     | 393790760 - 393790914     |      2,561 |        2,561 |   100.0%
2026-01-16T05:06     | 393813811 - 393813964     |      3,568 |        3,568 |   100.0%
2026-01-16T05:31     | 393817590 - 393817741     |      2,911 |        2,911 |   100.0%
...
-----------------------------------------------------------------------------------------------
TOTAL                | 22 periods                |    102,927 |      102,927 |   100.0%
===============================================================================================

Time range: 2026-01-16 01:20 to 2026-01-16 15:40 UTC
Leader periods: 22
Total bundles received: 102,927
Total bundle results sent: 102,927
Overall send rate: 100.0%
Average bundles per leader period: 4,678

No failures detected.

Connection health:
  Heartbeats received (during leader periods): 660
  Heartbeats received (total): 29,006
  Unhealthy connection events: 0 (healthy throughout)
```

### Sample Output with Failures

If failures are detected, a second table is displayed:

```
=======================================FAILURES DETECTED=======================================
Time (UTC)           | Slot Range                | Sched Fail | Outbound Fail |    Total
-----------------------------------------------------------------------------------------------
2026-01-16T05:06     | 393813811 - 393813964     |         12 |             3 |       15
-----------------------------------------------------------------------------------------------
TOTAL FAILURES       |                           |         12 |             3 |       15
===============================================================================================

Total failures: 15 (0.01% of bundles)
  Scheduler failures: 12
  Outbound failures: 3
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
6. Aggregates data by minute and produces the report

Bundle activity only occurs during your validator's leader slots, so this effectively shows your leader slot activity with BAM.

## Metrics Tracked

From `bam_connection-metrics`:
- `bundle_received` - Bundles received from BAM node
- `bundleresult_sent` - Bundle results sent back
- `bundle_forward_to_scheduler_fail` - Failed to forward bundle to scheduler
- `outbound_fail` - Failed outbound messages
- `heartbeat_received` - Heartbeats received from BAM node
- `unhealthy_connection_count` - Connection health check failures
