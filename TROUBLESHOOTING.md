# TROUBLESHOOTING — the traps we actually hit

Symptom first, cause second, fix third. Most of these are one command away.

---

## "hermes: command not found" on the VPS

The CLI lives in the **agent user's** environment, not on the admin user's PATH (by design).

```bash
sudo -iu <AGENT_USER> env HOME=/home/<AGENT_USER> XDG_RUNTIME_DIR=/run/user/$(id -u <AGENT_USER>) bash -l
hermes gateway status
```

## A skill fails with `ModuleNotFoundError: googleapiclient` (or `edge_tts`)

The gateway runs its **own** Python (`~/.hermes/tools/python-*/`), which is not on `PATH` for
plain login shells. Skill scripts launched from a shell therefore pick the wrong interpreter.

Fix: a wrapper that selects the venv which can import the dependency, and use it in the skill's
commands — `install-voice.sh` and the Google skill scripts assume
`~/.local/bin/hermes-python` exists. Copy `examples/hermes-python` to `~/.local/bin/hermes-python`
and `chmod 755` it.

Runtime packages for tools must be installed into the gateway interpreter:

```bash
P=$(echo ~/.hermes/tools/python-*/bin/python3)
~/.hermes/tools/uv-*/uv pip install --python "$P" edge-tts
```

## `hermes config` refuses to save: "has a formatting error"

A YAML edit produced a duplicate key (we managed it by inserting a second `language:`).
Hermes refuses to overwrite a file it cannot parse, and runs on the last good copy.

```bash
hermes config check                      # shows line + reason
hermes config edit                       # fix it
ls -1t ~/.hermes/backups/config/ | head  # newest good copies
```

Always `hermes config check` after editing `config.yaml` by hand.

## Typed messages get a voice reply, or every reply arrives as voice + text

`voice.auto_tts: true` means "read replies aloud" to the runner, while the adapter only voices
voice *input*. The two paths disagree, so you get doubled or unwanted audio.

Fix: `voice.auto_tts: false`, empty/absent `~/.hermes/gateway_voice_mode.json`, then
`hermes gateway restart`. Opt in per chat with `/voice on` (voice in → voice out) or
`/voice tts` (everything voiced). `/voice off` returns to text.

## "I didn't understand" right after a voice note

The transcript was garbage, not the model. Forced language helps; model matters more:

```yaml
stt:
  language: "it"
  groq:
    model: "whisper-large-v3"   # slower, better; turbo guesses on quiet audio
```

## Voice bubble arrives as a plain audio file

Telegram only shows the round voice bubble for Ogg/**Opus**. Install `ffmpeg` (the TTS tool then
transcodes) — `sudo scripts/install-voice.sh` does both.

Log line `TTS wrote mp3 bytes into a .ogg path … transcoding to real Ogg/Opus` is normal.

## Groq returns 429 / "rate limit"

Free tier has daily audio minutes and requests-per-minute caps. Wait, or switch to
`whisper-large-v3-turbo` (cheaper minutes) / a local `faster-whisper` model (CPU cost).

## Telegram bot says nothing at all

1. Is the service up? `systemctl --user status hermes-gateway`
2. Is the token valid? `hermes send -t telegram "ping"` — this bypasses the model entirely.
3. Is the sender authorized? `hermes pairing list`, then `hermes pairing approve telegram <code>`.
4. Check the log: `tail -50 ~/.hermes/logs/agent.log`

## `hermes send` works, but the cron job never arrives

- `hermes cron list` → is `Deliver` exactly `telegram:<chat_id>`?
- `hermes cron runs <id>` → did it run, and with what result?
- Cron posts through `hermes send`, so a wrong chat id looks like "no message", not an error.

## A voice note sent while a reply is still being delivered gets a text answer

Known quirk: queued follow-ups can bypass the auto-TTS branch. Harmless if voice replies are off
(the default). If you use `/voice on` heavily, send the next note after the reply lands.

## Gateway doesn't pick up a config change

`hermes gateway restart` (graceful, drains active turns). A config reload in place is not a
thing: the process re-reads most settings per turn, but provider/base_url/TTS wiring needs the
restart.

## Old model still answering after a switch

`model.default` is per turn; check what is live:

```bash
hermes cron list                       # jobs can pin their own --model
hermes config check
grep -n -A3 '^model:' ~/.hermes/config.yaml
```

## Weird `WARNING … unknown toolsets hermes-teams / hermes-google_chat`

Cosmetic: those platform blocks exist in config but the toolset names don't. Ignore, or clean the
`platform_toolsets` entries.

## `Auxiliary Nous client unavailable` warnings

Only matters if you intended to use Nous Portal. With another provider they are noise.

## Stale update lock

If `hermes update` was interrupted:

```bash
ls -la ~/.hermes/.hermes-update-in-progress.lock   # inspect first
rm ~/.hermes/.hermes-update-in-progress.lock       # only if no update is running
```

## Google stops working after ~7 days

The OAuth app is still in *Testing*: Google expires refresh tokens weekly. Publish the app
(OAuth consent screen → Publish app) and re-run `setup.py --auth-url` / `--auth-code` once.

## Anything inside `~/.hermes/hermes-agent/` disappears after an update

Hermes updates overwrite its own source. Never patch it for permanent changes — put logic in
`~/.hermes/skills/`, in a cron `--script`, or in this repo.

## Memory

`hermes_cli.mem_trim … rss_kib=350000` lines are normal housekeeping on a 4 GB box. The gateway
idles around 350–550 MB. If the OOM killer starts visiting, add swap (bootstrap already adds 2 GB)
and drop `auto_tts` / browser toolsets.
