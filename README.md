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

## Sample Output

```
$ python3 bam-leader-activity.py
Analyzing: /home/sol/logs/validator.log
Please wait, processing logs...

=====================================================================================
Time (UTC)           | Slot Range                |    Bundles | Results Sent
-------------------------------------------------------------------------------------
2026-01-16T01:20     | 393779538 - 393779692     |      2,150 |        2,150
2026-01-16T01:35     | 393781801 - 393781953     |      2,198 |        2,198
2026-01-16T02:34     | 393790760 - 393790914     |      2,561 |        2,561
2026-01-16T05:06     | 393813811 - 393813964     |      3,568 |        3,568
2026-01-16T05:31     | 393817590 - 393817741     |      2,911 |        2,911
2026-01-16T05:44     | 393819579 - 393819729     |      3,491 |        3,491
2026-01-16T06:13     | 393823987 - 393824142     |      2,918 |        2,918
2026-01-16T06:20     | 393825049 - 393825203     |      2,618 |        2,618
2026-01-16T06:25     | 393825812 - 393825962     |      6,776 |        6,776
2026-01-16T08:18     | 393842927 - 393843073     |      3,624 |        3,624
2026-01-16T08:46     | 393847180 - 393847332     |      4,044 |        4,044
2026-01-16T08:50     | 393847788 - 393847931     |      3,117 |        3,117
2026-01-16T09:17     | 393851882 - 393852030     |      4,122 |        4,122
2026-01-16T10:06     | 393859309 - 393859463     |      3,660 |        3,660
2026-01-16T10:12     | 393860215 - 393860365     |      3,722 |        3,722
2026-01-16T10:47     | 393865529 - 393865683     |      3,240 |        3,240
2026-01-16T11:34     | 393872670 - 393872821     |      3,391 |        3,391
2026-01-16T13:05     | 393886514 - 393886664     |      4,471 |        4,471
2026-01-16T13:10     | 393887268 - 393887417     |      4,313 |        4,313
2026-01-16T14:53     | 393902923 - 393903076     |      4,506 |        4,506
2026-01-16T15:25     | 393907771 - 393907919     |     15,840 |       15,840
-------------------------------------------------------------------------------------
TOTAL                | 21 periods                |     87,241 |       87,241
=====================================================================================

Time range: 2026-01-16 01:20 to 2026-01-16 15:25 UTC
Leader periods: 21
Total bundles received: 87,241
Total bundle results sent: 87,241
Average bundles per leader period: 4,154
```

## Output

Produces a table showing:

| Column | Description |
|--------|-------------|
| Time (UTC) | Minute window when bundle activity occurred |
| Slot Range | The slots being processed during that period |
| Bundles | Number of bundles received from block builders |
| Results Sent | Number of bundle results sent back |

Includes a summary line with totals and additional statistics.

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
4. Aggregates data by minute and produces the report

Bundle activity only occurs during your validator's leader slots, so this effectively shows your leader slot activity with BAM.
