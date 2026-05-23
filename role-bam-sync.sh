#!/usr/bin/env bash
#
# role-bam-sync.sh — drive BAM URL based on this host's current
# validator role.
#
# Behavior:
#   primary (running staked identity)   → set-bam-node.sh --auto
#                                         (lowest-latency healthy region)
#   standby (running unstaked identity) → set-bam-node.sh --off
#   other (validator down, unknown id)  → no-op, propagate error
#
# Idempotent: the underlying agave-validator set-bam-config call is
# safe to re-issue; set-bam-node.sh --auto skips when already on the
# best region. Safe to call from cron and from set_identity_*.sh.
#
# Network-agnostic (set-bam-node.sh auto-detects mainnet/testnet).
# Role-agnostic (role-gate.sh determines role at runtime).
set -uo pipefail

REPO_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
ROLE_GATE="$REPO_DIR/role-gate.sh"
SET_BAM="$REPO_DIR/set-bam-node.sh"

# Cron's PATH lacks the Solana install dir.
export PATH="$HOME/.local/share/solana/install/active_release/bin:$PATH"

"$ROLE_GATE"
_role_rc=$?

case $_role_rc in
    0)
        echo "role-bam-sync: primary identity — selecting best BAM region"
        exec "$SET_BAM" --auto
        ;;
    1)
        echo "role-bam-sync: standby identity — disabling BAM"
        exec "$SET_BAM" --off
        ;;
    *)
        echo "role-bam-sync: role-gate.sh exited $_role_rc — taking no BAM action" >&2
        exit "$_role_rc"
        ;;
esac
