#!/usr/bin/env python3
"""
BAM Leader Slot Activity Analyzer

Analyzes validator logs to correlate BAM (Block Assembly Marketplace) bundle activity with leader slots.
Produces a table showing when bundles were received during leader slots.

Supports reading from a log file or from journalctl.
"""

import os
import re
import sys
import subprocess
from collections import defaultdict
from datetime import datetime

# =============================================================================
# CONFIGURATION - Set your defaults here
# =============================================================================
DEFAULT_LOG_PATH = os.path.expanduser("~/logs/validator.log")
DEFAULT_SERVICE = "sol.service"

# Vote transaction cost from solana source: SIMPLE_VOTE_USAGE_COST
VOTE_CU_COST = 3428

# Table separator widths (based on column formats)
BAM_TABLE_WIDTH = 91
LEADER_TABLE_WIDTH = 126
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
  {sys.argv[0]}                      Use default log file ({DEFAULT_LOG_PATH})
  {sys.argv[0]} /path/to/file.log    Read from specified log file
  {sys.argv[0]} -j [service]         Read from journalctl (default: {DEFAULT_SERVICE})
  {sys.argv[0]} --journal [service]  Read from journalctl (default: {DEFAULT_SERVICE})

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

def format_lamports(lamports):
    """Format lamports as SOL with appropriate precision"""
    sol = lamports / 1_000_000_000
    if sol >= 1:
        return f"{sol:.4f}"
    elif sol >= 0.001:
        return f"{sol:.6f}"
    else:
        return f"{sol:.9f}"

def analyze_logs(line_source, source_name):
    """Analyze log lines and produce the report"""

    print(f"Analyzing: {source_name}")
    print("Processing logs", end="", flush=True)

    # Data structures to collect metrics per minute (for BAM bundle activity)
    bundle_data = defaultdict(lambda: {
        "bundles": 0,
        "results_sent": 0,
        "scheduler_fail": 0,
        "outbound_fail": 0,
        "unhealthy_count": 0,
        "heartbeat_received": 0,
        "count": 0
    })
    slot_data = defaultdict(list)  # minute -> list of slots

    # Per-slot leader metrics
    leader_slots_announced = set()  # replay_stage-my_leader_slot
    leader_slot_metrics = {}  # slot -> metrics dict

    # Global health tracking (across all time, not just active periods)
    global_heartbeats = 0
    global_unhealthy = 0

    # Regex patterns for BAM metrics
    bundle_rx = re.compile(r'bundle_received=(\d+)i')
    results_rx = re.compile(r'bundleresult_sent=(\d+)i')
    scheduler_fail_rx = re.compile(r'bundle_forward_to_scheduler_fail=(\d+)i')
    outbound_fail_rx = re.compile(r'outbound_fail=(\d+)i')
    unhealthy_rx = re.compile(r'unhealthy_connection_count=(\d+)i')
    heartbeat_rx = re.compile(r'heartbeat_received=(\d+)i')
    slot_rx = re.compile(r'bank frozen: (\d+)')

    # Regex patterns for leader slot metrics
    my_leader_slot_rx = re.compile(r'replay_stage-my_leader_slot slot=(\d+)i')
    cost_tracker_rx = re.compile(
        r'cost_tracker_stats,is_leader=true bank_slot=(\d+)i '
        r'block_cost=(\d+)i vote_cost=(\d+)i transaction_count=(\d+)i.*?'
        r'total_transaction_fee=(\d+)i total_priority_fee=(\d+)i'
    )
    broadcast_rx = re.compile(
        r'broadcast-process-shreds-stats slot=(\d+)i.*?'
        r'slot_broadcast_time=(\d+)i'
    )
    scheduler_timing_rx = re.compile(
        r'banking_stage_scheduler_slot_timing.*?'
        r'receive_time_us=(\d+)i.*?'
        r'schedule_time_us=(\d+)i.*?'
        r'slot=(\d+)i'
    )

    line_count = 0
    progress_interval = 100000
    for line in line_source:
        line_count += 1
        if line_count % progress_interval == 0:
            print(".", end="", flush=True)
        _, minute_key = parse_timestamp(line)
        if not minute_key:
            continue

        # Check for BAM metrics
        if 'bam_connection-metrics' in line:
            bundle_match = bundle_rx.search(line)
            results_match = results_rx.search(line)
            scheduler_fail_match = scheduler_fail_rx.search(line)
            outbound_fail_match = outbound_fail_rx.search(line)
            unhealthy_match = unhealthy_rx.search(line)
            heartbeat_match = heartbeat_rx.search(line)

            if bundle_match:
                bundles = int(bundle_match.group(1))
                bundle_data[minute_key]["bundles"] += bundles
                bundle_data[minute_key]["count"] += 1

            if results_match:
                results = int(results_match.group(1))
                bundle_data[minute_key]["results_sent"] += results

            if scheduler_fail_match:
                scheduler_fail = int(scheduler_fail_match.group(1))
                bundle_data[minute_key]["scheduler_fail"] += scheduler_fail

            if outbound_fail_match:
                outbound_fail = int(outbound_fail_match.group(1))
                bundle_data[minute_key]["outbound_fail"] += outbound_fail

            if unhealthy_match:
                unhealthy = int(unhealthy_match.group(1))
                bundle_data[minute_key]["unhealthy_count"] += unhealthy
                global_unhealthy += unhealthy

            if heartbeat_match:
                heartbeat = int(heartbeat_match.group(1))
                bundle_data[minute_key]["heartbeat_received"] += heartbeat
                global_heartbeats += heartbeat

        # Check for bank frozen (slot info)
        elif 'bank frozen:' in line:
            slot_match = slot_rx.search(line)
            if slot_match:
                slot = int(slot_match.group(1))
                slot_data[minute_key].append(slot)

        # Check for leader slot announcement
        elif 'replay_stage-my_leader_slot' in line:
            match = my_leader_slot_rx.search(line)
            if match:
                slot = int(match.group(1))
                leader_slots_announced.add(slot)

        # Check for cost tracker stats (leader slots)
        elif 'cost_tracker_stats,is_leader=true' in line:
            match = cost_tracker_rx.search(line)
            if match:
                slot = int(match.group(1))
                if slot not in leader_slot_metrics:
                    leader_slot_metrics[slot] = {}
                leader_slot_metrics[slot].update({
                    "block_cost": int(match.group(2)),
                    "vote_cost": int(match.group(3)),
                    "transaction_count": int(match.group(4)),
                    "total_fee": int(match.group(5)),
                    "priority_fee": int(match.group(6)),
                })

        # Check for broadcast stats
        elif 'broadcast-process-shreds-stats' in line:
            match = broadcast_rx.search(line)
            if match:
                slot = int(match.group(1))
                broadcast_time = int(match.group(2))
                if slot not in leader_slot_metrics:
                    leader_slot_metrics[slot] = {}
                leader_slot_metrics[slot]["broadcast_time_us"] = broadcast_time

        # Check for scheduler timing
        elif 'banking_stage_scheduler_slot_timing' in line:
            match = scheduler_timing_rx.search(line)
            if match:
                receive_time = int(match.group(1))
                schedule_time = int(match.group(2))
                slot = int(match.group(3))
                if slot not in leader_slot_metrics:
                    leader_slot_metrics[slot] = {}
                # Accumulate timing (there can be multiple entries per slot)
                leader_slot_metrics[slot]["receive_time_us"] = leader_slot_metrics[slot].get("receive_time_us", 0) + receive_time
                leader_slot_metrics[slot]["schedule_time_us"] = leader_slot_metrics[slot].get("schedule_time_us", 0) + schedule_time

    print(f" done ({line_count:,} lines)\n")

    if line_count == 0:
        print("No log lines found.")
        sys.exit(1)

    # Filter to only minutes with bundle activity (bundles > 0)
    active_minutes = sorted([m for m, d in bundle_data.items() if d["bundles"] > 0])

    # Outlier threshold (20% above/below average)
    OUTLIER_PCT = 0.20
    UP_ARROW = "▲"
    DOWN_ARROW = "▼"

    def get_indicator(value, avg):
        """Return indicator if value is significantly above/below average"""
        if avg == 0:
            return ""
        pct_diff = (value - avg) / avg
        if pct_diff > OUTLIER_PCT:
            return UP_ARROW
        elif pct_diff < -OUTLIER_PCT:
            return DOWN_ARROW
        return ""

    # Initialize totals for summary section
    total_bundles = 0
    total_results = 0
    total_scheduler_fail = 0
    total_outbound_fail = 0
    total_unhealthy = 0
    total_heartbeats = 0
    total_periods = 0
    avg_bundles = 0
    total_pct = 0

    # Print BAM Bundle Activity table (only if activity exists)
    if active_minutes:
        bundle_rows = []

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
            pct_sent = (results / bundles * 100) if bundles > 0 else 0

            bundle_rows.append({
                "minute": minute,
                "slot_range": slot_range,
                "bundles": bundles,
                "results": results,
                "pct_sent": pct_sent
            })

            total_bundles += bundles
            total_results += results
            total_scheduler_fail += data["scheduler_fail"]
            total_outbound_fail += data["outbound_fail"]
            total_unhealthy += data["unhealthy_count"]
            total_heartbeats += data["heartbeat_received"]

        total_periods = len(bundle_rows)
        avg_bundles = total_bundles / total_periods if total_periods > 0 else 0
        avg_results = total_results / total_periods if total_periods > 0 else 0

        print(f"{'BAM BUNDLE ACTIVITY':=^{BAM_TABLE_WIDTH}}")
        print(f"{'Time (UTC)':<20} | {'Slot Range':<25} | {'Bundles':>12} | {'Results Sent':>14} | {'% Sent':>8}")
        print("-" * BAM_TABLE_WIDTH)

        for row in bundle_rows:
            b_ind = get_indicator(row["bundles"], avg_bundles)
            r_ind = get_indicator(row["results"], avg_results)
            bundles_str = f"{row['bundles']:>10,}{b_ind:>2}"
            results_str = f"{row['results']:>12,}{r_ind:>2}"
            print(f"{row['minute']:<20} | {row['slot_range']:<25} | {bundles_str} | {results_str} | {row['pct_sent']:>7.1f}%")

        # Print summary
        print("-" * BAM_TABLE_WIDTH)
        periods_str = f"{total_periods} periods"
        total_pct = (total_results / total_bundles * 100) if total_bundles > 0 else 0
        print(f"{'TOTAL':<20} | {periods_str:<25} | {total_bundles:>12,} | {total_results:>14,} | {total_pct:>7.1f}%")
        print(f"{'(average)':<20} | {'':<25} | {avg_bundles:>12,.0f} | {avg_results:>14,.0f} |")
        print("=" * BAM_TABLE_WIDTH)

        # Print failures table if any failures occurred
        total_failures = total_scheduler_fail + total_outbound_fail
        if total_failures > 0:
            fail_minutes = sorted([m for m, d in bundle_data.items()
                                  if d["scheduler_fail"] > 0 or d["outbound_fail"] > 0])

            print(f"\n{'FAILURES DETECTED':=^{BAM_TABLE_WIDTH}}")
            print(f"{'Time (UTC)':<20} | {'Slot Range':<25} | {'Sched Fail':>12} | {'Outbound Fail':>14} | {'Total':>8}")
            print("-" * BAM_TABLE_WIDTH)

            for minute in fail_minutes:
                data = bundle_data[minute]
                slots = sorted(slot_data.get(minute, []))

                if slots:
                    slot_range = f"{slots[0]} - {slots[-1]}" if len(slots) > 1 else str(slots[0])
                else:
                    slot_range = "(no slot data)"

                sched_fail = data["scheduler_fail"]
                out_fail = data["outbound_fail"]
                total_min_fail = sched_fail + out_fail

                print(f"{minute:<20} | {slot_range:<25} | {sched_fail:>12,} | {out_fail:>14,} | {total_min_fail:>8,}")

            print("-" * BAM_TABLE_WIDTH)
            print(f"{'TOTAL FAILURES':<20} | {'':<25} | {total_scheduler_fail:>12,} | {total_outbound_fail:>14,} | {total_failures:>8,}")
            print("=" * BAM_TABLE_WIDTH)
    else:
        print("No BAM bundle activity found.")
        print("This validator does not appear to be running BAM (Block Assembly Marketplace).\n")

    # Print Leader Slot Metrics table
    if leader_slot_metrics or leader_slots_announced:
        # Detect skipped slots
        skipped_slots = leader_slots_announced - set(leader_slot_metrics.keys())

        # Combine all leader slots (produced + skipped) for display
        all_leader_slots = sorted(set(leader_slot_metrics.keys()) | skipped_slots)

        # First pass: collect data and calculate averages
        slot_rows = []
        total_txns = 0
        total_votes = 0
        total_user = 0
        total_block_cost = 0
        total_time_us = 0
        total_total_fee = 0
        total_priority_fee = 0
        slot_count = 0
        skipped_count = 0

        for slot in all_leader_slots:
            if slot in skipped_slots:
                slot_rows.append({"slot": slot, "skipped": True})
                skipped_count += 1
            else:
                m = leader_slot_metrics[slot]

                txns = m.get("transaction_count", 0)
                vote_cost = m.get("vote_cost", 0)
                block_cost = m.get("block_cost", 0)
                total_fee = m.get("total_fee", 0)
                priority_fee = m.get("priority_fee", 0)
                broadcast_time = m.get("broadcast_time_us", 0)
                receive_time = m.get("receive_time_us", 0)
                schedule_time = m.get("schedule_time_us", 0)

                # Estimate vote vs user transactions
                est_votes = vote_cost // VOTE_CU_COST if vote_cost > 0 else 0
                est_user = max(0, txns - est_votes)

                # Total slot time (use broadcast time as primary, fall back to receive+schedule)
                slot_time_us = broadcast_time if broadcast_time > 0 else (receive_time + schedule_time)
                slot_time_ms = slot_time_us / 1000

                slot_rows.append({
                    "slot": slot,
                    "skipped": False,
                    "txns": txns,
                    "votes": est_votes,
                    "user": est_user,
                    "block_cost": block_cost,
                    "time_ms": slot_time_ms,
                    "total_fee": total_fee,
                    "priority_fee": priority_fee
                })

                total_txns += txns
                total_votes += est_votes
                total_user += est_user
                total_block_cost += block_cost
                total_time_us += slot_time_us
                total_total_fee += total_fee
                total_priority_fee += priority_fee
                slot_count += 1

        # Calculate averages
        avg_txns = total_txns / slot_count if slot_count > 0 else 0
        avg_votes = total_votes / slot_count if slot_count > 0 else 0
        avg_user = total_user / slot_count if slot_count > 0 else 0
        avg_block_cost = total_block_cost / slot_count if slot_count > 0 else 0
        avg_time_ms = (total_time_us / slot_count / 1000) if slot_count > 0 else 0

        # Print table with indicators
        print(f"\n{'LEADER SLOT METRICS':=^{LEADER_TABLE_WIDTH}}")
        print(f"{'Slot':<26} | {'Txns':>8} | {'Votes':>8} | {'User':>8} | {'Block CUs':>15} | {'Time (ms)':>12} | {'Total Fee':>14} | {'Priority Fee':>14}")
        print("-" * LEADER_TABLE_WIDTH)

        for row in slot_rows:
            if row["skipped"]:
                print(f"{row['slot']:<26} | {'---':>8} | {'---':>8} | {'---':>8} | {'---':>15} | {'---':>12} | {'---':>14} | {'SKIPPED':>14}")
            else:
                t_ind = get_indicator(row["txns"], avg_txns)
                v_ind = get_indicator(row["votes"], avg_votes)
                u_ind = get_indicator(row["user"], avg_user)
                b_ind = get_indicator(row["block_cost"], avg_block_cost)
                tm_ind = get_indicator(row["time_ms"], avg_time_ms)

                txns_str = f"{row['txns']:>6,}{t_ind:>2}"
                votes_str = f"{row['votes']:>6,}{v_ind:>2}"
                user_str = f"{row['user']:>6,}{u_ind:>2}"
                block_str = f"{row['block_cost']:>13,}{b_ind:>2}"
                time_str = f"{row['time_ms']:>10.1f}{tm_ind:>2}"

                print(f"{row['slot']:<26} | {txns_str} | {votes_str} | {user_str} | {block_str} | {time_str} | {format_lamports(row['total_fee']):>14} | {format_lamports(row['priority_fee']):>14}")

        print("-" * LEADER_TABLE_WIDTH)
        # Calculate average fees
        avg_total_fee = total_total_fee / slot_count if slot_count > 0 else 0
        avg_priority_fee = total_priority_fee / slot_count if slot_count > 0 else 0

        print(f"{'TOTAL':<26} | {total_txns:>8,} | {total_votes:>8,} | {total_user:>8,} | {total_block_cost:>15,} |              | {format_lamports(total_total_fee):>14} | {format_lamports(total_priority_fee):>14}")
        slots_label = f"({slot_count} produced, {skipped_count} skipped)"
        print(f"{slots_label:<26} |          |          |          |                 |              |                |")
        print(f"{'AVERAGE':<26} | {avg_txns:>8,.0f} | {avg_votes:>8,.0f} | {avg_user:>8,.0f} | {avg_block_cost:>15,.0f} | {avg_time_ms:>12.1f} | {format_lamports(avg_total_fee):>14} | {format_lamports(avg_priority_fee):>14}")
        print("=" * LEADER_TABLE_WIDTH)
    else:
        print("No leader slot data found in logs.")
        print("This validator is not on the leader schedule for the log period.")
        print("(This is expected for hot-standby validators.)\n")

    # Additional stats
    if active_minutes:
        first_time = active_minutes[0].replace('T', ' ')
        last_time = active_minutes[-1].replace('T', ' ')
        print(f"\nTime range: {first_time} to {last_time} UTC")
        print(f"Leader periods: {total_periods}")
        print(f"Total bundles received: {total_bundles:,}")
        print(f"Total bundle results sent: {total_results:,}")
        print(f"Overall send rate: {total_pct:.1f}%")

        if total_periods > 1:
            avg_bundles = total_bundles / total_periods
            print(f"Average bundles per leader period: {avg_bundles:,.0f}")

        # Failure stats
        total_failures = total_scheduler_fail + total_outbound_fail
        if total_failures > 0:
            fail_rate = (total_failures / total_bundles * 100) if total_bundles > 0 else 0
            print(f"\nTotal failures: {total_failures:,} ({fail_rate:.2f}% of bundles)")
            print(f"  Scheduler failures: {total_scheduler_fail:,}")
            print(f"  Outbound failures: {total_outbound_fail:,}")
        else:
            print(f"\nNo failures detected.")

        # Connection health stats
        print(f"\nConnection health:")
        print(f"  Heartbeats received (during leader periods): {total_heartbeats:,}")
        print(f"  Heartbeats received (total): {global_heartbeats:,}")
        if global_unhealthy > 0:
            print(f"  Unhealthy connection events: {global_unhealthy:,}")
        else:
            print(f"  Unhealthy connection events: 0 (healthy throughout)")

    # Leader slot summary (shown regardless of bundle activity)
    if leader_slot_metrics or leader_slots_announced:
        print(f"\nLeader slot summary:")
        print(f"  Slots produced: {slot_count}")
        print(f"  Slots skipped: {skipped_count}")
        if skipped_count > 0:
            skip_rate = (skipped_count / (slot_count + skipped_count)) * 100
            print(f"  Skip rate: {skip_rate:.2f}%")
        print(f"  Total transactions: {total_txns:,} ({total_votes:,} votes, {total_user:,} user)")
        print(f"  Total compute units: {total_block_cost:,}")
        print(f"  Total fees: {format_lamports(total_total_fee)} SOL")
        print(f"  Total priority fees: {format_lamports(total_priority_fee)} SOL")
        if slot_count > 0:
            print(f"  Avg transactions per slot: {total_txns // slot_count:,}")
            print(f"  Avg block time: {avg_time_ms:.1f} ms")

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
    # Check if journalctl exists
    try:
        subprocess.run(['which', 'journalctl'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: journalctl not found. Is systemd installed?")
        sys.exit(1)

    # Check if service has any logs
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
    # Parse arguments
    if len(sys.argv) == 1:
        # No arguments - use default log file if it exists
        if not os.path.exists(DEFAULT_LOG_PATH):
            print(f"Error: Default log file not found: {DEFAULT_LOG_PATH}")
            print(f"\nPlease specify a log file path or use -j for journalctl.")
            print(f"Run '{sys.argv[0]} --help' for usage information.")
            sys.exit(1)
        verify_log_file(DEFAULT_LOG_PATH)
        analyze_logs(get_lines_from_file(DEFAULT_LOG_PATH), DEFAULT_LOG_PATH)

    elif sys.argv[1] in ['-h', '--help']:
        print_usage()
        sys.exit(0)

    elif sys.argv[1] in ['-j', '--journal']:
        # Use journalctl
        service = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SERVICE
        display_name = service if service.endswith('.service') else f"{service}.service"

        verify_journalctl_service(service)
        analyze_logs(get_lines_from_journalctl(service), f"journalctl -u {display_name}")

    else:
        # Assume it's a log file path
        log_file = sys.argv[1]
        verify_log_file(log_file)
        analyze_logs(get_lines_from_file(log_file), log_file)

if __name__ == "__main__":
    main()
