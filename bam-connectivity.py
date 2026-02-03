#!/usr/bin/env python3
"""
BAM Connectivity Status Monitor

Monitors BAM (Block Assembly Marketplace) connection status from validator logs.
Shows connection events, health metrics, errors, and overall connectivity summary.

Supports reading from a log file or from journalctl.
"""

import os
import re
import sys
import subprocess
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

# =============================================================================
# CONFIGURATION
# =============================================================================
DEFAULT_LOG_PATH = os.path.expanduser("~/logs/validator.log")
DEFAULT_SERVICE = "sol.service"
DEFAULT_HOURS = 24
DEFAULT_STARTUP_SCRIPT = os.path.expanduser("~/validator.sh")

# Table widths
TABLE_WIDTH = 100
# =============================================================================


def parse_timestamp(line):
    """Extract timestamp from log line, return as (datetime, timestamp_str)"""
    match = re.match(r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
    if match:
        ts_str = match.group(1)
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
            return dt, ts_str
        except ValueError:
            pass
    return None, None


def ping_host(host, count=5):
    """Ping a host and return average latency in ms, or None if failed"""
    try:
        result = subprocess.run(
            ['ping', '-c', str(count), '-W', '2', host],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            # Parse avg from: rtt min/avg/max/mdev = 1.234/2.345/3.456/0.123 ms
            match = re.search(r'rtt min/avg/max/mdev = [\d.]+/([\d.]+)/', result.stdout)
            if match:
                return float(match.group(1))
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


def extract_hostname_from_url(url):
    """Extract hostname from a URL"""
    if not url:
        return None
    # Add scheme if missing for urlparse
    if not url.startswith(('http://', 'https://', 'ws://', 'wss://')):
        url = 'https://' + url
    parsed = urlparse(url)
    return parsed.hostname


def extract_bam_url_from_script(script_path):
    """
    Extract --bam-url value from a validator startup script or systemd service file.

    Supports:
    - Shell scripts: --bam-url <value> or --bam-url=<value>
    - Systemd service files: same patterns within ExecStart or similar

    Returns (bam_url, error_message) tuple. If successful, error_message is None.
    """
    script_path = os.path.expanduser(script_path)

    if not os.path.exists(script_path):
        return None, f"Startup script not found: {script_path}"

    if not os.access(script_path, os.R_OK):
        return None, f"Cannot read startup script: {script_path}"

    try:
        with open(script_path, 'r', errors='replace') as f:
            content = f.read()
    except Exception as e:
        return None, f"Error reading {script_path}: {e}"

    # Pattern 1: --bam-url=<value> (no space, with equals)
    match = re.search(r'--bam-url[=\s]+([^\s\\]+)', content)
    if match:
        url = match.group(1).strip('"\'')
        return url, None

    # Pattern 2: --bam-url on one line, value on next (common in multi-line shell scripts)
    # Look for --bam-url followed by backslash-newline, then the value
    match = re.search(r'--bam-url\s*\\\s*\n\s*([^\s\\]+)', content)
    if match:
        url = match.group(1).strip('"\'')
        return url, None

    return None, f"No --bam-url found in {script_path}"


def print_usage():
    print(f"""BAM Connectivity Status Monitor

Monitors BAM connection status from validator logs.

Usage:
  {sys.argv[0]}                      Use default log file ({DEFAULT_LOG_PATH})
  {sys.argv[0]} /path/to/file.log    Read from specified log file
  {sys.argv[0]} -j [service]         Read from journalctl (default: {DEFAULT_SERVICE}, last {DEFAULT_HOURS}h)
  {sys.argv[0]} --hours N            Set time span for journalctl (default: {DEFAULT_HOURS})

Options:
  --verbose                          Show all connection events (not just state changes)
  --metrics                          Show per-minute health metrics table (hidden by default)
  --bam-url URL                      Check ping latency to BAM host (extracts hostname from URL)
  --startup-script PATH              Path to validator startup script to extract --bam-url
                                     (default: {DEFAULT_STARTUP_SCRIPT})
  --no-ping                          Skip automatic BAM host ping check

The script automatically extracts --bam-url from the startup script ({DEFAULT_STARTUP_SCRIPT})
if it exists. Use --startup-script to specify an alternative path, or --bam-url to override.

Examples:
  {sys.argv[0]}                      # Use default log file, auto-detect BAM URL
  {sys.argv[0]} -j --hours 4         # Last 4 hours from journalctl
  {sys.argv[0]} -j sol --verbose     # Show all events from sol.service
  {sys.argv[0]} -j --bam-url wss://ny.mainnet.block.engine.jito.wtf  # Override BAM URL
  {sys.argv[0]} -j --startup-script /etc/systemd/system/sol.service  # Use service file
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


def get_lines_from_journalctl(service, hours=None):
    """Generator that yields lines from journalctl for a service"""
    if not service.endswith('.service'):
        service = f"{service}.service"

    cmd = ['journalctl', '-u', service, '--no-pager', '-o', 'short-iso']
    if hours is not None:
        cmd.extend(['--since', f'{hours} hours ago'])

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


def format_duration(seconds):
    """Format duration in human readable format"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        mins = seconds / 60
        return f"{mins:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"


def analyze_logs(line_source, source_name, verbose=False, show_metrics=True, bam_url=None):
    """Analyze log lines for BAM connectivity status"""

    print(f"Analyzing: {source_name}")
    print("Processing logs", end="", flush=True)

    # Connection events
    events = []

    # Per-minute metrics from bam_connection-metrics datapoint
    minute_metrics = defaultdict(lambda: {
        "heartbeat_received": 0,
        "unhealthy_count": 0,
        "bundle_received": 0,
        "outbound_fail": 0,
        "samples": 0
    })

    # Error/warning counts
    error_counts = defaultdict(int)
    warning_counts = defaultdict(int)

    # Regex patterns for connection events
    patterns = {
        # Connection state changes (info level)
        "connected": re.compile(r'BAM connection established'),
        "url_changed": re.compile(r'BAM URL changed'),
        "identity_updater": re.compile(r'BAM Manager: Added BAM connection key updater'),
        "new_identity": re.compile(r'BAM Manager: detected new identity (\S+)'),

        # Connection problems (warn level)
        "not_healthy": re.compile(r'BAM connection not healthy'),
        "connection_lost": re.compile(r'BAM connection lost'),
        "identity_timeout": re.compile(r'BAM Manager: timed out waiting for new identity'),

        # Connection errors (error level)
        "connect_failed": re.compile(r'Failed to connect to BAM with url: ([^:]+): (.+)'),
        "stream_failed": re.compile(r'Failed to start scheduler stream: (.+)'),
        "auth_failed": re.compile(r'Failed to (prepare auth response|send initial auth proof|get auth challenge)'),
        "inbound_closed": re.compile(r'Inbound stream closed'),
        "inbound_error": re.compile(r'Failed to receive message from inbound stream: (.+)'),
        "config_failed": re.compile(r'Failed to get config: (.+)'),
        "unsupported_msg": re.compile(r'Received unsupported versioned message'),

        # URL change via admin RPC (debug level)
        "set_bam_url": re.compile(r'set_bam_url old=\s*([^,]+),\s*new=(.+)'),
    }

    # Metrics patterns
    heartbeat_rx = re.compile(r'heartbeat_received=(\d+)i')
    unhealthy_rx = re.compile(r'unhealthy_connection_count=(\d+)i')
    bundle_rx = re.compile(r'bundle_received=(\d+)i')
    outbound_fail_rx = re.compile(r'outbound_fail=(\d+)i')

    # Datapoint patterns
    manually_disconnected_rx = re.compile(r'bam_manually_disconnected.*previous_bam_url="([^"]*)"')
    identity_changed_rx = re.compile(r'bam-manager_identity-changed.*identity_changed_to="([^"]*)"')

    line_count = 0
    progress_interval = 100000
    first_ts = None
    last_ts = None

    for line in line_source:
        line_count += 1
        if line_count % progress_interval == 0:
            print(".", end="", flush=True)

        dt, ts_str = parse_timestamp(line)

        if dt:
            if first_ts is None:
                first_ts = dt
            last_ts = dt

        # Check for bam_connection-metrics datapoint
        if 'bam_connection-metrics' in line and ts_str:
            minute_key = ts_str[:16]  # YYYY-MM-DDTHH:MM

            hb_match = heartbeat_rx.search(line)
            if hb_match:
                minute_metrics[minute_key]["heartbeat_received"] += int(hb_match.group(1))

            uh_match = unhealthy_rx.search(line)
            if uh_match:
                minute_metrics[minute_key]["unhealthy_count"] += int(uh_match.group(1))

            br_match = bundle_rx.search(line)
            if br_match:
                minute_metrics[minute_key]["bundle_received"] += int(br_match.group(1))

            of_match = outbound_fail_rx.search(line)
            if of_match:
                minute_metrics[minute_key]["outbound_fail"] += int(of_match.group(1))

            minute_metrics[minute_key]["samples"] += 1
            continue

        # Check for manually disconnected datapoint
        if 'bam_manually_disconnected' in line:
            match = manually_disconnected_rx.search(line)
            prev_url = match.group(1) if match else "unknown"
            events.append({
                "timestamp": ts_str or "unknown",
                "dt": dt,
                "type": "disconnected",
                "level": "info",
                "message": f"BAM manually disconnected (was: {prev_url})"
            })
            continue

        # Check for identity changed datapoint
        if 'bam-manager_identity-changed' in line:
            match = identity_changed_rx.search(line)
            new_id = match.group(1) if match else "unknown"
            events.append({
                "timestamp": ts_str or "unknown",
                "dt": dt,
                "type": "identity_change",
                "level": "info",
                "message": f"Identity changed to {new_id}"
            })
            continue

        # Check for connection events
        for event_type, pattern in patterns.items():
            match = pattern.search(line)
            if match:
                # Determine level and message
                if event_type == "connected":
                    level = "info"
                    message = "BAM connection established"
                elif event_type == "url_changed":
                    level = "info"
                    message = "BAM URL changed (will reconnect)"
                elif event_type == "identity_updater":
                    level = "info"
                    message = "BAM Manager initialized"
                elif event_type == "new_identity":
                    level = "info"
                    message = f"New identity detected: {match.group(1)}"
                elif event_type == "not_healthy":
                    level = "warn"
                    message = "Connection not healthy (no heartbeat for 6s)"
                    warning_counts["not_healthy"] += 1
                elif event_type == "connection_lost":
                    level = "warn"
                    message = "BAM connection lost"
                    warning_counts["connection_lost"] += 1
                elif event_type == "identity_timeout":
                    level = "warn"
                    message = "Timed out waiting for new identity in cluster info"
                    warning_counts["identity_timeout"] += 1
                elif event_type == "connect_failed":
                    level = "error"
                    url = match.group(1) if match.lastindex >= 1 else "?"
                    err = match.group(2) if match.lastindex >= 2 else "unknown"
                    message = f"Connection failed to {url}: {err[:50]}"
                    error_counts["connect_failed"] += 1
                elif event_type == "stream_failed":
                    level = "error"
                    message = f"Scheduler stream failed: {match.group(1)[:50]}"
                    error_counts["stream_failed"] += 1
                elif event_type == "auth_failed":
                    level = "error"
                    message = f"Authentication failed: {match.group(1)}"
                    error_counts["auth_failed"] += 1
                elif event_type == "inbound_closed":
                    level = "error"
                    message = "Inbound stream closed unexpectedly"
                    error_counts["inbound_closed"] += 1
                elif event_type == "inbound_error":
                    level = "error"
                    message = f"Inbound stream error: {match.group(1)[:50]}"
                    error_counts["inbound_error"] += 1
                elif event_type == "config_failed":
                    level = "error"
                    message = f"Config fetch failed: {match.group(1)[:50]}"
                    error_counts["config_failed"] += 1
                elif event_type == "unsupported_msg":
                    level = "error"
                    message = "Received unsupported protocol message"
                    error_counts["unsupported_msg"] += 1
                elif event_type == "set_bam_url":
                    level = "debug"
                    old_url = match.group(1).strip()
                    new_url = match.group(2).strip()
                    message = f"BAM URL changed via RPC: {old_url} -> {new_url}"
                else:
                    continue

                events.append({
                    "timestamp": ts_str or "unknown",
                    "dt": dt,
                    "type": event_type,
                    "level": level,
                    "message": message
                })
                break

    print(f" done ({line_count:,} lines)\n")

    if line_count == 0:
        print("No log lines found.")
        sys.exit(1)

    # Check if BAM is configured at all
    has_bam_activity = len(events) > 0 or len(minute_metrics) > 0

    if not has_bam_activity:
        print("No BAM activity detected in logs.")
        print("This validator may not have --bam-url configured.")
        print("\nTo enable BAM, start the validator with:")
        print("  --bam-url <BAM_NODE_URL>")

        # Still show ping check if we have a BAM URL - useful for troubleshooting
        if bam_url:
            hostname = extract_hostname_from_url(bam_url)
            if hostname:
                print(f"\n{'NETWORK CHECK':=^{TABLE_WIDTH}}")
                print(f"BAM host: {hostname}")
                print("Checking network latency...", end=" ", flush=True)
                ping_ms = ping_host(hostname)
                if ping_ms is not None:
                    if ping_ms > 35:
                        if sys.stdout.isatty():
                            print(f"\033[91m{ping_ms:.1f}ms - HIGH LATENCY (>35ms)\033[0m")
                            print(f"  \033[93m^ This may cause connectivity issues!\033[0m")
                        else:
                            print(f"{ping_ms:.1f}ms - HIGH LATENCY (>35ms) ** POSSIBLE ISSUE **")
                    elif ping_ms > 20:
                        if sys.stdout.isatty():
                            print(f"\033[93m{ping_ms:.1f}ms\033[0m (moderate)")
                        else:
                            print(f"{ping_ms:.1f}ms (moderate)")
                    else:
                        if sys.stdout.isatty():
                            print(f"\033[92m{ping_ms:.1f}ms\033[0m (good)")
                        else:
                            print(f"{ping_ms:.1f}ms (good)")
                else:
                    print("FAILED (host unreachable or ping blocked)")
                print(f"{'=' * TABLE_WIDTH}")
        return

    # Print connection events timeline
    print(f"{'BAM CONNECTION EVENTS':=^{TABLE_WIDTH}}")

    if events:
        # Filter events for display
        display_events = events if verbose else [e for e in events if e["level"] in ("warn", "error") or e["type"] in ("connected", "disconnected", "connection_lost")]

        if display_events:
            print(f"{'Timestamp':<24} | {'Level':<6} | Message")
            print("-" * TABLE_WIDTH)

            for event in display_events:
                level_str = event["level"].upper()
                # Add color indicators for terminal
                if event["level"] == "error":
                    level_display = f"\033[91m{level_str:<6}\033[0m" if sys.stdout.isatty() else level_str
                elif event["level"] == "warn":
                    level_display = f"\033[93m{level_str:<6}\033[0m" if sys.stdout.isatty() else level_str
                else:
                    level_display = level_str

                print(f"{event['timestamp']:<24} | {level_display:<6} | {event['message']}")

            print("-" * TABLE_WIDTH)
        else:
            print("No significant connection events (use --verbose to see all)")
    else:
        print("No connection state change events found")

    print(f"{'=' * TABLE_WIDTH}")

    # Print per-minute health metrics
    if show_metrics and minute_metrics:
        # Only show minutes with activity
        active_minutes = sorted([m for m, d in minute_metrics.items() if d["samples"] > 0])

        if active_minutes:
            print(f"\n{'HEALTH METRICS (per minute)':=^{TABLE_WIDTH}}")
            print(f"{'Time (UTC)':<20} | {'Heartbeats':>12} | {'Unhealthy':>10} | {'Bundles':>10} | {'Out Fail':>10}")
            print("-" * TABLE_WIDTH)

            total_heartbeats = 0
            total_unhealthy = 0
            total_bundles = 0
            total_outbound_fail = 0

            for minute in active_minutes:
                d = minute_metrics[minute]
                hb = d["heartbeat_received"]
                uh = d["unhealthy_count"]
                br = d["bundle_received"]
                of = d["outbound_fail"]

                total_heartbeats += hb
                total_unhealthy += uh
                total_bundles += br
                total_outbound_fail += of

                # Highlight unhealthy minutes
                if uh > 0:
                    if sys.stdout.isatty():
                        print(f"\033[93m{minute:<20} | {hb:>12,} | {uh:>10,} | {br:>10,} | {of:>10,}\033[0m")
                    else:
                        print(f"{minute:<20} | {hb:>12,} | {uh:>10,} | {br:>10,} | {of:>10,} *")
                else:
                    print(f"{minute:<20} | {hb:>12,} | {uh:>10,} | {br:>10,} | {of:>10,}")

            print("-" * TABLE_WIDTH)
            print(f"{'TOTAL':<20} | {total_heartbeats:>12,} | {total_unhealthy:>10,} | {total_bundles:>10,} | {total_outbound_fail:>10,}")
            print(f"{'=' * TABLE_WIDTH}")

    # Print summary
    print(f"\n{'SUMMARY':=^{TABLE_WIDTH}}")

    # Network latency check
    if bam_url:
        hostname = extract_hostname_from_url(bam_url)
        if hostname:
            print(f"BAM host: {hostname}")
            print("Checking network latency...", end=" ", flush=True)
            ping_ms = ping_host(hostname)
            if ping_ms is not None:
                if ping_ms > 35:
                    if sys.stdout.isatty():
                        print(f"\033[91m{ping_ms:.1f}ms - HIGH LATENCY (>35ms)\033[0m")
                        print(f"  \033[93m^ This may cause connectivity issues!\033[0m")
                    else:
                        print(f"{ping_ms:.1f}ms - HIGH LATENCY (>35ms) ** POSSIBLE ISSUE **")
                elif ping_ms > 20:
                    if sys.stdout.isatty():
                        print(f"\033[93m{ping_ms:.1f}ms\033[0m (moderate)")
                    else:
                        print(f"{ping_ms:.1f}ms (moderate)")
                else:
                    if sys.stdout.isatty():
                        print(f"\033[92m{ping_ms:.1f}ms\033[0m (good)")
                    else:
                        print(f"{ping_ms:.1f}ms (good)")
            else:
                print("FAILED (host unreachable or ping blocked)")
            print()

    # Time range
    if first_ts and last_ts:
        duration = (last_ts - first_ts).total_seconds()
        print(f"Time range: {first_ts.strftime('%Y-%m-%d %H:%M:%S')} to {last_ts.strftime('%Y-%m-%d %H:%M:%S')} ({format_duration(duration)})")

    # Connection events summary
    connected_events = [e for e in events if e["type"] == "connected"]
    disconnected_events = [e for e in events if e["type"] in ("connection_lost", "disconnected", "not_healthy")]

    print(f"\nConnection events:")
    print(f"  Connections established: {len(connected_events)}")
    print(f"  Disconnections/unhealthy: {len(disconnected_events)}")

    # Calculate uptime estimate
    if minute_metrics:
        total_minutes = len([m for m, d in minute_metrics.items() if d["samples"] > 0])
        unhealthy_minutes = len([m for m, d in minute_metrics.items() if d["unhealthy_count"] > 0])
        healthy_minutes = total_minutes - unhealthy_minutes

        if total_minutes > 0:
            uptime_pct = (healthy_minutes / total_minutes) * 100
            print(f"\nConnection health:")
            print(f"  Active minutes: {total_minutes}")
            print(f"  Healthy minutes: {healthy_minutes}")
            print(f"  Unhealthy minutes: {unhealthy_minutes}")
            print(f"  Estimated uptime: {uptime_pct:.1f}%")

    # Errors and warnings
    if error_counts or warning_counts:
        print(f"\nIssues detected:")
        for err_type, count in sorted(error_counts.items(), key=lambda x: -x[1]):
            print(f"  ERROR - {err_type}: {count}")
        for warn_type, count in sorted(warning_counts.items(), key=lambda x: -x[1]):
            print(f"  WARN  - {warn_type}: {count}")
    else:
        print(f"\nNo errors or warnings detected")

    # Overall status
    print(f"\nOverall BAM status: ", end="")
    if error_counts:
        if sys.stdout.isatty():
            print("\033[91mISSUES DETECTED\033[0m - check errors above")
        else:
            print("ISSUES DETECTED - check errors above")
    elif warning_counts:
        if sys.stdout.isatty():
            print("\033[93mMOSTLY HEALTHY\033[0m - some warnings")
        else:
            print("MOSTLY HEALTHY - some warnings")
    else:
        if sys.stdout.isatty():
            print("\033[92mHEALTHY\033[0m")
        else:
            print("HEALTHY")

    print(f"{'=' * TABLE_WIDTH}")


def verify_log_file(log_file):
    """Check if log file exists and is readable"""
    if not os.path.exists(log_file):
        print(f"Error: Log file not found: {log_file}")
        print(f"\nPlease specify a valid log file or use -j for journalctl.")
        print(f"Run '{sys.argv[0]} --help' for usage information.")
        sys.exit(1)
    if not os.path.isfile(log_file):
        print(f"Error: Not a file: {log_file}")
        sys.exit(1)
    if not os.access(log_file, os.R_OK):
        print(f"Error: Permission denied: {log_file}")
        sys.exit(1)


def verify_journalctl_service(service):
    """Check if journalctl is available and service has logs"""
    try:
        subprocess.run(['which', 'journalctl'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: journalctl not found. Is systemd installed?")
        sys.exit(1)

    if not service.endswith('.service'):
        service_name = f"{service}.service"
    else:
        service_name = service

    result = subprocess.run(
        ['journalctl', '-u', service_name, '-n', '1', '--no-pager', '-o', 'cat'],
        capture_output=True,
        text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(f"Error: No logs found for service: {service_name}")
        print(f"\nCheck that the service name is correct and has log entries.")
        print(f"Run '{sys.argv[0]} --help' for usage information.")
        sys.exit(1)


def main():
    args = sys.argv[1:]

    # Extract options
    hours = DEFAULT_HOURS
    verbose = False
    show_metrics = False
    bam_url = None
    startup_script = DEFAULT_STARTUP_SCRIPT
    no_ping = False

    if '--hours' in args:
        try:
            idx = args.index('--hours')
            hours = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        except (IndexError, ValueError):
            print("Error: --hours requires a numeric value")
            sys.exit(1)

    if '--bam-url' in args:
        try:
            idx = args.index('--bam-url')
            bam_url = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        except IndexError:
            print("Error: --bam-url requires a URL value")
            sys.exit(1)

    if '--startup-script' in args:
        try:
            idx = args.index('--startup-script')
            startup_script = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        except IndexError:
            print("Error: --startup-script requires a path value")
            sys.exit(1)

    if '--no-ping' in args:
        no_ping = True
        args.remove('--no-ping')

    if '--verbose' in args:
        verbose = True
        args.remove('--verbose')

    if '--metrics' in args:
        show_metrics = True
        args.remove('--metrics')

    # Auto-detect bam_url from startup script if not explicitly provided
    if not bam_url and not no_ping:
        detected_url, _ = extract_bam_url_from_script(startup_script)
        if detected_url:
            bam_url = detected_url
            print(f"Detected --bam-url from {startup_script}: {bam_url}\n")
        elif os.path.exists(os.path.expanduser(startup_script)):
            # File exists but no --bam-url found
            print(f"Note: No --bam-url found in {startup_script}")
            print("      Use --bam-url to specify manually, or --no-ping to skip latency check\n")
        else:
            # Default startup script doesn't exist
            print(f"Note: Startup script not found: {startup_script}")
            print("      Update DEFAULT_STARTUP_SCRIPT in this script, use --startup-script,")
            print("      or use --bam-url to specify the BAM URL manually\n")

    if len(args) == 0:
        if not os.path.exists(DEFAULT_LOG_PATH):
            print(f"Error: Default log file not found: {DEFAULT_LOG_PATH}")
            print(f"\nPlease specify a log file path or use -j for journalctl.")
            print(f"Run '{sys.argv[0]} --help' for usage information.")
            sys.exit(1)
        verify_log_file(DEFAULT_LOG_PATH)
        analyze_logs(get_lines_from_file(DEFAULT_LOG_PATH), DEFAULT_LOG_PATH, verbose, show_metrics, bam_url)

    elif args[0] in ['-h', '--help']:
        print_usage()
        sys.exit(0)

    elif args[0] in ['-j', '--journal']:
        service = args[1] if len(args) > 1 and not args[1].startswith('-') else DEFAULT_SERVICE
        display_name = service if service.endswith('.service') else f"{service}.service"
        verify_journalctl_service(service)
        analyze_logs(
            get_lines_from_journalctl(service, hours),
            f"journalctl -u {display_name} (last {hours}h)",
            verbose,
            show_metrics,
            bam_url
        )

    else:
        log_file = args[0]
        verify_log_file(log_file)
        analyze_logs(get_lines_from_file(log_file), log_file, verbose, show_metrics, bam_url)


if __name__ == "__main__":
    main()
