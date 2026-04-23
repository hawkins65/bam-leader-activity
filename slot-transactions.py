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
  ./slot-transactions.py --leader-slots 412321072,412321073,412321074,412321075
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict

# Load RPC URL from validator config
VALIDATOR_CONFIG = os.path.expanduser("~/.config/validator/rpc.conf")
DEFAULT_EXPLORER_URL = "https://solscan.io/tx"
VALIDATOR_SH = os.environ.get("VALIDATOR_SH", os.path.expanduser("~/validator.sh"))

# Jito on-chain addresses per network. Source:
# https://jito-foundation.gitbook.io/mev/mev-payment-and-distribution/on-chain-addresses
# Testnet and mainnet use entirely different keys — never mix them.
JITO_ADDRESSES = {
    "mainnet": {
        "tip_payment_program": "T1pyyaTNZsKv2WcRAB8oVnk93mLJw2XzjtVYqCsaHqt",
        "tip_distribution_program": "4R3gSG8BpU4t19KYj8CfnbtRpnT8gtk4dvTHxVRwc2r7",
        "merkle_upload_authority": "8F4jGUmxF36vQ6yabnsxX6AQVXdKBhs8kGSUuRKSg8Xt",
        "tip_accounts": frozenset({
            "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
            "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
            "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
            "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
            "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
            "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
            "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
            "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
        }),
    },
    "testnet": {
        "tip_payment_program": "GJHtFqM9agxPmkeKjHny6qiRKrXZALvvFGiKf11QE7hy",
        "tip_distribution_program": "DzvGET57TAgEDxvm3ERUM4GNcsAJdqjDLCne9sdfY4wf",
        "merkle_upload_authority": "7T4inmPmtNBX3MhLwJ9hFsSMnGJYYkKioVABSNTWVRuS",
        "tip_accounts": frozenset({
            "BkMx5bRzQeP6tUZgzEs3xeDWJfQiLYvNDqSgmGZKYJDq",
            "CwWZzvRgmxj9WLLhdoWUVrHZ1J8db3w2iptKuAitHqoC",
            "4uRnem4BfVpZBv7kShVxUYtcipscgZMSHi3B9CSL6gAA",
            "AzfhMPcx3qjbvCK3UUy868qmc5L451W341cpFqdL3EBe",
            "84DrGKhycCUGfLzw8hXsUYX9SnWdh2wW3ozsTPrC5xyg",
            "7aewvu8fMf1DK4fKoMXKfs3h3wpAQ7r7D8T1C71LmMF",
            "G2d63CEgKBdgtpYT2BuheYQ9HFuFCenuHLNyKVpqAuSD",
            "F7ThiQUBYiEcyaxpmMuUeACdoiSLKg4SZZ8JSfpFNwAf",
        }),
    },
}


def detect_network():
    """Pick mainnet/testnet via NETWORK env var, else parse VALIDATOR_SH.

    Mirrors detect-network.sh. Raises RuntimeError if the answer is
    ambiguous rather than silently defaulting.
    """
    env = os.environ.get("NETWORK")
    if env:
        if env in ("mainnet", "testnet"):
            return env
        raise RuntimeError(
            f"NETWORK env var is set to {env!r}; must be 'mainnet' or 'testnet'"
        )

    if not os.path.isfile(VALIDATOR_SH):
        raise RuntimeError(
            f"{VALIDATOR_SH} not found; set NETWORK=mainnet|testnet in the "
            "environment to bypass detection"
        )

    try:
        with open(VALIDATOR_SH) as f:
            contents = f.read()
    except OSError as e:
        raise RuntimeError(f"Could not read {VALIDATOR_SH}: {e}") from e

    has_mainnet = bool(re.search(r"entrypoint\d*\.mainnet-beta\.solana\.com", contents))
    has_testnet = bool(re.search(r"entrypoint\d*\.testnet\.solana\.com", contents))

    if has_mainnet and not has_testnet:
        return "mainnet"
    if has_testnet and not has_mainnet:
        return "testnet"
    if has_mainnet and has_testnet:
        raise RuntimeError(
            f"{VALIDATOR_SH} contains BOTH mainnet and testnet entrypoints; "
            "set NETWORK=mainnet|testnet to disambiguate"
        )
    raise RuntimeError(
        f"{VALIDATOR_SH} contains no recognizable --entrypoint flag; "
        "set NETWORK=mainnet|testnet to bypass detection"
    )


def _load_validator_config():
    """Parse ~/.config/validator/rpc.conf into a dict. Internal helper."""
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
    return config


def load_rpc_url(network):
    """Load the RPC URL for the given network from validator config."""
    config = _load_validator_config()
    if config is None:
        print(f"Error: Config not found: {VALIDATOR_CONFIG}", file=sys.stderr)
        sys.exit(1)

    key = "MAINNET_RPC_URL" if network == "mainnet" else "TESTNET_RPC_URL"
    rpc_url = config.get(key)
    if not rpc_url:
        print(f"Error: {key} not set in {VALIDATOR_CONFIG}", file=sys.stderr)
        sys.exit(1)
    return rpc_url


def load_validator_identity():
    """Load validator identity from config."""
    config = _load_validator_config()
    if config is None:
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
            "rewards": True,
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


def extract_slot_data(rpc_url, slot, jito_cfg, validator_identity):
    """Extract transaction data from a single slot.

    Tip accounting:
      - tips_in_lamports: sum of positive deltas into tip PDAs during our
        leader slots. This IS our tip revenue — tips deposited while we're
        leader belong to us (they'll be swept into our TipDistributionAccount
        by change_tip_receiver, typically at the next leader rotation).
      - self_drain_lamports: sum of outflows FROM tip PDAs in txns signed by
        OUR validator_identity (actual sweeps executed during this window —
        these come from prior leader slots, not necessarily these ones, so
        this is informational only, not our revenue for this rotation).
      - tip_anomalies: withdrawals from tip PDAs signed by something that is
        NOT a validator identity (neither ours nor any other). Other
        validators' own sweeps landing in our block are silently ignored.
        We can't distinguish "other validator identity" from "unknown" from
        a single block alone, so we use a conservative heuristic: a txn is
        treated as a legitimate sweep if it invokes the tip_payment_program
        at all. Anything else touching a tip PDA with a negative delta is
        anomalous.
    """
    block = get_block(rpc_url, slot)

    if block is None:
        return {"slot": slot, "error": "rpc_error", "transactions": []}

    if block.get("skipped"):
        return {"slot": slot, "skipped": True, "transactions": []}

    tip_accounts = jito_cfg["tip_accounts"]
    tip_payment_program = jito_cfg["tip_payment_program"]

    # Authoritative leader fee credit for this block from block.rewards[]
    # where rewardType == "Fee" AND pubkey matches our validator identity.
    # The pubkey filter is critical: when called on a merged-window slot
    # range (see leader-capture-monitor.sh), some blocks are produced by
    # OTHER validators and their Fee rewards would otherwise be counted
    # as ours. Per SIMD-0096, priority fees are no longer burned (100% to
    # leader); base fees still have the 50% burn — the reward entry
    # already reflects the final post-burn leader credit.
    leader_fee_lamports = 0
    for r in block.get("rewards") or []:
        if r.get("rewardType") != "Fee":
            continue
        if validator_identity and r.get("pubkey") != validator_identity:
            continue
        leader_fee_lamports += r.get("lamports", 0)

    tips_in_lamports = 0
    self_drain_lamports = 0
    tip_anomalies = []

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

        # Build full account key list (static + loaded from ALT) so indices
        # align with pre/postBalances. Order: static, loaded writable, loaded readonly.
        static_keys = tx.get("message", {}).get("accountKeys", []) or []
        loaded = meta.get("loadedAddresses") or {}
        all_keys = list(static_keys) \
            + list(loaded.get("writable") or []) \
            + list(loaded.get("readonly") or [])

        # Fee payer / first signer — for change_tip_receiver txns this is the
        # leader validator whose tips are being swept.
        fee_payer = static_keys[0] if static_keys else None
        invokes_tip_program = tip_payment_program in all_keys

        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        for idx, key in enumerate(all_keys):
            if key not in tip_accounts:
                continue
            if idx >= len(pre) or idx >= len(post):
                continue
            delta = post[idx] - pre[idx]
            if delta > 0:
                tips_in_lamports += delta
            elif delta < 0:
                if fee_payer == validator_identity:
                    # Our own sweep — this is our tip revenue.
                    self_drain_lamports += -delta
                elif invokes_tip_program:
                    # Another validator's sweep. Normal noise, ignore silently.
                    pass
                else:
                    # Something touched a tip PDA and drained it WITHOUT
                    # going through the tip payment program. Flag.
                    tip_anomalies.append({
                        "signature": sig,
                        "fee_payer": fee_payer,
                        "account": key,
                        "lamports": delta,
                    })

        is_vote = "Vote111111111111111111111111111111111111111" in static_keys
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
        "leader_fee_lamports": leader_fee_lamports,
        # Gross positive inflow into tip PDAs in this slot (informational).
        "tips_in_lamports": tips_in_lamports,
        # Outflow in txns signed by our identity = our tip revenue.
        "self_drain_lamports": self_drain_lamports,
        # Suspicious withdrawals we couldn't explain.
        "tip_anomalies": tip_anomalies,
    }


def format_sol(lamports):
    """Format lamports as SOL."""
    return f"{lamports / 1e9:.6f}"


def output_text(slots_data, explorer_url, validator_identity, network):
    """Output human-readable text format."""
    total_txns = 0
    total_success = 0
    total_failed = 0
    gross_fees_paid = 0
    leader_fees_earned = 0
    total_tips_in = 0
    total_self_drain = 0
    total_compute = 0
    skipped_slots = 0
    error_slots = 0
    anomaly_events = []  # [(slot, anomaly), ...]

    for sd in slots_data:
        if sd.get("skipped"):
            skipped_slots += 1
            continue
        if sd.get("error"):
            error_slots += 1
            continue
        leader_fees_earned += sd.get("leader_fee_lamports", 0)
        total_tips_in += sd.get("tips_in_lamports", 0)
        total_self_drain += sd.get("self_drain_lamports", 0)
        for a in sd.get("tip_anomalies") or []:
            anomaly_events.append((sd["slot"], a))
        for tx in sd["transactions"]:
            total_txns += 1
            if tx["success"]:
                total_success += 1
            else:
                total_failed += 1
            gross_fees_paid += tx["fee"]
            total_compute += tx["compute_units"]

    active_slots = len(slots_data) - skipped_slots - error_slots
    # Authoritative tip revenue: gross inflow into tip PDAs during our leader
    # slots. These tips belong to us — change_tip_receiver will sweep them
    # into our TipDistributionAccount at the next sweep point. The self-drain
    # measured in THIS window reflects sweeps of PRIOR rotations' tips, so
    # it's informational, not the revenue earned from the current slots.
    tip_revenue = total_tips_in
    total_revenue = leader_fees_earned + tip_revenue

    print(f"\n{'LEADER SLOT TRANSACTION REPORT':=^100}")
    if validator_identity:
        print(f"Validator: {validator_identity}")
    print(f"Network:   {network}")
    first_slot = slots_data[0]["slot"]
    last_slot = slots_data[-1]["slot"]
    print(f"Slots: {first_slot}–{last_slot} ({len(slots_data)} total, {active_slots} produced, {skipped_slots} skipped)")
    print(f"\nSummary:")
    print(f"  Total non-vote transactions: {total_txns:,}")
    print(f"  Successful: {total_success:,}")
    print(f"  Failed: {total_failed:,}")
    print(f"  Fees earned (leader credit): {format_sol(leader_fees_earned)} SOL ({leader_fees_earned:,} lamports)")
    print(f"  Jito tips earned (tip-PDA inflow): {format_sol(tip_revenue)} SOL ({tip_revenue:,} lamports)")
    print(f"  Total leader revenue:        {format_sol(total_revenue)} SOL")
    print(f"  Sweeps executed in window (informational): {format_sol(total_self_drain)} SOL "
          "(outflows from tip PDAs in txns we signed; reflects prior rotations' tips, not this one)")
    print(f"  Gross fees paid (non-vote):  {format_sol(gross_fees_paid)} SOL ({gross_fees_paid:,} lamports)")
    if anomaly_events:
        total_anomaly = sum(-a["lamports"] for _, a in anomaly_events)
        print(f"  ⚠️  Tip anomalies: {len(anomaly_events)} event(s), "
              f"{format_sol(total_anomaly)} SOL drained outside tip_payment_program")
        for slot, a in anomaly_events:
            print(f"      slot {slot}: {format_sol(-a['lamports'])} SOL from {a['account']} "
                  f"(payer {a['fee_payer']}, sig {a['signature']})")
    print(f"  Total compute units: {total_compute:,}")
    if total_txns > 0:
        print(f"  Avg fee per txn: {format_sol(gross_fees_paid // total_txns)} SOL")
        print(f"  Avg compute per txn: {total_compute // total_txns:,} CU")

    # Per-slot breakdown
    print(f"\n{'-' * 120}")
    print(f"{'Slot':<14} | {'Status':<7} | {'Txns':>6} | {'OK':>6} | {'Fail':>5} | {'Fees (SOL)':>12} | {'Tip Rev (SOL)':>14} | {'Compute':>12}")
    print(f"{'-' * 120}")

    for sd in slots_data:
        slot = sd["slot"]
        if sd.get("skipped"):
            print(f"{slot:<14} | {'SKIPPED':<7} | {'-':>6} | {'-':>6} | {'-':>5} | {'-':>12} | {'-':>14} | {'-':>12}")
            continue
        if sd.get("error"):
            print(f"{slot:<14} | {'ERROR':<7} | {'-':>6} | {'-':>6} | {'-':>5} | {'-':>12} | {'-':>14} | {'-':>12}")
            continue

        txns = sd["transactions"]
        n_success = sum(1 for t in txns if t["success"])
        n_failed = sum(1 for t in txns if not t["success"])
        earned = sd.get("leader_fee_lamports", 0)
        tip_rev = sd.get("tips_in_lamports", 0)
        compute = sum(t["compute_units"] for t in txns)
        print(f"{slot:<14} | {'OK':<7} | {len(txns):>6} | {n_success:>6} | {n_failed:>5} | {format_sol(earned):>12} | {format_sol(tip_rev):>14} | {compute:>12,}")

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


def output_json(slots_data, explorer_url, validator_identity, network):
    """Output JSON format."""
    total_txns = 0
    total_success = 0
    total_failed = 0
    gross_fees_paid = 0
    leader_fees_earned = 0
    total_tips_in = 0
    total_self_drain = 0
    skipped_slots = 0
    anomalies_flat = []

    for sd in slots_data:
        if sd.get("skipped"):
            skipped_slots += 1
            continue
        if sd.get("error"):
            continue
        leader_fees_earned += sd.get("leader_fee_lamports", 0)
        total_tips_in += sd.get("tips_in_lamports", 0)
        total_self_drain += sd.get("self_drain_lamports", 0)
        for a in sd.get("tip_anomalies") or []:
            anomalies_flat.append({"slot": sd["slot"], **a})
        for tx in sd["transactions"]:
            total_txns += 1
            gross_fees_paid += tx["fee"]
            if tx["success"]:
                total_success += 1
            else:
                total_failed += 1

    anomaly_lamports = sum(-a["lamports"] for a in anomalies_flat)
    # Tip revenue = inflow into tip PDAs during our leader slots (these tips
    # belong to us, will be swept to our TipDistributionAccount). self-drain
    # in THIS window comes from prior rotations' tips and is informational.
    tip_revenue = total_tips_in
    total_revenue = leader_fees_earned + tip_revenue

    output = {
        "validator": validator_identity,
        "network": network,
        "summary": {
            "first_slot": slots_data[0]["slot"],
            "last_slot": slots_data[-1]["slot"],
            "total_slots": len(slots_data),
            "skipped_slots": skipped_slots,
            "total_non_vote_transactions": total_txns,
            "successful": total_success,
            "failed": total_failed,
            # Authoritative leader credit from block.rewards[]. Post-SIMD-0096
            # the leader receives 100% of priority fees + 50% of base fees.
            "total_fees_lamports": leader_fees_earned,
            "total_fees_sol": leader_fees_earned / 1e9,
            # Authoritative tip revenue: gross inflow into tip PDAs during
            # our leader slots. These tips are ours — they'll be swept to
            # our TipDistributionAccount by change_tip_receiver at the next
            # sweep point. (Back-compat field name: total_tips_*.)
            "total_tips_lamports": tip_revenue,
            "total_tips_sol": tip_revenue / 1e9,
            "total_revenue_lamports": total_revenue,
            "total_revenue_sol": total_revenue / 1e9,
            # Same value, explicit name.
            "tip_inflow_lamports": total_tips_in,
            "tip_inflow_sol": total_tips_in / 1e9,
            # Informational: sweeps executed in this window (change_tip_receiver
            # outflows signed by us). Reflects PRIOR rotations' tips, not
            # the current one — don't add to revenue.
            "sweeps_executed_lamports": total_self_drain,
            "sweeps_executed_sol": total_self_drain / 1e9,
            # Suspicious withdrawals we couldn't attribute. Should be 0.
            "tip_anomaly_count": len(anomalies_flat),
            "tip_anomaly_lamports": anomaly_lamports,
            "tip_anomaly_sol": anomaly_lamports / 1e9,
            # Legacy field names for shell-script back-compat:
            "tip_withdrawal_count": len(anomalies_flat),
            "tip_withdrawal_lamports": anomaly_lamports,
            "tip_withdrawal_sol": anomaly_lamports / 1e9,
            # Gross fees paid by users (pre-burn, non-vote only) for comparison.
            "gross_fees_paid_lamports": gross_fees_paid,
            "gross_fees_paid_sol": gross_fees_paid / 1e9,
        },
        "tip_anomalies": anomalies_flat,
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
    leader_slots_arg = None
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
        elif args[i] == "--leader-slots" and i + 1 < len(args):
            leader_slots_arg = args[i + 1]
            i += 2
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

    if leader_slots_arg is None and (first_slot is None or last_slot is None):
        print("Error: --slots FIRST LAST or --leader-slots LIST is required",
              file=sys.stderr)
        sys.exit(1)

    try:
        network = detect_network()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    jito_cfg = JITO_ADDRESSES[network]
    rpc_url = rpc_url_override or load_rpc_url(network)
    validator_identity = load_validator_identity()

    if leader_slots_arg is not None:
        slots = sorted(int(s) for s in leader_slots_arg.split(",") if s.strip())
        if not slots:
            print("Error: --leader-slots produced an empty slot list", file=sys.stderr)
            sys.exit(1)
        first_slot, last_slot = slots[0], slots[-1]
    else:
        slots = list(range(first_slot, last_slot + 1))
    print(f"Querying {len(slots)} slots ({first_slot}–{last_slot}) on {network}...", file=sys.stderr)

    slots_data = []
    for slot in slots:
        print(f"  Slot {slot}...", file=sys.stderr, end="", flush=True)
        sd = extract_slot_data(rpc_url, slot, jito_cfg, validator_identity)
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
        output_json(slots_data, explorer_url, validator_identity, network)
    else:
        output_text(slots_data, explorer_url, validator_identity, network)


if __name__ == "__main__":
    main()
