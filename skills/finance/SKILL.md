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

## Merchants: search the legal name, trust the ATECO code

After every import, this is your job in the same turn:

1. `finance-ledger pending` gives the list, biggest spend first.
2. **A name with a legal form is a registered company, and companies publish an ATECO
   code.** S.r.l., S.r.l.s., S.p.A., S.a.s., S.n.c., "Ditta Individuale", Srl, Spa: for
   those, search the web for the legal name, not the trade name:
   - `<name> codice ateco`
   - `<name> partita iva`
   - useful sources: ufficiocamerale.it, reportaziende.it, ateco.infocamere.it, registroimprese
3. Save the code, never a category you chose yourself:
   ```bash
   finance-ledger classify --key "fabbrica v 184 s r l" --ateco 56.10 --source "<url>"
   ```
   The script maps ATECO to category and **refuses codes it does not know**, so an
   unmapped code stays unclassified instead of becoming a wrong guess.
4. **Spesa means supermarkets and food shops only** (47.11, 47.2x). A generic shop, a
   clothing store, a hardware store: those are Shopping, not Spesa.
5. **Bars and restaurants are 56.x.** A name that merely looks like a bar, with no code
   and no confirmation, is not enough: leave it unclassified.
6. No legal form and nothing found on the web: leave it in `Da classificare`. Ask the
   user only for the biggest amounts (5 at most, with movements and total), and mark
   them asked. Never invent, never guess to fill the table.
7. If you already assigned a category without a code and it turns out wrong:
   `finance-ledger unclassify --key "<key>"`.

Valid categories: Spesa, Bar e ristoranti, Trasporti, Casa e bollette, Salute,
Shopping, Abbonamenti, Viaggi, Svago, Regali, Tasse e burocrazia, Investimenti,
Entrate, Rimborsi, Trasferimenti.

## Other commands

```bash
finance-ledger report              # rebuild the dashboard without importing
finance-ledger list                # statements imported, with reconcile status
finance-ledger pending             # merchants to identify
```
