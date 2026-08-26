#!/usr/bin/env bash
# Self-check for leader-capture-monitor.sh's numeric logic:
#
#   - the 18:15 CT day boundary, and count_vote_txns' clipping of a rotation
#     interval that straddles it. A money path: an error silently over- or
#     under-states the daily vote cost, which is what "Today net" is compared
#     against by the collect_balance sweep.
#   - get_slot_duration's per-network sanity band. Too high a value collapses
#     separate leader rotations into one capture window; a band that excludes
#     the network's real slot time silently substitutes the nominal.
#
#   ./test-leader-capture.sh   # exits 0 on pass, 1 on the first failure
set -uo pipefail

SCRIPT="${1:-$(dirname "$(readlink -f "$0")")/leader-capture-monitor.sh}"
[[ -r "$SCRIPT" ]] || { echo "cannot read $SCRIPT" >&2; exit 1; }

# Pull the real definitions out of the script rather than restating them, so a
# change to the script is a change to what is under test.
eval "$(grep -E '^(DAY_ROLLOVER_HHMM|DAY_TZ|VOTE_MAX_INTERVAL_SLOTS)=' "$SCRIPT")"
eval "$(awk '/^central_day_label\(\) \{/,/^\}/' "$SCRIPT")"
eval "$(awk '/^day_start_ts\(\) \{/,/^\}/' "$SCRIPT")"
eval "$(awk '/^count_vote_txns\(\) \{/,/^\}/' "$SCRIPT")"
eval "$(awk '/^get_slot_duration\(\) \{/,/^\}/' "$SCRIPT")"
# The per-network bands live in a `case $NETWORK` block that cannot be sourced
# without a validator config, so read them straight out of that block.
net_band() { awk -v net="$1" '
    $0 ~ "^ *"net"\\)" {inblock=1}
    inblock && /SLOT_DURATION_(DEFAULT|MIN|MAX)=/ {
        sub(/^ */,""); sub(/ *#.*/,""); print
    }
    inblock && /^ *;;/ {exit}' "$SCRIPT"; }

VOTE_MAX_PAGES=5
VALIDATOR_IDENTITY="TestIdentity1111111111111111111111111111111"
fail=0
check() { # check <label> <expected> <actual>
    if [[ "$2" == "$3" ]]; then printf 'ok   %s\n' "$1"
    else printf 'FAIL %s: expected %s, got %s\n' "$1" "$2" "$3"; fail=1; fi
}

# ── day boundary ────────────────────────────────────────────────────────────
# 2026-08-24 18:15 America/Chicago is CDT (UTC-5) -> 23:15 UTC.
check "day_start_ts CDT" \
    "$(date -u -d '2026-08-24 23:15:00' +%s)" "$(day_start_ts 2026-08-24)"
# 2026-01-15 18:15 America/Chicago is CST (UTC-6) -> 00:15 UTC next day.
check "day_start_ts CST" \
    "$(date -u -d '2026-01-16 00:15:00' +%s)" "$(day_start_ts 2026-01-15)"
# One second after the boundary must label as that day, one second before as the
# previous one - otherwise the clip is applied against the wrong boundary.
B=$(day_start_ts 2026-08-24)
check "label at boundary"     "2026-08-24" "$(central_day_label "$B")"
check "label before boundary" "2026-08-23" "$(central_day_label "$((B - 1))")"

# ── count_vote_txns boundary clipping ───────────────────────────────────────
# Fixture: 4 signatures, newest first. Two land after the boundary, two before.
# rpc_call is stubbed, so no network and no RPC key is needed.
rpc_call() {
    if [[ -z "${_page_served:-}" ]]; then
        _page_served=1
        cat <<JSON
{"result":[
 {"slot":1004,"blockTime":$((B + 200)),"signature":"s4"},
 {"slot":1003,"blockTime":$((B + 100)),"signature":"s3"},
 {"slot":1002,"blockTime":$((B - 100)),"signature":"s2"},
 {"slot":1001,"blockTime":$((B - 200)),"signature":"s1"}]}
JSON
    else
        echo '{"result":[]}'
    fi
}

_page_served=""; check "no boundary counts all 4" "4 exact" "$(count_vote_txns 1000 1004)"
_page_served=""; check "boundary drops the 2 before" "2 exact" "$(count_vote_txns 1000 1004 "$B")"
_page_served=""; check "to_slot excludes newer sigs" "3 exact" "$(count_vote_txns 1000 1003)"
_page_served=""; check "empty interval short-circuits" "0 exact" "$(count_vote_txns 1004 1004)"

# The est fallback must stay clamped - it is the only path that can invent hours
# of vote cost, and the caller clamps it to VOTE_MAX_INTERVAL_SLOTS.
rpc_call() { echo 'not json'; }
read -r n mode <<< "$(count_vote_txns 0 999999)"
check "rpc failure falls back to est" "est" "$mode"
(( n > VOTE_MAX_INTERVAL_SLOTS )) && check "est exceeds clamp (caller clamps it)" "1" "1"

# ── get_slot_duration per-network band ──────────────────────────────────────
# A getRecentPerformanceSamples reply of $2 slots in 60s.
sample() { rpc_call() { echo "{\"result\":[{\"samplePeriodSecs\":60,\"numSlots\":$1}]}"; }; }
in_range() { awk -v d="$1" -v lo="$2" -v hi="$3" 'BEGIN{exit !(d >= lo && d <= hi)}'; }

for net in mainnet testnet; do
    band=$(net_band "$net")
    [[ -n "$band" ]] || { echo "FAIL no $net band found in $SCRIPT"; fail=1; continue; }
    eval "$band"
    check "$net band is ordered" "1" \
        "$(in_range "$SLOT_DURATION_DEFAULT" "$SLOT_DURATION_MIN" "$SLOT_DURATION_MAX" && echo 1 || echo 0)"
done

# 307 slots in 60s = 0.1954 s/slot, a real testnet reading measured 2026-08-26.
# It must be accepted on testnet and must NOT be silently replaced by 0.420 -
# that substitution was the bug.
eval "$(net_band testnet)"; sample 307
d=$(get_slot_duration)
check "testnet accepts a real testnet sample" "1" "$(in_range "$d" 0.15 0.25 && echo 1 || echo 0)"

# 150 slots in 60s = 0.4 s/slot, a real mainnet reading.
eval "$(net_band mainnet)"; sample 150
d=$(get_slot_duration)
check "mainnet accepts a real mainnet sample" "1" "$(in_range "$d" 0.35 0.45 && echo 1 || echo 0)"

# The upper bound is the one that matters: too long a slot merges separate
# rotations into one window. 50 slots in 60s = 1.2 s/slot must be rejected.
sample 50
check "mainnet rejects an over-long slot" "$SLOT_DURATION_DEFAULT" "$(get_slot_duration)"

# numSlots 0 yields no value at all; the nominal must stand in.
sample 0
check "zero numSlots falls back to nominal" "$SLOT_DURATION_DEFAULT" "$(get_slot_duration)"

exit $fail
