#!/usr/bin/env bash
# Role gate for cron-driven scripts in this repo.
#
# Exits 0 when this host's running validator identity matches the host's
# staked keypair (we're acting as primary — caller should proceed).
# Exits 1 when it matches the unstaked keypair (we're acting as standby —
# caller should skip). Exits 2 on any unexpected state (no keypairs found,
# admin RPC unreachable, identity matches neither, etc).
#
# Typical caller:   /home/sol/bam-leader-activity/role-gate.sh || exit 0
#
# Discovery is self-contained, no external config required:
#   - LEDGER_DIR is read from the running agave-validator process (via
#     detect_ledger_dir in lib-env.sh).
#   - Staked / unstaked keypairs are found by filename convention under
#     $HOME: *staked-identity-*.json and *unstaked-identity-*.json.
#   - Their pubkeys are derived with solana-keygen.
# The running identity is read from the validator's admin RPC via
# `agave-validator contact-info`, so the gate also tracks hot-swaps
# performed via `agave-validator set-identity`.
set -uo pipefail

# Cron's PATH lacks the Solana install dir.
export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"

# shellcheck source=lib-env.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib-env.sh"
LEDGER_DIR=$(detect_ledger_dir)

shopt -s nullglob
_unstaked_files=("$HOME"/*unstaked-identity-*.json)
_staked_files=()
for _f in "$HOME"/*staked-identity-*.json; do
    [[ "$_f" == *unstaked-identity-* ]] || _staked_files+=("$_f")
done
shopt -u nullglob

(( ${#_staked_files[@]} == 1 )) || {
    echo "role-gate: expected exactly 1 *staked-identity-*.json in $HOME, found ${#_staked_files[@]}" >&2
    exit 2
}
(( ${#_unstaked_files[@]} == 1 )) || {
    echo "role-gate: expected exactly 1 *unstaked-identity-*.json in $HOME, found ${#_unstaked_files[@]}" >&2
    exit 2
}

STAKED_PUBKEY=$(solana-keygen pubkey "${_staked_files[0]}")
UNSTAKED_PUBKEY=$(solana-keygen pubkey "${_unstaked_files[0]}")

RUNNING_ID=$(agave-validator --ledger "$LEDGER_DIR" contact-info --output json 2>/dev/null \
    | jq -r .id 2>/dev/null)

case "$RUNNING_ID" in
    "$STAKED_PUBKEY")   exit 0 ;;
    "$UNSTAKED_PUBKEY") exit 1 ;;
    "") echo "role-gate: could not read running identity (validator down?)" >&2; exit 2 ;;
    *)  echo "role-gate: running identity '$RUNNING_ID' is neither staked ($STAKED_PUBKEY) nor unstaked ($UNSTAKED_PUBKEY)" >&2; exit 2 ;;
esac
