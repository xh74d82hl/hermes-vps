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

## Merchants: search, reason, classify

After every import this is your job in the same turn. You have a search command, so
there is no excuse to leave a name you could have looked up:

```bash
finance-ledger search "TicketSms" --limit 3      # Tavily, no browser
```

It returns title, url and snippet. Two searches are almost always enough: the name
alone, then the name plus the city.

Then:

1. **Reason from the evidence**, not from a code alone. `TicketSms` is a site that
   sells concert and club tickets: Svago. `Mobbi` is a power bank rental: it does not
   fit any category cleanly, so leave it.
2. **Use the exact `key` printed by `pending`** when classifying. Never build the key
   yourself, or the classification lands on nothing:
   ```bash
   finance-ledger classify --key "<key from pending>" --name "Nome leggibile" \
     --category "Svago" --source "ticketsms.it" --confidence 0.9 --by agent
   ```
3. **Companies with a legal form**: prefer the ATECO code when one is found, because
   it is objective:
   ```bash
   finance-ledger ateco --name "Mo Sarpi Srl Milano"     # -> 56.11.12 -> Bar e ristoranti
   finance-ledger classify --key "..." --ateco 56.11.12 --source "<url>"
   ```
   If the code contradicts the evidence (a real estate code for a 4,50 € charge), do
   not use it: it is a different company with the same name.
4. **Spesa means supermarkets and food shops.** A generic shop is Shopping.
5. **Ask the user only when the evidence is absent or contradictory**, five at most,
   with movements and total, and mark them asked. Personal names behind Sumup, `Pos1`,
   generic terminals: those are theirs to answer.
6. Never invent. `Da classificare` is an honest state; a wrong category poisons every
   following month.

Valid categories: Spesa, Bar e ristoranti, Trasporti, Casa e bollette, Salute,
Shopping, Abbonamenti, Viaggi, Svago, Regali, Tasse e burocrazia, Investimenti,
Entrate, Rimborsi, Trasferimenti.

## Other commands

```bash
finance-ledger report              # rebuild the dashboard without importing
finance-ledger list                # statements imported, with reconcile status
finance-ledger pending             # merchants to identify
```
