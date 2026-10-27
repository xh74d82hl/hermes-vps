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
    # drop the payment processor, keep the merchant that follows it
    s = re.sub(r"\b(sumup|paypal|sq|amex|visa|maestro|nexive|nexi|satispay)\b\s*\*?\s*", " ", s)
    s = re.sub(r"\b(lime\*?|amzn mktp|pos|pagamento|acquisto|addebito)\b", " ", s)
    s = re.sub(r"\b[a-z]{0,3}\d{3,}\b", " ", s)          # store ids / phone numbers
    s = re.sub(r"[^a-zàèéìòù\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] or "sconosciuto"


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


def rekey(conn: sqlite3.Connection) -> int:
    """Recompute merchant keys after a change to the normaliser."""
    rows = conn.execute("SELECT id, merchant_raw, merchant_key FROM txn").fetchall()
    changed = 0
    for r in rows:
        key = merchant_key(r["merchant_raw"])
        if key != r["merchant_key"]:
            conn.execute("UPDATE txn SET merchant_key=? WHERE id=?", (key, r["id"]))
            changed += 1
        conn.execute("INSERT OR IGNORE INTO merchant (key, name) VALUES (?,?)",
                     (key, r["merchant_raw"]))
    conn.execute("DELETE FROM merchant WHERE key NOT IN (SELECT DISTINCT merchant_key FROM txn)")
    conn.commit()
    return changed


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
:root{
  --ink:#191918; --muted:#6f6f6a; --rule:rgba(0,0,0,.09); --paper:#fbfaf7; --card:#fff;
  --accent:#2f4f7a; --warn:#9c4a1f; --ok:#2f6b46;
  --c1:#3a5f8f; --c2:#4f8a8b; --c3:#a8863f; --c4:#a05a4a; --c5:#7c6a9c; --c6:#b9b7ae;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--paper);color:var(--ink);
  font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  padding:40px 28px 64px;max-width:1060px;margin:0 auto;
  -webkit-font-smoothing:antialiased;font-feature-settings:"tnum"}
header{display:flex;align-items:baseline;justify-content:space-between;gap:16px;
  border-bottom:1px solid var(--rule);padding-bottom:14px;margin-bottom:26px}
h1{font-size:20px;font-weight:600;letter-spacing:-.01em;margin:0}
.stamp{color:var(--muted);font-size:12px;letter-spacing:.04em;text-transform:uppercase}
h2{font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);
  margin:38px 0 12px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.kpi{background:var(--card);border:1px solid var(--rule);border-radius:3px;padding:15px 16px 13px}
.kpi .label{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
.kpi .value{font-size:29px;line-height:1.15;margin-top:5px;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.kpi .hint{font-size:12px;color:var(--muted);margin-top:3px}
.kpi.accent{border-left:3px solid var(--accent)}
.cols{display:grid;grid-template-columns:minmax(240px,300px) 1fr;gap:34px;align-items:center}
.donut{display:block;margin:0 auto}
.legend{list-style:none;margin:0;padding:0;font-size:13.5px}
.legend li{display:flex;align-items:center;gap:9px;padding:4px 0}
.legend .dot{width:9px;height:9px;border-radius:2px;flex:none}
.legend .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend .v{font-variant-numeric:tabular-nums;color:var(--muted)}
.bars{list-style:none;margin:0;padding:0}
.bars li{display:grid;grid-template-columns:1fr 96px;gap:12px;align-items:center;padding:6px 0}
.bars .fill{height:9px;background:var(--accent);border-radius:2px;opacity:.85}
.bars .name{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars .v{text-align:right;font-variant-numeric:tabular-nums;font-size:13.5px}
.months{display:flex;gap:26px;align-items:flex-end;height:130px;padding:0 2px}
.months .m{display:flex;flex-direction:column;justify-content:flex-end;align-items:center;
  gap:7px;flex:1;height:100%}
.months .bar{width:100%;max-width:88px;background:var(--accent);border-radius:2px;opacity:.85}
.months .lbl{font-size:12px;color:var(--muted)}
.months .amt{font-size:12.5px;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--rule)}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--rule);font-size:13.5px}
th{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
  background:#f6f5f1;font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.tag{display:inline-block;font-size:11px;letter-spacing:.05em;text-transform:uppercase}
.ok{color:var(--ok)}.warn{color:var(--warn)}.muted{color:var(--muted)}
.empty{color:var(--muted);font-size:13.5px;padding:10px 0}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--rule);
  color:var(--muted);font-size:11.5px;display:flex;justify-content:space-between;gap:12px}
@media (max-width:720px){
  body{padding:24px 16px 48px}
  .kpis{grid-template-columns:repeat(2,1fr)}
  .cols{grid-template-columns:1fr;gap:22px}
  .kpi .value{font-size:24px}
}
"""


def money(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if v < 0 else "") + "€ " + s


def donut(segments: list[tuple[str, float, str]], total: float, center: str) -> str:
    """Segments as (label, value, colour). Part-whole for a handful of categories."""
    r, stroke = 62.0, 26.0
    circ = 2 * 3.141592653589793 * r
    parts, offset = [], 0.0
    for _label, value, colour in segments:
        frac = value / total if total else 0
        length = circ * frac
        parts.append(
            f'<circle r="{r}" cx="90" cy="90" fill="none" stroke="{colour}" stroke-width="{stroke}"'
            f' stroke-dasharray="{length:.2f} {circ - length:.2f}"'
            f' stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 90 90)"></circle>')
        offset += length
    return (
        '<svg class="donut" width="180" height="180" viewBox="0 0 180 180" role="img">'
        f'{"".join(parts)}'
        '<text x="90" y="86" text-anchor="middle" font-size="15" fill="#191918"'
        f' font-variant-numeric="tabular-nums">{html.escape(center)}</text>'
        '<text x="90" y="105" text-anchor="middle" font-size="10.5" fill="#6f6f6a"'
        ' letter-spacing="1.2">TOTALE</text>'
        '</svg>')


def report(conn: sqlite3.Connection, out: Path) -> dict:
    s = summary(conn)
    months = [r["m"] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM txn ORDER BY m")]
    per_month = defaultdict(float)
    for r in conn.execute(
            "SELECT substr(date,1,7) m, SUM(-amount) v FROM txn WHERE bucket='purchase'"
            " GROUP BY m"):
        per_month[r["m"]] = r["v"]

    cats = conn.execute(
        "SELECT category, SUM(-amount) v, COUNT(*) n FROM txn WHERE bucket='purchase'"
        " GROUP BY category ORDER BY v DESC").fetchall()
    palette = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)"]
    top_cats, rest = cats[:5], sum(c["v"] for c in cats[5:])
    if rest > 0:
        top_cats = list(top_cats) + [{"category": "Altre", "v": rest, "n": 0}]
    segments = [(c["category"], c["v"], "#" + col) for c, col in
                zip(top_cats, ["3a5f8f", "4f8a8b", "a8863f", "a05a4a", "7c6a9c", "b9b7ae"])]

    merchants = conn.execute(
        "SELECT m.name, COUNT(*) n, SUM(-t.amount) v FROM txn t"
        " JOIN merchant m ON m.key=t.merchant_key WHERE t.bucket='purchase'"
        " GROUP BY m.key ORDER BY v DESC LIMIT 8").fetchall()
    statements = s["statements"]
    days = 0
    for st in statements:
        if st["period_start"] and st["period_end"]:
            a = date.fromisoformat(st["period_start"])
            b = date.fromisoformat(st["period_end"])
            days += max((b - a).days + 1, 0)
    per_day = s["purchases"] / days if days else 0
    unclassified_pct = (100 * s["unclassified"] / s["purchases"]) if s["purchases"] else 0

    detail = conn.execute(
        "SELECT date, merchant_raw, category, bucket, amount, check_ok FROM txn"
        " ORDER BY date DESC LIMIT 80").fetchall()

    o = [f"<!doctype html><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>Spese</title><style>{CSS}</style>",
         "<header><h1>Spese</h1>"
         f"<div class='stamp'>{len(months)} mesi · aggiornato {date.today().strftime('%d/%m/%Y')}</div>"
         "</header>",
         "<div class='kpis'>",
         f"<div class='kpi accent'><div class='label'>Acquisti</div>"
         f"<div class='value'>{money(s['purchases'])}</div>"
         f"<div class='hint'>esclusi trasferimenti e movimenti interni</div></div>",
         f"<div class='kpi'><div class='label'>Media al giorno</div>"
         f"<div class='value'>{money(per_day)}</div>"
         f"<div class='hint'>su {days} giorni di estratto</div></div>",
         f"<div class='kpi'><div class='label'>Entrate</div>"
         f"<div class='value'>{money(s['income'])}</div>"
         f"<div class='hint'>stipendi e accrediti</div></div>",
         f"<div class='kpi'><div class='label'>Da classificare</div>"
         f"<div class='value'>{unclassified_pct:.0f}%</div>"
         f"<div class='hint'>{money(s['unclassified'])} senza categoria</div></div>",
         "</div>"]

    o.append("<h2>Dove vanno gli acquisti</h2><div class='cols'>")
    o.append(donut(segments, s["purchases"], money(s["purchases"]))
             if segments else "<div class='empty'>nessun acquisto importato</div>")
    o.append("<ul class='legend'>")
    for (label, value, colour), cat in zip(segments, top_cats):
        share = 100 * value / s["purchases"] if s["purchases"] else 0
        o.append(f"<li><span class='dot' style='background:#{colour}'></span>"
                 f"<span class='name'>{html.escape(label)}</span>"
                 f"<span class='v'>{money(value)} · {share:.0f}%</span></li>")
    o.append("</ul></div>")

    o.append("<h2>Top esercenti</h2><ul class='bars'>")
    top_v = max((m["v"] for m in merchants), default=0) or 1
    for m in merchants:
        o.append(f"<li><div><div class='name'>{html.escape(m['name'][:58])}</div>"
                 f"<div class='fill' style='width:{100 * m['v'] / top_v:.1f}%'></div></div>"
                 f"<div class='v'>{money(m['v'])}</div></li>")
    o.append("</ul>")

    if per_month:
        o.append("<h2>Acquisti per mese</h2><div class='months'>")
        top_m = max(per_month.values()) or 1
        for m in sorted(per_month):
            h = max(6, int(100 * per_month[m] / top_m))
            o.append(f"<div class='m'><div class='amt'>{money(per_month[m])}</div>"
                     f"<div class='bar' style='height:{h}%'></div>"
                     f"<div class='lbl'>{m}</div></div>")
        o.append("</div>")

    o.append("<h2>Estratti</h2><table><tr><th>File</th><th>Periodo</th><th class='n'>Righe</th>"
             "<th>Stato</th></tr>")
    for st in statements:
        state = ("<span class='tag ok'>quadra</span>" if st["reconciled"]
                 else "<span class='tag warn'>non quadra</span>")
        if st["partial"]:
            state += " <span class='tag muted'>parziale</span>"
        o.append(f"<tr><td>{html.escape(st['filename'][:44])}</td>"
                 f"<td>{st['period_start']} → {st['period_end']}</td>"
                 f"<td class='n'>{st['rows']}</td><td>{state}</td></tr>")
    o.append("</table>")

    o.append("<h2>Categorie</h2><table><tr><th>Categoria</th><th class='n'>Totale</th>"
             "<th class='n'>Mov.</th><th class='n'>Quota</th></tr>")
    for c in cats:
        cls = " class='warn'" if c["category"] == "Da classificare" else ""
        share = 100 * c["v"] / s["purchases"] if s["purchases"] else 0
        o.append(f"<tr><td{cls}>{html.escape(c['category'])}</td>"
                 f"<td class='n'>{money(c['v'])}</td><td class='n'>{c['n']}</td>"
                 f"<td class='n'>{share:.0f}%</td></tr>")
    o.append("</table>")

    o.append(f"<h2>Altri flussi</h2><table><tr><th>Voce</th><th class='n'>Totale</th></tr>"
             f"<tr><td>Trasferimenti a persone</td><td class='n'>{money(s['transfers'])}</td></tr>"
             f"<tr><td>Movimenti interni (pocket, deposito)</td>"
             f"<td class='n'>{money(s['internal'])}</td></tr>"
             f"<tr><td>Rimborsi</td><td class='n'>{money(s['refunds'])}</td></tr>"
             f"<tr><td>Righe non quadrate</td>"
             f"<td class='n {'warn' if s['bad_rows'] else ''}'>{s['bad_rows']}</td></tr></table>")

    o.append("<h2>Dettaglio</h2><table><tr><th>Data</th><th>Esercente</th><th>Categoria</th>"
             "<th>Tipo</th><th class='n'>Importo</th></tr>")
    for r in detail:
        cls = " class='warn'" if r["category"] == "Da classificare" else ""
        flag = "" if r["check_ok"] else " !"
        o.append(f"<tr><td>{r['date']}{flag}</td><td>{html.escape(r['merchant_raw'][:52])}</td>"
                 f"<td{cls}>{html.escape(r['category'])}</td><td class='muted'>{r['bucket']}</td>"
                 f"<td class='n'>{money(r['amount'])}</td></tr>")
    o.append("</table>")

    o.append(f"<footer><span>finance-ledger · dati locali, nessun cloud</span>"
             f"<span>{len(detail)} movimenti mostrati</span></footer>")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(o), encoding="utf-8")
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

    srv = sub.add_parser("serve", help="serve the dashboard on 127.0.0.1 (use an SSH tunnel)")
    srv.add_argument("--port", type=int, default=8099)
    srv.add_argument("--dir", type=Path, default=DEFAULT_OUT.parent)

    rk = sub.add_parser("rekey", help="recompute merchant keys")
    rk.add_argument("--db", type=Path, default=DEFAULT_DB)

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
    elif a.cmd == "rekey":
        print(f"rekey: {rekey(conn)} righe aggiornate")
    elif a.cmd == "serve":
        import functools
        import http.server
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(a.dir))
        print(f"http://127.0.0.1:{a.port}/dashboard.html  (ctrl-c per fermare)")
        http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
