---
name: intake
description: "Documenti e link ricevuti in chat: riconosce cosa sono, dice cosa contengono e chiede prima di creare task, eventi o note. Usa quando arriva un PDF, un allegato o un link e non è chiaro cosa farne."
version: 1.0.0
author: Hermes VPS setup
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Documents, PDF, Intake, Tasks, Docs, Links]
    related_skills: [finance, pdf, google-workspace]
---

# Intake

Un file o un link arriva in chat. Prima di fare qualsiasi cosa, **capisci cosa è**, poi
proponi, poi chiedi. Creare un task sbagliato o una nota inutile costa più di una domanda.

## 1. Riconosci la famiglia

```bash
finance-ledger detect "<percorso>"
```

| Risposta | Cosa significa | Cosa fai |
|---|---|---|
| `type: statement` | estratto conto riconosciuto | passa alla skill `finance`: `finance-ledger ingest`, senza chiedere nulla |
| `type: statement-unparsed` | sembra un estratto ma il parser non lo legge | dillo, allega l'anteprima, chiedi conferma prima di qualsiasi altra cosa |
| `type: document` | tutto il resto: fattura, contratto, prenotazione, ricevuta | vai al punto 2 |

Per i link non c'è `detect`: usa `web_extract` (o `curl`) e leggi il contenuto.

## 2. Dì cosa hai capito, in due righe

Sempre: cosa è, e i tre dati che contano se ci sono.

> Fattura Enel, 78,40 €, scadenza 18/11. Contratto di locazione, 4 pagine, canone 900 €/mese.
> Prenotazione hotel, 12-14 dicembre, cancellazione gratuita fino al 10.

Se non capisci il documento, dillo e fermati: meglio "non riesco a leggerlo" che un
riassunto inventato.

## 3. Chiedi, con opzioni chiare

Una domanda sola, con le opzioni che hanno senso per quel documento:

| Documento | Opzioni da proporre |
|---|---|
| fattura, bolletta, multa | "creo un task con scadenza <data>?" / "salvo una nota?" / "niente" |
| prenotazione, biglietto, appuntamento | "creo un evento in calendario il <data>?" / "creo solo il promemoria?" / "niente" |
| contratto, documento da conservare | "salvo una nota in Docs con i dati principali?" / "niente" |
| link | "lo riassumo in una nota?" / "lo aggiungo a un task?" / "niente" |

**Mai creare task, eventi o note senza una risposta.** Nemmeno se sembra ovvio: un
documento può essere una cosa che hai già fatto, o che riguarda qualcun altro.

## 4. Esegui, e conferma in una riga

- task → `google-workspace`, tasks create con titolo, scadenza e nota con il riferimento
- evento → calendar create, durata 30 minuti se non è indicata
- nota → **non copiare il documento**. Titolo, due righe di sintesi, importo o data, e il
  nome del file se serve. I dati sensibili restano dove sono.

Poi una riga: "fatto: task 'pagare Enel' per il 18/11".

## Cosa non fare

- Non classificare un documento come estratto conto solo perché è un PDF: usa `detect`.
- Non chiedere tre cose insieme. Una domanda, tre opzioni.
- Non riversare fatture o contratti in Docs: sono dati personali, e una nota non è un archivio.
- Se il file è già stato importato (estratto conto ripetuto), `finance-ledger` risponde
  `already-imported`: dillo e fermati.
