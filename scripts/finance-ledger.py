#!/usr/bin/env python3
"""finance-ledger — statement PDFs in, reconciled ledger and dashboard out.

The amount of every row is taken from the **running balance**, not from guessing
which amount column it sits in: saldo(after) - saldo(before) is authoritative and
the printed amount must agree with it. A statement that does not close is imported
as `reconciled=0` and says so, instead of quietly producing wrong totals.

Statement sections handled: current account, Pockets, Deposito (each with its own
balance chain). "In sospeso" (pending) and "stornate" (reversed) are counted but
not imported, so a refund cannot inflate a month.

Commands
  ingest  <pdf>...        import + reconcile + rebuild the dashboard (idempotent)
  report  --out FILE      rebuild the dashboard
  list                    statements imported, with their reconcile status
  pending                 merchants the agent still has to identify
  classify --key K --category C [--name N]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

CATEGORIES = [
    "Spesa", "Bar e ristoranti", "Trasporti", "Casa e bollette", "Salute",
    "Shopping", "Abbonamenti", "Viaggi", "Svago", "Regali", "Tasse e burocrazia",
    "Investimenti", "Entrate", "Rimborsi", "Trasferimenti", "Da classificare",
]

MONTHS = {m: i for i, m in enumerate(
    "gen feb mar apr mag giu lug ago set ott nov dic".split(), 1)}
def month_num(name: str) -> int:
    return MONTHS[name.strip()[:3].lower()]


MONTH_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31,
              11: 30, 12: 31}

DATE_RE = re.compile(
    r"^\s*(\d{1,2})\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)\s+(\d{4})\s+(.*)$")
AMOUNT_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")

# Sections: (label in the PDF, product key, has a Saldo column, is imported)
SECTIONS = [
    ("In sospeso", "pending", False, False),
    ("Transazioni del conto", "conto", True, True),
    ("Transazioni dei Pocket", "pocket", True, True),
    ("Transazioni deposito", "deposito", True, True),
    ("Transazioni stornate", "reversed", False, False),
]

INTERNAL_RE = re.compile(
    r"conto potenziato|conto deposito|pocket|^deposito$|accredita eur|"
    r"interessi netti|interessi|top ?up|ricarica|"
    r"investment portfolio|income sorter|rocchi vincenzo|isybank",
    re.I,
)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,}\b")
NOISE_RE = re.compile(
    r"segnala carta|scansiona|revolut bank|pagina \d|assistenza|depositi sono protetti|"
    r"otteni assistenza|sito web|partiva iva|registrazione|\+370", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS statement (
    id            TEXT PRIMARY KEY,          -- sha256 of the file
    filename      TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    period_start  TEXT,
    period_end    TEXT,
    partial       INTEGER NOT NULL DEFAULT 0,
    reconciled    INTEGER NOT NULL DEFAULT 0,
    rows          INTEGER NOT NULL DEFAULT 0,
    problems      TEXT
);
CREATE TABLE IF NOT EXISTS txn (
    id           TEXT PRIMARY KEY,           -- Revolut UUID when present
    statement_id TEXT NOT NULL,
    product      TEXT NOT NULL,              -- conto | pocket | deposito
    date         TEXT NOT NULL,
    amount       REAL NOT NULL,
    balance_after REAL,
    amount_printed REAL,
    check_ok     INTEGER NOT NULL DEFAULT 1,
    merchant_raw TEXT NOT NULL,
    merchant_key TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT 'Da classificare',
    bucket       TEXT NOT NULL DEFAULT 'purchase',
    note         TEXT
);
CREATE TABLE IF NOT EXISTS merchant (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'Da classificare',
    confidence  REAL NOT NULL DEFAULT 0,
    resolved_by TEXT NOT NULL DEFAULT '',
    resolved_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS txn_date ON txn(date);
"""


# --------------------------------------------------------------------------- parse


@dataclass
class Row:
    date: str
    description: str
    amounts: list[float]
    meta: list[str] = field(default_factory=list)
    product: str = ""
    imported: bool = True

    @property
    def uuid(self) -> str:
        for m in self.meta:
            if m.startswith("ID transazione:"):
                return m.split(":", 1)[1].strip()
        return ""

    @property
    def merchant(self) -> str:
        for m in self.meta:
            if m.startswith(("A:", "Da:")):
                return m[2:].strip()
        return self.description.strip()


@dataclass
class Statement:
    path: Path
    period_start: str = ""
    period_end: str = ""
    rows: list[Row] = field(default_factory=list)
    summary: dict[str, dict[str, float]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=lambda: {"pending": 0, "reversed": 0})


def _num(raw: str) -> float:
    return float(raw.replace(".", "").replace(",", "."))


def parse_statement(path: Path) -> Statement:
    text = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                          capture_output=True, text=True, check=True).stdout
    st = Statement(path=path)

    # the statement period lives on the transaction section headers; the plan line at the
    # top of the PDF has its own "dal giorno ... al giorno ..." and is not the statement's
    for line in text.splitlines():
        if not line.strip().startswith(tuple(lbl for lbl, *_ in SECTIONS)):
            continue
        m = re.search(r"dal giorno (\d+) (\w+) (\d{4}) al giorno (\d+) (\w+) (\d{4})", line)
        if not m:
            m = re.search(r"da (\d+) (\w+) (\d{4}) a (\d+) (\w+) (\d{4})", line)
        if m:
            st.period_start = f"{m.group(3)}-{month_num(m.group(2)):02d}-{int(m.group(1)):02d}"
            st.period_end = f"{m.group(6)}-{month_num(m.group(5)):02d}-{int(m.group(4)):02d}"
            break

    # "Riepilogo del saldo": the product name and its four amounts are often on
    # different lines, so keep collecting until four numbers arrive.
    in_summary, pending_key, buf = False, None, []
    for line in text.splitlines():
        if "Riepilogo del saldo" in line:
            in_summary = True
            continue
        if not in_summary:
            continue
        if line.strip().startswith("Il saldo sul tuo"):
            break
        # no \b after "Conto (conto corrente)": ")" followed by a space is not a word boundary
        m = re.match(r"^\s*(Conto \(conto corrente\)|Pocket personali e di gruppo|Deposito|Totale)(?=\s|$)", line)
        if m:
            pending_key = ("conto" if m.group(1).startswith("Conto")
                           else "pocket" if "Pocket" in m.group(1)
                           else "deposito" if m.group(1).startswith("Deposito") else "totale")
            buf = [_num(a.group(1)) for a in AMOUNT_RE.finditer(line)]
        elif pending_key:
            buf += [_num(a.group(1)) for a in AMOUNT_RE.finditer(line)]
        else:
            continue
        if pending_key and len(buf) >= 4:
            st.summary[pending_key] = dict(opening=buf[0], out=buf[1], into=buf[2], closing=buf[3])
            pending_key = None

    product, columns = None, None
    has_balance = False
    current: Row | None = None
    for line in text.splitlines():
        stripped = line.strip()
        for label, key, balance, imported in SECTIONS:
            if stripped.startswith(label):
                product, has_balance = key, balance
                current = None
                break
        if "Denaro in uscita" in line and "Denaro in entrata" in line:
            columns = (line.index("Denaro in uscita"), line.index("Denaro in entrata"))
            current = None
            continue
        if product is None or columns is None:
            continue

        m = DATE_RE.match(line)
        if m:
            amounts = [_num(a.group(1)) for a in AMOUNT_RE.finditer(line)]
            if not amounts:
                current = None
                continue
            rest = m.group(4)
            first = AMOUNT_RE.search(rest)
            desc = (rest[:first.start()] if first else rest).strip()
            imported = product not in ("pending", "reversed")
            current = Row(
                date=f"{m.group(3)}-{month_num(m.group(2)):02d}-{int(m.group(1)):02d}",
                description=desc, amounts=amounts, product=product, imported=imported)
            if imported:
                st.rows.append(current)
            else:
                st.skipped[product] += 1
            continue

        if current is not None and stripped:
            if stripped.startswith(("A:", "Da:", "ID transazione:", "Carta:")):
                current.meta.append(stripped)
            elif current.meta and not NOISE_RE.search(stripped):
                current.meta[-1] += " " + stripped
    return st


def merchant_key(raw: str) -> str:
    s = raw.lower()
    s = re.sub(r"\b(sumup|paypal|sq \*|lime\*|amzn mktp|pos|pagamento|acquisto|addebito)\b", " ", s)
    s = re.sub(r"\b[a-z]*\d{3,}\b", " ", s)
    s = re.sub(r"\*[^*]*$", " ", s)
    s = re.sub(r"[^a-zàèéìòù\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:60] or "sconosciuto"


def bucket_for(amount: float, merchant: str) -> str:
    if INTERNAL_RE.search(merchant):
        return "internal"
    if IBAN_RE.search(merchant):
        return "income" if amount > 0 else "transfer"
    if re.search(r"storno|rimborso|refund", merchant, re.I):
        return "refund"
    return "income" if amount > 0 else "purchase"


# --------------------------------------------------------------------------- db


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def ingest(conn: sqlite3.Connection, pdf: Path) -> dict:
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if conn.execute("SELECT 1 FROM statement WHERE id=?", (digest,)).fetchone():
        row = conn.execute("SELECT * FROM statement WHERE id=?", (digest,)).fetchone()
        return {"status": "already-imported", "filename": row["filename"],
                "period": f"{row['period_start']} → {row['period_end']}",
                "reconciled": bool(row["reconciled"])}

    st = parse_statement(pdf)
    if not st.rows or not st.summary:
        return {"status": "unrecognised", "filename": pdf.name,
                "hint": "nessuna tabella Revolut trovata: serve un estratto conto con saldo per riga"}
    problems: list[str] = []
    # opening balance per product: without it the first transaction of each product has no
    # previous balance and would be silently dropped
    state: dict[str, float] = {k: v["opening"] for k, v in st.summary.items() if k != "totale"}
    added = 0

    for row in st.rows:
        # amount from the balance chain; the printed amount must agree
        if len(row.amounts) >= 2:
            balance_after = row.amounts[-1]
            printed = row.amounts[0]
            previous = state.get(row.product)
            if previous is None:
                state[row.product] = balance_after
                continue  # first row of a product: no prior balance to diff
            amount = round(balance_after - previous, 2)
            ok = abs(abs(amount) - printed) <= 0.01
            state[row.product] = balance_after
        else:
            problems.append(f"{row.date}: riga senza saldo ({row.description[:30]})")
            continue
        if not ok:
            problems.append(
                f"{row.date}: saldo dice {amount:+.2f}, stampato {printed:.2f} ({row.description[:30]})")

        merchant = row.merchant
        key = merchant_key(merchant)
        bucket = bucket_for(amount, merchant)
        # both legs of an internal transfer carry the same UUID, so the product is part of the key
        txid = (f"{row.uuid}:{row.product}" if row.uuid else hashlib.sha1(
            f"{row.product}|{row.date}|{amount}|{key}|{balance_after}".encode()).hexdigest())
        known = conn.execute("SELECT category FROM merchant WHERE key=?", (key,)).fetchone()
        category = known["category"] if known else contract_category(bucket)
        conn.execute(
            "INSERT OR IGNORE INTO txn (id, statement_id, product, date, amount, balance_after,"
            " amount_printed, check_ok, merchant_raw, merchant_key, category, bucket)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, digest, row.product, row.date, amount, balance_after, printed, 1 if ok else 0,
             merchant, key, category, bucket))
        conn.execute("INSERT OR IGNORE INTO merchant (key, name) VALUES (?,?)", (key, merchant))
        added += 1

    # per-product totals must match the statement's own out/in columns
    notes: list[str] = []
    for product, summary in st.summary.items():
        if product == "totale":
            continue
        got = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END),0) o,"
            "       COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) i,"
            "       COALESCE(SUM(amount),0) net"
            " FROM txn WHERE statement_id=? AND product=?", (digest, product)).fetchone()
        d_out, d_in = got["o"] - summary["out"], got["i"] - summary["into"]
        closes = abs(summary["opening"] - summary["out"] + summary["into"] - summary["closing"]) <= 0.01
        if abs(d_out) <= 0.02 and abs(d_in) <= 0.02:
            continue
        if closes and abs(d_out - d_in) <= 0.02:
            # same amount missing on both sides (gross vs net interest): the closing
            # balance is unaffected, so the ledger is still exact
            notes.append(f"{product}: riepilogo lordo/netto {d_out:+.2f} € su uscite ed entrate, "
                         f"saldo invariato")
            continue
        problems.append(
            f"{product}: uscite {got['o']:.2f} vs {summary['out']:.2f}, "
            f"entrate {got['i']:.2f} vs {summary['into']:.2f}")

    # the statement's own summary must close: opening - out + in == closing
    for product, s in st.summary.items():
        if abs(s["opening"] - s["out"] + s["into"] - s["closing"]) > 0.01:
            problems.append(f"riepilogo {product}: non chiude "
                            f"({s['opening']:.2f} - {s['out']:.2f} + {s['into']:.2f} != {s['closing']:.2f})")

    partial = 0
    if st.period_end:
        y, mo, d = (int(x) for x in st.period_end.split("-"))
        if d < MONTH_DAYS[mo] or int(st.period_start[-2:]) > 1:
            partial = 1

    reconciled = 0 if problems else 1
    conn.execute(
        "INSERT INTO statement (id, filename, imported_at, period_start, period_end, partial,"
        " reconciled, rows, problems) VALUES (?,?,?,?,?,?,?,?,?)",
        (digest, pdf.name, date.today().isoformat(), st.period_start, st.period_end,
         partial, reconciled, added, "\n".join((problems + notes)[:20])))
    conn.commit()

    return {
        "status": "imported",
        "filename": pdf.name,
        "period": f"{st.period_start} → {st.period_end}",
        "partial": bool(partial),
        "reconciled": bool(reconciled),
        "rows": added,
        "skipped": st.skipped,
        "problems": problems[:10],
        "notes": notes[:10],
        "spend": round(conn.execute(
            "SELECT COALESCE(SUM(-amount),0) FROM txn WHERE statement_id=? AND bucket='purchase'",
            (digest,)).fetchone()[0], 2),
    }


def contract_category(bucket: str) -> str:
    return {"income": "Entrate", "transfer": "Trasferimenti",
            "internal": "Trasferimenti", "refund": "Rimborsi"}.get(bucket, "Da classificare")


# --------------------------------------------------------------------------- queries


def pending_merchants(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.key, m.name, COUNT(*) n, SUM(-t.amount) spent"
        " FROM txn t JOIN merchant m ON m.key = t.merchant_key"
        " WHERE t.bucket='purchase' AND t.category='Da classificare'"
        " GROUP BY m.key ORDER BY spent DESC").fetchall()


def classify(conn: sqlite3.Connection, key: str, name: str, category: str,
             confidence: float = 1.0, by: str = "manual") -> None:
    if category not in CATEGORIES:
        raise SystemExit(f"categoria sconosciuta: {category}\nvalide: {', '.join(CATEGORIES)}")
    conn.execute(
        "INSERT INTO merchant (key, name, category, confidence, resolved_by, resolved_at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name,"
        " category=excluded.category, confidence=excluded.confidence,"
        " resolved_by=excluded.resolved_by, resolved_at=excluded.resolved_at",
        (key, name, category, confidence, by, date.today().isoformat()))
    conn.execute("UPDATE txn SET category=? WHERE merchant_key=?", (category, key))
    conn.commit()


def summary(conn: sqlite3.Connection) -> dict:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    sp = lambda b, s: round(one(
        "SELECT COALESCE(SUM(-amount),0) FROM txn WHERE bucket=? AND amount<0" + s, b), 2)
    return {
        "purchases": sp("purchase", ""),
        "transfers": sp("transfer", ""),
        "internal": sp("internal", ""),
        "income": round(one("SELECT COALESCE(SUM(amount),0) FROM txn WHERE bucket='income'"), 2),
        "refunds": round(one("SELECT COALESCE(SUM(amount),0) FROM txn WHERE bucket='refund'"), 2),
        "unclassified": round(one(
            "SELECT COALESCE(SUM(-amount),0) FROM txn"
            " WHERE bucket='purchase' AND category='Da classificare'"), 2),
        "bad_rows": one("SELECT COUNT(*) FROM txn WHERE check_ok=0"),
        "statements": [dict(r) for r in conn.execute(
            "SELECT filename, period_start, period_end, partial, reconciled, rows, problems"
            " FROM statement ORDER BY period_start")],
    }


# --------------------------------------------------------------------------- dashboard

CSS = """
:root{--ink:#1b1b1d;--muted:#6c6c72;--line:#e4e4ea;--accent:#0b5cad;--warn:#a3441a;--ok:#1d7a4a}
*{box-sizing:border-box}
body{margin:0;padding:32px;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     color:var(--ink);background:#fafafa}
h1{font-size:22px;margin:0 0 2px}h2{font-size:13px;margin:30px 0 8px;text-transform:uppercase;
     letter-spacing:.07em;color:var(--muted)}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.grid{display:flex;gap:14px;flex-wrap:wrap}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:150px}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.card .v{font-size:22px;font-variant-numeric:tabular-nums;margin-top:2px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:7px 11px;text-align:left;border-bottom:1px solid var(--line);font-size:13.5px}
th{background:#f3f3f6;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
td.n{text-align:right;font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:none}
.bar{height:7px;background:var(--accent);border-radius:4px}
.ok{color:var(--ok)}.warn{color:var(--warn)}
footer{margin-top:28px;color:var(--muted);font-size:11.5px}
"""


def money(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if v < 0 else "") + "€ " + s


def report(conn: sqlite3.Connection, out: Path) -> dict:
    s = summary(conn)
    months = [r["m"] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM txn ORDER BY m")]
    by_month = conn.execute(
        "SELECT substr(date,1,7) m, bucket, SUM(-amount) v FROM txn WHERE amount<0"
        " GROUP BY m, bucket ORDER BY m").fetchall()
    per_month = defaultdict(lambda: defaultdict(float))
    for r in by_month:
        per_month[r["m"]][r["bucket"]] += r["v"]

    cats = conn.execute(
        "SELECT category, SUM(-amount) v, COUNT(*) n FROM txn"
        " WHERE bucket='purchase' GROUP BY category ORDER BY v DESC").fetchall()
    top = conn.execute(
        "SELECT m.name, COUNT(*) n, SUM(-t.amount) v FROM txn t JOIN merchant m ON m.key=t.merchant_key"
        " WHERE t.bucket='purchase' GROUP BY m.key ORDER BY v DESC LIMIT 15").fetchall()
    pending = pending_merchants(conn)
    recent = conn.execute(
        "SELECT date, merchant_raw, category, bucket, amount, check_ok FROM txn"
        " ORDER BY date DESC LIMIT 60").fetchall()

    parts = [f"<!doctype html><meta charset='utf-8'><title>Finance</title><style>{CSS}</style>",
             "<h1>Finance</h1>",
             f"<div class='sub'>{len(months)} mesi · {s['purchases'] and ''}"
             f"{money(s['purchases'])} di acquisti · {money(s['unclassified'])} da classificare</div>",
             "<div class='grid'>"]
    for label, value in (("Acquisti", s["purchases"]), ("Entrate", s["income"]),
                         ("Trasferimenti a persone", s["transfers"]),
                         ("Movimenti interni (esclusi)", s["internal"]),
                         ("Rimborsi", s["refunds"])):
        parts.append(f"<div class='card'><div class='k'>{label}</div>"
                     f"<div class='v'>{money(value)}</div></div>")
    parts.append(f"<div class='card'><div class='k'>Righe non quadrate</div>"
                 f"<div class='v {'warn' if s['bad_rows'] else 'ok'}'>{s['bad_rows']}</div></div></div>")

    parts.append("<h2>Estratti importati</h2><table><tr><th>File</th><th>Periodo</th><th>Righe</th>"
                 "<th>Stato</th></tr>")
    for st in s["statements"]:
        state = ("<span class='ok'>quadra</span>" if st["reconciled"]
                 else f"<span class='warn'>non quadra</span>")
        if st["partial"]:
            state += " <span class='muted'>· parziale</span>"
        parts.append(f"<tr><td>{html.escape(st['filename'][:44])}</td>"
                     f"<td>{st['period_start']} → {st['period_end']}</td>"
                     f"<td class='n'>{st['rows']}</td><td>{state}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Mesi</h2><table><tr><th></th>"
                 + "".join(f"<th class='n'>{m}</th>" for m in months) + "</tr>")
    for bucket, label in (("purchase", "Acquisti"), ("transfer", "Trasferimenti"),
                          ("internal", "Interni"), ("income", "Entrate")):
        parts.append(f"<tr><td>{label}</td>"
                     + "".join(f"<td class='n'>{money(per_month[m].get(bucket, 0))}</td>"
                               for m in months) + "</tr>")
    parts.append("</table>")

    parts.append("<h2>Acquisti per categoria</h2><table><tr><th>Categoria</th><th>Totale</th>"
                 "<th>Mov.</th><th></th></tr>")
    top_val = max((c["v"] for c in cats), default=1) or 1
    for c in cats:
        width = int(100 * c["v"] / top_val)
        cls = " class='warn'" if c["category"] == "Da classificare" else ""
        parts.append(f"<tr><td{cls}>{html.escape(c['category'])}</td>"
                     f"<td class='n'>{money(c['v'])}</td><td class='n'>{c['n']}</td>"
                     f"<td style='width:35%'><div class='bar' style='width:{width}%'></div></td></tr>")
    parts.append("</table>")

    parts.append(f"<h2>Da classificare ({len(pending)})</h2>")
    if pending:
        parts.append("<table><tr><th>Esercente</th><th>Mov.</th><th>Totale</th></tr>")
        for p in pending[:40]:
            parts.append(f"<tr><td>{html.escape(p['name'][:66])}</td><td class='n'>{p['n']}</td>"
                         f"<td class='n'>{money(-p['spent'])}</td></tr>")
        parts.append("</table>")

    parts.append("<h2>Top esercenti</h2><table><tr><th>Esercente</th><th>Mov.</th><th>Totale</th></tr>")
    for t in top:
        parts.append(f"<tr><td>{html.escape(t['name'][:66])}</td><td class='n'>{t['n']}</td>"
                     f"<td class='n'>{money(-t['v'])}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Ultimi movimenti</h2><table><tr><th>Data</th><th>Esercente</th>"
                 "<th>Categoria</th><th>Tipo</th><th>Importo</th></tr>")
    for r in recent:
        cls = " class='warn'" if r["category"] == "Da classificare" else ""
        flag = "" if r["check_ok"] else " ⚠"
        parts.append(f"<tr><td>{r['date']}{flag}</td><td>{html.escape(r['merchant_raw'][:52])}</td>"
                     f"<td{cls}>{html.escape(r['category'])}</td><td>{r['bucket']}</td>"
                     f"<td class='n'>{money(r['amount'])}</td></tr>")
    parts.append("</table>")
    parts.append(f"<footer>finance-ledger · {date.today().isoformat()} · locale, nessun cloud</footer>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(parts), encoding="utf-8")
    return {"dashboard": str(out), **{k: v for k, v in s.items() if k != "statements"}}


# --------------------------------------------------------------------------- cli

DEFAULT_DB = Path.home() / ".hermes" / "finance" / "finance.db"
DEFAULT_OUT = Path.home() / ".hermes" / "finance" / "dashboard.html"


def main() -> None:
    p = argparse.ArgumentParser(prog="finance-ledger")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="import statement PDFs, reconcile, rebuild the dashboard")
    ing.add_argument("pdfs", nargs="+", type=Path)
    ing.add_argument("--db", type=Path, default=DEFAULT_DB)
    ing.add_argument("--out", type=Path, default=DEFAULT_OUT)

    rep = sub.add_parser("report", help="rebuild the dashboard")
    rep.add_argument("--db", type=Path, default=DEFAULT_DB)
    rep.add_argument("--out", type=Path, default=DEFAULT_OUT)

    ls = sub.add_parser("list", help="statements and reconcile status")
    ls.add_argument("--db", type=Path, default=DEFAULT_DB)

    pen = sub.add_parser("pending", help="merchants still to identify")
    pen.add_argument("--db", type=Path, default=DEFAULT_DB)

    cl = sub.add_parser("classify", help="set a merchant category")
    cl.add_argument("--db", type=Path, default=DEFAULT_DB)
    cl.add_argument("--key", required=True)
    cl.add_argument("--name", default="")
    cl.add_argument("--category", required=True)
    cl.add_argument("--confidence", type=float, default=1.0)

    a = p.parse_args()
    conn = connect(a.db)

    if a.cmd == "ingest":
        results = [ingest(conn, pdf) for pdf in a.pdfs]
        results.append(report(conn, a.out))
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif a.cmd == "report":
        print(json.dumps(report(conn, a.out), indent=2, ensure_ascii=False))
    elif a.cmd == "list":
        print(json.dumps(summary(conn)["statements"], indent=2, ensure_ascii=False))
    elif a.cmd == "pending":
        print(json.dumps([dict(r) for r in pending_merchants(conn)], indent=2, ensure_ascii=False))
    elif a.cmd == "classify":
        classify(conn, a.key, a.name or a.key, a.category, a.confidence)
        print("ok")


if __name__ == "__main__":
    main()
