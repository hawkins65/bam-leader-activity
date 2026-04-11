#!/usr/bin/env bash
# Detect whether the host validator is running on mainnet or testnet by
# inspecting the --entrypoint flags in validator.sh. Source this file and
# call `detect_network` — it echoes `mainnet` or `testnet` on stdout.
#
# Overrides:
#   NETWORK=mainnet|testnet   skip detection, trust the caller
#   VALIDATOR_SH=/path/to/sh  look at a non-default validator.sh

detect_network() {
    if [[ -n "${NETWORK:-}" ]]; then
        case "$NETWORK" in
            mainnet|testnet) echo "$NETWORK"; return 0 ;;
            *) echo "ERROR: NETWORK must be 'mainnet' or 'testnet' (got '$NETWORK')" >&2; return 1 ;;
        esac
    fi

    local validator_sh="${VALIDATOR_SH:-$HOME/validator.sh}"
    if [[ ! -f "$validator_sh" ]]; then
        echo "ERROR: cannot detect network — $validator_sh not found (set NETWORK or VALIDATOR_SH)" >&2
        return 1
    fi

    if grep -qE 'entrypoint[^ ]*\.testnet\.solana\.com' "$validator_sh"; then
        echo testnet
    elif grep -qE 'entrypoint[^ ]*\.mainnet-beta\.solana\.com' "$validator_sh"; then
        echo mainnet
    else
        echo "ERROR: could not determine network from $validator_sh (no testnet/mainnet-beta entrypoint)" >&2
        return 1
    fi
}
