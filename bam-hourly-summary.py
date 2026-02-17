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
BOT_USERNAME = "Validator Log Summary"
BOT_AVATAR = "https://trillium.so/images/trillium-default.png"
CLAUDE_MODEL = "claude-sonnet-4-20250514"
HOSTNAME = os.uname().nodename

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


def send_discord_embed(summary: str, data: dict, hours: int, dry_run: bool) -> None:
    """Post BAM summary to Discord."""
    totals = data["metric_totals"]
    has_failures = (
        totals.get("bundle_forward_to_scheduler_fail", 0) > 0
        or totals.get("outbound_fail", 0) > 0
    )
    has_events = len(data["events"]) > 0

    if has_failures or has_events:
        color = 0xFF0000 if len(data["events"]) >= 5 else 0xFFAA00
    else:
        color = 0x00FF00

    time_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    title = f"BAM Hourly Summary — {time_str}"

    bundles = totals.get("bundle_received", 0)
    failures = (
        totals.get("bundle_forward_to_scheduler_fail", 0)
        + totals.get("outbound_fail", 0)
    )

    if len(summary) > 4000:
        summary = summary[:3997] + "..."
    escaped_summary = json.dumps(summary)[1:-1]

    payload = json.dumps({
        "username": BOT_USERNAME,
        "avatar_url": BOT_AVATAR,
        "embeds": [{
            "title": title,
            "description": escaped_summary,
            "color": color,
            "fields": [
                {"name": "Host", "value": HOSTNAME, "inline": True},
                {"name": "Window", "value": f"Past {hours}h", "inline": True},
                {"name": "Bundles", "value": str(bundles), "inline": True},
                {"name": "Heartbeats", "value": str(totals.get("heartbeat_received", 0)), "inline": True},
                {"name": "Failures", "value": str(failures), "inline": True},
                {"name": "Events", "value": str(len(data["events"])), "inline": True},
            ],
            "footer": {"text": "BAM Hourly Summary — AI Analysis"},
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    }).encode("utf-8")

    if dry_run:
        log("DRY RUN — would send to Discord:")
        print(summary)
        return

    req = urllib.request.Request(
        get_discord_webhook(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"Discord notification sent (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        log(f"WARNING: Discord webhook failed with HTTP {e.code}")


def send_healthy_embed(data: dict, hours: int, dry_run: bool) -> None:
    """Post structured all-clear BAM status."""
    totals = data["metric_totals"]
    now_utc = datetime.now(timezone.utc)
    time_str = now_utc.strftime("%H:%M UTC")
    title = f"BAM Hourly Summary — {time_str}"

    # Build structured description matching testnet format
    lines = [
        f"\u2705 BAM connection healthy — no issues detected",
        "",
        f"Scheduler events: {data['metric_minutes']}",
        f"Leader slots: {data['leader_slots']}",
        "",
        "Metrics:",
    ]

    # Show all non-zero metric totals as bullet points
    for field in [
        "bundle_received", "bundleresult_sent",
        "heartbeat_received", "heartbeat_sent", "leaderstate_sent",
    ]:
        val = totals.get(field, 0)
        if val > 0:
            lines.append(f"\u2022 {field}: {val}")

    description = "\n".join(lines)

    payload = json.dumps({
        "username": BOT_USERNAME,
        "avatar_url": BOT_AVATAR,
        "embeds": [{
            "title": title,
            "description": description,
            "color": 0x00FF00,
            "fields": [
                {"name": "Host", "value": HOSTNAME, "inline": True},
            ],
            "footer": {"text": "BAM Hourly Summary"},
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    }).encode("utf-8")

    if dry_run:
        log("DRY RUN — would send to Discord:")
        print(f"\n{title}\n{description}\n")
        return

    req = urllib.request.Request(
        get_discord_webhook(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"Discord notification sent (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        log(f"WARNING: Discord webhook failed with HTTP {e.code}")


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

    send_discord_embed(ai_summary, data, args.hours, args.dry_run)
    log("Done.")


if __name__ == "__main__":
    main()
