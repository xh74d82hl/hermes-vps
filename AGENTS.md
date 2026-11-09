# AGENTS.md — hermes-vps

Operating guide for anyone (human or model) touching this server. Read it before changing
anything. It describes the machine as it is, not as it was planned.

## What this is

A self-hosted **Hermes Agent** on a Debian VPS, reachable through Telegram. This repository
holds the install and operations scripts; the agent's data lives outside it, on the box.

- VPS: SSH alias `agent`, admin user `vincenzo`, agent user `hermes` (no sudo, no docker).
- Deployed copy of this repo: `/opt/hermes-vps` (git clone, pulls as `vincenzo`).
- Public repo: `github.com/xh74d82hl/hermes-vps`. Nothing personal goes in it.

## Ground rules

1. **Ask before**: pushing, deleting or rewriting data, touching secrets, restarting services
   that carry a conversation.
2. **Secrets never travel**: not in chat, not in the repo. They live in
   `/home/hermes/.hermes/.env`, `/etc/hermes-backup.env`, `/etc/hermes-alert.env`,
   `~/.hermes/enablebanking/`, and the Google JSON tokens.
3. **Backup before data changes**: `cp -p finance.db finance.db.pre-<what>-<date>` and keep it
   `0600`.
4. **Never invent**: a category without evidence, a number without a query, a "quadra" when it
   does not. Say "non verificabile" instead.
5. **After every change**: `sudo -n bash /opt/hermes-vps/scripts/doctor.sh`,
   `finance-ledger check`, `hermes cron list` (as the agent user).

## Access patterns

```bash
# run something as the agent, with the right runtime dirs
sudo -n -iu hermes env HOME=/home/hermes XDG_RUNTIME_DIR=/run/user/1001 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus bash -lc '<command>'

# the agent's python (has the Google libraries)
/home/hermes/.local/bin/hermes-python

# deploy a file to the clone without fighting ownership
ssh agent "sudo -n tee /opt/hermes-vps/<path> >/dev/null" < <local path>
ssh agent "sudo -n chown vincenzo:vincenzo /opt/hermes-vps/<path>"
```

## Where things live

| Path | What |
|---|---|
| `~/.hermes/config.yaml` | model chain, display, cron |
| `~/.hermes/.env` | 16 keys: Telegram, Groq, opencode-go, Tavily… |
| `~/.hermes/google_token.json` | Google OAuth (not env) |
| `~/.hermes/skills/productivity/{finance,intake,reminders,voice-brief}` | local skills |
| `~/.hermes/scripts/` | `finance-ledger.py`, `hermes-remind.py`, `brief-*.sh` |
| `~/.hermes/finance/finance.db` | the ledger (0600) |
| `~/.hermes/finance/preferences.md` | the user's rules for money, read by the skill |
| `~/.hermes/finance/internal.json` | own name / other banks, never committed |
| `~/.hermes/enablebanking/` | app id, private key, session |
| `/etc/hermes-backup.env` | restic, B2, heartbeat |
| `/etc/hermes-alert.env` | alert bot token |

## What runs on its own

| When | What |
|---|---|
| 03:30 daily | restic backup to B2, heartbeat ping |
| 07:00 daily | morning brief (mail, calendar, tasks) |
| 07:15 daily | the same brief rewritten for voice, sent as a voice note |
| 08:00 daily | resource guard: disk, memory, gateway, backup age, Google and bank consent expiry. Silent unless a threshold is crossed |
| Sat 08:30 | bank sync (Enable Banking) into the ledger |
| Mon 08:00 | finance: classify new merchants, then reconcile, review, report |
| 1st of month 09:00 | recurring charges and category anomalies |
| Sun 09:00 | health digest (doctor, cron, backups, resources, auth) |
| quarterly, 1st 04:30 | restore drill |
| on unit failure | Telegram alert with the last journal lines |

## Finance

### Two sources, not one

| Source | Nature | Contains |
|---|---|---|
| statement PDF | a **snapshot** with a closing balance | transactions booked up to that day |
| bank feed (PSD2) | the **flow**, weekly | later rows, card checks at 0 €, internal transfers with English names |

Period totals **legitimately differ** between the two. The referee is the **balance chain**:
statement closing + later movements must equal the account balance. Never "fix" totals to make
them match.

### Schema

| Object | Role |
|---|---|
| `fact_stmt` | statement rows, immutable |
| `fact_feed` | bank rows, pending included |
| `product_cover` | how far each product is covered by a statement |
| `period` | `reconciled` 0/1 per month, basis (`saldo`), date |
| `txn` | **view** joining both: statement rows, plus feed rows that are after coverage or have no statement twin (matched one-to-one, so two identical purchases stay two) |
| `txn_v1` | the old mixed table, kept as a safety net |
| `merchant` | the only place a category lives; the view reads it from here |

### Commands

```bash
finance-ledger ingest <pdf>          # parse, reconcile the file, merge, rebuild dashboards
finance-ledger report [--month]      # dashboard: dashboard.html + one page per month
finance-ledger check                 # level 1 (file) and level 2 (archive/balance) per statement
finance-ledger periods               # the per-month reconciled flag
finance-ledger reconcile             # statement vs feed: pairs, leftovers, balance chain; writes the flag
finance-ledger reconcile-link <api_id> <stmt_id>   # a pair the human/model judged identical
finance-ledger pending | review | audit | known    # classification work
finance-ledger enrich --limit 15     # pending merchants with search evidence, one payload
finance-ledger classify --key K [--ateco 47.11] [--category C] [--source S] [--confidence F]
finance-ledger classify-batch        # JSON on stdin
finance-ledger literals              # classify what the trade name says outright
finance-ledger repair                # bucket/category inconsistencies, cash withdrawals
finance-ledger query "<select>"      # read-only SQL for ad-hoc questions
finance-ledger bank-sync [--days N]  # pull the feed
finance-ledger account-label "Conto potenziato"
finance-ledger detect <file>         # statement or generic document?
```

### Classification policy

- **Only with evidence**: name that states the trade, chain rule, ATECO code, or a web search.
  Otherwise it stays `Da classificare`. A wrong category poisons every later month.
- `Spesa` = supermarkets and food shops (ATECO 47.11, 47.2x). A generic shop is `Shopping`.
- `Smoking` for tabaccheria/tabacchi/47.26 — it wins over the bar rule.
- `Pagamenti a rate` for PayPal pay-in-3, Klarna, Scalapay: payment methods, not shops.
- `Contanti` for cash withdrawals.
- `Aperitivi e serate` for nights out and alcohol, distinct from `Bar e ristoranti` (lunches).
- Chains are decided by name (Conad, Lidl, Eni, Trenitalia, Netflix, Zara, Ikea, Unieuro…).
- Personal names behind Sumup cannot be resolved by any database: ask the user, once.

### Known limits, measured not assumed

- Revolut PSD2 returns **no time of day and no merchant category**: 0 of 200 rows measured,
  `transaction_date` and `merchant_category_code` always null, `transaction_id` null too. Do
  not promise time-based rules.
- The savings account is exposed as `SVGS` with the holder's name, no product label, and a
  balance of 0.00. Its real balance is derived from the last statement closing plus later
  movements, and the dashboard says `derivato`.
- The statement parser understands Revolut's Italian layout only.
- Scanned PDFs are not readable: OCR of a phone photo returns noise. Ask for a screenshot.

### Current state (2026-11-09)

Purchases € 3.329,91 · income € 3.831 · unclassified € 494,52 (31 merchants).
October and November pass the balance check; September is "non verificabile" because its PDF
is gone. Bank consent expires **2027-04-22**; the guard warns 21 days before.
Open: 31 merchants to classify (6 need the user's knowledge), and the `Aperitivi e serate`
category is new, so old bar rows are not migrated yet.

## How to resume with no context

1. `cd /opt/hermes-vps && git pull`
2. `sudo -n bash scripts/doctor.sh` — the machine's own verdict.
3. `finance-ledger check`, `finance-ledger periods`, `finance-ledger pending`, `finance-ledger review`
4. Read `~/.hermes/skills/productivity/finance/SKILL.md` and `~/.hermes/finance/preferences.md`.
5. Ask the user what changed since the date above; do not guess.

## Conventions in this repo

- Scripts are bash or python3 stdlib only; no new dependencies without a reason.
- Every install script is idempotent and lives in `scripts/install-*.sh`.
- Systemd units go in `systemd/`, timers enabled by the installer, failures wired to
  `hermes-alert@%n.service`.
- Comments explain **why**, one line. The code shows what.
- The public repo carries no names, IBANs, merchant lists or data files.
