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
for p in python3 jq curl bc ping pgrep agave-validator solana solana-keygen; do
  command -v $p >/dev/null && echo "OK: $p" || echo "MISSING: $p"
done
dpkg -s python3-venv >/dev/null 2>&1 && echo "OK: python3-venv" || echo "MISSING: python3-venv"
test -f "$HOME/validator.sh" && echo "OK: ~/validator.sh" || echo "MISSING: ~/validator.sh"
```

`ping` and `pgrep` are used by `set-bam-node.sh` and `lib-env.sh`.
`agave-validator`, `solana`, and `solana-keygen` come from the Solana
install (typically `~/.local/share/solana/install/active_release/bin/`);
if they're missing, install Solana first — nothing in this repo will work
without them. `~/validator.sh` is the startup script `lib-env.sh` falls
back to when the validator isn't currently running.

**Fix if missing:**
```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip jq curl bc iputils-ping procps
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

**Fix if missing — never overwrites an existing file:**
```bash
mkdir -p ~/.config/validator
# cp -n = no-clobber: silently does nothing if rpc.conf already exists
cp -n ~/bam-leader-activity/rpc.conf.example ~/.config/validator/rpc.conf
chmod 600 ~/.config/validator/rpc.conf
$EDITOR ~/.config/validator/rpc.conf   # fill in real values on a fresh file
```

If the verify step above reported `MISSING: <KEY>` for an existing file,
**don't `cp` over it** — open it in your editor and append the missing
keys (find them in `rpc.conf.example`). Existing values are never touched
by this flow.

The template enumerates every key used by repo scripts plus the
host-side keys (Telegram, CoinMarketCap, etc.) that scripts outside
this repo read from the same file. Comment out or remove what you
don't use.

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

The repo ships `install-cron.sh` — an idempotent installer that manages a
marker-delimited block in the `sol` user's crontab. The same set works on
primary and standby; `role-gate.sh` makes each script self-skip when not
the intended role.

**Verify (read-only diff, exit 0 = up to date, 1 = changes pending):**
```bash
~/bam-leader-activity/install-cron.sh --check
```

**Fix (apply changes — shows diff, prompts before writing):**
```bash
~/bam-leader-activity/install-cron.sh        # interactive (recommended)
~/bam-leader-activity/install-cron.sh --yes  # non-interactive (automation)
```

What the installer does:

- Manages a block delimited by `# BEGIN bam-leader-activity (managed by install-cron.sh)`
  and `# END bam-leader-activity`. Re-running replaces the block in place.
- On first install, **absorbs** pre-existing crontab entries that reference
  the managed commands (with their comment + adjacent `CRON_TZ`) so the
  managed block becomes the single source of truth. Cron entries outside
  the block that don't reference managed commands are never touched.
- Brackets the daily-summary entry with `CRON_TZ=America/Chicago` above and
  `CRON_TZ=UTC` below, so entries outside the block stay in their original TZ.
- Pre-flight warns about missing target files (e.g. no Python venv, no
  `~/logs`) but does not block installation.

Managed entries:

| Schedule | Command | Role |
|---|---|---|
| `*/5` | `/home/sol/bash/monitor_log_errors.sh --once` | (external script) |
| `0 * * * *` | `hourly_log_error_summary.py` | primary only |
| `*/5` | `bam-log-monitor.sh --once` | primary only |
| `*/5` | `role-bam-sync.sh` | both — drives BAM by role |
| `15 18 * * *` (America/Chicago) | `daily-summary.sh` | primary only |

To remove: `crontab -e` and delete the lines between the BEGIN/END markers.

Notes:

- `monitor_log_errors.sh` lives in `/home/sol/bash/` (not this repo). If a
  given host doesn't use it, edit the entry out via `crontab -e` after
  install (`install-cron.sh` puts it in unconditionally to match
  org-standard hosts).
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
