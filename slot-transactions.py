#!/usr/bin/env python3
"""
Slot Transaction Extractor

Queries RPC getBlock for leader slots and extracts transaction signatures.
Replaces the old bundle-txn-signatures.py approach which relied on DEBUG logs
that don't exist in the BAM code path.

Usage:
  ./slot-transactions.py --slots 412321072 412321075
  ./slot-transactions.py --slots 412321072 412321075 --json
  ./slot-transactions.py --slots 412321072 412321075 --summary
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict

# Load RPC URL from validator config
VALIDATOR_CONFIG = os.path.expanduser("~/.config/validator/rpc.conf")
DEFAULT_EXPLORER_URL = "https://solscan.io/tx"


def load_rpc_url():
    """Load RPC URL from the shared validator config file."""
    config = {}
    try:
        with open(VALIDATOR_CONFIG) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        print(f"Error: Config not found: {VALIDATOR_CONFIG}", file=sys.stderr)
        sys.exit(1)

    rpc_url = config.get("MAINNET_RPC_URL")
    if not rpc_url:
        print(f"Error: MAINNET_RPC_URL not set in {VALIDATOR_CONFIG}", file=sys.stderr)
        sys.exit(1)
    return rpc_url


def load_validator_identity():
    """Load validator identity from config."""
    config = {}
    try:
        with open(VALIDATOR_CONFIG) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    return config.get("VALIDATOR_IDENTITY")


def rpc_request(rpc_url, method, params):
    """Make a JSON-RPC request."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode()

    req = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"RPC HTTP error: {e.code} {e.reason}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"RPC connection error: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"RPC error: {e}", file=sys.stderr)
        return None


def get_block(rpc_url, slot):
    """Fetch a confirmed block with full transaction details."""
    result = rpc_request(rpc_url, "getBlock", [
        slot,
        {
            "encoding": "json",
            "transactionDetails": "full",
            "rewards": False,
            "maxSupportedTransactionVersion": 0,
        },
    ])

    if result is None:
        return None

    if "error" in result:
        err = result["error"]
        # Slot was skipped or not available
        if err.get("code") in [-32004, -32007, -32009, -32014]:
            return {"skipped": True, "slot": slot}
        print(f"  Slot {slot}: RPC error: {err.get('message', err)}", file=sys.stderr)
        return None

    return result.get("result")


def extract_slot_data(rpc_url, slot):
    """Extract transaction data from a single slot."""
    block = get_block(rpc_url, slot)

    if block is None:
        return {"slot": slot, "error": "rpc_error", "transactions": []}

    if block.get("skipped"):
        return {"slot": slot, "skipped": True, "transactions": []}

    transactions = []
    for tx_wrapper in block.get("transactions", []):
        tx = tx_wrapper.get("transaction", {})
        meta = tx_wrapper.get("meta", {})

        signatures = tx.get("signatures", [])
        if not signatures:
            continue

        sig = signatures[0]
        err = meta.get("err")
        fee = meta.get("fee", 0)

        # Check if this is a vote transaction
        account_keys = tx.get("message", {}).get("accountKeys", [])
        is_vote = "Vote111111111111111111111111111111111111111" in account_keys

        if is_vote:
            continue

        # Get compute units consumed
        compute_units = meta.get("computeUnitsConsumed", 0)

        transactions.append({
            "signature": sig,
            "success": err is None,
            "error": str(err) if err else None,
            "fee": fee,
            "compute_units": compute_units,
        })

    return {
        "slot": slot,
        "block_time": block.get("blockTime"),
        "parent_slot": block.get("parentSlot"),
        "transactions": transactions,
        "total_non_vote_txns": len(transactions),
    }


def format_sol(lamports):
    """Format lamports as SOL."""
    return f"{lamports / 1e9:.6f}"


def output_text(slots_data, explorer_url, validator_identity):
    """Output human-readable text format."""
    total_txns = 0
    total_success = 0
    total_failed = 0
    total_fees = 0
    total_compute = 0
    skipped_slots = 0
    error_slots = 0

    for sd in slots_data:
        if sd.get("skipped"):
            skipped_slots += 1
            continue
        if sd.get("error"):
            error_slots += 1
            continue
        for tx in sd["transactions"]:
            total_txns += 1
            if tx["success"]:
                total_success += 1
            else:
                total_failed += 1
            total_fees += tx["fee"]
            total_compute += tx["compute_units"]

    active_slots = len(slots_data) - skipped_slots - error_slots

    print(f"\n{'LEADER SLOT TRANSACTION REPORT':=^100}")
    if validator_identity:
        print(f"Validator: {validator_identity}")
    first_slot = slots_data[0]["slot"]
    last_slot = slots_data[-1]["slot"]
    print(f"Slots: {first_slot}–{last_slot} ({len(slots_data)} total, {active_slots} produced, {skipped_slots} skipped)")
    print(f"\nSummary:")
    print(f"  Total non-vote transactions: {total_txns:,}")
    print(f"  Successful: {total_success:,}")
    print(f"  Failed: {total_failed:,}")
    print(f"  Total fees: {format_sol(total_fees)} SOL ({total_fees:,} lamports)")
    print(f"  Total compute units: {total_compute:,}")
    if total_txns > 0:
        print(f"  Avg fee per txn: {format_sol(total_fees // total_txns)} SOL")
        print(f"  Avg compute per txn: {total_compute // total_txns:,} CU")

    # Per-slot breakdown
    print(f"\n{'-' * 100}")
    print(f"{'Slot':<14} | {'Status':<10} | {'Txns':>6} | {'Success':>7} | {'Failed':>6} | {'Fees (SOL)':>14} | {'Compute':>12}")
    print(f"{'-' * 100}")

    for sd in slots_data:
        slot = sd["slot"]
        if sd.get("skipped"):
            print(f"{slot:<14} | {'SKIPPED':<10} | {'-':>6} | {'-':>7} | {'-':>6} | {'-':>14} | {'-':>12}")
            continue
        if sd.get("error"):
            print(f"{slot:<14} | {'ERROR':<10} | {'-':>6} | {'-':>7} | {'-':>6} | {'-':>14} | {'-':>12}")
            continue

        txns = sd["transactions"]
        n_success = sum(1 for t in txns if t["success"])
        n_failed = sum(1 for t in txns if not t["success"])
        fees = sum(t["fee"] for t in txns)
        compute = sum(t["compute_units"] for t in txns)
        print(f"{slot:<14} | {'OK':<10} | {len(txns):>6} | {n_success:>7} | {n_failed:>6} | {format_sol(fees):>14} | {compute:>12,}")

    print(f"{'=' * 100}")

    # All signatures
    print(f"\n{'ALL TRANSACTION SIGNATURES':=^100}")
    if explorer_url:
        print(f"(With explorer links to {explorer_url})\n")
    else:
        print("(One per line)\n")

    all_sigs = []
    for sd in slots_data:
        if sd.get("skipped") or sd.get("error"):
            continue
        for tx in sd["transactions"]:
            all_sigs.append((tx["signature"], tx["success"], sd["slot"]))

    for sig, success, slot in all_sigs:
        status = "OK" if success else "FAIL"
        if explorer_url:
            print(f"[{status}] {explorer_url}/{sig}")
        else:
            print(f"[{status}] {sig}")

    print(f"\nTotal signatures: {len(all_sigs):,}")
    print(f"{'=' * 100}")


def output_json(slots_data, explorer_url, validator_identity):
    """Output JSON format."""
    total_txns = 0
    total_success = 0
    total_failed = 0
    total_fees = 0
    skipped_slots = 0

    for sd in slots_data:
        if sd.get("skipped"):
            skipped_slots += 1
            continue
        if sd.get("error"):
            continue
        for tx in sd["transactions"]:
            total_txns += 1
            total_fees += tx["fee"]
            if tx["success"]:
                total_success += 1
            else:
                total_failed += 1

    output = {
        "validator": validator_identity,
        "summary": {
            "first_slot": slots_data[0]["slot"],
            "last_slot": slots_data[-1]["slot"],
            "total_slots": len(slots_data),
            "skipped_slots": skipped_slots,
            "total_non_vote_transactions": total_txns,
            "successful": total_success,
            "failed": total_failed,
            "total_fees_lamports": total_fees,
            "total_fees_sol": total_fees / 1e9,
        },
        "explorer_url": explorer_url,
        "slots": slots_data,
    }
    print(json.dumps(output, indent=2))


def main():
    args = sys.argv[1:]

    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)

    # Parse arguments
    first_slot = None
    last_slot = None
    output_format = "text"
    summary_only = False
    explorer_url = DEFAULT_EXPLORER_URL
    rpc_url_override = None

    i = 0
    while i < len(args):
        if args[i] == "--slots" and i + 2 < len(args):
            first_slot = int(args[i + 1])
            last_slot = int(args[i + 2])
            i += 3
        elif args[i] == "--json":
            output_format = "json"
            i += 1
        elif args[i] == "--summary":
            summary_only = True
            i += 1
        elif args[i] == "--rpc":
            rpc_url_override = args[i + 1]
            i += 2
        elif args[i] == "--no-links":
            explorer_url = None
            i += 1
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    if first_slot is None or last_slot is None:
        print("Error: --slots FIRST LAST is required", file=sys.stderr)
        sys.exit(1)

    rpc_url = rpc_url_override or load_rpc_url()
    validator_identity = load_validator_identity()

    slots = list(range(first_slot, last_slot + 1))
    print(f"Querying {len(slots)} slots ({first_slot}–{last_slot})...", file=sys.stderr)

    slots_data = []
    for slot in slots:
        print(f"  Slot {slot}...", file=sys.stderr, end="", flush=True)
        sd = extract_slot_data(rpc_url, slot)
        slots_data.append(sd)
        if sd.get("skipped"):
            print(" skipped", file=sys.stderr)
        elif sd.get("error"):
            print(f" error: {sd['error']}", file=sys.stderr)
        else:
            print(f" {sd['total_non_vote_txns']} txns", file=sys.stderr)
        # Brief pause to avoid rate limiting
        if slot != slots[-1]:
            time.sleep(0.1)

    if output_format == "json":
        output_json(slots_data, explorer_url, validator_identity)
    else:
        output_text(slots_data, explorer_url, validator_identity)


if __name__ == "__main__":
    main()
