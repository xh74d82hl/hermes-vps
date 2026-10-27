# SETUP — from a fresh VPS to a talking agent

Every command below is something we actually ran, in this order, on Debian 13. Replace
`<IP>`, `<ADMIN_USER>`, `<AGENT_USER>` (default `hermes`) and `<CHAT_ID>` with your values.

Two rules for the whole document:

- **Never paste a secret into a chat, a shell history or a file in this repo.** Use the
  `read -rsp` recipe in §4/§6.
- Run agent-side commands as the agent user, with its runtime dir, or the user service is
  invisible to you:

```bash
sudo -iu <AGENT_USER> env HOME=/home/<AGENT_USER> XDG_RUNTIME_DIR=/run/user/$(id -u <AGENT_USER>) bash -l
```

---

## 1. Base VPS (~10 min)

On your laptop, in the repo:

```bash
cp vars.example.env vars.env
$EDITOR vars.env        # ADMIN_USER, ADMIN_SSH_PUBKEY (a PUBLIC key), AGENT_USER, TIMEZONE
rsync -a --exclude .git --exclude vars.env ./ root@<IP>:/opt/hermes-vps/
ssh root@<IP> 'cd /opt/hermes-vps && ./bootstrap.sh'
```

`bootstrap.sh` is idempotent. It installs packages, creates the admin user with your key and
passwordless sudo, creates the agent user with **no** sudo/SSH, enables linger for its user
services, adds swap, enables ufw (deny in, SSH only), fail2ban and unattended upgrades.

Verify, **in a new terminal**:

```bash
ssh <ADMIN_USER>@<IP>          # must work, by key
sudo ufw status                # only 22/tcp
free -h                        # swap present
sudo fail2ban-client status sshd
```

Only now lock SSH down:

```bash
sudo /opt/hermes-vps/scripts/harden-ssh.sh
```

Verify from another terminal: `ssh -o PubkeyAuthentication=no root@<IP>` must be refused.

> The repo lives in `/opt/hermes-vps` and is readable by the admin user — that is how you run
> the next scripts without copying files around.

---

## 2. Hermes Agent (~10 min)

```bash
sudo /opt/hermes-vps/scripts/install-hermes.sh
```

The script downloads the installer, prints its SHA256 so you can glance at it, then runs it
non-interactively **as the agent user**. Then switch to that user and finish configuration:

```bash
sudo -iu <AGENT_USER> env XDG_RUNTIME_DIR=/run/user/$(id -u <AGENT_USER>) bash -l
hermes setup              # provider + API key + model
hermes gateway install    # creates the systemd USER service
systemctl --user status hermes-gateway
```

Check that it comes back after a reboot (`sudo reboot`, then `systemctl --user status
hermes-gateway` again — linger was enabled in step 1).

Secrets end up in `~/.hermes/.env` (`0600`). Keep it that way:

```bash
ls -l ~/.hermes/.env          # -rw------- 
```

---

## 3. Chat model

`hermes setup` writes a `model:` block into `~/.hermes/config.yaml`. The shape we use:

```yaml
model:
  default: "deepseek-v4.1-flash"
  provider: "opencode-go"
  base_url: "https://opencode.ai/zen/go/v1"

fallback_providers:
  - provider: "opencode-go"
    model: "beam-free"
```

Notes that cost us time:

- `model.default` (not `model.name`). `hermes config check` validates the file — run it after
  every edit.
- A free model is fine for chat but noticeably weaker at multi-step tool use; keep a stronger
  one as primary and the free one as fallback (or vice versa).
- Test a model without touching the gateway:

```bash
hermes -z "Reply with exactly: model ok"
```

- Model changes are read per turn, but `model.provider`/`base_url` changes want a gateway
  restart: `hermes gateway restart`.

---

## 4. Telegram (~5 min)

1. In Telegram, talk to [@BotFather](https://t.me/BotFather): `/newbot`, pick a name and a
   username, copy the token (`123456789:AA...`).
2. Store the token **without putting it in your shell history**:

```bash
sudo -iu <AGENT_USER> bash -s <<'EOF'
read -rsp 'Telegram bot token: ' T; echo
cp -a ~/.hermes/.env ~/.hermes/.env.bak.$(date +%s)
if grep -q '^TELEGRAM_BOT_TOKEN=' ~/.hermes/.env; then
  sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$T|" ~/.hermes/.env
else
  printf 'TELEGRAM_BOT_TOKEN=%s\n' "$T" >> ~/.hermes/.env
fi
chmod 600 ~/.hermes/.env; echo saved
EOF
```

3. Restart and DM the bot (`/start`). The bot refuses unknown users by default: it answers with
   a **pairing code**. Approve it:

```bash
hermes pairing list                 # shows the pending request + your Telegram user id
hermes pairing approve telegram <code>
```

4. Optional: make that chat the default delivery target so `--deliver telegram` (no chat id)
   works. Either `/set-home` inside the chat, or `hermes send --list telegram` to read the id.

5. Verify delivery without involving the model:

```bash
hermes send -t telegram:<CHAT_ID> "delivery test $(date '+%H:%M')"
```

---

## 5. Gmail + Calendar (~20 min, optional)

This uses **your own** OAuth client, so no third-party app ever holds your mail. Scopes we
grant: `gmail.readonly`, `calendar.readonly`, `calendar.events` — read mail, read calendars,
create/modify events. Nothing can send or delete mail. Tasks and notes are covered at the end of
this section, if you want them.

1. <https://console.cloud.google.com> → new project (e.g. `hermes-agent`).
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen**: User type *External*, app name `hermes`, your address as support
   contact, and add **yourself** as a test user.
   Scopes: `.../auth/gmail.readonly`, `.../auth/calendar.readonly`, `.../auth/calendar.events`.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON.
5. Copy it to the VPS and register it (the file is a credential: `0600`):

```bash
scp ~/Downloads/client_secret_*.json <ADMIN_USER>@<IP>:/tmp/gs.json
sudo -u <AGENT_USER> ~/.local/bin/hermes-python \
  ~/.hermes/skills/productivity/google-workspace/scripts/setup.py --client-secret /tmp/gs.json
rm /tmp/gs.json
```

6. Authorize and verify:

```bash
# as the agent user
~/.local/bin/hermes-python ~/.hermes/skills/productivity/google-workspace/scripts/setup.py --auth-url
# open the URL, approve, copy the code, then:
~/.local/bin/hermes-python ~/.hermes/skills/productivity/google-workspace/scripts/setup.py --auth-code <CODE>
~/.local/bin/hermes-python ~/.hermes/skills/productivity/google-workspace/scripts/setup.py --check        # AUTHENTICATED
~/.local/bin/hermes-python ~/.hermes/skills/productivity/google-workspace/scripts/setup.py --check-live   # LIVE_CHECK_OK
```

7. **Publish the app** (OAuth consent screen → *Publish app*). While the app stays in
   *Testing*, Google expires the refresh token after **7 days** and you re-authorize weekly.
   Publishing with restricted scopes keeps showing an "unverified app" warning — acceptable for
   personal use; full verification (CASA review) is only needed to distribute it publicly.
8. Enable the skill and try it in chat: *"what's on my calendar tomorrow"*, *"add lunch with
   Anna on Friday at 13:00"*, *"any unread mail from my bank?"*.

### Tasks and notes (optional)

Mail and calendar are enough for the daily brief. If you also want the agent to manage reminders
and notes, add three scopes and switch on three APIs. Both steps are required: a scope lets the
agent ask, the API lets Google answer.

1. **Enable the APIs** — <https://console.cloud.google.com/apis/library>, press **Enable** on each:
   - Google Tasks API
   - Google Docs API
   - Google Drive API

2. **Add the scopes** to the list in step 3 above:

   ```
   https://www.googleapis.com/auth/tasks
   https://www.googleapis.com/auth/documents
   https://www.googleapis.com/auth/drive.file
   ```

3. **Authorize again** (steps 6–7). The consent screen re-asks for the permissions you already
   granted; that is how Google refreshes the grant.

4. **Use it**:

   ```bash
   $GAPI tasks lists                                     # your task lists
   $GAPI tasks list                                      # open tasks
   $GAPI tasks create --title "Call the accountant" --due 2026-11-02
   $GAPI tasks complete TASK_ID
   $GAPI docs create --title "Note: tax 2026" --body "first line"
   $GAPI docs append DOC_ID --text "follow-up"
   $GAPI drive search "name contains 'Note:'" --raw-query
   ```

The due date is `YYYY-MM-DD`; Tasks alerts on that date, so anything needing a specific hour
belongs in a calendar event. Notes are ordinary Google Docs, titled `Note: ...` so a search finds
them. Google Keep has no API for personal accounts and cannot be used.

The `drive.file` scope only exposes files the agent created itself — the notes it writes, not your
existing documents. Reading older Docs needs the wider `auth/drive` scope and another authorization.

The two files that matter afterwards:

| Path | What |
|---|---|
| `~/.hermes/google_client_secret.json` | the OAuth client (`0600`) |
| `~/.hermes/google_token.json` | the live grant + refresh token (`0600`) |

Deleting the client **in the console** breaks the grant. Deleting the JSON you downloaded on
your laptop is harmless — the VPS copy is the one in use.

---

## 6. Voice (~15 min, optional)

Speech in uses Groq's Whisper endpoint (free tier, no card). Speech out uses Microsoft Edge
neural voices (free, no account). `ffmpeg` is only there to repackage MP3 into the Ogg/Opus a
Telegram *voice bubble* requires.

1. Install the OS + Python pieces:

```bash
sudo /opt/hermes-vps/scripts/install-voice.sh
```

It installs `ffmpeg`, then `edge-tts` into the interpreter the gateway actually runs (the
`~/.hermes/tools/python-*/` one, **not** the system Python).

2. Groq key (free): <https://console.groq.com> → API Keys → create. Store it like the Telegram
   token, with `GROQ_API_KEY` instead of `TELEGRAM_BOT_TOKEN`.

3. Add the config (see `examples/config.snippets.yaml`):

```yaml
stt:
  enabled: true
  provider: "groq"
  language: "it"          # or "" for auto-detect

tts:
  provider: "edge"
  edge:
    voice: "it-IT-ElsaNeural"

voice:
  auto_tts: false         # no automatic voice replies; opt in per chat with /voice
```

4. Enable the toolsets for Telegram and restart:

```bash
hermes tools enable stt tts --platform telegram
hermes config check
hermes gateway restart
```

5. Send the bot a voice note. Expected: it transcribes and answers **in text**.

### The three voice modes

| Command (in the chat) | Behaviour |
|---|---|
| *(default)* | voice in → text out |
| `/voice on` | voice in → voice out, text stays text |
| `/voice tts` | everything gets a voice reply ("show" mode) |
| `/voice off` | back to text only |

`auto_tts: false` plus an empty `~/.hermes/gateway_voice_mode.json` means *nothing* is voiced
unless you ask. This matters: with `auto_tts: true` the gateway ends up voicing typed messages
too, which is how we got doubled voice+text replies.

### Voice on demand, and for scheduled jobs

Any job or script can push a voice note, independent of chat mode:

```bash
P=~/.hermes/tools/python-*/bin/python3
$P -m edge_tts --voice it-IT-ElsaNeural --text "Buongiorno." --write-media /tmp/a.mp3
ffmpeg -y -loglevel error -i /tmp/a.mp3 -c:a libopus -b:a 48k /tmp/a.ogg
hermes send -t telegram:<CHAT_ID> "MEDIA:/tmp/a.ogg"
```

That is also the whole recipe for an alarm clock: cron + the three lines above, no model call.
(Telegram voice notes notify but don't auto-play; a *ringing* alarm needs a real phone call.)

### Voice gotchas

- Latency per turn is roughly `STT 1–2 s + model 2–5 s + TTS 1–2 s` — walkie-talkie, not a call.
- `whisper-large-v3-turbo` is fast but invents words on quiet/mumbly audio;
  `whisper-large-v3` (`stt.groq.model`) is ~1–2 s slower and noticeably more accurate. Keep the
  `language` hint set; auto-detect misreads short clips.
- Cloud STT uploads your audio to Groq, and TTS sends reply text to Microsoft. Self-hosting both
  is possible (faster-whisper + Piper) at the cost of CPU/RAM — a 2 vCPU box can only afford
  `base`/`small`.

---

## 7. The 07:00 brief (~5 min)

A cron job inside Hermes, delivered to Telegram, using the Google skill:

```bash
hermes cron create "0 7 * * *" "$(cat /opt/hermes-vps/examples/morning-brief.md)" \
  --name morning-brief \
  --deliver telegram:<CHAT_ID> \
  --skill google-workspace \
  --model deepseek-v4.1-flash
```

Then:

```bash
hermes cron list                 # schedule, next run, delivery target, last result
hermes cron run <id>             # run it on the next scheduler tick
hermes cron runs <id>            # history, ok/failed
```

Notes:

- Delivery is independent of chat sessions: a `/clear` or a new session does not change where
  the job posts. Hard-coding `telegram:<CHAT_ID>` is more predictable than `--deliver origin`.
- If the job needs a voice note, drop `--deliver` text output and add a `--script` that writes
  the audio and calls `hermes send ... MEDIA:` (see §6).
- Posting a message and you typing at the same second is fine — two separate chat messages.

---

## 8. Backups (~10 min, optional but recommended)

Restic, client-side encrypted, to Backblaze B2 or any S3-compatible bucket (Hetzner, Wasabi,
Cloudflare R2, MinIO, real AWS S3).

1. Create a **private** bucket and an application key limited to that bucket (read/write;
   delete permission optional).
2. On the VPS:

```bash
sudo cp /opt/hermes-vps/backup.example.env /etc/hermes-backup.env
sudo chmod 600 /etc/hermes-backup.env
sudo $EDITOR /etc/hermes-backup.env     # repository URL, key id, app key, restic passphrase
sudo /opt/hermes-vps/scripts/setup-backup.sh
```

The script writes `/usr/local/sbin/hermes-backup`, dumps live SQLite databases safely, and
installs a nightly timer at 03:30 with a random delay. The unit caps a run at `TimeoutStartSec=2h`:
a unit that hangs on a dead network never fails, so it would never alert either.

3. Verify:

```bash
sudo systemctl start hermes-backup.service
sudo systemctl status hermes-backup.service
sudo sh -c '. /etc/hermes-backup.env; restic snapshots'
systemctl list-timers | grep hermes-backup
```

4. Close the one hole the server cannot close by itself — **a dead VPS sends no alert**.
   Create a free check at [healthchecks.io](https://healthchecks.io) (20 checks, no card), set
   its period to 1 day and a grace of 2 hours, enable the email and/or Telegram integration, then
   put its ping URL in `/etc/hermes-backup.env`:

   ```bash
   sudo nano /etc/hermes-backup.env      # HEARTBEAT_URL=https://hc-ping.com/<uuid>
   ```

   The backup now pings `/start` when it begins, the bare URL on success and `/fail` on error.
   If none arrives, the external service tells you — exactly the case the local alert channel
   cannot cover. It is one `curl`: no model, no tokens.

5. Do one **test restore** into `/tmp` before you trust it.

> `RESTIC_PASSWORD` is the only key to the backup. Lose it and the data is gone forever.
> Store it in your password manager, not only on the VPS.

Repository URL examples:

| Provider | `RESTIC_REPOSITORY` |
|---|---|
| Backblaze B2 | `s3:https://s3.eu-central-003.backblazeb2.com/YOUR-BUCKET/hermes` |
| Hetzner | `s3:https://nbg1.your-objectstorage.com/YOUR-BUCKET/hermes` |
| Cloudflare R2 | `s3:https://<account>.r2.cloudflarestorage.com/YOUR-BUCKET/hermes` |
| AWS S3 | `s3:s3.eu-south-1.amazonaws.com/YOUR-BUCKET/hermes` |

---

## 9. Reminders (~10 min, optional)

"Remind me to call the dentist tomorrow at 9" should ask once if the time is missing, land
somewhere you can see it, and actually fire. Three things have to agree, so one command writes all
three:

| Part | What it is | Why |
|---|---|---|
| clock | a `hermes cron` **one-shot** job, `--no-agent` | fires without the model, so a reminder survives an expired login or an empty quota |
| mirror | Google **Calendar** event (with a real popup reminder) when it has a time, Google **Task** when it only has a day | the phone rings from Calendar; Tasks is the list |
| ledger | `~/.hermes/reminders.db` | so `done` / `snooze` / `rm` cannot update only half of it |

The split is not a preference. The Google Tasks API stores **the date only** — it discards the
time and has no reminder field — so a Task can never ring at 15:30. A Calendar event can.

```bash
sudo /opt/hermes-vps/scripts/install-reminders.sh
```

That installs `/usr/local/bin/hermes-remind`, the CLI at `~/.hermes/scripts/hermes-remind.py`, and
the `reminders` skill that teaches the agent to ask instead of guessing.

```bash
hermes-remind new --text "chiamare il dentista" --at "2026-10-28T09:00:00+01:00"
hermes-remind new --text "rinnovare il passaporto" --at "2026-11-02T09:00:00+01:00" --mirror task
hermes-remind new --text "togliere la pasta" --at "in 9m" --mirror none --voice
hermes-remind list
hermes-remind done r-7f3a
hermes-remind snooze r-7f3a 10m
```

Verify with a real one, two minutes out — and then restart the gateway before it fires, to see the
late path:

```bash
hermes-remind new --text "prova" --at "in 2m" --mirror none
hermes gateway restart          # or stop, wait past the time, start
hermes-remind list              # the row goes pending → fired
hermes cron list                # the job is named remind:r-xxxx
```

What it does not do: recurring reminders (`hermes cron create "every monday 9am"` instead), and
phone calls — a Telegram bot can only send a message or a voice note.

## 10. Statements (optional)

Send a bank statement PDF to the bot and get a reconciled ledger plus a dashboard. The
import is idempotent (same file twice changes nothing) and refuses to lie: every amount
is taken from the running **saldo** chain, not from a guess about columns, and a
statement that does not add up is stored as unreconciled instead of producing
confident wrong totals.

```bash
sudo apt-get install -y poppler-utils      # pdftotext
sudo /opt/hermes-vps/scripts/install-finance.sh
```

Then just send the PDF in chat. The reply is three lines (period, reconciled, spend);
the merchant classification runs in a separate one-shot job so the chat does not sit
there for minutes.

```bash
finance-ledger list                 # statements imported, with reconcile status
finance-ledger report               # rebuild the dashboard
finance-ledger pending              # merchants not classified yet
finance-ledger enrich --limit 15    # pending + web evidence, one payload for the model
finance-ledger known                # export what is already decided
```

The dashboard is a local file, served on demand over a tunnel, never exposed:

```bash
ssh -L 8099:127.0.0.1:8099 agent
sudo -u hermes env HOME=/home/hermes finance-ledger serve
# http://127.0.0.1:8099/dashboard.html
```

**Merchant research needs a search key.** Brave's free tier is gone; Tavily gives 1.000
credits a month without a card. Put `TAVILY_API_KEY=` in `/home/<agent>/.hermes/.env`.
With a key, a company's ATECO code is looked up and mapped to a category (47.11 ->
Spesa, 56.x -> Bar e ristoranti, 47.26 -> Smoking); without one, only names whose trade
is in the name are classified and the rest stay open.

Data stays on the box, 0600, in `/home/<agent>/.hermes/finance/`: `finance.db`,
`known-merchants.md`, `preferences.md` (domain rules read by the skill) and
`internal.json` (your own name and other banks, so their transfers count as internal;
keep it out of the repo).

Limits: the parser understands Revolut's Italian statement layout. Other banks need
their own parser. Partial months are flagged, pocket/deposit moves are excluded from
spending, and a category is only assigned with evidence.

## 11. Failure alerts (~5 min, optional)

systemd can run a script when a unit fails, which is how you learn that a backup died without
checking the box every morning. The sender is plain `curl` against the Telegram Bot API, not
`hermes send`: when something is broken, the gateway may be part of what is broken.

Use a **dedicated bot** so alerts land in their own chat, can be muted separately, and survive the
main bot being reconfigured or revoked.

1. Telegram → **@BotFather** → `/newbot`, username ending in `bot`. Copy the token, then **press
   Start** in the new bot's chat (a bot cannot message you first).
2. Install the units:

   ```bash
   sudo /opt/hermes-vps/scripts/install-alerts.sh
   ```

   It installs `/usr/local/sbin/hermes-alert`, the `hermes-alert@.service` template, and a drop-in
   that makes `hermes-backup.service` call it on failure.

3. Fill in the credentials — `0600 root:root`, never in this repo:

   ```bash
   sudo nano /etc/hermes-alert.env     # ALERT_BOT_TOKEN, ALERT_CHAT_ID (your numeric user id)
   ```

4. Test both paths. First the sender, then a unit that really fails:

   ```bash
   sudo hermes-alert hermes-backup.service
   cat > /tmp/alert-test.service <<'EOF'
   [Unit]
   OnFailure=hermes-alert@%n.service
   [Service]
   ExecStart=/bin/false
   EOF
   sudo cp /tmp/alert-test.service /etc/systemd/system/ && sudo systemctl daemon-reload
   sudo systemctl start alert-test.service; sleep 5
   journalctl -u 'hermes-alert@*' -n 10 --no-pager
   sudo rm /etc/systemd/system/alert-test.service && sudo systemctl daemon-reload
   ```

   Two messages should arrive in the new bot's chat. The second one proves the `OnFailure` hook, not
   just the script.

What this covers: a failed unit, with the last 8 journal lines, deduplicated to one message per
unit per 5 minutes. What it does not cover: the VPS being dead, or the timer never firing — for
that, have the backup ping an external heartbeat service on success and alert on silence.

## 12. Final checklist

```bash
sudo /opt/hermes-vps/scripts/doctor.sh
```

…covers the same things by hand:

- [ ] `ssh -o PubkeyAuthentication=no root@<IP>` refused; `ufw status` = SSH only
- [ ] reboot → `systemctl --user status hermes-gateway` is `active (running)`
- [ ] Telegram text round-trip works
- [ ] voice note → transcribed, answered in text
- [ ] `hermes send -t telegram:<CHAT_ID> "test"` works with no model involved
- [ ] `hermes cron list` shows `morning-brief`, next run 07:00, last run `ok`
- [ ] `hermes pairing list` shows only you
- [ ] `restic snapshots` shows a snapshot; test restore succeeded
- [ ] a failing unit produces a Telegram alert (section 11)
- [ ] a statement PDF sent in chat imports, reconciles and renders a dashboard
- [ ] `hermes-remind new --at "in 2m"` arrives, and `hermes-remind done <id>` clears it
- [ ] `~/.hermes/.env`, `google_token.json`, `/etc/hermes-backup.env` are all `0600`

---

## 13. Order of operations that avoids pain

1. Base OS + hardening **before** installing anything that runs as a service.
2. Hermes + gateway **before** Telegram/Google/voice: every later step is "edit config, restart,
   test in chat".
3. Telegram early — it is your only feedback channel for the rest.
4. Google last among the "online" pieces: it has the most clicks and a 7-day trap if you forget
   to publish the app.
5. Backups after everything works, then never touch the box without checking `restic snapshots`
   the day after.
