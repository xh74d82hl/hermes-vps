---
name: voice-brief
description: "Riscrive il brief del mattino per l'ascolto e lo manda come nota vocale. Usa quando il brief scritto è già stato consegnato e serve la versione da sentire."
version: 1.0.0
author: Hermes VPS setup
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Voice, Brief, Telegram, TTS]
---

# Voice brief

Il brief scritto è già arrivato in chat. Questo compito produce **un'altra cosa**, non la
sua lettura: una versione pensata per essere ascoltata in macchina o mentre fai altro.

## Cosa leggere

Un solo comando, non improvvisare percorsi:

```bash
~/.hermes/scripts/brief-latest.sh
```

Stampa il testo dell'ultimo brief. Se l'output è vuoto, o contiene un errore di
esecuzione, rispondi `[SILENT]` e fermati.

## Come riscriverla

Non riassumere: **riscrivi per l'orecchio**. Regole dure:

- **90 secondi, massimo 2 minuti.** Oltre, non lo ascolta nessuno. Circa 230 parole.
- **Niente link, niente URL, niente elenchi puntati, niente tabelle, niente markdown.**
- **Massimo tre impegni**, ognuno con l'ora detta a parole: "alle nove e mezza", non "09:30".
- **Un solo numero per frase.** "Hai due cose: la prima alle nove e mezza, la seconda alle tre."
- **Importi a parole**: "ventidue euro", non "€ 22".
- **Frasi corte.** Punti e virgole al posto di parentesi e trattini. Una idea per frase.
- **Pause vere**: metti un punto dove vuoi che respiri. Non usare puntini di sospensione.
- **Taglia l'inutile**: meteo se non cambia niente, mail promozionali, dettagli di cortesia,
  tutto ciò che non richiede un'azione oggi.
- Chiusura in una frase, senza "buona giornata" se suona finto.

Struttura che funziona: cosa c'è oggi (max 3), cosa devi fare tu, una riga di chiusura.

## Come consegnarla

Scrivi il testo riscritto in un file, poi:

```bash
~/.hermes/scripts/brief-speak.sh /tmp/brief-voice-$(date +%F).txt
```

Stampa `MEDIA:/tmp/....ogg`. **La tua risposta finale deve essere solo quella riga**, senza
commenti prima o dopo. Se lo script non stampa niente, rispondi `[SILENT]`.

Le note già in cache: il testo scritto resta in chat, il vocale è per quando non puoi leggere.
