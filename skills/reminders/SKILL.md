---
name: reminders
description: "Promemoria che suonano davvero: chiedi cosa/quando, mettili su Google Calendar o Tasks e su Telegram. Usa per 'ricordami', 'remind me', 'promemoria', 'sveglia', 'tra 20 minuti', 'non dimenticare'."
version: 1.0.0
author: Hermes VPS setup
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Reminders, Tasks, Calendar, Cron, Productivity]
    related_skills: [google-workspace]
---

# Reminders

A reminder is three things at once, and only one command writes all three:

1. **the clock** — a `hermes cron` one-shot job that runs `hermes-remind fire <id>`
   with `--no-agent`, so it needs no model and no provider quota to fire;
2. **your mirror** — a Google Calendar event (with a real popup reminder) when the
   reminder has a time, or a Google Task when it only has a day;
3. **the ledger** — `~/.hermes/reminders.db`, which records what was created so
   `done` / `snooze` / `rm` cannot miss half of it.

**Never create the cron job, the Task or the Calendar event yourself.** Use
`hermes-remind`, or the three will drift apart and you will not be able to say
whether a reminder exists.

## The command

```bash
hermes-remind new --text "chiamare il dentista" --at "2026-10-28T09:00:00+01:00"
hermes-remind new --text "rinnovare il passaporto" --at "2026-11-02T09:00:00+01:00" --mirror task
hermes-remind new --text "togliere la pasta" --at "in 9m" --mirror none
hermes-remind list
hermes-remind done r-7f3a
hermes-remind snooze r-7f3a 10m
hermes-remind rm r-7f3a
```

`--at` takes an ISO timestamp **with the offset** (Europe/Rome, e.g. `+01:00` in
winter, `+02:00` in summer) or a relative form (`in 20m`, `in 2h`, `in 1d`).
Never pass natural language (`domani 9`) — resolve it first.

## What to ask, and when to shut up

Extract **what** (the user's own words, never paraphrased) and **when**. Then:

| The user said | Do this |
|---|---|
| an explicit time ("domani alle 15") | create it, no questions, then confirm |
| a relative time ("tra 20 minuti") | create it, no questions |
| a date but no time ("domani", "venerdì") | **ask once**: 09:00, 19:00, or "solo in lista senza ora" |
| a vague slot ("domani mattina", "dopo pranzo") | propose a concrete hour, say it is a guess, ask once |
| no time at all ("ricordami di chiamare Marco") | **ask once**, same three options |
| "ogni lunedì", "tutti i giorni" | say plainly that this is recurring and set it up with `hermes cron create` instead; `hermes-remind` is one-shot |

Hard rules:

- **At most two questions**, total. If the user says "fai tu" / "decidi tu" / "basta
  domande", decide, create, and state exactly what you chose.
- **Never invent a time silently.** A guess is allowed only when it is labelled as one.
- Never add people, places or details they did not say.
- Reply in their language.

## Which mirror

| Situation | Flag |
|---|---|
| has a time of day | `--mirror calendar` (default): event + popup at that minute, free/busy transparent |
| date only, or they said "mettilo in lista" | `--mirror task`: Google Task with that due date |
| they said "non metterlo sul calendario" / it is trivial | `--mirror none`: Telegram only |

The clock is always Telegram, because **the Google Tasks API cannot store a time**
(it keeps the date only, by design). A Calendar event is what makes the phone ring
at 15:30; a Task cannot.

Voice: add `--voice` **only** if they ask for it ("a voce", "mandami un audio",
"chiamami"). A voice note arrives with the text, not instead of it.

## Confirming

After `new`, say one line with what actually exists:

> ⏰ mer 28/10 09:00 — "chiamare il dentista" · calendario + Telegram · `r-7f3a`

If the output contains `warnings` (Calendar or Tasks refused, usually an expired
Google login), say that the **reminder will still fire on Telegram** and only the
calendar/task copy is missing, with the reason. If it contains `error` (the cron
job could not be created), say the opposite, clearly: it is **not** scheduled.

## Afterwards

- "fatto", "fatto grazie", "l'ho chiamato" → `hermes-remind done <ref>` (this also
  completes the Google Task). Use the `r-xxxx` id from the message when they reply
  to it; otherwise a distinctive word from the text is enough.
- "tra 10 minuti", "snooze", "ricordamelo dopo" → `hermes-remind snooze <ref> 10m`.
- "annulla", "lascia perdere" → `hermes-remind rm <ref>`.
- "che promemoria ho?", "cosa devo fare oggi?" → `hermes-remind list`.

Do not complete Google Tasks by hand for these: `done` keeps the ledger honest.
For reminders created outside this skill, `google-workspace` is still the tool.
