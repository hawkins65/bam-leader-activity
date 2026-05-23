# Setup — monitoring, alerts, cron

For bringing this repo up on a validator host (mainnet or testnet, primary
or standby), whether fresh or partially configured. Everything here assumes
the `sol` user owns the validator process and the clone lives at
`/home/sol/bam-leader-activity`.

The repo is **network-agnostic** (mainnet/testnet auto-detected from
`solana config` or `~/validator.sh`) and **role-agnostic** (primary/standby
detected at runtime from the running validator identity vs on-disk keypair
files). Same deployment steps work everywhere — host-specific behavior
falls out of runtime detection.

## How to use this document

Each section has a **Verify** block (idempotent checks) and a **Fix if
missing** block. Run the verify commands first; only run the fix steps for
items that come back missing or wrong. Running fix steps on an already-
configured host is safe *except* where explicitly noted (e.g. overwriting
`rpc.conf` would clobber existing secrets — the verify step tells you to
skip the fix in that case).

Run everything as the `sol` user unless a step uses `sudo`.

## 1. Clone the repo

**Verify:**
```bash
test -d /home/sol/bam-leader-activity/.git && echo "OK: repo present" || echo "MISSING"
```

**Fix if missing:**
```bash
sudo -u sol -i
cd ~
git clone https://github.com/hawkins65/bam-leader-activity.git
cd bam-leader-activity
```

If present, just `git -C /home/sol/bam-leader-activity pull --ff-only` to
update.

## 2. System packages

**Verify:**
```bash
for p in python3 jq curl bc socat ping pgrep; do command -v $p >/dev/null && echo "OK: $p" || echo "MISSING: $p"; done
dpkg -s python3-venv >/dev/null 2>&1 && echo "OK: python3-venv" || echo "MISSING: python3-venv"
```

`socat` is the admin-RPC fallback path for `bam-log-monitor.sh`. `ping` and
`pgrep` are used by `set-bam-node.sh` and `lib-env.sh` respectively.

**Fix if missing:**
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip jq curl bc socat iputils-ping procps
```

## 3. Python environment

A shared venv at `/home/sol/python/venv` is used by
`hourly_log_error_summary.py`.

**Verify:**
```bash
test -x /home/sol/python/venv/bin/python3 && echo "OK: venv" || echo "MISSING: venv"
/home/sol/python/venv/bin/python3 -c "import requests" 2>/dev/null && echo "OK: requests" || echo "MISSING: requests"
```

**Fix if missing:**
```bash
mkdir -p ~/python
python3 -m venv ~/python/venv
~/python/venv/bin/pip install --upgrade pip requests
```

Other scripts (`slot-transactions.py`, `bam-hourly-summary.py`) use system
`python3` and stdlib only.

## 4. Logs directory

**Verify:**
```bash
test -d ~/logs && echo "OK: ~/logs" || echo "MISSING"
```

**Fix if missing:** `mkdir -p ~/logs`

All scripts append here:

| Script | Log file |
|---|---|
| `leader-capture-monitor.sh` | `~/logs/leader-capture-monitor.log` |
| `daily-summary.sh` | `~/logs/daily-summary.log` |
| `bam-log-monitor.sh` | `~/logs/bam_monitor.log` |
| `hourly_log_error_summary.py` | `~/logs/hourly_log_summary.log` |
| `monitor_log_errors.sh` | `~/logs/log_monitor.log` |

## 5. Validator RPC / identity config

Scripts read `~/.config/validator/rpc.conf`.

**Verify (do NOT overwrite if keys already present):**
```bash
f=~/.config/validator/rpc.conf
if [[ -f $f ]]; then
  for k in MAINNET_RPC_URL TESTNET_RPC_URL VALIDATOR_IDENTITY VOTE_ACCOUNT; do
    grep -q "^$k=" "$f" && echo "OK: $k" || echo "MISSING: $k"
  done
else
  echo "MISSING: file $f"
fi
```

**Fix if missing** (create only if the file doesn't exist — if it exists but
a key is missing, edit in place instead):
```bash
mkdir -p ~/.config/validator
cat > ~/.config/validator/rpc.conf <<'EOF'
# Sourced by monitoring scripts — do NOT commit.
MAINNET_RPC_URL=https://your-mainnet-rpc/
MAINNET_RPC_URL_ALT=https://your-mainnet-rpc-fallback/
TESTNET_RPC_URL=https://api.testnet.solana.com/
VALIDATOR_IDENTITY=YourValidatorPubkeyHere
VALIDATOR_IDENTITY_KEYPAIR=/home/sol/validator-keypair.json
VOTE_ACCOUNT=YourVoteAccountPubkey
EOF
chmod 600 ~/.config/validator/rpc.conf
```

Network detection (`detect-network.sh`) reads `~/validator.sh`. On testnet,
ensure the startup script contains a testnet entrypoint
(e.g. `--entrypoint entrypoint.testnet.solana.com:8001`) or export
`NETWORK=testnet` before invoking the scripts. Verify:

```bash
bash -c 'source /home/sol/bam-leader-activity/detect-network.sh && detect_network'
```

## 6. Discord webhook + embed helper

Both pieces are required for any notifications.

**Verify:**
```bash
test -s ~/.config/discord/webhook && echo "OK: webhook file" || echo "MISSING: webhook"
test -f ~/999_discord_embed.sh && echo "OK: embed helper present" || echo "MISSING: embed helper"
bash -n ~/999_discord_embed.sh 2>/dev/null && echo "OK: embed helper syntax" || echo "MISSING/BROKEN: embed helper syntax"
(source ~/999_discord_embed.sh 2>/dev/null; declare -F send_discord_embed >/dev/null) && echo "OK: send_discord_embed defined" || echo "MISSING: send_discord_embed function"
```

**Fix if missing** (skip webhook creation if the file already has a URL):
```bash
mkdir -p ~/.config/discord
[[ -s ~/.config/discord/webhook ]] || {
    echo "https://discord.com/api/webhooks/XXX/YYY" > ~/.config/discord/webhook
    chmod 600 ~/.config/discord/webhook
    echo "EDIT ~/.config/discord/webhook with your real webhook URL"
}
```

If `~/999_discord_embed.sh` is missing, copy the minimal reference from
`README.md` → **Discord Notifications → Minimal reference implementation**
to that path and `chmod +x ~/999_discord_embed.sh`.

## 7. BAM URL role sync (network/role-agnostic)

Keeps the validator's BAM URL aligned with the running identity:

- **Primary** (running staked identity) → BAM set to the lowest-latency
  healthy region (via `set-bam-node.sh --auto`).
- **Standby** (running unstaked identity) → BAM disabled
  (via `set-bam-node.sh --off`).

Network is auto-detected (mainnet/testnet) by `set-bam-node.sh`. Role is
determined at runtime by comparing the running validator identity (admin
RPC `contact-info`) against on-disk keypair files. **No per-host config
file is required for this layer** — `rpc.conf` (Section 5) is only needed
for the revenue/log-summary features.

Repo scripts involved:

| File | Role |
|---|---|
| `lib-env.sh` | Sourceable; `detect_ledger_dir()` reads `--ledger` from the running validator process (falls back to `~/validator.sh`, then `/mnt/ledger`). |
| `role-gate.sh` | Exit 0 = primary, 1 = standby, 2 = error (validator down, keypair files missing, identity mismatch). |
| `set-bam-node.sh` | Apply BAM URL: interactive picker, explicit region, `--off`, or `--auto`. |
| `role-bam-sync.sh` | Cron entry point: dispatches `--auto` on primary, `--off` on standby. |

**Verify keypair files** — exactly one staked and one unstaked file must
live in `$HOME`. The name prefix doesn't matter (`testnet-staked-identity-…`,
`mainnet-staked-identity-…`, etc.), only that the substrings `staked-identity-`
and `unstaked-identity-` distinguish them:

```bash
shopt -s nullglob
sf=(); for f in ~/*staked-identity-*.json; do [[ "$f" == *unstaked-identity-* ]] || sf+=("$f"); done
uf=( ~/*unstaked-identity-*.json )
echo "staked:   ${#sf[@]} match — ${sf[*]##*/}"
echo "unstaked: ${#uf[@]} match — ${uf[*]##*/}"
```

**Fix if missing:** place this host's staked and unstaked keypair JSON
files in `$HOME`. The failover scripts (`~/set_identity_{staked,unstaked}.sh`)
are host-specific and not in this repo — they're what flips
`~/identity.json` symlink to the active keypair.

**Verify role detection** (works on either host, either network):

```bash
~/bam-leader-activity/role-gate.sh; echo "role-gate exit=$? (0=primary, 1=standby, 2=error)"
```

**Smoke test the full chain:**

```bash
~/bam-leader-activity/role-bam-sync.sh
```

Expected on standby: `standby identity — disabling BAM` → exit 0.
Expected on primary: `primary identity — selecting best BAM region` → exit 0
(applies or reports already-on-best).

Cron entry installed in Section 9.

## 8. Leader capture monitor (systemd)

Continuous service; posts per-rotation reports to Discord. Design is in
`README-leader-capture-monitor.md`.

**Verify:**
```bash
systemctl is-enabled leader-capture-monitor.service 2>/dev/null || echo "MISSING: not enabled"
systemctl is-active  leader-capture-monitor.service 2>/dev/null || echo "MISSING: not active"
# Drift check: is the installed unit in sync with the repo copy?
diff -q /etc/systemd/system/leader-capture-monitor.service \
        /home/sol/bam-leader-activity/leader-capture-monitor.service \
  && echo "OK: unit file in sync" || echo "DRIFT: unit file differs from repo"
```

**Fix if missing or drifted:**
```bash
sudo cp /home/sol/bam-leader-activity/leader-capture-monitor.service \
    /etc/systemd/system/leader-capture-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now leader-capture-monitor.service
sudo systemctl restart leader-capture-monitor.service   # only if it was already running on drift
sudo systemctl status  leader-capture-monitor.service
```

Tail: `tail -f ~/logs/leader-capture-monitor.log`.

## 9. Cron entries

Full intended crontab for the `sol` user. The same set works on primary
and standby — `role-gate.sh` makes each script self-skip when not the
intended role.

```cron
# Log monitoring - error detection every 5 minutes
*/5 * * * * /home/sol/bash/monitor_log_errors.sh --once >> /home/sol/logs/log_monitor.log 2>&1

# Hourly AI-powered log error summary (gated to primary)
0 * * * * /home/sol/python/venv/bin/python3 /home/sol/bam-leader-activity/hourly_log_error_summary.py >> /home/sol/logs/hourly_log_summary.log 2>&1

# BAM error monitoring every 5 minutes (gated to primary)
*/5 * * * * /home/sol/bam-leader-activity/bam-log-monitor.sh --once >> /home/sol/logs/bam_monitor.log 2>&1

# BAM URL role sync — every 5 minutes (BAM on/off by current validator role)
*/5 * * * * /home/sol/bam-leader-activity/role-bam-sync.sh >> /home/sol/logs/role_bam_sync.log 2>&1

# Daily leader revenue summary — fires 18:15 America/Chicago (gated to primary)
CRON_TZ=America/Chicago
15 18 * * * /home/sol/bam-leader-activity/daily-summary.sh
```

**Verify** which entries are already installed:
```bash
crontab -l 2>/dev/null | grep -E "monitor_log_errors|hourly_log_error_summary|bam-log-monitor|role-bam-sync|daily-summary" || echo "MISSING: no matching cron entries"
crontab -l 2>/dev/null | grep -q "^CRON_TZ=America/Chicago" && echo "OK: CRON_TZ set" || echo "MISSING: CRON_TZ=America/Chicago"
```

**Fix if missing** — `crontab -e` and add only the lines that didn't appear
above. Do NOT replace the whole crontab. The `CRON_TZ=America/Chicago` line
must sit immediately above the `daily-summary.sh` entry (any later entries
will also inherit that TZ — add `CRON_TZ=UTC` below them if that's wrong).

Notes:

- `CRON_TZ=America/Chicago` applies to every entry **below** it, so keep
  the daily-summary entry last (or duplicate `CRON_TZ=UTC` before any
  later entries that must stay in UTC).
- `monitor_log_errors.sh` lives in `/home/sol/bash/` (not this repo). If
  you don't use it, drop that line.
- `hourly_log_error_summary.py` needs whatever API credentials the script
  expects (check the file for `os.environ` reads) — configure before
  enabling.

## 10. Daily revenue summary — how it works

- `leader-capture-monitor.sh` appends one JSON line per capture to
  `/home/sol/bam-leader-activity/daily_totals.jsonl`, tagged with a
  `central_day` label anchored to **18:15 America/Chicago**. CDT/CST is
  handled automatically by `TZ=America/Chicago`.
- Each per-rotation Discord embed already shows the rolling subtotal for
  the current central day.
- The cron entry above fires `daily-summary.sh` at 18:15 CT. At that moment
  the day that just closed has label = yesterday (CT). The script sums all
  ledger lines with that label and posts a single summary embed.

Manual invocations:

```bash
# Dry run against yesterday
./daily-summary.sh --dry-run

# Re-post for a specific day
./daily-summary.sh --day 2026-04-14
```

The ledger file is append-only history — safe to keep indefinitely, or
rotate/archive monthly if size becomes an issue.

## 11. Smoke tests

```bash
# Role detection (network/role-agnostic — works on every host)
./role-gate.sh; echo "role-gate exit=$? (0=primary, 1=standby, 2=error)"

# Role-driven BAM URL sync
./role-bam-sync.sh

# Capture monitor sees the schedule and picks a window (skips on standby)
./leader-capture-monitor.sh --once --dry-run --verbose

# Daily summary against whatever's in the ledger (skips on standby)
./daily-summary.sh --dry-run

# BAM log monitor one-shot (skips on standby)
./bam-log-monitor.sh --once
```

## 12. What's intentionally NOT automated

- Secrets (`rpc.conf`, Discord webhook, any API keys) — create by hand.
- `~/999_discord_embed.sh` — you provide the implementation; it can be
  shared across hosts via your own dotfiles repo.
- `~/validator.sh` — the validator startup script itself.
