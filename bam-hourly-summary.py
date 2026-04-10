#!/usr/bin/env python3
"""
bam-hourly-summary.py — Hourly AI-powered BAM health summary with Discord alerting.

Collects BAM connection metrics and errors from the validator log over the past hour,
sends them to Claude for analysis, and posts a categorized health summary to Discord.

Usage:
    ./bam-hourly-summary.py                  # Default: last 1 hour
    ./bam-hourly-summary.py --hours 2        # Last 2 hours
    ./bam-hourly-summary.py --dry-run        # Print summary, don't post to Discord
    ./bam-hourly-summary.py --verbose        # Extra debug output
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

VALIDATOR_LOG = Path.home() / "logs" / "validator.log"
DISCORD_WEBHOOK_FILE = Path.home() / ".config" / "discord" / "webhook"
DISCORD_EMBED_SCRIPT = Path.home() / "999_discord_embed.sh"
BOT_USERNAME = "BAM Hourly Summary"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
HOSTNAME = os.uname().nodename
SCRIPT_PATH = f"{HOSTNAME}:{os.path.abspath(__file__)}"

# ── BAM-specific patterns ─────────────────────────────────────────────────────

# Connection state events
BAM_EVENT_PATTERNS = [
    r"BAM connection established",
    r"BAM connection lost",
    r"BAM connection not healthy",
    r"Failed to connect to BAM",
    r"Failed to start scheduler stream",
    r"Failed to (?:prepare auth response|send initial auth proof|get auth challenge)",
    r"Inbound stream closed",
    r"Failed to receive message from inbound stream",
    r"Failed to get config",
    r"Received unsupported versioned message",
    r"BAM Manager: timed out waiting for new identity",
    r"BAM URL changed",
    r"BAM Manager: detected new identity",
]

BAM_EVENT_REGEX = re.compile("|".join(BAM_EVENT_PATTERNS))

# Metric line pattern
BAM_METRIC_REGEX = re.compile(r"bam_connection-metrics")

# Individual metric field extractors
METRIC_FIELDS = {
    "bundle_received": re.compile(r"bundle_received=(\d+)i"),
    "bundleresult_sent": re.compile(r"bundleresult_sent=(\d+)i"),
    "bundle_forward_to_scheduler_fail": re.compile(r"bundle_forward_to_scheduler_fail=(\d+)i"),
    "outbound_fail": re.compile(r"outbound_fail=(\d+)i"),
    "unhealthy_connection_count": re.compile(r"unhealthy_connection_count=(\d+)i"),
    "heartbeat_received": re.compile(r"heartbeat_received=(\d+)i"),
    "heartbeat_sent": re.compile(r"heartbeat_sent=(\d+)i"),
    "leaderstate_sent": re.compile(r"leaderstate_sent=(\d+)i"),
}

# Leader slot pattern (same as bam-leader-activity.py)
LEADER_SLOT_REGEX = re.compile(r"replay_stage-my_leader_slot")

# Timestamp extraction
TS_PATTERN = re.compile(r"\[?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def vlog(msg: str, verbose: bool) -> None:
    if verbose:
        log(f"[DEBUG] {msg}")


def get_discord_webhook() -> str:
    url = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if url:
        return url
    if DISCORD_WEBHOOK_FILE.exists():
        url = DISCORD_WEBHOOK_FILE.read_text().strip()
        if url:
            return url
    print("ERROR: No Discord webhook found.", file=sys.stderr)
    sys.exit(1)


def get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    config_path = Path.home() / ".config" / "anthropic" / "api_key"
    if config_path.exists():
        key = config_path.read_text().strip()
        if key:
            return key
    print("ERROR: No Anthropic API key found.", file=sys.stderr)
    sys.exit(1)


def parse_timestamp(line: str) -> datetime | None:
    match = TS_PATTERN.search(line)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return None


def collect_bam_data(hours: int, verbose: bool) -> dict:
    """Collect BAM events and metrics from the validator log for the past N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = {
        "events": [],           # Connection state change events
        "metric_totals": defaultdict(int),  # Aggregated metric totals
        "metric_minutes": 0,    # Number of metric data points
        "anomaly_lines": [],    # Lines with non-zero failure/unhealthy metrics
        "errors": [],           # Error-level BAM log lines
        "leader_slots": 0,      # Count of leader slots in window
    }

    if not VALIDATOR_LOG.exists():
        log(f"ERROR: {VALIDATOR_LOG} not found")
        return result

    file_size = VALIDATOR_LOG.stat().st_size
    vlog(f"Validator log size: {file_size // 1_000_000}MB", verbose)

    # Read from end for large files
    try:
        proc = subprocess.run(
            ["tac", str(VALIDATOR_LOG)],
            capture_output=True,
            text=True,
            timeout=45,
        )
        lines = proc.stdout.splitlines()
    except Exception as e:
        log(f"ERROR reading log: {e}")
        return result

    vlog(f"Processing {len(lines)} lines (newest first)", verbose)

    for line in lines:
        ts = parse_timestamp(line)
        if ts and ts < cutoff:
            break  # Past our window (reading newest-first)

        # BAM connection events
        if BAM_EVENT_REGEX.search(line):
            result["events"].append(line.strip())

        # BAM metric lines
        if BAM_METRIC_REGEX.search(line):
            result["metric_minutes"] += 1
            for field, regex in METRIC_FIELDS.items():
                m = regex.search(line)
                if m:
                    val = int(m.group(1))
                    result["metric_totals"][field] += val

            # Check for anomalies
            for anomaly_field in ["unhealthy_connection_count", "outbound_fail", "bundle_forward_to_scheduler_fail"]:
                m = METRIC_FIELDS[anomaly_field].search(line)
                if m and int(m.group(1)) > 0:
                    result["anomaly_lines"].append(line.strip())
                    break

        # Leader slot lines
        if LEADER_SLOT_REGEX.search(line):
            result["leader_slots"] += 1

        # General ERROR lines mentioning BAM
        if " ERROR " in line and ("bam" in line.lower() or "BAM" in line):
            result["errors"].append(line.strip())

    # Reverse to chronological order
    result["events"].reverse()
    result["anomaly_lines"].reverse()
    result["errors"].reverse()

    return result


def build_summary_text(data: dict, hours: int) -> str:
    """Build a text summary of BAM data for the AI prompt."""
    parts = []
    totals = data["metric_totals"]
    minutes = data["metric_minutes"]

    parts.append(f"=== BAM Metrics Summary (past {hours}h, {minutes} data points) ===")
    parts.append(f"  Bundles received:        {totals.get('bundle_received', 0)}")
    parts.append(f"  Bundle results sent:     {totals.get('bundleresult_sent', 0)}")
    parts.append(f"  Scheduler failures:      {totals.get('bundle_forward_to_scheduler_fail', 0)}")
    parts.append(f"  Outbound failures:       {totals.get('outbound_fail', 0)}")
    parts.append(f"  Unhealthy conn count:    {totals.get('unhealthy_connection_count', 0)}")
    parts.append(f"  Heartbeats received:     {totals.get('heartbeat_received', 0)}")
    parts.append(f"  Heartbeats sent:         {totals.get('heartbeat_sent', 0)}")
    parts.append(f"  Leader states sent:      {totals.get('leaderstate_sent', 0)}")

    if data["events"]:
        parts.append(f"\n=== Connection Events ({len(data['events'])}) ===")
        for ev in data["events"][:30]:
            parts.append(f"  {ev[:300]}")
        if len(data["events"]) > 30:
            parts.append(f"  ... and {len(data['events']) - 30} more events")

    if data["anomaly_lines"]:
        parts.append(f"\n=== Metric Anomalies ({len(data['anomaly_lines'])} lines with failures/unhealthy) ===")
        for al in data["anomaly_lines"][:20]:
            parts.append(f"  {al[:300]}")
        if len(data["anomaly_lines"]) > 20:
            parts.append(f"  ... and {len(data['anomaly_lines']) - 20} more")

    if data["errors"]:
        parts.append(f"\n=== BAM Error Log Lines ({len(data['errors'])}) ===")
        for err in data["errors"][:20]:
            parts.append(f"  {err[:300]}")
        if len(data["errors"]) > 20:
            parts.append(f"  ... and {len(data['errors']) - 20} more")

    return "\n".join(parts)


def call_claude_api(api_key: str, summary_text: str, hours: int) -> str:
    """Send BAM data to Claude for analysis."""
    prompt = f"""You are analyzing BAM (Block Assembly Marketplace) connection and bundle metrics from a Solana mainnet validator (hostname: {HOSTNAME}).

BAM is the marketplace where MEV searchers submit bundles to validators. Key things to monitor:
- Connection health: heartbeats flowing, no unhealthy periods
- Bundle flow: bundles being received and results sent back
- Failures: scheduler failures mean bundles can't be processed; outbound failures mean results can't be sent
- Zero bundles during non-leader slots is NORMAL — bundles only arrive near leader slots

Here is the BAM data from the past {hours} hour(s):

{summary_text}

Provide a concise BAM health summary with 3-5 bullet points, categorized by severity:
- 🔴 CRITICAL: BAM connection down, persistent failures, no heartbeats
- 🟠 HIGH: Elevated failure rates, intermittent disconnections, bundle delivery issues
- 🟡 MEDIUM: Occasional anomalies, minor metric deviations
- 🟢 LOW: Informational, normal behavior

For each bullet:
- Describe the operational impact on bundle revenue / MEV extraction
- Note specific counts/rates where relevant
- Suggest action if needed

End with: "BAM Status: [EMOJI] [One-sentence overall assessment]"

Be concise and validator-operator focused."""

    request_body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API error {e.code}: {body}") from e


def _call_discord_embed(severity: str, title: str, description: str,
                        footer_extra: str = "", pagerduty: bool = False) -> bool:
    """Send a Discord embed via the standard 999_discord_embed.sh script."""
    if not DISCORD_EMBED_SCRIPT.exists():
        log(f"ERROR: Discord embed script not found: {DISCORD_EMBED_SCRIPT}")
        return False

    # Use \n literals — the bash script converts them to real newlines
    description = description.replace('\n', '\\n')

    webhook = get_discord_webhook()
    cmd = (
        f'source "{DISCORD_EMBED_SCRIPT}" && '
        f'send_discord_embed "{webhook}" "{severity}" '
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


def send_discord_summary(summary: str, data: dict, hours: int, dry_run: bool) -> None:
    """Post BAM summary to Discord via standard embed helper."""
    totals = data["metric_totals"]
    has_failures = (
        totals.get("bundle_forward_to_scheduler_fail", 0) > 0
        or totals.get("outbound_fail", 0) > 0
    )
    has_events = len(data["events"]) > 0

    if has_failures or has_events:
        severity = "error" if len(data["events"]) >= 5 else "warning"
    else:
        severity = "ok"

    time_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    title = f"BAM Hourly Summary — {time_str}"

    bundles = totals.get("bundle_received", 0)
    failures = (
        totals.get("bundle_forward_to_scheduler_fail", 0)
        + totals.get("outbound_fail", 0)
    )

    # Append key metrics to description
    description = summary
    description += f"\n\n**Bundles:** {bundles} | **Heartbeats:** {totals.get('heartbeat_received', 0)}"
    description += f" | **Failures:** {failures} | **Events:** {len(data['events'])}"

    if dry_run:
        log("DRY RUN — would send to Discord:")
        print(description)
        return

    _call_discord_embed(severity, title, description)


def send_healthy_embed(data: dict, hours: int, dry_run: bool) -> None:
    """Post structured all-clear BAM status."""
    totals = data["metric_totals"]
    time_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    title = f"BAM Hourly Summary — {time_str}"

    lines = [
        "\u2705 BAM connection healthy — no issues detected",
        "",
        f"Scheduler events: {data['metric_minutes']}",
        f"Leader slots: {data['leader_slots']}",
        "",
        "Metrics:",
    ]

    for field in [
        "bundle_received", "bundleresult_sent",
        "heartbeat_received", "heartbeat_sent", "leaderstate_sent",
    ]:
        val = totals.get(field, 0)
        if val > 0:
            lines.append(f"\u2022 {field}: {val}")

    description = "\n".join(lines)

    if dry_run:
        log("DRY RUN — would send to Discord:")
        print(f"\n{title}\n{description}\n")
        return

    _call_discord_embed("ok", title, description)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hourly BAM health summary")
    parser.add_argument("--hours", type=int, default=1, help="Hours to look back (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without posting to Discord")
    parser.add_argument("--verbose", action="store_true", help="Enable debug output")
    args = parser.parse_args()

    log(f"Starting BAM hourly summary (past {args.hours}h)")

    data = collect_bam_data(args.hours, args.verbose)

    totals = data["metric_totals"]
    event_count = len(data["events"])
    failure_count = (
        totals.get("bundle_forward_to_scheduler_fail", 0)
        + totals.get("outbound_fail", 0)
    )
    anomaly_count = len(data["anomaly_lines"])

    log(f"Metrics: {data['metric_minutes']} data points, {totals.get('bundle_received', 0)} bundles, "
        f"{totals.get('heartbeat_received', 0)} heartbeats")
    log(f"Issues: {event_count} events, {failure_count} failures, {anomaly_count} anomaly lines")

    needs_analysis = event_count > 0 or failure_count > 0 or anomaly_count > 0

    if not needs_analysis:
        send_healthy_embed(data, args.hours, args.dry_run)
        log("Done — BAM healthy, no issues.")
        return

    # Build summary and send to Claude
    summary_text = build_summary_text(data, args.hours)
    api_key = get_api_key()
    log("Sending BAM data to Claude for analysis...")

    try:
        ai_summary = call_claude_api(api_key, summary_text, args.hours)
    except Exception as e:
        log(f"ERROR: AI analysis failed: {e}")
        ai_summary = f"**AI analysis unavailable.**\n"
        ai_summary += f"Bundles: {totals.get('bundle_received', 0)}, "
        ai_summary += f"Failures: {failure_count}, Events: {event_count}"

    send_discord_summary(ai_summary, data, args.hours, args.dry_run)
    log("Done.")


if __name__ == "__main__":
    main()
