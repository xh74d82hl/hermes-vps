# hermes-vps

A reproducible, hardened VPS that runs a personal [Hermes Agent](https://hermes-agent.nousresearch.com)
you talk to on Telegram. Built for one tenant per box: a second VPS for family or a second
personality is the same repo with a different `vars.env`.

Tested on **Debian 13** (2 vCPU / 3.8 GB RAM / 50 GB disk). It should also work on Debian 12 and
Ubuntu 22.04+; the scripts only use `apt`, `systemd` and user services.

## What you get

| Layer | What it does |
|---|---|
| Base OS | firewall (SSH only), fail2ban, unattended upgrades, 2 GB swap, key-only SSH, no root login |
| Accounts | `ADMIN_USER` (sudo, your SSH key) + `AGENT_USER` (no sudo, no SSH login, no docker group) |
| Agent | Hermes installed for the agent user, gateway as a systemd **user** service, survives reboot |
| Chat | Telegram bot, access controlled by pairing code (not an open bot) |
| Mail / calendar | Gmail **read-only** + Calendar **read + create events**, via your own Google OAuth client |
| Tasks / notes | Google Tasks (reminders with due dates) + notes as Google Docs, same client (optional) |
| Voice in | Whisper on Groq (free tier, no card) — you send a voice note, it gets transcribed |
| Voice out | Microsoft Edge neural voices (free, no key) — available on demand and to scheduled jobs |
| Schedule | A 07:00 daily brief (unread mail + today's calendar) delivered to Telegram |
| Backups | Optional nightly encrypted [restic](https://restic.net) backup to Backblaze B2 or any S3-compatible bucket |
| Alerts | Telegram message when a unit fails, via a dedicated bot (no gateway dependency) |
| Reminders | "Ricordami di…" → Google Calendar event or Task + a Telegram message at the right minute |

## What it costs

| Item | Cost |
|---|---|
| VPS | ~€5/month, 2 vCPU / 4 GB is plenty |
| Hermes Agent | free (open source) |
| Chat model | depends on provider, e.g. `opencode-go` key with a free or cheap model |
| Speech-to-text | Groq free tier (daily minutes cap) |
| Text-to-speech | Edge TTS: free, no account |
| Backups | B2 ~$6/TB/month, or your own S3 bucket |

## Before you start

1. A fresh VPS with your **SSH public key** installed by the provider (root login by key).
2. An API key for an OpenAI-compatible chat provider (this repo was built with `opencode-go`).
3. A Telegram account, to create a bot with [@BotFather](https://t.me/BotFather) (free, 2 minutes).
4. *Optional:* a free Groq key (voice input), a Google Cloud project (mail/calendar),
   an S3-compatible bucket (backups).

## Quickstart

```bash
cp vars.example.env vars.env   # set ADMIN_USER, ADMIN_SSH_PUBKEY, AGENT_USER, TIMEZONE
rsync -a --exclude .git --exclude vars.env ./ root@<IP>:/opt/hermes-vps/
ssh root@<IP> 'cd /opt/hermes-vps && ./bootstrap.sh'

# in a NEW terminal — must work before you continue:
ssh <ADMIN_USER>@<IP>
sudo /opt/hermes-vps/scripts/harden-ssh.sh
sudo /opt/hermes-vps/scripts/install-hermes.sh
```

Then follow **[SETUP.md](SETUP.md)** step by step: Hermes setup, Telegram, model, Google,
voice, the 07:00 job and backups. **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** collects the
traps we actually hit while building this. **[RESTORE.md](RESTORE.md)** is what you open the day the
VPS dies.

## Layout

```
bootstrap.sh              base OS: packages, users, swap, ufw, fail2ban, upgrades
vars.example.env          template for vars.env (git-ignored, holds your public key only)
scripts/harden-ssh.sh     disable root login + password auth
scripts/install-hermes.sh install Hermes for the unprivileged agent user
scripts/install-tectonic.sh  optional LaTeX engine (CV/PDF generation)
scripts/install-voice.sh  ffmpeg + edge-tts inside the gateway's Python
scripts/setup-backup.sh   nightly restic backup to S3/B2 + systemd timer
scripts/install-alerts.sh Telegram alerts when a unit fails (dedicated bot)
scripts/install-reminders.sh  reminders that fire without the model + Calendar/Tasks mirror
scripts/doctor.sh         read-only health check of the whole box
systemd/                  backup service + timer
examples/                 config snippets, venv wrapper, morning-brief prompt
backup.example.env        template for /etc/hermes-backup.env
alert.example.env         template for /etc/hermes-alert.env (alert bot credentials)
SETUP.md                  the step-by-step walkthrough
TROUBLESHOOTING.md        gotchas, symptoms, fixes
RESTORE.md                recovering the agent after the box dies
```

## Security model

- SSH: keys only, no root, no passwords, `AllowUsers` limited to the admin, fail2ban on.
- The agent user is unprivileged: no sudo, no SSH login, not in the `docker` group
  (that group is root-equivalent).
- Secrets live only in `/home/<AGENT_USER>/.hermes/.env` and `/etc/hermes-backup.env`,
  both `0600`, never in this repo. `vars.example.env` holds a **public** key by design.
- Destructive shell commands and outbound actions stay behind Hermes' approval flow.
- `hermes send` can push a Telegram message without the model in the loop, which is what the
  scheduled jobs and any "alarm" use.

## What is not in this repo

- No `.env`, no tokens, no OAuth client secret, no private keys, no chat IDs.
- No personal data: the job-search assistant this box also runs (`~/jobs`, CVs) is deliberately
  excluded.
- `vars.env`, `jobs/` and every `*.env` except the examples are git-ignored.

## Notes

- Hermes updates itself; anything you patch inside `~/.hermes/hermes-agent/` will be overwritten
  by an update. Keep local changes in `~/.hermes/skills/` or in this repo instead.
- Verified on our box: bootstrap, harden-ssh, install-hermes, install-tectonic, Telegram gateway,
  Google OAuth (read mail, write events), Groq STT, Edge TTS, cron delivery, `hermes send`.
- Not verified here yet: the B2 backup run end-to-end (bucket credentials were not created).

MIT licensed.
