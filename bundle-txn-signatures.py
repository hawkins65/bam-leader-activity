#!/usr/bin/env python3
"""
Bundle Transaction Signature Extractor

Extracts transaction signatures from Jito-Solana bundle execution DEBUG logs.
Parses logs to show which transactions were included in bundles during leader slots.

IMPORTANT: Requires DEBUG logging enabled for the bundle_stage module.

To enable DEBUG logging:

  Option 1: Environment variable (requires validator restart)
    RUST_LOG="solana=info,solana_core::bundle_stage=debug"

  Option 2: Runtime change (no restart required)
    agave-validator -l /path/to/ledger set-log-filter "solana=info,solana_core::bundle_stage=debug"

  The default log level if RUST_LOG is not set is: solana=info,agave=info

Supports reading from a log file or from journalctl.
"""

import os
import re
import sys
import subprocess
from collections import defaultdict
from datetime import datetime

# =============================================================================
# CONFIGURATION
# =============================================================================
DEFAULT_LOG_PATH = os.path.expanduser("~/logs/validator.log")
DEFAULT_SERVICE = "sol.service"
DEFAULT_HOURS = 24
DEFAULT_EXPLORER_URL = "https://solscan.io/tx"
DEFAULT_CLUSTER = "mainnet"  # Options: mainnet, testnet, devnet

# Table widths
BUNDLE_TABLE_WIDTH = 140
# =============================================================================


def parse_timestamp(line):
    """Extract timestamp from log line"""
    match = re.match(r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
    if match:
        ts_str = match.group(1)
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
            return dt, ts_str
        except ValueError:
            pass
    return None, None


def print_usage():
    print(f"""Bundle Transaction Signature Extractor

Extracts transaction signatures from Jito bundle execution logs.

Requires DEBUG logging for bundle_stage. To enable:

  Option 1: Environment variable (requires validator restart)
    RUST_LOG="solana=info,solana_core::bundle_stage=debug"

  Option 2: Runtime (no restart required)
    agave-validator -l /path/to/ledger set-log-filter "solana=info,solana_core::bundle_stage=debug"

Usage:
  {sys.argv[0]}                      Use default log file ({DEFAULT_LOG_PATH})
  {sys.argv[0]} /path/to/file.log    Read from specified log file
  {sys.argv[0]} -j [service]         Read from journalctl (default: {DEFAULT_SERVICE}, last {DEFAULT_HOURS}h)
  {sys.argv[0]} --hours N            Set time span for journalctl (default: {DEFAULT_HOURS})

Options:
  --summary                          Show summary only (no individual signatures)
  --csv                              Output in CSV format
  --json                             Output in JSON format
  --explorer-url URL                 Base URL for transaction explorer links
                                     (default: {DEFAULT_EXPLORER_URL})
  --cluster CLUSTER                  Solana cluster: mainnet, testnet, devnet
                                     (default: {DEFAULT_CLUSTER})
  --no-links                         Don't generate explorer links

Examples:
  {sys.argv[0]}                      # Use default log file (mainnet)
  {sys.argv[0]} -j --hours 4         # Last 4 hours from journalctl
  {sys.argv[0]} -j sol --summary     # Summary only from sol.service
  {sys.argv[0]} -j --cluster testnet # Use testnet explorer links
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


def extract_signatures(sig_text):
    """
    Extract base58 signatures from the debug output format.
    Solana signatures are 64 bytes, base58 encoded = 87-88 characters.
    Format in logs: [[sig1], [sig2]] or [sig1, sig2] depending on nesting
    """
    # Match base58 signatures (alphanumeric, 43-88 chars, no 0/O/I/l)
    # Solana signatures are typically 87-88 chars in base58
    sig_pattern = re.compile(r'[1-9A-HJ-NP-Za-km-z]{43,88}')
    signatures = sig_pattern.findall(sig_text)

    # Filter to likely valid signatures (64 bytes = ~87-88 base58 chars)
    valid_sigs = [s for s in signatures if 80 <= len(s) <= 90]
    return valid_sigs


def parse_bundle_result(result_text):
    """Parse the bundle execution result"""
    if 'Ok(())' in result_text or 'Ok(' in result_text:
        return 'success'
    elif 'LockError' in result_text:
        return 'lock_error'
    elif 'TransactionFailure' in result_text:
        return 'tx_failure'
    elif 'ExceedsBlockCostLimit' in result_text:
        return 'cost_limit'
    elif 'ExceedsBundleCost' in result_text:
        return 'bundle_cost_limit'
    elif 'TipError' in result_text:
        return 'tip_error'
    elif 'Err(' in result_text:
        return 'error'
    return 'unknown'


def analyze_logs(line_source, source_name, output_format='table', summary_only=False, explorer_url=None, cluster=None):
    """Analyze log lines and extract bundle transaction signatures"""

    print(f"Analyzing: {source_name}", file=sys.stderr)
    print("Processing logs", end="", file=sys.stderr, flush=True)

    # Pattern to match bundle execution debug logs
    # Format: "execution results: bundle signatures: [...], result: ..."
    bundle_exec_pattern = re.compile(
        r'execution results: bundle signatures: (\[.*?\]), result: ([^,]+)'
    )

    # Also try to capture the slot from nearby context
    slot_pattern = re.compile(r'bank[_\s:]+(\d{6,})')

    # Data collection
    bundles = []  # List of bundle records
    current_slot = None

    # Stats
    line_count = 0
    bundle_count = 0
    total_txns = 0
    results_count = defaultdict(int)

    progress_interval = 100000

    for line in line_source:
        line_count += 1
        if line_count % progress_interval == 0:
            print(".", end="", file=sys.stderr, flush=True)

        # Try to track current slot from various log messages
        slot_match = slot_pattern.search(line)
        if slot_match:
            current_slot = int(slot_match.group(1))

        # Look for bundle execution results
        if 'execution results: bundle signatures:' in line:
            ts, ts_str = parse_timestamp(line)

            # Extract the signatures portion and result
            match = bundle_exec_pattern.search(line)
            if match:
                sig_text = match.group(1)
                result_text = match.group(2)

                signatures = extract_signatures(sig_text)
                result = parse_bundle_result(result_text)

                if signatures:
                    bundle_count += 1
                    total_txns += len(signatures)
                    results_count[result] += 1

                    bundles.append({
                        'timestamp': ts_str or 'unknown',
                        'slot': current_slot,
                        'signatures': signatures,
                        'txn_count': len(signatures),
                        'result': result,
                        'raw_result': result_text.strip()
                    })

    print(f" done ({line_count:,} lines)", file=sys.stderr)

    if line_count == 0:
        print("No log lines found.", file=sys.stderr)
        sys.exit(1)

    if bundle_count == 0:
        print("\nNo bundle execution logs found.", file=sys.stderr)
        print("Make sure DEBUG logging is enabled for bundle_stage:", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Option 1: Environment variable (requires validator restart)", file=sys.stderr)
        print('    RUST_LOG="solana=info,solana_core::bundle_stage=debug"', file=sys.stderr)
        print("", file=sys.stderr)
        print("  Option 2: Runtime (no restart required)", file=sys.stderr)
        print('    agave-validator -l /path/to/ledger set-log-filter "solana=info,solana_core::bundle_stage=debug"', file=sys.stderr)
        sys.exit(1)

    # Output based on format
    if output_format == 'json':
        output_json(bundles, bundle_count, total_txns, results_count, explorer_url, cluster)
    elif output_format == 'csv':
        output_csv(bundles, summary_only, explorer_url, cluster)
    else:
        output_table(bundles, bundle_count, total_txns, results_count, summary_only, explorer_url, cluster)


def make_explorer_link(sig, explorer_url, cluster=None):
    """Create an explorer URL for a signature"""
    if explorer_url:
        url = f"{explorer_url}/{sig}"
        if cluster and cluster != "mainnet":
            url += f"?cluster={cluster}"
        return url
    return sig


def output_json(bundles, bundle_count, total_txns, results_count, explorer_url, cluster):
    """Output in JSON format"""
    import json

    # Add explorer links to bundles if URL provided
    if explorer_url:
        for b in bundles:
            b['signature_links'] = [make_explorer_link(sig, explorer_url, cluster) for sig in b['signatures']]

    output = {
        'summary': {
            'total_bundles': bundle_count,
            'total_transactions': total_txns,
            'avg_txns_per_bundle': round(total_txns / bundle_count, 2) if bundle_count > 0 else 0,
            'results': dict(results_count)
        },
        'explorer_url': explorer_url,
        'cluster': cluster,
        'bundles': bundles
    }

    print(json.dumps(output, indent=2))


def output_csv(bundles, summary_only, explorer_url, cluster):
    """Output in CSV format"""
    if summary_only:
        print("timestamp,slot,txn_count,result")
        for b in bundles:
            print(f"{b['timestamp']},{b['slot'] or ''},{b['txn_count']},{b['result']}")
    else:
        if explorer_url:
            print("timestamp,slot,txn_count,result,signature,link")
            for b in bundles:
                for sig in b['signatures']:
                    link = make_explorer_link(sig, explorer_url, cluster)
                    print(f"{b['timestamp']},{b['slot'] or ''},{b['txn_count']},{b['result']},{sig},{link}")
        else:
            print("timestamp,slot,txn_count,result,signature")
            for b in bundles:
                for sig in b['signatures']:
                    print(f"{b['timestamp']},{b['slot'] or ''},{b['txn_count']},{b['result']},{sig}")


def output_table(bundles, bundle_count, total_txns, results_count, summary_only, explorer_url, cluster):
    """Output in table format"""

    # Print summary
    print(f"\n{'BUNDLE TRANSACTION SIGNATURES':=^{BUNDLE_TABLE_WIDTH}}")
    print(f"\nSummary:")
    print(f"  Total bundles processed: {bundle_count:,}")
    print(f"  Total transactions: {total_txns:,}")
    if bundle_count > 0:
        print(f"  Avg transactions per bundle: {total_txns / bundle_count:.1f}")
    if explorer_url:
        cluster_suffix = f" (cluster={cluster})" if cluster and cluster != "mainnet" else ""
        print(f"  Explorer URL: {explorer_url}{cluster_suffix}")

    print(f"\nResults breakdown:")
    for result, count in sorted(results_count.items(), key=lambda x: -x[1]):
        pct = (count / bundle_count * 100) if bundle_count > 0 else 0
        print(f"  {result}: {count:,} ({pct:.1f}%)")

    if summary_only:
        print(f"\n{'=' * BUNDLE_TABLE_WIDTH}")
        return

    # Print detailed bundle list
    print(f"\n{'-' * BUNDLE_TABLE_WIDTH}")
    print(f"{'Timestamp':<24} | {'Slot':<12} | {'Txns':>5} | {'Result':<15} | Transaction Signatures")
    print(f"{'-' * BUNDLE_TABLE_WIDTH}")

    for b in bundles:
        slot_str = str(b['slot']) if b['slot'] else '?'
        first_sig = b['signatures'][0] if b['signatures'] else ''

        # Print first line with bundle info
        print(f"{b['timestamp']:<24} | {slot_str:<12} | {b['txn_count']:>5} | {b['result']:<15} | {first_sig}")

        # Print additional signatures (if any)
        for sig in b['signatures'][1:]:
            print(f"{'':<24} | {'':<12} | {'':<5} | {'':<15} | {sig}")

    print(f"{'=' * BUNDLE_TABLE_WIDTH}")

    # Print all unique signatures with explorer links
    print(f"\n{'ALL TRANSACTION SIGNATURES':=^{BUNDLE_TABLE_WIDTH}}")
    if explorer_url:
        print(f"(With explorer links to {explorer_url})\n")
    else:
        print("(One per line for easy lookup on Solana explorers)\n")

    all_sigs = []
    for b in bundles:
        all_sigs.extend(b['signatures'])

    # Deduplicate while preserving order
    seen = set()
    unique_sigs = []
    for sig in all_sigs:
        if sig not in seen:
            seen.add(sig)
            unique_sigs.append(sig)

    for sig in unique_sigs:
        if explorer_url:
            print(f"{make_explorer_link(sig, explorer_url, cluster)}")
        else:
            print(sig)

    print(f"\nTotal unique signatures: {len(unique_sigs):,}")
    print(f"{'=' * BUNDLE_TABLE_WIDTH}")


def verify_log_file(log_file):
    """Check if log file exists and is readable"""
    if not os.path.exists(log_file):
        print(f"Error: Log file not found: {log_file}")
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
        sys.exit(1)


def main():
    args = sys.argv[1:]

    # Extract options
    hours = DEFAULT_HOURS
    output_format = 'table'
    summary_only = False
    explorer_url = DEFAULT_EXPLORER_URL
    cluster = DEFAULT_CLUSTER

    if '--hours' in args:
        try:
            idx = args.index('--hours')
            hours = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        except (IndexError, ValueError):
            print("Error: --hours requires a numeric value")
            sys.exit(1)

    if '--explorer-url' in args:
        try:
            idx = args.index('--explorer-url')
            explorer_url = args[idx + 1].rstrip('/')  # Remove trailing slash if present
            args = args[:idx] + args[idx + 2:]
        except (IndexError, ValueError):
            print("Error: --explorer-url requires a URL value")
            sys.exit(1)

    if '--cluster' in args:
        try:
            idx = args.index('--cluster')
            cluster = args[idx + 1].lower()
            args = args[:idx] + args[idx + 2:]
            if cluster not in ('mainnet', 'testnet', 'devnet'):
                print("Error: --cluster must be mainnet, testnet, or devnet")
                sys.exit(1)
        except (IndexError, ValueError):
            print("Error: --cluster requires a value (mainnet, testnet, devnet)")
            sys.exit(1)

    if '--no-links' in args:
        explorer_url = None
        cluster = None
        args.remove('--no-links')

    if '--summary' in args:
        summary_only = True
        args.remove('--summary')

    if '--csv' in args:
        output_format = 'csv'
        args.remove('--csv')

    if '--json' in args:
        output_format = 'json'
        args.remove('--json')

    if len(args) == 0:
        if not os.path.exists(DEFAULT_LOG_PATH):
            print(f"Error: Default log file not found: {DEFAULT_LOG_PATH}")
            print(f"Run '{sys.argv[0]} --help' for usage information.")
            sys.exit(1)
        verify_log_file(DEFAULT_LOG_PATH)
        analyze_logs(get_lines_from_file(DEFAULT_LOG_PATH), DEFAULT_LOG_PATH, output_format, summary_only, explorer_url, cluster)

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
            output_format,
            summary_only,
            explorer_url,
            cluster
        )

    else:
        log_file = args[0]
        verify_log_file(log_file)
        analyze_logs(get_lines_from_file(log_file), log_file, output_format, summary_only, explorer_url, cluster)


if __name__ == "__main__":
    main()
