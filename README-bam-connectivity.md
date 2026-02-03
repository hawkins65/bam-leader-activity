# BAM Connectivity Status Monitor

Monitors BAM (Block Assembly Marketplace) connection status from Solana validator logs. Shows connection events, health metrics, errors, and overall connectivity summary.

## Usage

```bash
./bam-connectivity.py [OPTIONS]
```

## Options

| Option | Description |
|--------|-------------|
| (none) | Use default log file (set via `DEFAULT_LOG_PATH` in script) |
| `/path/to/file.log` | Read from specified log file |
| `-j [service]` | Read from journalctl using specified service (default: `sol.service`) |
| `--journal [service]` | Same as `-j` |
| `--hours N` | Time span for journalctl in hours (default: 24) |
| `--verbose` | Show all connection events (not just state changes) |
| `--no-metrics` | Skip per-minute health metrics table |
| `-h`, `--help` | Show help message |

## Examples

```bash
# Use default log file
./bam-connectivity.py

# Use a specific log file
./bam-connectivity.py /home/sol/logs/validator.log

# Read from journalctl, last 24 hours
./bam-connectivity.py -j

# Read from journalctl, last 4 hours
./bam-connectivity.py -j --hours 4

# Show all events (including info-level)
./bam-connectivity.py -j --verbose

# Skip the per-minute metrics table
./bam-connectivity.py -j --no-metrics
```

## Configuration

Edit the variables at the top of the script to set your defaults:

```python
DEFAULT_LOG_PATH = "~/logs/validator.log"  # Default log file path
DEFAULT_SERVICE = "sol.service"            # Default systemd service name
DEFAULT_HOURS = 24                         # Default time span for journalctl (hours)
```

## Output

### Connection Events Table

Shows significant connection state changes. By default, only warnings and errors are shown. Use `--verbose` to see all events.

| Level | Description |
|-------|-------------|
| INFO | Connection established, URL changes, identity changes |
| WARN | Connection not healthy, connection lost |
| ERROR | Connection failures, authentication errors, stream errors |

### Health Metrics Table

Per-minute aggregation of connection health metrics from `bam_connection-metrics` datapoint:

| Column | Description |
|--------|-------------|
| Time (UTC) | Minute window |
| Heartbeats | Heartbeats received from BAM node |
| Unhealthy | Health check failure counts |
| Bundles | Bundles received from block builders |
| Out Fail | Failed outbound messages |

Minutes with unhealthy events are highlighted.

### Summary

- Time range covered
- Connection/disconnection event counts
- Estimated uptime percentage (based on healthy vs unhealthy minutes)
- Error and warning breakdown
- Overall status: HEALTHY / MOSTLY HEALTHY / ISSUES DETECTED

## Sample Output

```
Analyzing: journalctl -u sol.service (last 24h)
Processing logs.............. done (5,234,567 lines)

========================================BAM CONNECTION EVENTS========================================
Timestamp                | Level  | Message
----------------------------------------------------------------------------------------------------
2026-01-20T08:15:23      | INFO   | BAM connection established
2026-01-20T14:32:45      | WARN   | Connection not healthy (no heartbeat for 6s)
2026-01-20T14:32:46      | INFO   | BAM connection established
----------------------------------------------------------------------------------------------------
====================================================================================================

=======================================HEALTH METRICS (per minute)===================================
Time (UTC)           |   Heartbeats |  Unhealthy |    Bundles |   Out Fail
----------------------------------------------------------------------------------------------------
2026-01-20T08:15     |          120 |          0 |          0 |          0
2026-01-20T08:16     |          120 |          0 |          0 |          0
...
2026-01-20T14:32     |           80 |          3 |        156 |          0
...
----------------------------------------------------------------------------------------------------
TOTAL                |       28,560 |          3 |      4,521 |          0
====================================================================================================

============================================SUMMARY=================================================
Time range: 2026-01-20 08:15:23 to 2026-01-20 22:45:12 (14.5h)

Connection events:
  Connections established: 2
  Disconnections/unhealthy: 1

Connection health:
  Active minutes: 872
  Healthy minutes: 871
  Unhealthy minutes: 1
  Estimated uptime: 99.9%

Issues detected:
  WARN  - not_healthy: 1

Overall BAM status: MOSTLY HEALTHY - some warnings
====================================================================================================
```

## Connection Events Tracked

### State Changes (info level)
| Event | Log Message |
|-------|-------------|
| Connected | `BAM connection established` |
| URL Changed | `BAM URL changed` |
| Manager Init | `BAM Manager: Added BAM connection key updater` |
| New Identity | `BAM Manager: detected new identity` |
| Manual Disconnect | `bam_manually_disconnected` datapoint |

### Warnings
| Event | Log Message |
|-------|-------------|
| Not Healthy | `BAM connection not healthy` (no heartbeat for 6s) |
| Connection Lost | `BAM connection lost` |
| Identity Timeout | `BAM Manager: timed out waiting for new identity` |

### Errors
| Event | Log Message |
|-------|-------------|
| Connect Failed | `Failed to connect to BAM with url` |
| Stream Failed | `Failed to start scheduler stream` |
| Auth Failed | `Failed to prepare auth response` / `send initial auth proof` / `get auth challenge` |
| Inbound Closed | `Inbound stream closed` |
| Inbound Error | `Failed to receive message from inbound stream` |
| Config Failed | `Failed to get config` |
| Unsupported Msg | `Received unsupported versioned message` |

## Health Metrics

From `bam_connection-metrics` datapoint (emitted every 25ms when active):

| Metric | Description |
|--------|-------------|
| `heartbeat_received` | Heartbeats received from BAM node (expected every 5s) |
| `unhealthy_connection_count` | Health check intervals where connection was unhealthy |
| `bundle_received` | Bundles received from block builders |
| `outbound_fail` | Failed outbound message sends |

## Requirements

- Python 3.6+
- No external libraries required (uses only standard library)
- For journalctl mode: systemd-based system with journalctl available

## Troubleshooting

### "No BAM activity detected in logs"

The validator does not have `--bam-url` configured. To enable BAM:

```bash
# Add to validator startup command:
--bam-url <BAM_NODE_URL>
```

### High unhealthy count

- Check network connectivity to BAM node
- Verify BAM node URL is correct
- Check if BAM node is operational

### Frequent reconnections

- Network instability between validator and BAM node
- BAM node restarts or maintenance
- Identity rotation (expected during hot-swap)

## Related Scripts

- `bam-leader-activity.py` - Analyzes bundle activity during leader slots
- `bundle-txn-signatures.py` - Extracts transaction signatures from bundle logs (requires DEBUG logging)
