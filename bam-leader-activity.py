#!/usr/bin/env python3
"""
BAM Leader Slot Activity Analyzer

Analyzes validator logs to correlate BAM bundle activity with leader slots.
Produces a table showing when bundles were received during leader slots.

Supports reading from a log file or from journalctl.
"""

import re
import sys
import subprocess
from collections import defaultdict
from datetime import datetime

# =============================================================================
# CONFIGURATION - Set your defaults here
# =============================================================================
VALIDATOR_LOG = "/home/sol/logs/validator.log"
SERVICE_NAME = "sol.service"
# =============================================================================

def parse_timestamp(line):
    """Extract timestamp from log line, return as (datetime, minute_key)"""
    match = re.match(r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
    if match:
        ts_str = match.group(1)
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
            minute_key = ts_str[:16]  # YYYY-MM-DDTHH:MM
            return dt, minute_key
        except ValueError:
            pass
    return None, None

def print_usage():
    print(f"""BAM Leader Slot Activity Analyzer

Usage:
  {sys.argv[0]}                      Use default log file ({VALIDATOR_LOG})
  {sys.argv[0]} /path/to/file.log    Read from specified log file
  {sys.argv[0]} -j [service]         Read from journalctl (default: {SERVICE_NAME})
  {sys.argv[0]} --journal [service]  Read from journalctl (default: {SERVICE_NAME})

Examples:
  {sys.argv[0]}                      # Use default log file
  {sys.argv[0]} /var/log/solana.log  # Use specific log file
  {sys.argv[0]} -j                   # Use journalctl with default service
  {sys.argv[0]} -j myvalidator       # Use journalctl with myvalidator.service
""")

def get_lines_from_file(log_file):
    """Generator that yields lines from a log file"""
    try:
        with open(log_file, 'r', errors='replace') as f:
            for line in f:
                yield line
    except FileNotFoundError:
        print(f"Error: File not found: {log_file}")
        sys.exit(1)
    except PermissionError:
        print(f"Error: Permission denied: {log_file}")
        sys.exit(1)

def get_lines_from_journalctl(service):
    """Generator that yields lines from journalctl for a service"""
    if not service.endswith('.service'):
        service = f"{service}.service"

    cmd = ['journalctl', '-u', service, '--no-pager', '-o', 'cat']

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        for line in process.stdout:
            yield line

        process.wait()
        if process.returncode != 0:
            stderr = process.stderr.read()
            if stderr:
                print(f"Warning: journalctl returned: {stderr.strip()}")

    except FileNotFoundError:
        print("Error: journalctl not found. Is systemd installed?")
        sys.exit(1)
    except Exception as e:
        print(f"Error running journalctl: {e}")
        sys.exit(1)

def analyze_logs(line_source, source_name):
    """Analyze log lines and produce the report"""

    print(f"Analyzing: {source_name}")
    print("Please wait, processing logs...\n")

    # Data structures to collect metrics per minute
    bundle_data = defaultdict(lambda: {"bundles": 0, "results_sent": 0, "count": 0})
    slot_data = defaultdict(list)  # minute -> list of slots

    # Regex patterns
    bundle_rx = re.compile(r'bundle_received=(\d+)i')
    results_rx = re.compile(r'bundleresult_sent=(\d+)i')
    slot_rx = re.compile(r'bank frozen: (\d+)')

    line_count = 0
    for line in line_source:
        line_count += 1
        _, minute_key = parse_timestamp(line)
        if not minute_key:
            continue

        # Check for BAM metrics
        if 'bam_connection-metrics' in line:
            bundle_match = bundle_rx.search(line)
            results_match = results_rx.search(line)

            if bundle_match:
                bundles = int(bundle_match.group(1))
                bundle_data[minute_key]["bundles"] += bundles
                bundle_data[minute_key]["count"] += 1

            if results_match:
                results = int(results_match.group(1))
                bundle_data[minute_key]["results_sent"] += results

        # Check for bank frozen (slot info)
        elif 'bank frozen:' in line:
            slot_match = slot_rx.search(line)
            if slot_match:
                slot = int(slot_match.group(1))
                slot_data[minute_key].append(slot)

    if line_count == 0:
        print("No log lines found.")
        sys.exit(1)

    # Filter to only minutes with bundle activity (bundles > 0)
    active_minutes = sorted([m for m, d in bundle_data.items() if d["bundles"] > 0])

    if not active_minutes:
        print(f"No bundle activity found in {line_count:,} log lines.")
        sys.exit(0)

    # Print table header
    print("=" * 85)
    print(f"{'Time (UTC)':<20} | {'Slot Range':<25} | {'Bundles':>10} | {'Results Sent':>12}")
    print("-" * 85)

    # Totals for summary
    total_bundles = 0
    total_results = 0
    total_periods = 0

    for minute in active_minutes:
        data = bundle_data[minute]
        slots = sorted(slot_data.get(minute, []))

        # Format slot range
        if slots:
            if len(slots) == 1:
                slot_range = str(slots[0])
            else:
                slot_range = f"{slots[0]} - {slots[-1]}"
        else:
            slot_range = "(no slot data)"

        bundles = data["bundles"]
        results = data["results_sent"]

        print(f"{minute:<20} | {slot_range:<25} | {bundles:>10,} | {results:>12,}")

        total_bundles += bundles
        total_results += results
        total_periods += 1

    # Print summary
    print("-" * 85)
    periods_str = f"{total_periods} periods"
    print(f"{'TOTAL':<20} | {periods_str:<25} | {total_bundles:>10,} | {total_results:>12,}")
    print("=" * 85)

    # Additional stats
    if active_minutes:
        first_time = active_minutes[0].replace('T', ' ')
        last_time = active_minutes[-1].replace('T', ' ')
        print(f"\nTime range: {first_time} to {last_time} UTC")
        print(f"Leader periods: {total_periods}")
        print(f"Total bundles received: {total_bundles:,}")
        print(f"Total bundle results sent: {total_results:,}")

        if total_periods > 1:
            avg_bundles = total_bundles / total_periods
            print(f"Average bundles per leader period: {avg_bundles:,.0f}")

def main():
    # Parse arguments
    if len(sys.argv) == 1:
        # No arguments - use default log file
        analyze_logs(get_lines_from_file(VALIDATOR_LOG), VALIDATOR_LOG)

    elif sys.argv[1] in ['-h', '--help']:
        print_usage()
        sys.exit(0)

    elif sys.argv[1] in ['-j', '--journal']:
        # Use journalctl
        if len(sys.argv) > 2:
            service = sys.argv[2]
        else:
            service = SERVICE_NAME

        if not service.endswith('.service'):
            display_name = f"{service}.service"
        else:
            display_name = service

        analyze_logs(get_lines_from_journalctl(service), f"journalctl -u {display_name}")

    else:
        # Assume it's a log file path
        log_file = sys.argv[1]
        analyze_logs(get_lines_from_file(log_file), log_file)

if __name__ == "__main__":
    main()
