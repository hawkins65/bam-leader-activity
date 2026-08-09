#!/usr/bin/env python3
"""
BAM epoch summary.

leader-capture-monitor.sh writes one captures/bam_activity_*.json (BAM telemetry)
and one captures/slot_txns_*.json (revenue) per leader rotation. Individually
those answer "how was that rotation"; this rolls a whole epoch up so you can see
how BAM is performing over time, and correlate tips against bundles.

An epoch (~61 leader groups over ~48h for this validator) is the natural unit:
leader slots are assigned per epoch, so any shorter window samples a biased slice
of the schedule.

Fires on epoch rollover. Run it from cron as often as you like -- it is a no-op
until the epoch actually changes, and it records what it has already reported in
a state file so a missed run is caught up on the next one.

Usage:
  bam-epoch-summary.py [--dry-run] [--force] [--verbose]
    --dry-run   print the summary, post nothing, do not advance state
    --force     report even if the epoch has not rolled over yet
"""

import os
import sys
import json
import glob
import time
import subprocess
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CAPTURES_DIR = os.path.join(SCRIPT_DIR, "captures")
STATE_FILE = os.path.join(HOME, ".bam_epoch_summary.state")
DISCORD_EMBED = os.path.join(HOME, "999_discord_embed.sh")
WEBHOOK_FILE = os.path.join(HOME, ".config/discord/webhook")
RPC_URL = "http://127.0.0.1:8899"

DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv
VERBOSE = "--verbose" in sys.argv


def log(msg):
    if VERBOSE or DRY_RUN:
        print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] {msg}")


def rpc(method):
    """Single JSON-RPC call against the local validator."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method})
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "10", RPC_URL, "-X", "POST",
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=15,
        ).stdout
        return json.loads(out).get("result")
    except Exception as e:
        log(f"RPC {method} failed: {e}")
        return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)      # atomic: never leave a half-written state


def collect(pattern, prefix, since_ts, until_ts):
    """Load capture JSONs in [since_ts, until_ts], keyed by their rotation stamp.

    leader-capture-monitor.sh names both files for a rotation with the same
    YYYYmmdd_HHMMSS stamp, so keying on it lets the BAM telemetry and the
    revenue/slot data for a given rotation be paired back up.
    """
    out = {}
    for path in sorted(glob.glob(os.path.join(CAPTURES_DIR, pattern))):
        try:
            mtime = os.path.getmtime(path)
            if not (since_ts <= mtime <= until_ts):
                continue
            stamp = os.path.basename(path)[len(prefix):-len(".json")]
            with open(path) as f:
                out[stamp] = json.load(f)
        except Exception as e:
            log(f"skipping {path}: {e}")
    return out


def produced_skipped(txn):
    """Slots produced/skipped for a rotation, from slot-transactions.py.

    Deliberately NOT taken from the BAM JSON: bam-leader-activity.py derives
    leader slots from cost_tracker_stats/replay_stage lines, which frequently do
    not appear inside a ~90s rotation window (observed 2026-08-09: 0 slots there
    while slot-transactions.py saw 2,487 transactions). Using the BAM figure
    would report zero slots for an entire epoch and silence the
    produced-slots-but-no-bundles check entirely.
    """
    s = txn.get("summary", {}) if txn else {}
    total = s.get("total_slots", 0) or 0
    skipped = s.get("skipped_slots", 0) or 0
    return max(total - skipped, 0), skipped


def main():
    epoch_info = rpc("getEpochInfo")
    if not epoch_info:
        log("could not read epoch info (validator down?) - nothing to do")
        return 1
    current_epoch = epoch_info.get("epoch")

    state = load_state()
    last_epoch = state.get("last_epoch")
    # First ever run: record where we are and report nothing, rather than
    # emitting a bogus summary over whatever captures happen to be on disk.
    if last_epoch is None and not FORCE:
        save_state({"last_epoch": current_epoch, "last_report_ts": time.time()})
        log(f"initialised at epoch {current_epoch}; first summary at the next rollover")
        return 0

    if not FORCE and current_epoch == last_epoch:
        log(f"still in epoch {current_epoch} - nothing to do")
        return 0

    since_ts = state.get("last_report_ts", time.time() - 48 * 3600)
    until_ts = time.time()
    reported_epoch = last_epoch if last_epoch is not None else current_epoch

    bam = collect("bam_activity_*.json", "bam_activity_", since_ts, until_ts)
    txns = collect("slot_txns_*.json", "slot_txns_", since_ts, until_ts)
    if not bam:
        log("no BAM captures in window - nothing to report")
        if not DRY_RUN:
            save_state({"last_epoch": current_epoch, "last_report_ts": until_ts})
        return 0

    rotations = len(bam)
    vals = bam.values()
    bundles = sum(d.get("bam", {}).get("bundles_received", 0) for d in vals)
    sent = sum(d.get("bam", {}).get("results_sent", 0) for d in vals)
    sched_fail = sum(d.get("bam", {}).get("scheduler_fail", 0) for d in vals)
    out_fail = sum(d.get("bam", {}).get("outbound_fail", 0) for d in vals)
    unhealthy = sum(d.get("bam", {}).get("unhealthy_connection_events", 0) for d in vals)
    heartbeats = sum(d.get("bam", {}).get("heartbeats_total", 0) for d in vals)

    produced = skipped = 0
    dry_rotations = 0
    for stamp, bd in bam.items():
        p, s = produced_skipped(txns.get(stamp))
        produced += p
        skipped += s
        # The failure that is invisible in revenue numbers alone: we led, and BAM
        # delivered nothing. Only counted where BAM is actually in use on this
        # host (heartbeats present), same reasoning as the per-rotation alert.
        got_bundles = bd.get("bam", {}).get("bundles_received", 0)
        has_bam = bd.get("bam", {}).get("heartbeats_total", 0) > 0 or got_bundles > 0
        if has_bam and p > 0 and got_bundles == 0:
            dry_rotations += 1

    send_rate = (sent / bundles * 100) if bundles else None

    tips = sum(t.get("summary", {}).get("total_tips_sol", 0) for t in txns.values())
    fees = sum(t.get("summary", {}).get("total_fees_sol", 0) for t in txns.values())
    tips_per_bundle = (tips / bundles) if bundles else None

    lines = [
        f"**Epoch {reported_epoch}** — {rotations} rotations, {produced} slots produced"
        + (f", {skipped} skipped" if skipped else ""),
        f"**Bundles:** {bundles:,} received → {sent:,} sent"
        + (f" ({send_rate:.2f}%)" if send_rate is not None else ""),
        f"**Failures:** {sched_fail:,} scheduler, {out_fail:,} outbound",
        f"**Connection:** {heartbeats:,} heartbeats, {unhealthy:,} unhealthy events",
        f"**Revenue:** {tips:.6f} SOL tips, {fees:.6f} SOL fees"
        + (f" — {tips_per_bundle:.9f} SOL/bundle" if tips_per_bundle is not None else ""),
    ]
    if dry_rotations:
        lines.append(f"🚨 **{dry_rotations} rotation(s) produced slots but received 0 bundles**")

    desc = "\n".join(lines)
    severity = "warning" if (dry_rotations or unhealthy or
                             (send_rate is not None and send_rate < 95)) else "info"

    if DRY_RUN:
        print(f"\n[dry-run] severity={severity}\n{desc}\n")
        return 0

    webhook = ""
    try:
        with open(WEBHOOK_FILE) as f:
            webhook = f.read().strip()
    except Exception:
        pass

    if webhook and os.path.exists(DISCORD_EMBED):
        subprocess.run(
            ["bash", "-c",
             f'source "{DISCORD_EMBED}" && send_discord_embed "{webhook}" "{severity}" '
             f'"BAM Epoch Summary" "{desc}" username="BAM Epoch" pagerduty=false'],
            check=False, capture_output=True,
        )
        log("posted to Discord")
    else:
        print(desc)

    save_state({"last_epoch": current_epoch, "last_report_ts": until_ts})
    return 0


if __name__ == "__main__":
    sys.exit(main())
