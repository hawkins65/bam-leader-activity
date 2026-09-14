#!/home/sol/python/venv/bin/python3
"""
Hourly AI-powered log error summary for Solana Validator.
Collects errors from the past hour, sends to Claude API for analysis,
posts a Discord embed with the summary.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

_webhook_path = Path.home() / ".config" / "discord" / "webhook"
DISCORD_WEBHOOK = _webhook_path.read_text().strip() if _webhook_path.exists() else ""

# Skip unless the running validator is using the staked identity. Both of these
# used to exit at import, which meant the file could not be imported anywhere
# but a live validator host - see ops/ha/tests/hourly_leader_revenue_test.py.
if __name__ == "__main__":
    if subprocess.run(["/home/sol/bam-leader-activity/role-gate.sh"]).returncode != 0:
        sys.exit(0)
    if not DISCORD_WEBHOOK:
        print("ERROR: Discord webhook not found at ~/.config/discord/webhook", file=sys.stderr)
        sys.exit(1)
DISCORD_EMBED_SCRIPT = Path.home() / "999_discord_embed.sh"
BOT_USERNAME = "Validator Log Summary"
HOSTNAME = subprocess.run(['hostname'], capture_output=True, text=True).stdout.strip()
SCRIPT_PATH = f"{HOSTNAME}:{os.path.abspath(__file__)}"
# 2026-08-28: this was ['solana', 'address'] with no path. `solana` is not on
# PATH for this user - not under cron, not even interactively - so this raised an
# uncaught FileNotFoundError at import and killed the ENTIRE script on every run.
# It had been doing so ~24x/day for at least the full 7-day log retention; nobody
# saw it because the monitor that reads this log was itself broken. Absolute path
# plus a guard so a missing/renamed binary degrades to "unknown" instead of
# taking the whole summary down.
SOLANA_BIN = os.environ.get("SOLANA_BIN") or str(
    Path.home() / ".local/share/solana/install/active_release/bin/solana")
try:
    VALIDATOR_IDENTITY = subprocess.run(
        [SOLANA_BIN, 'address'], capture_output=True, text=True, timeout=15
    ).stdout.strip() or "unknown"
except (FileNotFoundError, OSError, subprocess.SubprocessError):
    VALIDATOR_IDENTITY = "unknown"
LOG_DIR = Path.home() / "logs"
# 2026-08-28: claude-sonnet-4-20250514 was retired and returned HTTP 404 on EVERY
# run (~24x/day for at least the full log retention). It then ran claude-opus-5
# with adaptive thinking, which cost ~3x Sonnet and spent most of its output on
# thinking for a 4-6 bullet Discord summary. 2026-09-14: claude-sonnet-5 with
# thinking disabled - the model never sets severity or pages (both come from the
# error count below), so this is summarisation only. The parser still takes the
# first text block rather than content[0], in case thinking is re-enabled.
CLAUDE_MODEL = "claude-sonnet-5"
LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100MB
VALIDATOR_LOG = LOG_DIR / "validator.log"
CAPTURES_DIR = Path.home() / "bam-leader-activity" / "captures"
# Per-rotation ledger written by leader-capture-monitor.sh. It is the only place
# the vote-transaction count lives - slot-transactions.py reports what landed in
# OUR blocks, and votes are paid in everyone else's.
DAILY_LEDGER = Path.home() / "bam-leader-activity" / "daily_totals.jsonl"
VOTE_LAMPORTS = 5000   # flat base fee per vote txn (one signature)
VALIDATOR_SH = Path.home() / "validator.sh"
# The same file runs on mainnet and testnet hosts; only the prompt's network word
# differed, which is why the testnet copy used to be a separate md5.
NETWORK = "testnet" if HOSTNAME.startswith("testnet") else "mainnet"
SLOT_TRANSACTIONS_SCRIPT = Path.home() / "bam-leader-activity" / "slot-transactions.py"

# BAM connection error patterns (from bam-hourly-summary.py)
BAM_ERROR_PATTERNS = [
    "BAM connection lost",
    "BAM connection not healthy",
    "Failed to connect to BAM",
    "Failed to start scheduler stream",
    "Inbound stream closed",
    "Failed to get config",
]

BAM_METRIC_KEYS = [
    "bundle_received",
    "bundleresult_sent",
    "bundle_forward_to_scheduler_fail",
    "outbound_fail",
    "unhealthy_connection_count",
    "heartbeat_received",
    "heartbeat_sent",
    "leaderstate_sent",
]

ERROR_PATTERN = re.compile(
    r' ERROR |'
    r'\[ERROR\]|'
    r'\[FATAL\]|'
    r' FATAL |'
    r'Traceback|'
    r'Exception:|'
    r'FAILED:|'
    r'failed to|'
    r'Error:.*failed|'
    r'panicked at|'
    r'panic!|'
    r'SIGABRT|'
    r'SIGSEGV|'
    r'out of memory|'
    r'OOM'
)

EXCLUDE_PATTERN = re.compile(
    r'Failed: 0|'
    r'Failed updates: 0|'
    r'errors=0|'
    r'failures=0|'
    r'relayer_stage|'
    r'relayer_url'
)

# Low-severity errors: real ERROR-level lines that are normal validator behavior.
# Excluded from the genuine error list but counted separately and reported to AI as LOW.
TRACKED_LOW_SEVERITY = [
    {
        "name": "BAM Connection Errors",
        "pattern": re.compile(r'bam_connection\].*Failed to start scheduler stream|bam_manager\].*Failed to connect to BAM'),
        "description": "BAM/Jito block engine connection retries (auto-recovering, monitored by dedicated BAM monitor)",
    },
    {
        # 2026-08-28: this fired ~59/5min for the whole time new-amsterdam held the
        # JUNK identity and produced a spurious HIGH "BAM connectivity degraded"
        # summary. It is EXPECTED on a standby: BAM refuses any validator that is
        # not on the leader schedule, and the junk identity never is. It stopped
        # ~4 min after promotion on its own. Only worth attention if it persists
        # while this host holds the STAKED identity.
        "name": "BAM rejects non-leader identity",
        "pattern": re.compile(r'bam_connection\].*Validator is not on the leader schedule'),
        "description": "BAM refusing a validator that is not on the leader schedule - NORMAL while this host is the HA standby holding the unstaked/junk identity; clears within minutes of promotion. Only escalate if it continues while the host is staked and voting.",
    },
    {
        "name": "Dead Slot from Other Leaders",
        "pattern": re.compile(r'datapoint: replay-stage-mark_dead_slot'),
        "description": "Other validators' bad blocks rejected during replay (normal network behavior)",
    },
    {
        "name": "Scheduler Accumulate Error",
        "pattern": re.compile(r'solana_unified_scheduler_pool\].*error is detected while accumulating'),
        "description": "Transaction failed during unified scheduler accumulation (AccountNotFound, AlreadyProcessed, etc. — normal replay noise from other leaders' blocks)",
    },
    {
        "name": "Entry Error (Block Replay)",
        "pattern": re.compile(r'datapoint: validator_process_entry_error'),
        "description": "Transaction failed during block replay (invalid txns included by other leaders — normal network noise, all failure reasons)",
    },
    {
        "name": "Vote State Dropped (Other Validators)",
        "pattern": re.compile(r'vote_state\].*dropped vote.*failed to match hash'),
        "description": "Other validators' votes dropped during block replay due to hash mismatches (normal consensus behavior, not this validator's votes)",
    },
    {
        "name": "Tower Restore",
        "pattern": re.compile(r'failed tower restore'),
        "description": "Tower file missing on restart, rebuilt from vote account (one-time per restart)",
    },
    {
        "name": "Tip Programs Transaction Error",
        "pattern": re.compile(r'Error running tip programs for transactions|consume-worker-error.*tip_programs_error'),
        "description": "Jito tip program execution failed for a transaction (DeFi swap/arb failure, not a validator issue)",
    },
]

# Solana log timestamp: [2026-02-15T00:00:06.663056547Z ...]
# 2026-08-29: this used to require a literal "[" and an ISO "T" separator, which
# only validator.log actually writes. Every other log in ~/logs uses
# "[YYYY-MM-DD HH:MM:SS]" (log() below, bam_monitor) or a bare
# "YYYY-MM-DD HH:MM:SS UTC" prefix (bam-failover). None of those parsed, so the
# cutoff filter never applied to them - see _collect_errors_forward. All fleet
# hosts run Etc/UTC, so a naive timestamp is read as UTC.
TIMESTAMP_RE = re.compile(r'^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})')


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    key_file = Path.home() / ".config" / "anthropic" / "api_key"
    if key_file.exists():
        return key_file.read_text().strip()
    return None


def parse_timestamp(line):
    """Extract datetime from a Solana log line."""
    m = TIMESTAMP_RE.match(line)
    if m:
        try:
            return datetime.fromisoformat(m.group(1) + "+00:00")
        except ValueError:
            pass
    return None


def classify_error(line):
    """Classify an error line. Returns ('tracked', index) or ('genuine', None)."""
    for i, entry in enumerate(TRACKED_LOW_SEVERITY):
        if entry["pattern"].search(line):
            return ('tracked', i)
    return ('genuine', None)


def collect_errors_from_file(filepath, cutoff_time, verbose=False):
    """Collect error lines from a log file that are newer than cutoff_time.
    Returns (genuine_errors, tracked_counts) where tracked_counts is a Counter
    mapping tracked pattern index -> count."""
    file_size = filepath.stat().st_size

    if file_size > LARGE_FILE_THRESHOLD:
        if verbose:
            log(f"  Large file ({file_size / 1024 / 1024:.0f}MB), using tac for {filepath.name}")
        return _collect_errors_tac(filepath, cutoff_time)
    else:
        if verbose:
            log(f"  Reading {filepath.name} ({file_size / 1024 / 1024:.1f}MB)")
        return _collect_errors_forward(filepath, cutoff_time)


# 2026-08-28: errors[] previously grew without limit; the 8000-char truncation
# further down applies only AFTER the whole list is built. validator.log grew
# 143 GB in one day during an incident, and a fault-flood hour could build a
# multi-GB list of Python strings before that truncation ever ran. Cap while
# appending, and make the cap visible in the output rather than silent.
MAX_ERRORS_COLLECTED = 5000


# A Python traceback's frames match neither ERROR_PATTERN nor TIMESTAMP_RE, so a
# line-at-a-time collector kept the bare "Traceback (most recent call last):"
# header and threw away the exception type and stack - the only two things that
# make it triageable. Frames are appended to the header's entry, so one traceback
# stays ONE error for counting and dedup purposes.
CONTINUATION_RE = re.compile(
    r'^(?:\s+\S|\.{3}|[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)\b|'
    r'During handling of|The above exception)')
MAX_TRACEBACK_FRAMES = 25


def _process_line(line, errors, tracked_counts):
    """Check if a line is an error, classify it, and add to appropriate bucket.

    Returns True when the line was appended as a genuine error, so the caller
    can attach the traceback frames that follow it."""
    if ERROR_PATTERN.search(line) and not EXCLUDE_PATTERN.search(line):
        kind, idx = classify_error(line)
        if kind == 'tracked':
            tracked_counts[idx] += 1
        elif len(errors) < MAX_ERRORS_COLLECTED:
            errors.append(line.rstrip())
            return True
        elif len(errors) == MAX_ERRORS_COLLECTED:
            errors.append(
                f"... error collection capped at {MAX_ERRORS_COLLECTED} lines; "
                "further errors in this file were not collected")
    return False


def _collect_errors_forward(filepath, cutoff_time):
    """Read file forward, collect errors within time window."""
    errors = []
    tracked_counts = Counter()
    # 2026-08-29: a line with no parseable timestamp used to skip the cutoff
    # check entirely, so an untimestamped record - a Python traceback dumped
    # into the log by cron - was re-reported EVERY hour, forever. 18 tracebacks
    # from the 08-28 `solana`-not-on-PATH bug were still being posted as "18
    # errors this hour" a day after that bug was fixed. A line with no timestamp
    # now inherits the timestamp of the record it belongs to.
    last_ts = None
    preamble = []   # lines before the file's FIRST timestamp; they belong to a
                    # record that started in an already-rotated file, so they
                    # inherit the first timestamp we do see.
    frames = 0

    def take(line):
        nonlocal frames
        if frames and errors and CONTINUATION_RE.match(line):
            frames -= 1
            errors[-1] += "\n" + line.rstrip()
            return
        frames = MAX_TRACEBACK_FRAMES if _process_line(line, errors, tracked_counts) else 0

    try:
        with open(filepath, 'r', errors='replace') as f:
            for line in f:
                ts = parse_timestamp(line)
                if ts is None:
                    if last_ts is None:
                        if len(preamble) < MAX_ERRORS_COLLECTED:
                            preamble.append(line)
                    elif last_ts >= cutoff_time:
                        take(line)
                    continue
                if last_ts is None and preamble:
                    if ts >= cutoff_time:
                        for held in preamble:
                            take(held)
                    preamble.clear()
                last_ts = ts
                if ts >= cutoff_time:
                    take(line)
    except Exception as e:
        log(f"  Error reading {filepath}: {e}")
    if last_ts is None:
        # No timestamp anywhere in the file - we cannot date anything in it, so
        # keep the old behaviour rather than going silently blind on it.
        for held in preamble:
            take(held)
    return errors, tracked_counts


def _collect_errors_tac(filepath, cutoff_time):
    """Read file from end using tac, stop when we pass the time window.

    No traceback joining here: tac yields lines in reverse, so frames arrive
    before their header. This path only runs on files >100MB - in practice just
    validator.log, whose lines are all timestamped and single-line."""
    errors = []
    tracked_counts = Counter()
    try:
        proc = subprocess.Popen(
            ['tac', str(filepath)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )
        for line in proc.stdout:
            ts = parse_timestamp(line)
            if ts and ts < cutoff_time:
                break
            _process_line(line, errors, tracked_counts)
        proc.terminate()
        proc.wait()
    except Exception as e:
        log(f"  Error reading {filepath} with tac: {e}")
    errors.reverse()
    return errors, tracked_counts


def deduplicate_errors(errors):
    """Deduplicate errors by normalizing and hashing."""
    seen = set()
    unique = []
    for line in errors:
        normalized = line
        normalized = re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.\d]*Z?', 'TS', normalized)
        normalized = re.sub(r'\bpid[=: ]\d+', 'pid=P', normalized)
        normalized = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?', 'IP', normalized)
        normalized = re.sub(r'\bslot[=: ]\d+', 'slot=S', normalized)
        normalized = re.sub(r'\b\d{6,}\b', 'N', normalized)
        h = hashlib.md5(normalized.encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            unique.append(line)
    return unique


def extract_bam_summary(cutoff_time, verbose=False):
    """Extract BAM connectivity and metrics from validator.log for the time window.

    NOTE: Overlaps bam-leader-activity.py, deliberately and only partially.
    Both sum the same `datapoint: bam_` metric keys, so a change to those keys
    must be made in BOTH places. Everything else here is unique to this script --
    BAM_ERROR_PATTERNS categorisation, auth-failure detection, and leader-slot
    detection from `bam_scheduler` "Bank boundary detected" lines -- and
    bam-leader-activity.py produces none of it. Replacing this with a call to
    that script (considered 2026-08-09) would therefore LOSE the error
    categorisation this hourly summary exists to provide. Do not "deduplicate"
    it without first porting those three behaviours across.
    """
    if not VALIDATOR_LOG.exists():
        return None

    metric_re = re.compile(r'(\w+)=(\d+)i')
    metrics = Counter()
    error_categories = Counter()
    connection_errors = 0
    scheduler_events = 0
    leader_slots = set()

    try:
        file_size = VALIDATOR_LOG.stat().st_size
        if file_size > LARGE_FILE_THRESHOLD:
            proc = subprocess.Popen(
                ['tac', str(VALIDATOR_LOG)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            line_source = proc.stdout
        else:
            proc = None
            line_source = open(VALIDATOR_LOG, 'r', errors='replace')

        for line in line_source:
            ts = parse_timestamp(line)
            if proc and ts and ts < cutoff_time:
                break
            if not proc and ts and ts < cutoff_time:
                continue

            if 'bam' not in line.lower() and 'BAM' not in line:
                continue

            # Check connection errors
            line_lower = line.lower()
            matched_error = False
            for pattern in BAM_ERROR_PATTERNS:
                if pattern in line:
                    connection_errors += 1
                    error_categories[pattern] += 1
                    matched_error = True
                    break
            if not matched_error and 'auth' in line_lower and 'fail' in line_lower:
                connection_errors += 1
                error_categories["auth failure"] += 1
                matched_error = True

            # Extract metrics from datapoint lines
            if 'datapoint: bam_' in line:
                scheduler_events += 1
                for key, val in metric_re.findall(line):
                    if key in BAM_METRIC_KEYS:
                        metrics[key] += int(val)

            # Leader slot detection from scheduler bank boundary
            if 'bam_scheduler' in line and 'Bank boundary detected' in line:
                slot_m = re.search(r'slot changed from \w+ to (\d+)', line)
                if slot_m:
                    leader_slots.add(slot_m.group(1))

        if proc:
            proc.terminate()
            proc.wait()
        else:
            line_source.close()

    except Exception as e:
        if verbose:
            log(f"Error extracting BAM data: {e}")
        return None

    has_data = connection_errors > 0 or scheduler_events > 0 or any(metrics.values())
    if not has_data:
        return None

    return {
        "metrics": dict(metrics),
        "connection_errors": connection_errors,
        "error_categories": dict(error_categories),
        "scheduler_events": scheduler_events,
        "leader_slots": len(leader_slots),
        "unhealthy": metrics.get("unhealthy_connection_count", 0),
        "bundles": metrics.get("bundle_received", 0),
        "heartbeats": metrics.get("heartbeat_received", 0),
        "outbound_fail": metrics.get("outbound_fail", 0),
    }


def read_commission_pct():
    """MEV commission from validator.sh (--commission-bps), as a percent.

    Read per run, not cached at import: a commission change on the box has to
    show up in the next report, the way leader-capture-monitor.sh does it.
    """
    try:
        m = re.search(r'--commission-bps\s+(\d+)', VALIDATOR_SH.read_text())
        return int(m.group(1)) / 100 if m else 0.0
    except OSError:
        return 0.0


def sum_vote_txns(cutoff_time, verbose=False):
    """Vote transactions paid for, from leader-capture-monitor.sh's ledger.

    Each ledger row carries the votes counted since the PREVIOUS rotation, so
    the first row inside the window reaches back before it. That fuzz is the
    price of not re-counting signatures over RPC here; the number is labelled
    "charged at rotations" in the embed for exactly that reason.

    Returns None when there is no ledger to read, which drops the vote and net
    lines rather than printing a zero that looks like a free hour.
    """
    if not DAILY_LEDGER.exists():
        return None
    cutoff_ts = cutoff_time.timestamp()
    total = 0
    rows = 0
    try:
        with open(DAILY_LEDGER) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if float(d.get("ts", 0)) < cutoff_ts:
                    continue
                total += int(d.get("vote_txns", 0) or 0)
                rows += 1
    except OSError as e:
        if verbose:
            log(f"  Could not read {DAILY_LEDGER}: {e}")
        return None
    if verbose:
        log(f"  Ledger: {rows} row(s) in window, {total} vote txns")
    return total if rows > 0 else None


def collect_leader_slot_earnings(cutoff_time, verbose=False):
    """Collect leader slot transaction/earnings data from capture JSON files
    written by leader-capture-monitor.sh during the past hour.

    Rolls up: txn counts, slot counts, fees (leader credit), Jito tip
    revenue, total revenue, and tip-anomaly events across every capture
    JSON whose mtime falls inside the window.
    """
    if not CAPTURES_DIR.exists():
        return None

    total_txns = 0
    total_success = 0
    total_failed = 0
    total_fees = 0
    total_tips = 0
    total_revenue = 0
    total_slots = 0
    total_skipped = 0
    tip_anomaly_count = 0
    tip_anomaly_lamports = 0
    withdrawal_count = 0
    withdrawal_lamports = 0
    total_compute_units = 0
    rotations = 0
    first_mtime = None

    for json_file in sorted(CAPTURES_DIR.glob("slot_txns_*.json")):
        # Check file modification time against cutoff
        mtime = datetime.fromtimestamp(json_file.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff_time:
            continue

        try:
            with open(json_file) as f:
                data = json.load(f)
            summary = data.get("summary", {})
            total_txns += summary.get("total_non_vote_transactions", 0)
            total_success += summary.get("successful", 0)
            total_failed += summary.get("failed", 0)
            total_fees += summary.get("total_fees_lamports", 0)
            total_tips += summary.get("total_tips_lamports", 0)
            # If the capture was written before total_revenue_lamports
            # existed, fall back to fees + tips so we still roll up cleanly.
            total_revenue += summary.get(
                "total_revenue_lamports",
                summary.get("total_fees_lamports", 0) + summary.get("total_tips_lamports", 0),
            )
            total_slots += summary.get("total_slots", 0)
            total_skipped += summary.get("skipped_slots", 0)
            tip_anomaly_count += summary.get("tip_anomaly_count", 0)
            tip_anomaly_lamports += summary.get("tip_anomaly_lamports", 0)
            withdrawal_count += summary.get("tip_withdrawal_count", 0)
            withdrawal_lamports += summary.get("tip_withdrawal_lamports", 0)
            total_compute_units += summary.get("total_compute_units", 0)
            rotations += 1
            if first_mtime is None or mtime < first_mtime:
                first_mtime = mtime
            if verbose:
                log(f"  Capture {json_file.name}: {summary.get('total_non_vote_transactions', 0)} txns, "
                    f"{summary.get('total_fees_sol', 0):.6f} SOL fees, "
                    f"{summary.get('total_tips_sol', 0):.6f} SOL tips")
        except Exception as e:
            if verbose:
                log(f"  Error reading capture {json_file.name}: {e}")

    if rotations == 0:
        return None

    produced = total_slots - total_skipped
    commission_pct = read_commission_pct()
    tips_to_validator = total_tips * commission_pct / 100
    total_to_validator = total_fees + tips_to_validator
    vote_txns = sum_vote_txns(cutoff_time, verbose=verbose)
    vote_lamports = None if vote_txns is None else vote_txns * VOTE_LAMPORTS

    return {
        "rotations": rotations,
        "commission_pct": commission_pct,
        "tips_to_validator_sol": tips_to_validator / 1e9,
        "total_to_validator_sol": total_to_validator / 1e9,
        "vote_txns": vote_txns,
        "vote_cost_sol": None if vote_lamports is None else vote_lamports / 1e9,
        "net_to_validator_sol": (None if vote_lamports is None
                                 else (total_to_validator - vote_lamports) / 1e9),
        "avg_cu_per_block": (total_compute_units // produced) if produced > 0 else 0,
        "withdrawal_count": withdrawal_count,
        "withdrawal_sol": withdrawal_lamports / 1e9,
        "total_slots": total_slots,
        "skipped_slots": total_skipped,
        "produced_slots": total_slots - total_skipped,
        "total_txns": total_txns,
        "successful": total_success,
        "failed": total_failed,
        "total_fees_lamports": total_fees,
        "total_fees_sol": total_fees / 1e9,
        "total_tips_lamports": total_tips,
        "total_tips_sol": total_tips / 1e9,
        "total_revenue_lamports": total_revenue,
        "total_revenue_sol": total_revenue / 1e9,
        "tip_anomaly_count": tip_anomaly_count,
        "tip_anomaly_sol": tip_anomaly_lamports / 1e9,
    }


def format_leader_embed(leader_data):
    """Format leader slot data for Discord embed display."""
    lines = []
    r = leader_data["rotations"]
    rotation_label = "rotation" if r == 1 else "rotations"
    lines.append(f"**Leader slots:** {leader_data['produced_slots']} produced across {r} {rotation_label}")
    if leader_data["skipped_slots"] > 0:
        lines.append(f"**Skipped:** {leader_data['skipped_slots']}")
    lines.append(f"**Transactions:** {leader_data['total_txns']:,} ({leader_data['successful']:,} success, {leader_data['failed']:,} failed)")
    if leader_data.get("avg_cu_per_block", 0) > 0:
        lines.append(f"**Avg CU/block:** {leader_data['avg_cu_per_block']:,} CU "
                     f"(over {leader_data['produced_slots']} produced)")
    lines.append(f"**Fees earned:** {leader_data['total_fees_sol']:.6f} SOL")
    lines.append(f"**Jito tip revenue:** {leader_data['total_tips_sol']:.6f} SOL")
    lines.append(f"**Total revenue:** {leader_data['total_revenue_sol']:.6f} SOL")
    # What actually reaches the validator: all fees, but only our commission of
    # the tips, less the vote fees paid to stay in the schedule. Same arithmetic
    # as leader-capture-monitor.sh's per-rotation report.
    lines.append(f"**Jito to Validator:** {leader_data['tips_to_validator_sol']:.6f} SOL "
                 f"({leader_data['commission_pct']:g}% commission)")
    lines.append(f"**Total to Validator:** {leader_data['total_to_validator_sol']:.6f} SOL")
    if leader_data.get("vote_cost_sol") is not None:
        lines.append(f"**Vote cost:** {leader_data['vote_cost_sol']:.6f} SOL "
                     f"({leader_data['vote_txns']:,} votes, charged at rotations)")
        # Negative is normal on a quiet hour and is not an alert: the votes paid
        # while waiting for a rotation can outrun what the rotation earned.
        lines.append(f"**Net to Validator:** {leader_data['net_to_validator_sol']:.6f} SOL")
    if leader_data.get("withdrawal_count", 0) > 0:
        lines.append(
            f"⚠️ **Tip account withdrawals:** {leader_data['withdrawal_count']} event(s), "
            f"{leader_data['withdrawal_sol']:.6f} SOL out"
        )
    if leader_data.get("tip_anomaly_count", 0) > 0:
        lines.append(
            f"⚠️ **Tip anomalies:** {leader_data['tip_anomaly_count']} event(s), "
            f"{leader_data['tip_anomaly_sol']:.6f} SOL unaccounted"
        )
    return "\n".join(lines)


def format_bam_section(bam_data):
    """Format BAM data into a string for the AI prompt."""
    lines = []
    lines.append(f"Connection errors: {bam_data['connection_errors']}")
    if bam_data['error_categories']:
        for cat, count in sorted(bam_data['error_categories'].items(), key=lambda x: -x[1]):
            lines.append(f"  - {cat}: {count}")
    lines.append(f"BAM metric datapoints: {bam_data['scheduler_events']}")
    for key in BAM_METRIC_KEYS:
        val = bam_data['metrics'].get(key, 0)
        if val > 0:
            lines.append(f"  {key}: {val}")
    return "\n".join(lines)


def format_bam_embed(bam_data):
    """Format BAM data for Discord embed display."""
    lines = []
    lines.append("**Metrics:**")
    for key in BAM_METRIC_KEYS:
        val = bam_data['metrics'].get(key, 0)
        if val > 0:
            lines.append(f"• {key}: {val}")
    if not any(bam_data['metrics'].get(k, 0) > 0 for k in BAM_METRIC_KEYS):
        lines.append("• No BAM metric datapoints in this period")
    return "\n".join(lines)


def call_claude_api(api_key, errors_text, error_count, tracked_counts, bam_data=None):
    """Send errors to Claude API for analysis using urllib."""
    # Build tracked low-severity section
    tracked_section = ""
    total_tracked = sum(tracked_counts.values())
    if total_tracked > 0:
        tracked_lines = []
        for idx, count in sorted(tracked_counts.items()):
            entry = TRACKED_LOW_SEVERITY[idx]
            severity_hint = "report as 🟡 MEDIUM" if count > 50 else "report as 🟢 LOW"
            tracked_lines.append(f"  - {entry['name']}: {count} occurrences ({severity_hint})\n    Description: {entry['description']}")
        tracked_section = (
            f"\n\nAdditionally, the following low-severity errors were detected but filtered from the main error list. "
            f"Report these at the indicated severity unless the count is unusually high:\n"
            + "\n".join(tracked_lines)
        )

    # BAM section for prompt
    bam_prompt_section = ""
    if bam_data:
        bam_prompt_section = f"""

**BAM (Block Auction Marketplace) Connectivity & Metrics:**
{format_bam_section(bam_data)}

Include a BAM-specific bullet point in your summary assessing BAM health (connection stability, bundle processing, heartbeat status)."""

    prompt = f"""You are analyzing error logs from a Solana {NETWORK} validator.
Validator identity: {VALIDATOR_IDENTITY}
There were {error_count} genuine errors ({len(errors_text.splitlines()) if errors_text.strip() else 0} unique patterns) in the past hour.{tracked_section}{bam_prompt_section}

Provide a concise summary with 4-6 bullet points. Categorize each by severity:
- 🔴 CRITICAL: Service down, data corruption, crashes
- 🟠 HIGH: Connectivity issues, repeated failures affecting operation
- 🟡 MEDIUM: Transient errors, retryable failures
- 🟢 LOW: Minor warnings, cosmetic issues, expected low-severity errors

For any MEDIUM or higher severity bullet, include the relevant validator pubkey (from the log lines) so it's clear which validator is affected. Our validator identity is {VALIDATOR_IDENTITY} — distinguish between issues affecting our validator vs. other validators on the network.

Start with an overall status line like:
"✅ Validator healthy — minor issues only" or "⚠️ Attention needed — connectivity problems detected" or "🚨 Critical issues detected"
{f"""
Here are the genuine error log lines:

{errors_text}""" if errors_text.strip() else ""}"""

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": prompt}]
    }).encode('utf-8')

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            # 2026-08-28: was data["content"][0]["text"]. With adaptive thinking on,
            # content[0] is a thinking block and that raised KeyError. Take the first
            # text block instead, and fail loudly rather than silently returning junk.
            text = next((b.get("text") for b in data.get("content", [])
                         if b.get("type") == "text"), None)
            if text is None:
                log(f"Claude API: no text block in response (stop_reason="
                    f"{data.get('stop_reason')}, blocks="
                    f"{[b.get('type') for b in data.get('content', [])]})")
            return text
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        log(f"Claude API error: HTTP {e.code}: {body}")
        return None
    except Exception as e:
        log(f"Claude API error: {e}")
        return None


def send_discord_embed(severity, title, description, footer_extra="", pagerduty=True):
    """Send a Discord embed via the standard 999_discord_embed.sh script.

    Severity levels: ok, info, warning, error, critical
    """
    if not DISCORD_EMBED_SCRIPT.exists():
        log(f"ERROR: Discord embed script not found: {DISCORD_EMBED_SCRIPT}")
        return False

    # Use \n literals — the bash script converts them to real newlines
    description = description.replace('\n', '\\n')
    # Escape backticks and $ to prevent bash interpretation inside double quotes
    description = description.replace('`', '\\`').replace('$', '\\$')
    title = title.replace('`', '\\`').replace('$', '\\$')

    cmd = (
        f'source "{DISCORD_EMBED_SCRIPT}" && '
        f'send_discord_embed "{DISCORD_WEBHOOK}" "{severity}" '
        f'"{title}" "{description}" '
        f'username="{BOT_USERNAME}" '
        f'script_path="{SCRIPT_PATH}"'
    )
    if footer_extra:
        cmd += f' footer_extra="{footer_extra}"'
    if not pagerduty:
        cmd += ' pagerduty=false'

    try:
        result = subprocess.run(
            ['bash', '-c', cmd],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log(f"Discord embed script error: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        log(f"Discord embed script error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Hourly AI log error summary")
    parser.add_argument("--hours", type=int, default=1, help="Hours to look back (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Collect and analyze but don't post to Discord")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    log(f"Starting hourly summary (looking back {args.hours}h)...")

    api_key = get_api_key()
    if not api_key:
        log("ERROR: No Anthropic API key found. Set ANTHROPIC_API_KEY or create ~/.config/anthropic/api_key")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.hours)

    if args.verbose:
        log(f"Cutoff time: {cutoff.isoformat()}")

    # Collect errors from all log files
    all_errors = {}
    total_tracked = Counter()
    for log_file in sorted(LOG_DIR.glob("*.log")):
        if args.verbose:
            log(f"Scanning {log_file.name}...")
        errors, tracked = collect_errors_from_file(log_file, cutoff, verbose=args.verbose)
        if errors:
            all_errors[log_file.name] = errors
        total_tracked += tracked

    # Flatten and count
    total_errors = sum(len(e) for e in all_errors.values())
    total_tracked_count = sum(total_tracked.values())
    log(f"Found {total_errors} genuine errors across {len(all_errors)} files, {total_tracked_count} tracked low-severity")

    if args.verbose and total_tracked_count > 0:
        for idx, count in sorted(total_tracked.items()):
            log(f"  Tracked: {TRACKED_LOW_SEVERITY[idx]['name']}: {count}")

    # Extract BAM connectivity & metrics
    log("Extracting BAM connectivity data...")
    bam_data = extract_bam_summary(cutoff, verbose=args.verbose)
    if bam_data:
        log(f"BAM: {bam_data['connection_errors']} errors, {bam_data['bundles']} bundles, {bam_data['heartbeats']} heartbeats")
    else:
        log("BAM: no data found (validator may not have --bam-url configured)")

    # Collect leader slot earnings from capture files
    log("Collecting leader slot earnings...")
    leader_data = collect_leader_slot_earnings(cutoff, verbose=args.verbose)
    if leader_data:
        log(
            f"Leader: {leader_data['rotations']} rotation(s), "
            f"{leader_data['total_txns']} txns, "
            f"{leader_data['total_fees_sol']:.6f} SOL fees + "
            f"{leader_data['total_tips_sol']:.6f} SOL tips = "
            f"{leader_data['total_revenue_sol']:.6f} SOL revenue"
        )
        if leader_data.get("tip_anomaly_count", 0) > 0:
            log(f"  ⚠️ {leader_data['tip_anomaly_count']} tip anomaly event(s) in window")
    else:
        log("Leader: no leader slot captures in the past hour")

    # If no genuine errors AND no tracked errors, all clear (but still include BAM + leader summary)
    if total_errors == 0 and total_tracked_count == 0:
        title = f"Hourly Log Summary — {now.strftime('%H:%M UTC')}"
        bam_section = ""
        if bam_data:
            if bam_data['connection_errors'] == 0 and bam_data['unhealthy'] == 0:
                bam_section = f"\n\n**BAM Status:** ✅ Healthy\n\n{format_bam_embed(bam_data)}"
            else:
                bam_section = f"\n\n**BAM Status:** ⚠️ {bam_data['connection_errors']} errors, {bam_data['unhealthy']} unhealthy\n\n{format_bam_embed(bam_data)}"
        leader_section = ""
        if leader_data:
            leader_section = f"\n\n**Leader Slot Earnings:**\n{format_leader_embed(leader_data)}"
        description = f"**No errors detected in the past hour.**\n\nAll monitored log files are clean.{bam_section}{leader_section}"

        if args.dry_run:
            log(f"DRY RUN — would post: {title}")
            log(description)
        else:
            send_discord_embed("ok", title, description, pagerduty=False)
        log("Done.")
        return

    # Deduplicate genuine errors
    flat_errors = []
    for fname, errs in all_errors.items():
        flat_errors.extend(errs)
    unique_errors = deduplicate_errors(flat_errors)

    if args.verbose:
        log(f"Unique genuine error patterns: {len(unique_errors)}")

    # Prepare error text for AI (limit to ~8000 chars for prompt)
    error_text = "\n".join(unique_errors)
    if len(error_text) > 8000:
        error_text = error_text[:8000] + "\n... (truncated)"

    # Call Claude API (pass tracked counts for LOW severity reporting, plus BAM data)
    log("Sending errors to Claude API for analysis...")
    summary = call_claude_api(api_key, error_text, total_errors, total_tracked, bam_data=bam_data)

    if summary is None:
        # Fallback summary
        parts = [f"⚠️ AI analysis unavailable. Raw error count: {total_errors} genuine errors from {len(all_errors)} files."]
        if all_errors:
            parts.append("\n".join(f"- **{fname}**: {len(errs)} errors" for fname, errs in all_errors.items()))
        if total_tracked_count > 0:
            parts.append(f"\n**Tracked low-severity ({total_tracked_count} total):**")
            for idx, count in sorted(total_tracked.items()):
                parts.append(f"- {TRACKED_LOW_SEVERITY[idx]['name']}: {count}")
        summary = "\n".join(parts)

    # Determine severity based on genuine errors
    if total_errors == 0:
        severity = "ok"       # only tracked low-severity
    elif total_errors < 10:
        severity = "warning"
    else:
        severity = "error"

    # Build title
    title_parts = []
    if total_errors > 0:
        title_parts.append(f"{total_errors} errors")
    if total_tracked_count > 0:
        title_parts.append(f"{total_tracked_count} tracked")
    title = f"Hourly Log Summary — {now.strftime('%H:%M UTC')} ({', '.join(title_parts)})"

    # Build description
    desc_parts = [summary]
    if all_errors:
        file_breakdown = "\n".join(f"• {fname}: {len(errs)} errors" for fname, errs in all_errors.items())
        desc_parts.append(f"\n**Files:**\n{file_breakdown}")
    if bam_data:
        if bam_data['connection_errors'] == 0 and bam_data['unhealthy'] == 0:
            desc_parts.append(f"\n**BAM Status:** ✅ Healthy\n\n{format_bam_embed(bam_data)}")
        else:
            desc_parts.append(f"\n**BAM Status:** ⚠️ {bam_data['connection_errors']} errors, {bam_data['unhealthy']} unhealthy\n\n{format_bam_embed(bam_data)}")
    if leader_data:
        desc_parts.append(f"\n**Leader Slot Earnings:**\n{format_leader_embed(leader_data)}")
    description = "\n".join(desc_parts)

    if args.dry_run:
        log(f"DRY RUN — would post: {title}")
        log(description)
    else:
        send_discord_embed(severity, title, description, pagerduty=False)

    log("Done.")


if __name__ == "__main__":
    main()
