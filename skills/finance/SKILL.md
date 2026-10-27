---
name: finance
description: "Estratti conto PDF: importali, riconciliali col saldo e aggiorna la dashboard. Usa quando arriva un PDF di estratto conto o l'utente dice 'estratto', 'spese', 'quanto ho speso', 'importa il conto'."
version: 1.0.0
author: Hermes VPS setup
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Finance, Statements, PDF, Budget, Revolut]
    related_skills: [pdf]
---

# Finance

One command does everything; **do not parse the PDF yourself and do not trust any
number you did not get from it**:

```bash
finance-ledger ingest <path-al-pdf>
```

It reads the statement, takes each amount from the running **saldo** chain (not from
a guess about columns), checks every row and every product total against the
statement's own summary, stores the transactions in `~/.hermes/finance/finance.db`,
and rewrites `~/.hermes/finance/dashboard.html`.

## When a PDF arrives in chat

Telegram saves attachments under `~/.hermes/cache/documents/` and gives you the
path. Run:

```bash
finance-ledger ingest "<the path you were given>"
```

Then reply with **at most three short lines**, built only from the JSON it prints.
No tables, no category list, no emoji: the dashboard carries the detail.

- `status: imported` → period, rows, whether it is `partial`, whether it is
  `reconciled`, and the `spend` figure. If `partial` is true, say plainly that the
  month is incomplete (e.g. "1–27 ottobre: mancano gli ultimi giorni").
- `problems` not empty → say the statement **does not add up**, quote the first
  problem, and that the totals are not to be trusted. Do not smooth it over.
- `notes` → mention only if the user asks why a number differs from the app
  (they are gross/net presentation differences that leave the balance correct).
- `status: already-imported` → you have seen this file before; give its period,
  do not re-import.
- `status: unrecognised` → say the PDF does not look like a statement and that
  nothing was saved.

Always attach the dashboard after a successful import, as its own line:

```
MEDIA:~/.hermes/finance/dashboard.html
```

## Guardare i dati quando servono

The dashboard is a file, not a service. To browse it on demand:

```bash
# on the laptop
ssh -L 8099:127.0.0.1:8099 agent
# on the VPS
sudo -u hermes env HOME=/home/hermes finance-ledger serve
# then open http://127.0.0.1:8099/dashboard.html
```

Never bind it to 0.0.0.0 and never expose the port.

## What the numbers mean

| Bucket | Meaning |
|---|---|
| `purchase` | actual spending, the only bucket split by category |
| `transfer` | money sent to people/accounts (excluded from spending) |
| `internal` | pocket/deposit moves inside Revolut (excluded, can be huge) |
| `income` | salary and money in |
| `refund` | reversals and refunds |

A month with €7.000 "out" is normal: most of it is pocket round-trips. Never report
the gross movement as spending.

## Merchants: you do the research, not the user

After every import, run `finance-ledger pending`. That list is your job, in the same
turn, not "next time":

1. **Research first.** For each merchant, use web search. Two or three queries are
   enough: the name plus the city, then the name alone. Chains, utilities, fuel
   stations, pharmacies, supermarkets, transport and delivery are usually obvious
   from the name alone, no search needed.
2. **Classify what you can justify**, one call per batch:
   ```bash
   echo '[{"key":"esselunga milano","name":"Esselunga","category":"Spesa","confidence":0.9}]' \
     | finance-ledger classify-batch
   ```
   Confidence: 0.9 when the search confirms it, 0.6 when the name is unambiguous but
   unverified, and never above 0.5 for a guess you would not defend.
3. **Ask only the unanswerable ones** in a single message: card-processor names like
   `Sumup *lin Liangzhu`, personal names, `Pos1`, generic terminals. Six at most per
   message, each with how many movements and the total, and offer two or three
   plausible categories. Then mark them asked:
   ```bash
   finance-ledger mark-asked "key one" "key two"
   ```
   When the user answers, classify those keys and confirm in one line that they will
   not be asked again.
4. **Never invent a category you cannot justify.** Leaving a merchant in
   `Da classificare` is honest; a wrong category corrupts every future month.

Valid categories: Spesa, Bar e ristoranti, Trasporti, Casa e bollette, Salute,
Shopping, Abbonamenti, Viaggi, Svago, Regali, Tasse e burocrazia, Investimenti,
Entrate, Rimborsi, Trasferimenti.

## Other commands

```bash
finance-ledger report              # rebuild the dashboard without importing
finance-ledger list                # statements imported, with reconcile status
finance-ledger pending             # merchants to identify
```
