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
import io
import json
import contextlib
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# =============================================================================
# CONFIGURATION - Set your defaults here
# =============================================================================
DEFAULT_LOG_PATH = os.path.expanduser("~/logs/validator.log")
DEFAULT_SERVICE = "sol.service"
DEFAULT_HOURS = 24  # Default time span for journalctl

# Vote transaction cost from solana source: SIMPLE_VOTE_USAGE_COST
VOTE_CU_COST = 3428

# Table separator widths (based on column formats)
BAM_TABLE_WIDTH = 91
LEADER_TABLE_WIDTH = 126

# Seconds of slack around the leader period when --leader-slots anchors the BAM
# sub-window. BAM streams bundles a beat ahead of the first leader slot and the
# bundleresult_sent datapoints trail bundle_received by a few hundred ms, so a
# tight window would undercount bundles at the head and depress send_rate_pct at
# the tail. Bundles are 0 outside leader periods, so slack costs nothing.
# parse_timestamp() resolves to whole seconds, hence seconds, not milliseconds.
LEADER_WINDOW_PRE_ROLL_S = 5
LEADER_WINDOW_POST_ROLL_S = 5
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
  {sys.argv[0]} -j [service]         Read from journalctl (default: {DEFAULT_SERVICE}, last {DEFAULT_HOURS}h)
  {sys.argv[0]} --journal [service]  Read from journalctl (default: {DEFAULT_SERVICE}, last {DEFAULT_HOURS}h)
  {sys.argv[0]} --hours N            Set time span for journalctl (default: {DEFAULT_HOURS})
  {sys.argv[0]} --since EPOCH        Only analyse lines at/after this UTC epoch-second
  {sys.argv[0]} --until EPOCH        Only analyse lines at/before this UTC epoch-second
  {sys.argv[0]} --json               Emit a machine-readable summary instead of tables
  {sys.argv[0]} --leader-slots CSV   Our slot numbers for this rotation. Ignores other
                                     validators' leader lines, and counts bundles over a
                                     window anchored on where those slots appear in the
                                     log rather than over --since/--until. Pass a
                                     generous --since with it.

Examples:
  {sys.argv[0]}                      # Use default log file
  {sys.argv[0]} /var/log/solana.log  # Use specific log file
  {sys.argv[0]} -j                   # Use journalctl with default service (last {DEFAULT_HOURS}h)
  {sys.argv[0]} -j myvalidator       # Use journalctl with myvalidator.service
  {sys.argv[0]} -j --hours 48        # Use journalctl, last 48 hours
  {sys.argv[0]} -j sol --hours 12    # Use journalctl for sol.service, last 12 hours
""")

def _seek_to_window(fh, size, since_dt):
    """Position fh so that everything from since_dt onwards is still ahead of it.

    validator.log is ~500MB and the per-rotation window is the last few minutes,
    so reading from byte 0 would mean ~16GB/day of pointless I/O on a staked
    validator (and would evict its page cache). Instead seek near the end and walk
    backwards in growing chunks until the first timestamp we can parse is at or
    before the window start -- that guarantees the window is fully covered.
    Falls back to the whole file if we can't establish that.
    """
    chunk = 8 * 1024 * 1024
    while chunk < size:
        fh.seek(size - chunk)
        fh.readline()          # discard the partial line we landed in
        pos = fh.tell()
        for _ in range(200):   # find the first line here we can date
            line = fh.readline()
            if not line:
                break
            dt, minute_key = parse_timestamp(line)
            if minute_key:
                if dt <= since_dt:
                    fh.seek(pos)
                    return
                break          # started too late - go further back
        chunk *= 4
    fh.seek(0)


def get_lines_from_file(log_file, since_dt=None):
    """Generator that yields lines from a log file, tail-seeking when windowed."""
    try:
        with open(log_file, 'r', errors='replace') as f:
            if since_dt is not None:
                try:
                    size = os.path.getsize(log_file)
                    if size > 8 * 1024 * 1024:
                        _seek_to_window(f, size, since_dt)
                except OSError:
                    pass       # any trouble: just read the whole thing
            for line in f:
                yield line
    except FileNotFoundError:
        print(f"Error: File not found: {log_file}")
        sys.exit(1)
    except PermissionError:
        print(f"Error: Permission denied: {log_file}")
        sys.exit(1)

def get_lines_from_journalctl(service, hours=None, since_dt=None, until_dt=None):
    """Generator that yields lines from journalctl for a service.

    When a window is given it is pushed down into journalctl rather than being
    filtered in Python: this is called once per leader rotation on a validator
    whose journal is large, and reading it all only to discard 99% of it is real
    I/O on a staked host.

    The `@<epoch>` form is used deliberately -- journalctl interprets a bare
    "YYYY-MM-DD HH:MM:SS" in LOCAL time, which would silently shift the window on
    any host that is not UTC, whereas @epoch is unambiguous.
    """
    if not service.endswith('.service'):
        service = f"{service}.service"

    cmd = ['journalctl', '-u', service, '--no-pager', '-o', 'cat']
    if since_dt is not None:
        cmd.extend(['--since', f'@{int(since_dt.replace(tzinfo=timezone.utc).timestamp())}'])
    elif hours is not None:
        cmd.extend(['--since', f'{hours} hours ago'])
    if until_dt is not None:
        cmd.extend(['--until', f'@{int(until_dt.replace(tzinfo=timezone.utc).timestamp())}'])

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

def analyze_logs(line_source, source_name, since_dt=None, until_dt=None,
                 leader_slot_filter=None):
    """Analyze log lines and produce the report.

    since_dt/until_dt bound the analysis to a UTC window (naive datetimes, to
    match the naive timestamps parsed out of the log). Returns a summary dict
    for --json consumers; the human-readable tables are still printed as before.

    leader_slot_filter is a set of slot numbers owned by the caller's rotation.
    When given it does two things:

      1. Leader-slot lines for any OTHER validator's slots are ignored, so the
         caller can pass a deliberately generous since/until without a
         neighbouring rotation polluting the leader-slot section.
      2. BAM bundle counts are re-derived over a sub-window anchored on where
         those slots actually appear in the log, instead of the caller's
         wall-clock window. leader-capture-monitor.sh stamps its window when an
         RPC poll first observes current_slot >= first_slot, which lags the real
         leader period by poll granularity + RPC lag; a rotation is only ~1.6s
         long, so that window routinely started AFTER the entire bundle burst
         and reported 0 bundles for a perfectly healthy BAM (8 of 15 rotations
         on 2026-08-09). Anchoring on the log removes the guesswork.
    """

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
    # Deliberately NOT narrowed by leader_slot_filter: heartbeats and unhealthy
    # events are the liveness signal for the BAM connection itself, which the
    # caller uses to decide whether this validator runs BAM at all. They must
    # describe the whole window, not the ~10s leader sub-window.
    global_heartbeats = 0
    global_unhealthy = 0

    # (dt, bundles, results, scheduler_fail, outbound_fail) per BAM datapoint,
    # buffered only when we will need to re-window them. The sub-window is not
    # known until the leader lines have been seen, so this cannot be a one-pass
    # accumulation. The caller's outer window is a few minutes of log at most.
    bam_points = []
    # dt of every replay_stage-my_leader_slot line for a slot we own.
    leader_slot_times = []

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
        dt, minute_key = parse_timestamp(line)
        if not minute_key:
            continue

        # Bound the analysis to an explicit window when asked.
        # leader-capture-monitor.sh calls this once per leader rotation (~30x/day);
        # without a window every call would re-scan the whole log and re-report
        # every previous rotation's data.
        if since_dt is not None and dt < since_dt:
            continue
        if until_dt is not None and dt > until_dt:
            continue

        # Check for BAM metrics
        if 'bam_connection-metrics' in line:
            bundle_match = bundle_rx.search(line)
            results_match = results_rx.search(line)
            scheduler_fail_match = scheduler_fail_rx.search(line)
            outbound_fail_match = outbound_fail_rx.search(line)
            unhealthy_match = unhealthy_rx.search(line)
            heartbeat_match = heartbeat_rx.search(line)

            if leader_slot_filter is not None:
                bam_points.append((
                    dt,
                    int(bundle_match.group(1)) if bundle_match else 0,
                    int(results_match.group(1)) if results_match else 0,
                    int(scheduler_fail_match.group(1)) if scheduler_fail_match else 0,
                    int(outbound_fail_match.group(1)) if outbound_fail_match else 0,
                ))

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
                if leader_slot_filter is not None and slot not in leader_slot_filter:
                    continue
                leader_slots_announced.add(slot)
                leader_slot_times.append(dt)

        # Check for cost tracker stats (leader slots)
        elif 'cost_tracker_stats,is_leader=true' in line:
            match = cost_tracker_rx.search(line)
            if match:
                slot = int(match.group(1))
                if leader_slot_filter is not None and slot not in leader_slot_filter:
                    continue
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
                if leader_slot_filter is not None and slot not in leader_slot_filter:
                    continue
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
                if leader_slot_filter is not None and slot not in leader_slot_filter:
                    continue
                if slot not in leader_slot_metrics:
                    leader_slot_metrics[slot] = {}
                # Accumulate timing (there can be multiple entries per slot)
                leader_slot_metrics[slot]["receive_time_us"] = leader_slot_metrics[slot].get("receive_time_us", 0) + receive_time
                leader_slot_metrics[slot]["schedule_time_us"] = leader_slot_metrics[slot].get("schedule_time_us", 0) + schedule_time

    print(f" done ({line_count:,} lines)\n")

    if line_count == 0:
        print("No log lines found.")
        sys.exit(1)

    # ---- leader-anchored BAM sub-window -------------------------------------
    # Only when the caller named its slots AND we found them in the log. If the
    # slots are absent the log simply does not cover the leader period, which is
    # an instrumentation gap, not a BAM outage -- report leader_window_found
    # False and fall through to the plain window totals so the caller can
    # downgrade its alert instead of paging on our own blind spot.
    leader_window_found = False
    anchored_window = None
    anchored = None
    if leader_slot_filter is not None and leader_slot_times:
        sub_start = min(leader_slot_times) - timedelta(seconds=LEADER_WINDOW_PRE_ROLL_S)
        sub_end = max(leader_slot_times) + timedelta(seconds=LEADER_WINDOW_POST_ROLL_S)
        leader_window_found = True
        anchored_window = (sub_start, sub_end)
        anchored = {"bundles": 0, "results": 0, "scheduler_fail": 0, "outbound_fail": 0}
        for pt_dt, pt_bundles, pt_results, pt_sched, pt_out in bam_points:
            if pt_dt < sub_start or pt_dt > sub_end:
                continue
            anchored["bundles"] += pt_bundles
            anchored["results"] += pt_results
            anchored["scheduler_fail"] += pt_sched
            anchored["outbound_fail"] += pt_out
        print(f"Leader-anchored BAM window: {sub_start:%Y-%m-%dT%H:%M:%SZ}"
              f" .. {sub_end:%Y-%m-%dT%H:%M:%SZ}"
              f" ({anchored['bundles']:,} bundles, {anchored['results']:,} sent)\n")
    elif leader_slot_filter is not None:
        print("Leader-anchored BAM window: leader slots not found in this log "
              "window -- BAM totals fall back to the requested window.\n")

    # Filter to only minutes with bundle activity (bundles > 0)
    active_minutes = sorted([m for m, d in bundle_data.items() if d["bundles"] > 0])

    # Outlier threshold (20% above/below average)
    OUTLIER_PCT = 0.20
    # Small block threshold (below this percentage of median for key metrics)
    SMALL_BLOCK_PCT = 0.25
    UP_ARROW = "▲"
    DOWN_ARROW = "▼"
    SMALL_BLOCK_MARKER = "◆"

    def get_indicator(value, median):
        """Return indicator if value is significantly above/below median"""
        if median == 0:
            return ""
        pct_diff = (value - median) / median
        if pct_diff > OUTLIER_PCT:
            return UP_ARROW
        elif pct_diff < -OUTLIER_PCT:
            return DOWN_ARROW
        return ""

    def get_median(values):
        """Calculate median of a list of values"""
        if not values:
            return 0
        sorted_values = sorted(values)
        n = len(sorted_values)
        if n % 2 == 0:
            return (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / 2
        return sorted_values[n // 2]

    def is_small_block(user_txns, block_cost, median_user, median_block_cost):
        """Check if a block is a small block (key metrics way below median)"""
        if median_user == 0 or median_block_cost == 0:
            return False
        user_ratio = user_txns / median_user
        cost_ratio = block_cost / median_block_cost
        # Both user txns AND block cost must be significantly below median
        return user_ratio < SMALL_BLOCK_PCT and cost_ratio < SMALL_BLOCK_PCT

    # Initialize totals for summary section
    total_bundles = 0
    total_results = 0
    total_scheduler_fail = 0
    total_outbound_fail = 0
    total_unhealthy = 0
    total_heartbeats = 0
    total_periods = 0
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

        # Calculate medians for outlier detection
        median_bundles = get_median([r["bundles"] for r in bundle_rows])
        median_results = get_median([r["results"] for r in bundle_rows])

        print(f"{'BAM BUNDLE ACTIVITY':=^{BAM_TABLE_WIDTH}}")
        print(f"{'Time (UTC)':<20} | {'Slot Range':<25} | {'Bundles':>12} | {'Results Sent':>14} | {'% Sent':>8}")
        print("-" * BAM_TABLE_WIDTH)

        for row in bundle_rows:
            b_ind = get_indicator(row["bundles"], median_bundles)
            r_ind = get_indicator(row["results"], median_results)
            bundles_str = f"{row['bundles']:>10,}{b_ind:>2}"
            results_str = f"{row['results']:>12,}{r_ind:>2}"
            print(f"{row['minute']:<20} | {row['slot_range']:<25} | {bundles_str} | {results_str} | {row['pct_sent']:>7.1f}%")

        # Print summary
        print("-" * BAM_TABLE_WIDTH)
        periods_str = f"{total_periods} periods"
        total_pct = (total_results / total_bundles * 100) if total_bundles > 0 else 0
        print(f"{'TOTAL':<20} | {periods_str:<25} | {total_bundles:>12,} | {total_results:>14,} | {total_pct:>7.1f}%")
        print(f"{'(median)':<20} | {'':<25} | {median_bundles:>12,.0f} | {median_results:>14,.0f} |")
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
        small_block_count = 0

        # Collect values for median calculation
        all_txns = []
        all_votes = []
        all_user_txns = []
        all_block_costs = []
        all_time_ms = []

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
                    "priority_fee": priority_fee,
                    "small_block": False  # Will be set in second pass
                })

                # Collect for median calculation
                all_txns.append(txns)
                all_votes.append(est_votes)
                all_user_txns.append(est_user)
                all_block_costs.append(block_cost)
                all_time_ms.append(slot_time_ms)

                total_txns += txns
                total_votes += est_votes
                total_user += est_user
                total_block_cost += block_cost
                total_time_us += slot_time_us
                total_total_fee += total_fee
                total_priority_fee += priority_fee
                slot_count += 1

        # Calculate medians for outlier detection
        median_txns = get_median(all_txns)
        median_votes = get_median(all_votes)
        median_user = get_median(all_user_txns)
        median_block_cost = get_median(all_block_costs)
        median_time_ms = get_median(all_time_ms)

        # Second pass: mark small blocks
        for row in slot_rows:
            if not row.get("skipped", False):
                if is_small_block(row["user"], row["block_cost"], median_user, median_block_cost):
                    row["small_block"] = True
                    small_block_count += 1

        # Print table with indicators
        print(f"\n{'LEADER SLOT METRICS':=^{LEADER_TABLE_WIDTH}}")
        print(f"{'Slot':<26} | {'Txns':>8} | {'Votes':>8} | {'User':>8} | {'Block CUs':>15} | {'Time (ms)':>12} | {'Total Fee':>14} | {'Priority Fee':>14}")
        print("-" * LEADER_TABLE_WIDTH)

        for row in slot_rows:
            if row["skipped"]:
                print(f"{row['slot']:<26} | {'---':>8} | {'---':>8} | {'---':>8} | {'---':>15} | {'---':>12} | {'---':>14} | {'SKIPPED':>14}")
            elif row.get("small_block"):
                # Small block - highlight with marker
                slot_str = f"{row['slot']} {SMALL_BLOCK_MARKER}"
                txns_str = f"{row['txns']:>6,}{DOWN_ARROW:>2}"
                votes_str = f"{row['votes']:>6,}  "
                user_str = f"{row['user']:>6,}{DOWN_ARROW:>2}"
                block_str = f"{row['block_cost']:>13,}{DOWN_ARROW:>2}"
                time_str = f"{row['time_ms']:>10.1f}  "

                print(f"{slot_str:<26} | {txns_str} | {votes_str} | {user_str} | {block_str} | {time_str} | {format_lamports(row['total_fee']):>14} | {format_lamports(row['priority_fee']):>14}")
            else:
                t_ind = get_indicator(row["txns"], median_txns)
                v_ind = get_indicator(row["votes"], median_votes)
                u_ind = get_indicator(row["user"], median_user)
                b_ind = get_indicator(row["block_cost"], median_block_cost)
                tm_ind = get_indicator(row["time_ms"], median_time_ms)

                txns_str = f"{row['txns']:>6,}{t_ind:>2}"
                votes_str = f"{row['votes']:>6,}{v_ind:>2}"
                user_str = f"{row['user']:>6,}{u_ind:>2}"
                block_str = f"{row['block_cost']:>13,}{b_ind:>2}"
                time_str = f"{row['time_ms']:>10.1f}{tm_ind:>2}"

                print(f"{row['slot']:<26} | {txns_str} | {votes_str} | {user_str} | {block_str} | {time_str} | {format_lamports(row['total_fee']):>14} | {format_lamports(row['priority_fee']):>14}")

        print("-" * LEADER_TABLE_WIDTH)
        # Calculate median fees
        median_total_fee = get_median([r["total_fee"] for r in slot_rows if not r.get("skipped")])
        median_priority_fee = get_median([r["priority_fee"] for r in slot_rows if not r.get("skipped")])

        print(f"{'TOTAL':<26} | {total_txns:>8,} | {total_votes:>8,} | {total_user:>8,} | {total_block_cost:>15,} |              | {format_lamports(total_total_fee):>14} | {format_lamports(total_priority_fee):>14}")
        slots_label = f"({slot_count} produced, {skipped_count} skipped)"
        print(f"{slots_label:<26} |          |          |          |                 |              |                |")
        print(f"{'MEDIAN':<26} | {median_txns:>8,.0f} | {median_votes:>8,.0f} | {median_user:>8,.0f} | {median_block_cost:>15,.0f} | {median_time_ms:>12.1f} | {format_lamports(median_total_fee):>14} | {format_lamports(median_priority_fee):>14}")
        print("=" * LEADER_TABLE_WIDTH)
    else:
        print("No leader slot data found in this log window.")
        print("No leader slots landed in the time range covered by the log being analyzed.")
        print("This does not mean the validator is off the schedule — check with")
        print("`solana leader-schedule` or ~/show-my-next-leader-slot.sh. To see leader")
        print("activity, analyze a longer window via `-j sol --hours N` or an older log file.")
        print("(A genuinely empty result is also expected for hot-standby validators.)\n")

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
            print(f"Median bundles per leader period: {median_bundles:,.0f}")

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
        if small_block_count > 0:
            small_block_pct = (small_block_count / slot_count) * 100 if slot_count > 0 else 0
            print(f"  Small blocks: {small_block_count} ({small_block_pct:.1f}%) {SMALL_BLOCK_MARKER}")
        print(f"  Total transactions: {total_txns:,} ({total_votes:,} votes, {total_user:,} user)")
        print(f"  Total compute units: {total_block_cost:,}")
        print(f"  Total fees: {format_lamports(total_total_fee)} SOL")
        print(f"  Total priority fees: {format_lamports(total_priority_fee)} SOL")
        if slot_count > 0:
            print(f"  Per-block median: {median_txns:,.0f} txns ({median_votes:,.0f} votes, {median_user:,.0f} user), {median_block_cost:,.0f} CUs")
            print(f"  Per-block median: {format_lamports(median_total_fee)} SOL fees, {format_lamports(median_priority_fee)} SOL priority")
            print(f"  Median block time: {median_time_ms:.1f} ms")

    # ---- structured summary for --json consumers ----------------------------
    # Built from the same values the tables above print, so the two can never
    # disagree. The BAM totals are initialised unconditionally further up; the
    # leader-slot locals only exist when leader metrics were seen in the window,
    # hence the _have_leader guard (a conditional expression short-circuits, so
    # the names are never evaluated when absent).
    #
    # The one deliberate exception: with --leader-slots the bundle counters below
    # come from the leader-anchored sub-window, which is narrower than the
    # per-minute table above. That is the point of the flag, and the printed
    # "Leader-anchored BAM window" line above states the same numbers.
    _have_leader = bool(leader_slot_metrics or leader_slots_announced)
    if anchored is not None:
        total_bundles = anchored["bundles"]
        total_results = anchored["results"]
        total_scheduler_fail = anchored["scheduler_fail"]
        total_outbound_fail = anchored["outbound_fail"]
    return {
        "source": source_name,
        "window": {
            "since": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if since_dt else None,
            "until": until_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if until_dt else None,
            "first_active_minute": active_minutes[0] if active_minutes else None,
            "last_active_minute": active_minutes[-1] if active_minutes else None,
            "active_minutes": len(active_minutes),
        },
        # The window the bundle counters below were actually taken over, and
        # whether it was anchored on the leader slots or fell back to the
        # caller's wall-clock window. A caller must not raise a "BAM delivered
        # 0 bundles" alert when leader_window_found is false.
        "bam_window": {
            "leader_slots_requested": sorted(leader_slot_filter) if leader_slot_filter else None,
            "leader_window_found": leader_window_found,
            "since": anchored_window[0].strftime("%Y-%m-%dT%H:%M:%SZ") if anchored_window else None,
            "until": anchored_window[1].strftime("%Y-%m-%dT%H:%M:%SZ") if anchored_window else None,
        },
        "bam": {
            "bundles_received": total_bundles,
            "results_sent": total_results,
            # None, not 0: "no bundles arrived" and "0% of bundles were sent on"
            # are different failures and must stay distinguishable downstream.
            "send_rate_pct": round((total_results / total_bundles) * 100, 2) if total_bundles else None,
            "scheduler_fail": total_scheduler_fail,
            "outbound_fail": total_outbound_fail,
            "failures_total": total_scheduler_fail + total_outbound_fail,
            "heartbeats_leader_periods": total_heartbeats,
            "heartbeats_total": global_heartbeats,
            "unhealthy_connection_events": global_unhealthy,
        },
        "leader_slots": {
            "produced": slot_count if _have_leader else 0,
            "skipped": skipped_count if _have_leader else 0,
            "small_blocks": small_block_count if _have_leader else 0,
            "transactions": total_txns if _have_leader else 0,
            "votes": total_votes if _have_leader else 0,
            "user_transactions": total_user if _have_leader else 0,
            "compute_units": total_block_cost if _have_leader else 0,
            "total_fee_lamports": total_total_fee if _have_leader else 0,
            "priority_fee_lamports": total_priority_fee if _have_leader else 0,
        },
    }

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

def _parse_window_arg(args, flag):
    """Pull `--since/--until <epoch-seconds>` out of args. Returns (args, dt|None).

    Epoch seconds, not a date string, because the caller is
    leader-capture-monitor.sh, which already has the rotation window as epoch
    seconds. Converted to a NAIVE UTC datetime to match parse_timestamp(), which
    parses the log's naive UTC timestamps.
    """
    if flag not in args:
        return args, None
    try:
        idx = args.index(flag)
        value = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
        return args, datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    except (IndexError, ValueError):
        print(f"Error: {flag} requires epoch seconds (integer)")
        print(f"Run '{sys.argv[0]} --help' for usage information.")
        sys.exit(1)


def _parse_leader_slots_arg(args):
    """Pull `--leader-slots N,M,...` out of args. Returns (args, set|None).

    Same CSV that leader-capture-monitor.sh already passes to
    slot-transactions.py, so the caller has it to hand.
    """
    if '--leader-slots' not in args:
        return args, None
    try:
        idx = args.index('--leader-slots')
        raw = args[idx + 1]
        slots = {int(s) for s in raw.split(',') if s.strip()}
        if not slots:
            raise ValueError("empty slot list")
        return args[:idx] + args[idx + 2:], slots
    except (IndexError, ValueError):
        print("Error: --leader-slots requires a comma-separated list of slot numbers")
        print(f"Run '{sys.argv[0]} --help' for usage information.")
        sys.exit(1)


def _run(line_source, source_name, since_dt, until_dt, json_mode,
         leader_slot_filter=None):
    """Run the analysis, emitting either the tables or a JSON summary.

    In JSON mode the table output is captured and discarded rather than the
    ~500 lines of print() being made conditional -- that keeps the human output
    and the machine output driven by exactly the same code path.
    """
    if not json_mode:
        return analyze_logs(line_source, source_name, since_dt, until_dt,
                            leader_slot_filter)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        summary = analyze_logs(line_source, source_name, since_dt, until_dt,
                               leader_slot_filter)
    print(json.dumps(summary, indent=2))
    return summary


def main():
    # Parse arguments
    args = sys.argv[1:]

    json_mode = '--json' in args
    if json_mode:
        args = [a for a in args if a != '--json']

    args, since_dt = _parse_window_arg(args, '--since')
    args, until_dt = _parse_window_arg(args, '--until')
    args, leader_slot_filter = _parse_leader_slots_arg(args)

    # Extract --hours if present
    hours = DEFAULT_HOURS
    if '--hours' in args:
        try:
            hours_idx = args.index('--hours')
            hours = int(args[hours_idx + 1])
            args = args[:hours_idx] + args[hours_idx + 2:]
        except (IndexError, ValueError):
            print("Error: --hours requires a numeric value")
            print(f"Run '{sys.argv[0]} --help' for usage information.")
            sys.exit(1)

    if len(args) == 0:
        # No arguments - use default log file if it exists
        if not os.path.exists(DEFAULT_LOG_PATH):
            print(f"Error: Default log file not found: {DEFAULT_LOG_PATH}")
            print(f"\nPlease specify a log file path or use -j for journalctl.")
            print(f"Run '{sys.argv[0]} --help' for usage information.")
            sys.exit(1)
        verify_log_file(DEFAULT_LOG_PATH)
        _run(get_lines_from_file(DEFAULT_LOG_PATH, since_dt), DEFAULT_LOG_PATH, since_dt, until_dt, json_mode,
             leader_slot_filter)

    elif args[0] in ['-h', '--help']:
        print_usage()
        sys.exit(0)

    elif args[0] in ['-j', '--journal']:
        # Use journalctl
        service = args[1] if len(args) > 1 else DEFAULT_SERVICE
        display_name = service if service.endswith('.service') else f"{service}.service"

        verify_journalctl_service(service)
        if since_dt or until_dt:
            label = f"journalctl -u {display_name} ({since_dt or 'start'} .. {until_dt or 'now'} UTC)"
        else:
            label = f"journalctl -u {display_name} (last {hours}h)"
        _run(get_lines_from_journalctl(service, hours, since_dt, until_dt), label,
             since_dt, until_dt, json_mode, leader_slot_filter)

    else:
        # Assume it's a log file path
        log_file = args[0]
        verify_log_file(log_file)
        _run(get_lines_from_file(log_file, since_dt), log_file, since_dt, until_dt, json_mode,
             leader_slot_filter)

if __name__ == "__main__":
    main()
