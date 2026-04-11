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
if _webhook_path.exists():
    DISCORD_WEBHOOK = _webhook_path.read_text().strip()
else:
    print("ERROR: Discord webhook not found at ~/.config/discord/webhook", file=sys.stderr)
    sys.exit(1)
DISCORD_EMBED_SCRIPT = Path.home() / "999_discord_embed.sh"
BOT_USERNAME = "Validator Log Summary"
HOSTNAME = subprocess.run(['hostname'], capture_output=True, text=True).stdout.strip()
SCRIPT_PATH = f"{HOSTNAME}:{os.path.abspath(__file__)}"
LOG_DIR = Path.home() / "logs"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100MB
VALIDATOR_LOG = LOG_DIR / "validator.log"
CAPTURES_DIR = Path.home() / "bam-leader-activity" / "captures"
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
TIMESTAMP_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')


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


def _process_line(line, errors, tracked_counts):
    """Check if a line is an error, classify it, and add to appropriate bucket."""
    if ERROR_PATTERN.search(line) and not EXCLUDE_PATTERN.search(line):
        kind, idx = classify_error(line)
        if kind == 'tracked':
            tracked_counts[idx] += 1
        else:
            errors.append(line.rstrip())


def _collect_errors_forward(filepath, cutoff_time):
    """Read file forward, collect errors within time window."""
    errors = []
    tracked_counts = Counter()
    try:
        with open(filepath, 'r', errors='replace') as f:
            for line in f:
                ts = parse_timestamp(line)
                if ts and ts < cutoff_time:
                    continue
                _process_line(line, errors, tracked_counts)
    except Exception as e:
        log(f"  Error reading {filepath}: {e}")
    return errors, tracked_counts


def _collect_errors_tac(filepath, cutoff_time):
    """Read file from end using tac, stop when we pass the time window."""
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
    """Extract BAM connectivity and metrics from validator.log for the time window."""
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
    rotations = 0

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
            rotations += 1
            if verbose:
                log(f"  Capture {json_file.name}: {summary.get('total_non_vote_transactions', 0)} txns, "
                    f"{summary.get('total_fees_sol', 0):.6f} SOL fees, "
                    f"{summary.get('total_tips_sol', 0):.6f} SOL tips")
        except Exception as e:
            if verbose:
                log(f"  Error reading capture {json_file.name}: {e}")

    if rotations == 0:
        return None

    return {
        "rotations": rotations,
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
    lines.append(f"**Fees earned:** {leader_data['total_fees_sol']:.6f} SOL")
    lines.append(f"**Jito tip revenue:** {leader_data['total_tips_sol']:.6f} SOL")
    lines.append(f"**Total revenue:** {leader_data['total_revenue_sol']:.6f} SOL")
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
    lines.append(f"Scheduler datapoints: {bam_data['scheduler_events']}")
    lines.append(f"Leader slots: {bam_data['leader_slots']}")
    for key in BAM_METRIC_KEYS:
        val = bam_data['metrics'].get(key, 0)
        if val > 0:
            lines.append(f"  {key}: {val}")
    return "\n".join(lines)


def format_bam_embed(bam_data):
    """Format BAM data for Discord embed display."""
    lines = []
    lines.append(f"**Scheduler events:** {bam_data['scheduler_events']}")
    lines.append(f"**Leader slots:** {bam_data['leader_slots']}")
    lines.append("")
    lines.append("**Metrics:**")
    for key in BAM_METRIC_KEYS:
        val = bam_data['metrics'].get(key, 0)
        if val > 0:
            lines.append(f"• `{key}`: {val}")
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

    prompt = f"""You are analyzing error logs from a Solana testnet validator.
There were {error_count} genuine errors ({len(errors_text.splitlines()) if errors_text.strip() else 0} unique patterns) in the past hour.{tracked_section}{bam_prompt_section}

Provide a concise summary with 4-6 bullet points. Categorize each by severity:
- 🔴 CRITICAL: Service down, data corruption, crashes
- 🟠 HIGH: Connectivity issues, repeated failures affecting operation
- 🟡 MEDIUM: Transient errors, retryable failures
- 🟢 LOW: Minor warnings, cosmetic issues, expected low-severity errors

Start with an overall status line like:
"✅ Validator healthy — minor issues only" or "⚠️ Attention needed — connectivity problems detected" or "🚨 Critical issues detected"
{f"""
Here are the genuine error log lines:

{errors_text}""" if errors_text.strip() else ""}"""

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
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
            return data["content"][0]["text"]
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
        file_breakdown = "\n".join(f"• `{fname}`: {len(errs)} errors" for fname, errs in all_errors.items())
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
