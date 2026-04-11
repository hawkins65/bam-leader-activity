#!/usr/bin/env bash
###############################################################################
# detect-network.sh — sourceable helper that exposes detect_network()
#
# Echoes "mainnet" or "testnet" on stdout and returns 0 on success; returns
# non-zero with a clear error message on failure. Nothing else is exported.
#
# Detection rules (in order):
#   1. If $NETWORK is set to exactly "mainnet" or "testnet", trust it.
#   2. Otherwise, read $VALIDATOR_SH (default $HOME/validator.sh) and grep
#      its --entrypoint flags:
#        entrypoint*.mainnet-beta.solana.com  -> mainnet
#        entrypoint*.testnet.solana.com       -> testnet
#   3. If neither rule fires, fail loudly — never silently default.
#
# Usage:
#   source detect-network.sh
#   NETWORK="$(detect_network)" || exit 1
###############################################################################

detect_network() {
    # Rule 1: env override
    if [[ -n "${NETWORK:-}" ]]; then
        case "$NETWORK" in
            mainnet|testnet)
                echo "$NETWORK"
                return 0
                ;;
            *)
                echo "detect_network: NETWORK env var is set to '$NETWORK'; must be 'mainnet' or 'testnet'" >&2
                return 1
                ;;
        esac
    fi

    # Rule 2: parse validator.sh entrypoint hostnames
    local validator_sh="${VALIDATOR_SH:-$HOME/validator.sh}"
    if [[ ! -f "$validator_sh" ]]; then
        echo "detect_network: $validator_sh not found; set NETWORK=mainnet|testnet in the environment to bypass detection" >&2
        return 1
    fi

    # grep -c prints a count and exits non-zero when the count is zero, so
    # swallow the non-zero exit with `|| true` to keep the numeric output.
    local has_mainnet has_testnet
    has_mainnet=$(grep -cE 'entrypoint[0-9]*\.mainnet-beta\.solana\.com' "$validator_sh" 2>/dev/null || true)
    has_testnet=$(grep -cE 'entrypoint[0-9]*\.testnet\.solana\.com' "$validator_sh" 2>/dev/null || true)
    has_mainnet=${has_mainnet:-0}
    has_testnet=${has_testnet:-0}

    if (( has_mainnet > 0 && has_testnet == 0 )); then
        echo "mainnet"
        return 0
    elif (( has_testnet > 0 && has_mainnet == 0 )); then
        echo "testnet"
        return 0
    elif (( has_mainnet > 0 && has_testnet > 0 )); then
        echo "detect_network: $validator_sh contains BOTH mainnet and testnet entrypoints; set NETWORK=mainnet|testnet to disambiguate" >&2
        return 1
    else
        echo "detect_network: $validator_sh contains no recognizable --entrypoint flag; set NETWORK=mainnet|testnet to bypass detection" >&2
        return 1
    fi
}
