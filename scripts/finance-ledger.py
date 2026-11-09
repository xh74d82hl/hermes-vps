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
import time
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))

CATEGORIES = [
    "Spesa", "Bar e ristoranti", "Trasporti", "Casa e bollette", "Salute",
    "Shopping", "Abbigliamento", "Casa e arredamento", "Elettronica",
    "Abbonamenti", "Viaggi", "Svago", "Smoking", "Pagamenti a rate", "Contanti",
    "Regali", "Tasse e burocrazia",
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

# Patterns that are specific to one person (own name, own other bank) do not belong in
# a public repo: they live in ~/.hermes/finance/internal.json, which is never committed.
INTERNAL_BASE = (
    r"conto potenziato|conto deposito|pocket|^deposito$|accredita eur|"
    r"interessi netti|interessi|top ?up|ricarica|"
    r"investment portfolio|income sorter|to eur|from eur|to conto|from conto|"
    r"^to |^from "
)


def _own_patterns() -> str:
    path = HERMES_HOME / "finance" / "internal.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    values = list(data.get("names", [])) + list(data.get("banks", []))
    return "|".join(re.escape(str(v)) for v in values if v)


_OWN = _own_patterns()
INTERNAL_RE = re.compile(INTERNAL_BASE + (f"|{_OWN}" if _OWN else ""), re.I)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,}\b")
NOISE_RE = re.compile(
    r"segnala carta|scansiona|revolut bank|pagina \d|assistenza|depositi sono protetti|"
    r"otteni assistenza|sito web|partiva iva|registrazione|\+370", re.I)

# ATECO 2007 -> category. Only unambiguous codes are mapped: a wrong category is worse
# than an unclassified merchant, because it silently poisons every later month.
ATECO_MAP = [
    ("47.11", "Spesa"),            # supermercati, minimarket, discount
    ("47.26", "Smoking"),          # commercio al dettaglio di prodotti del tabacco
    ("47.2", "Spesa"),             # alimentari specializzati (frutta, carne, pane)
    ("47.73", "Salute"), ("47.74", "Salute"), ("47.75", "Salute"),
    ("47.7", "Shopping"), ("47.5", "Shopping"), ("47.4", "Shopping"),
    ("47.6", "Shopping"), ("47.8", "Shopping"), ("47.9", "Shopping"),
    ("47.3", "Shopping"), ("47.1", "Shopping"), ("47.", "Shopping"),
    ("56.", "Bar e ristoranti"),   # ristorazione, bar
    ("55.", "Viaggi"),             # alloggio
    ("49.", "Trasporti"), ("50.", "Trasporti"), ("51.", "Trasporti"),
    ("52.", "Trasporti"), ("53.", "Trasporti"), ("45.", "Trasporti"),
    ("35.", "Casa e bollette"), ("36.", "Casa e bollette"), ("38.", "Casa e bollette"),
    ("68.", "Casa e bollette"), ("41.", "Casa e bollette"), ("43.", "Casa e bollette"),
    ("46.", "Shopping"),           # commercio all'ingrosso
    ("86.", "Salute"), ("87.", "Salute"), ("88.", "Salute"),
    ("90.", "Svago"), ("91.", "Svago"), ("92.", "Svago"), ("93.", "Svago"),
    ("96.", "Svago"),              # servizi alla persona
    ("62.", "Abbonamenti"), ("63.", "Abbonamenti"), ("58.", "Abbonamenti"),
    ("59.", "Abbonamenti"), ("60.", "Abbonamenti"), ("61.", "Abbonamenti"),
    ("73.", "Abbonamenti"), ("74.", "Abbonamenti"), ("85.", "Abbonamenti"),
    ("64.", "Investimenti"), ("65.", "Investimenti"), ("66.", "Investimenti"),
    ("84.", "Tasse e burocrazia"),
]


def category_from_ateco(code: str) -> str:
    """Return the category for an ATECO code, or '' when the code is not mapped.

    Accepts '47.11', '47.11.00', 'G/47.11', '47.1'. An unmapped code returns empty
    so the caller leaves the merchant unclassified instead of guessing.
    """
    clean = re.sub(r"[^0-9.]", "", (code or "").strip())
    if not clean:
        return ""
    for prefix, category in ATECO_MAP:
        if clean.startswith(prefix):
            return category
    return ""


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
    seen          INTEGER NOT NULL DEFAULT 0,
    problems      TEXT,
    summary       TEXT
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
CREATE TABLE IF NOT EXISTS account (
    uid         TEXT PRIMARY KEY,
    name        TEXT,
    kind        TEXT,            -- CACC, SVGS, ...
    iban_last4  TEXT,
    balance     REAL,
    balance_source TEXT,
    label       TEXT,
    currency    TEXT,
    updated_at  TEXT
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
                found = re.search(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", m)
                return found.group(0) if found else ""
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
    try:
        path.chmod(0o600)      # transactions are personal data
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(merchant)")}
    stmt_cols = {r["name"] for r in conn.execute("PRAGMA table_info(statement)")}
    for column, decl in (("seen", "INTEGER NOT NULL DEFAULT 0"), ("summary", "TEXT")):
        if column not in stmt_cols:
            conn.execute(f"ALTER TABLE statement ADD COLUMN {column} {decl}")
    acc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(account)")}
    for column in ("balance_source", "label"):
        if column not in acc_cols:
            conn.execute(f"ALTER TABLE account ADD COLUMN {column} TEXT")
    txn_cols = {r["name"] for r in conn.execute("PRAGMA table_info(txn)")}
    if "bank_status" not in txn_cols:
        conn.execute("ALTER TABLE txn ADD COLUMN bank_status TEXT")
    for column in ("asked_at", "ateco", "source"):
        if column not in cols:
            conn.execute(f"ALTER TABLE merchant ADD COLUMN {column} TEXT")
    conn.commit()
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
    seen = 0
    parsed: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])

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
        cur = conn.execute(
            "INSERT OR IGNORE INTO txn (id, statement_id, product, date, amount, balance_after,"
            " amount_printed, check_ok, merchant_raw, merchant_key, category, bucket)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, digest, row.product, row.date, amount, balance_after, printed, 1 if ok else 0,
             merchant, key, category, bucket))
        conn.execute("INSERT OR IGNORE INTO merchant (key, name) VALUES (?,?)", (key, merchant))
        if cur.rowcount:
            added += 1
        else:
            seen += 1
        if amount < 0:
            parsed[row.product][0] += -amount
        else:
            parsed[row.product][1] += amount

    # per-product totals must match the statement's own out/in columns
    notes: list[str] = []
    for product, summary in st.summary.items():
        if product == "totale":
            continue
        got_o, got_i = parsed.get(product, [0.0, 0.0])
        d_out, d_in = got_o - summary["out"], got_i - summary["into"]
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
            f"{product}: uscite {got_o:.2f} vs {summary['out']:.2f}, "
            f"entrate {got_i:.2f} vs {summary['into']:.2f}")

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
        " reconciled, rows, seen, problems, summary) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (digest, pdf.name, date.today().isoformat(), st.period_start, st.period_end,
         partial, reconciled, added, seen, "\n".join((problems + notes)[:20]),
         json.dumps(st.summary, ensure_ascii=False)))
    conn.commit()

    return {
        "status": "imported",
        "filename": pdf.name,
        "period": f"{st.period_start} → {st.period_end}",
        "partial": bool(partial),
        "reconciled": bool(reconciled),
        "rows": added,
        "already_present": seen,
        "skipped": st.skipped,
        "problems": problems[:10],
        "notes": notes[:10],
        "archive": archive_check(conn, dict(conn.execute(
            "SELECT id, period_start, period_end, summary FROM statement WHERE id=?",
            (digest,)).fetchone())),
        "spend": round(conn.execute(
            "SELECT COALESCE(SUM(-amount),0) FROM txn WHERE statement_id=? AND bucket='purchase'",
            (digest,)).fetchone()[0], 2),
    }


def contract_category(bucket: str) -> str:
    return {"income": "Entrate", "transfer": "Trasferimenti",
            "internal": "Trasferimenti", "refund": "Rimborsi"}.get(bucket, "Da classificare")


# --------------------------------------------------------------------------- queries


def pending_merchants(conn: sqlite3.Connection, include_asked: bool = False) -> list[sqlite3.Row]:
    recent = "" if include_asked else (
        " AND (m.asked_at IS NULL OR m.asked_at < date('now','-30 day'))")
    return conn.execute(
        "SELECT m.key, m.name, m.asked_at, COUNT(*) n, SUM(-t.amount) spent"
        " FROM txn t JOIN merchant m ON m.key = t.merchant_key"
        " WHERE t.bucket='purchase' AND t.category='Da classificare'" + recent +
        " GROUP BY m.key ORDER BY spent DESC").fetchall()


def mark_asked(conn: sqlite3.Connection, keys: list[str]) -> int:
    n = 0
    for key in keys:
        cur = conn.execute("UPDATE merchant SET asked_at=? WHERE key=?",
                           (date.today().isoformat(), key))
        n += cur.rowcount
    conn.commit()
    return n


def classify_batch(conn: sqlite3.Connection, rows: list[dict]) -> int:
    for row in rows:
        classify(conn, row["key"], row.get("name") or row["key"],
                 row.get("category", ""), float(row.get("confidence", 0.8)),
                 row.get("by", "agent"), row.get("ateco", ""), row.get("source", ""))
    return len(rows)


def classify(conn: sqlite3.Connection, key: str, name: str, category: str,
             confidence: float = 1.0, by: str = "manual", ateco: str = "",
             source: str = "") -> None:
    if ateco and not category:
        category = category_from_ateco(ateco)
        if not category:
            raise SystemExit(f"ATECO {ateco} non mappato: lascia l'esercente da classificare")
    if category not in CATEGORIES:
        raise SystemExit(f"categoria sconosciuta: {category}\nvalide: {', '.join(CATEGORIES)}")
    conn.execute(
        "INSERT INTO merchant (key, name, category, confidence, resolved_by, resolved_at,"
        " ateco, source) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
        " name=excluded.name, category=excluded.category, confidence=excluded.confidence,"
        " resolved_by=excluded.resolved_by, resolved_at=excluded.resolved_at,"
        " ateco=COALESCE(NULLIF(excluded.ateco,''), merchant.ateco),"
        " source=COALESCE(NULLIF(excluded.source,''), merchant.source)",
        (key, name, category, confidence, by, date.today().isoformat(), ateco, source))
    conn.execute("UPDATE txn SET category=? WHERE merchant_key=? AND bucket='purchase'",
                 (category, key))
    conn.commit()


def unclassify(conn: sqlite3.Connection, by: str = "", key: str = "") -> int:
    """Undo a classification so it will be researched again."""
    if key:
        cur = conn.execute("UPDATE merchant SET category='Da classificare', confidence=0,"
                           " resolved_by='', ateco=NULL, asked_at=NULL WHERE key=?", (key,))
    else:
        cur = conn.execute("UPDATE merchant SET category='Da classificare', confidence=0,"
                           " resolved_by='', asked_at=NULL WHERE resolved_by=?", (by,))
    conn.execute("UPDATE txn SET category='Da classificare' WHERE bucket='purchase'"
                 " AND merchant_key IN (SELECT key FROM merchant WHERE category='Da classificare')")
    conn.commit()
    return cur.rowcount


# Words that name the trade itself. "Ferramenta X" is a hardware store; asking the web
# is theatre. Ordered: the first match wins, so put the specific ones first.
# Chains whose name alone settles the category: no search, no ATECO, same answer
# every month. Checked before the generic trade words below.
CHAIN_RULES = [
    (r"conad|lidl|esselunga|carrefour|coop\b|eurospin|penny|pam\b|bennet|iper|"
     r"in'?s|sigma|todis|aldi|famila|unisuper|sisa|cecchi|gabrielli|pim\b", "Spesa"),
    (r"eni\b|eni\s?\d|q8|esso|tamoil|\bip\b|repsol|api\b|totalerg|tigros|"
     r"distributore", "Trasporti"),
    (r"trenitalia|italo|atac|flixbus|itabus|busitalia|uber|free now|taxi", "Trasporti"),
    (r"mcdonald|burger king|kfc|pizza hut|roadhouse|old wild west|wagamama", "Bar e ristoranti"),
    (r"netflix|spotify|disney|prime video|amazon prime|dazn|now tv|youtube premium|"
     r"apple\.com/bill|google one|opencode|openai|anthropic", "Abbonamenti"),
    (r"amazon", "Shopping"),
    (r"zara|h&m|\bhm\b|mango|decathlon|zalando|shein|temu|aliexpress", "Abbigliamento"),
    (r"ikea|leroy merlin|brico|obi\b|kasanova|jysk|maisons du monde|mondo convenienza",
     "Casa e arredamento"),
    (r"unieuro|mediaworld|euronics|apple store|samsung|comet\b", "Elettronica"),
    (r"prelievo|prelevamento|cash withdrawal|sportello automatico", "Contanti"),
]

LITERAL_RULES = [
    (r"paga in \d+ rate|pagamento rateale|klarna|scalapay|soisy|affirm", "Pagamenti a rate"),
    (r"ferramenta|brico|fai da te", "Shopping"),
    (r"tabaccheria|tabacchi|tobacco|smoking|sigarette", "Smoking"),
    (r"farmacia|parafarmacia", "Salute"),
    (r"panificio|forno|pasticceria|gelateria|gelato", "Bar e ristoranti"),
    (r"macelleria|pescheria|enoteca|alimentari|gastronomia|salumeria", "Spesa"),
    (r"\bminimarket\b|\bsupermercato|\bsupermercati\b|\bdiscount\b|\bipermercato\b|"
     r"\bmarket\b", "Spesa"),
    (r"bar\b|caffe|caffè|pizzeria|ristorante|trattoria|osteria|pub\b|bistrot|hamburger", "Bar e ristoranti"),
    (r"edicola|libreria|cartoleria", "Shopping"),
    (r"profumeria|gioielleria|ottica", "Shopping"),
    (r"lavanderia|parrucchier|barbier", "Svago"),
    (r"autofficina|gommista|carrozzeria|autolavaggio", "Trasporti"),
    (r"benzina|carburanti|distributore", "Trasporti"),
    (r"hotel|albergo|b&b|affittacamere|agriturismo", "Viaggi"),
]


def literal_category(name: str) -> str:
    low = (name or "").lower()
    for pattern, category in CHAIN_RULES + LITERAL_RULES:
        if re.search(pattern, low):
            return category
    return ""


def _secret(name: str) -> str:
    """Read a key from the environment or from ~/.hermes/.env, never printing it."""
    value = os.environ.get(name, "")
    if value:
        return value
    env_file = HERMES_HOME / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _codes_from_text(text: str) -> dict[str, str]:
    """Pull ATECO codes out of page text and map them, keeping the order of appearance."""
    found: dict[str, str] = {}
    for m in re.finditer(r"ateco\s*:?\s*(\d{2}(?:[.\s]\d{1,2}){0,2}|\d{6})", text, re.I):
        code = m.group(1).replace(" ", "")
        if len(code) == 6 and "." not in code:
            code = f"{code[:2]}.{code[2:4]}.{code[4:]}"
        found.setdefault(code, category_from_ateco(code))
    if any("." in c or len(c) == 6 for c in found):
        found = {c: v for c, v in found.items() if "." in c or len(c) == 6}
    return found


def _tavily(key: str, path: str, body: dict) -> dict:
    import urllib.request

    req = urllib.request.Request(
        f"https://api.tavily.com/{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def ateco_lookup(name: str) -> dict:
    """Find a company's ATECO code with plain HTTP calls, no browser.

    A registry page is the only source that reliably carries the code, so the first
    query is scoped to ufficiocamerale; generic "ateco" queries mostly match other
    companies that happen to be called Ateco.
    """
    queries = [f'site:ufficiocamerale.it "{name}"',
               f'site:reportaziende.it "{name}"',
               f'{name} "codice ateco"']
    key = _secret("TAVILY_API_KEY")
    if key:
        texts: list[str] = []
        urls: list[str] = []
        engine = "tavily"
        try:
            for query in queries:
                data = _tavily(key, "search", {"query": query, "max_results": 5,
                                               "search_depth": "basic"})
                for r in data.get("results", []):
                    texts.append(f"{r.get('title','')} {r.get('content','')} {r.get('url','')}")
                    urls.append(r.get("url", ""))
                codes = _codes_from_text(" ".join(texts))
                if codes:
                    break
            if not codes:
                # the search excerpt is often truncated before the ATECO line
                for url in urls[:4]:
                    if re.search(r"ufficiocamerale|reportaziende|registroaziende|"
                                 r"informazione-aziende|aziende\.it", url):
                        try:
                            page = _tavily(key, "extract", {"urls": [url]})
                            body = " ".join(r.get("raw_content", "") for r in page.get("results", []))
                            texts.append(body)
                            codes = _codes_from_text(body)
                        except Exception:
                            continue
                        if codes:
                            break
            snippets = [t[max(0, t.lower().find("ateco") - 90):
                          t.lower().find("ateco") + 110]
                        for t in texts if "ateco" in t.lower()][:3]
            return {"name": name, "queries": queries, "engine": engine,
                    "codes": codes, "snippets": snippets,
                    "error": "" if codes or texts else "tavily: nessun risultato"}
        except Exception as exc:
            fallback_error = f"tavily: {str(exc)[:80]}"
    else:
        fallback_error = "nessuna chiave TAVILY_API_KEY"

    # Last resort: scrape Brave/DDG HTML. Usually rate-limited from a datacenter IP,
    # so this only helps on a small VPS or a home connection.
    import urllib.error
    import urllib.parse
    import urllib.request

    query = f"{name} codice ateco"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.6",
    }
    errors = [fallback_error]
    for url in ("https://search.brave.com/search?q=" + urllib.parse.quote_plus(query),
                "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote_plus(query)):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=20
            ).read().decode("utf-8", "replace")
        except Exception as exc:
            errors.append(f"{url.split('/')[2]}: {str(exc)[:50]}")
            continue
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
        codes = _codes_from_text(text)
        if codes:
            return {"name": name, "queries": [query], "engine": "html",
                    "codes": codes, "snippets": [], "error": ""}
        errors.append(f"{url.split('/')[2]}: nessun codice")
    return {"name": name, "queries": [query], "engine": "html", "codes": {},
            "snippets": [], "error": "; ".join(errors[:4])}


def search_web(query: str, limit: int = 5) -> list[dict]:
    """Generic web search for the agent. Tavily first, then Brave, then HTML.

    This is what turns "what is this shop?" into one command instead of a browser.
    """
    key = _secret("TAVILY_API_KEY")
    if key:
        try:
            data = _tavily(key, "search", {"query": query, "max_results": limit,
                                           "search_depth": "basic"})
            return [{"title": r.get("title", ""), "url": r.get("url", ""),
                     "snippet": (r.get("content", "") or "")[:400]}
                    for r in data.get("results", [])]
        except Exception as exc:
            first_error = f"tavily: {str(exc)[:80]}"
    else:
        first_error = "nessuna chiave TAVILY_API_KEY"

    brave = _secret("BRAVE_API_KEY")
    if brave:
        import urllib.parse
        import urllib.request
        try:
            payload = urllib.request.urlopen(urllib.request.Request(
                "https://api.search.brave.com/res/v1/web/search?count="
                f"{limit}&q=" + urllib.parse.quote_plus(query),
                headers={"Accept": "application/json",
                         "X-Subscription-Token": brave}), timeout=20).read()
            data = json.loads(payload)
            return [{"title": r.get("title", ""), "url": r.get("url", ""),
                     "snippet": (r.get("description", "") or "")[:400]}
                    for r in (data.get("web", {}) or {}).get("results", [])]
        except Exception as exc:
            first_error += f"; brave: {str(exc)[:80]}"
    return [{"error": first_error}]


def enrich(conn: sqlite3.Connection, limit: int = 15) -> list[dict]:
    """One payload for the model: each pending merchant with its evidence already fetched.

    Without this the agent does one model call per merchant, which is what makes an
    import turn take minutes. Here the searches happen in one process and the model
    decides once.
    """
    rows = pending_merchants(conn)[:limit]
    out = []
    for row in rows:
        evidence = search_web(f"{row['name']}", 3)
        out.append({
            "key": row["key"],
            "name": row["name"],
            "n": row["n"],
            "spent": round(row["spent"], 2),
            "evidence": [{"title": e.get("title", "")[:90],
                          "snippet": e.get("snippet", "")[:180]}
                         for e in evidence if not e.get("error")],
        })
    return out


def _median(values: list[float]) -> float:
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def subscriptions(conn: sqlite3.Connection) -> list[dict]:
    """Recurring charges found in the ledger: same merchant, regular spacing, similar amount.

    Two months of data is enough to see the pattern, so a merchant with two payments about
    a month apart is reported as "probabile" instead of being hidden until month four.
    """
    rows = conn.execute(
        "SELECT merchant_key, merchant_raw, date, amount FROM txn"
        " WHERE bucket='purchase' AND amount < 0 ORDER BY merchant_key, date").fetchall()
    by_key: dict[str, list] = defaultdict(list)
    for r in rows:
        by_key[r["merchant_key"]].append(r)

    out = []
    for key, items in by_key.items():
        if len(items) < 2:
            continue
        dates = [date.fromisoformat(i["date"]) for i in items]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        median_gap = _median(gaps)
        amounts = [abs(i["amount"]) for i in items]
        spread = (max(amounts) - min(amounts)) / max(amounts) if max(amounts) else 0
        if not (5 <= median_gap <= 45) or spread > 0.35:
            continue
        if len(items) >= 3 and 25 <= median_gap <= 35:
            cadence, per_year = "mensile", 12
        elif len(items) == 2 and 25 <= median_gap <= 35:
            cadence, per_year = "probabile mensile", 12
        elif 6 <= median_gap <= 8:
            cadence, per_year = "settimanale", 52
        elif 26 <= median_gap <= 32:
            cadence, per_year = "mensile", 12
        else:
            continue
        avg = sum(amounts) / len(amounts)
        out.append({
            "name": items[0]["merchant_raw"][:44],
            "cadence": cadence,
            "count": len(items),
            "avg": round(avg, 2),
            "last": round(amounts[-1], 2),
            "delta_pct": round(100 * (amounts[-1] - avg) / avg) if avg else 0,
            "yearly": round(avg * per_year, 2),
        })
    return sorted(out, key=lambda x: -x["yearly"])


def anomalies(conn: sqlite3.Connection) -> list[dict]:
    """Categories that moved a lot against the previous months, ignoring partial months."""
    months = [r["m"] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM txn WHERE bucket='purchase' ORDER BY m")]
    if len(months) < 3:
        return []
    current = months[-1]
    baseline_months = months[-4:-1]          # up to three previous months
    per = defaultdict(lambda: defaultdict(float))
    for r in conn.execute(
            "SELECT substr(date,1,7) m, category, SUM(-amount) v FROM txn"
            " WHERE bucket='purchase' GROUP BY m, category"):
        per[r["m"]][r["category"]] += r["v"]
    out = []
    for cat, value in per[current].items():
        base = [per[m].get(cat, 0) for m in baseline_months]
        base = [b for b in base if b > 0]
        if len(base) < 2:
            continue
        mean = sum(base) / len(base)
        if mean <= 0 or value <= mean:
            continue
        change = 100 * (value - mean) / mean
        if change >= 40 and (value - mean) >= 20:
            out.append({"category": cat, "current": round(value, 2),
                        "mean": round(mean, 2), "change_pct": round(change)})
    return sorted(out, key=lambda x: -x["change_pct"])


def insights(conn: sqlite3.Connection) -> str:
    """Plain text for a monthly cron message. Empty string means nothing to say."""
    subs = subscriptions(conn)
    anom = anomalies(conn)
    lines = []
    if subs:
        lines.append("Abbonamenti e addebiti ricorrenti")
        for s in subs[:15]:
            note = ""
            if s["delta_pct"] >= 10:
                note = f", +{s['delta_pct']}% sull'ultimo"
            lines.append(f"- {s['name']}: {money(s['avg'])} {s['cadence']}, "
                         f"~{money(s['yearly'])}/anno{note}")
        lines.append(f"Totale ricorrente stimato: {money(sum(s['yearly'] for s in subs))}/anno")
    if anom:
        lines.append("")
        lines.append("Categorie fuori scala rispetto ai mesi prima")
        for a in anom[:8]:
            lines.append(f"- {a['category']}: {money(a['current'])} contro "
                         f"{money(a['mean'])} di media (+{a['change_pct']}%)")
    return "\n".join(lines)


def detect(path: Path) -> dict:
    """What is this file? Used by the intake skill so a statement is never mistaken
    for a document, and a document never gets parsed as a bank statement."""
    if path.suffix.lower() == ".pdf":
        try:
            st = parse_statement(path)
        except Exception:
            st = None
        if st and st.rows and st.summary:
            return {"type": "statement", "period": f"{st.period_start} -> {st.period_end}",
                    "rows": len(st.rows), "action": "finance-ledger ingest"}
        try:
            text = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                  capture_output=True, text=True, check=True).stdout
        except Exception as exc:
            return {"type": "unreadable", "error": str(exc)[:120]}
    else:
        text = path.read_text(encoding="utf-8", errors="replace")

    preview = re.sub(r"\s+", " ", text).strip()[:400]
    if re.search(r"estratto conto|denaro in uscita|saldo iniziale", text, re.I):
        return {"type": "statement-unparsed", "hint": "sembra un estratto conto ma il parser non lo legge",
                "preview": preview}
    return {"type": "document", "chars": len(text), "preview": preview}


def archive_check(conn: sqlite3.Connection, stmt: dict) -> dict:
    """Level 2: does the database hold exactly this statement for its own period?

    A statement can add up (level 1) and still be wrong in the archive: a duplicate, a
    row lost to an error, or a second statement covering the same days. This compares
    the stored rows inside the period against the totals the statement printed.
    """
    if not stmt.get("summary"):
        return {"known": False, "problems": ["riepilogo non salvato: ricontrollo impossibile"]}
    try:
        expected = json.loads(stmt["summary"])
    except (TypeError, ValueError):
        return {"known": False, "problems": ["riepilogo illeggibile"]}

    actual: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in conn.execute(
            "SELECT product, amount FROM txn WHERE date BETWEEN ? AND ? AND bucket<>'duplicate'",
            (stmt["period_start"], stmt["period_end"])):
        if r["amount"] < 0:
            actual[r["product"]][0] += -r["amount"]
        else:
            actual[r["product"]][1] += r["amount"]

    problems, notes = [], []
    for product, exp in expected.items():
        if product == "totale":
            continue
        got_o, got_i = actual.get(product, [0.0, 0.0])
        d_out, d_in = got_o - exp["out"], got_i - exp["into"]
        if abs(d_out) <= 0.02 and abs(d_in) <= 0.02:
            continue
        if abs(d_out - d_in) <= 0.02 and abs(d_out) <= 1.0:
            notes.append(f"{product}: differenza lordo/netto {d_out:+.2f}, saldo invariato")
            continue
        problems.append(f"{product}: in archivio {got_o:.2f}/{got_i:.2f}, "
                        f"l'estratto dice {exp['out']:.2f}/{exp['into']:.2f}")
    return {"known": True, "ok": not problems, "problems": problems, "notes": notes}


def backfill_summary(conn: sqlite3.Connection, pdf: Path) -> dict:
    """Store the statement summary for a file imported before it was collected.

    Without the summary the archive check cannot run; the PDF still has it.
    """
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    row = conn.execute("SELECT id, summary FROM statement WHERE id=?", (digest,)).fetchone()
    if not row:
        return {"status": "not-imported", "filename": pdf.name}
    if row["summary"]:
        return {"status": "already-there", "filename": pdf.name}
    st = parse_statement(pdf)
    if not st.summary:
        return {"status": "no-summary-in-file", "filename": pdf.name}
    conn.execute("UPDATE statement SET summary=? WHERE id=?",
                 (json.dumps(st.summary, ensure_ascii=False), digest))
    conn.commit()
    return {"status": "filled", "filename": pdf.name, "products": list(st.summary)}


def review(conn: sqlite3.Connection) -> list[dict]:
    """Classified merchants that were never confirmed: the precision risk."""
    rows = conn.execute(
        "SELECT m.key, m.name, m.category, m.confidence, m.resolved_by,"
        " COUNT(*) n, SUM(-t.amount) v"
        " FROM merchant m JOIN txn t ON t.merchant_key = m.key"
        " WHERE t.bucket='purchase' AND m.category <> 'Da classificare'"
        " AND m.confidence < 0.9"
        " GROUP BY m.key ORDER BY v DESC").fetchall()
    return [dict(r) for r in rows]


def audit(conn: sqlite3.Connection, sample: int = 20, seed: int = 0) -> list[dict]:
    """Random classified purchases, for a human to mark right or wrong.

    Without this, '85% classified' says how many labels exist, not how many are right.
    """
    import random

    rows = conn.execute(
        "SELECT id, date, merchant_raw, category, amount FROM txn"
        " WHERE bucket='purchase' AND category <> 'Da classificare' ORDER BY id").fetchall()
    picked = random.Random(seed).sample(rows, min(sample, len(rows)))
    return [dict(r) for r in picked]


def repair(conn: sqlite3.Connection) -> list[str]:
    """Rows whose bucket contradicts their category, left over from older imports."""
    fixed = []
    for bucket, category in (("transfer", "Trasferimenti"), ("income", "Entrate"),
                             ("refund", "Rimborsi"), ("internal", "Trasferimenti")):
        cur = conn.execute(
            "UPDATE txn SET bucket=?, category=? WHERE bucket='purchase' AND category=?",
            (bucket, category, category))
        if cur.rowcount:
            fixed.append(f"{cur.rowcount} righe da acquisto a {bucket}")
    # cash withdrawn is money that will be spent: show it as Contanti, not as a transfer
    cur = conn.execute(
        "UPDATE txn SET bucket='purchase', category='Contanti' WHERE bucket='transfer'"
        " AND merchant_key IN (SELECT key FROM merchant WHERE category='Contanti')")
    if cur.rowcount:
        fixed.append(f"{cur.rowcount} prelievi spostati in Contanti")
    conn.commit()
    return fixed


EB_HOME = HERMES_HOME / "enablebanking"
EB_BASE = "https://api.enablebanking.com"
EB_PRODUCT = {"CACC": "conto", "SVGS": "deposito"}


def _eb_token() -> str:
    """Sign the short-lived JWT Enable Banking requires, with openssl only."""
    import subprocess
    import tempfile

    app_id = ""
    for line in (EB_HOME / "app.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("APP_ID="):
            app_id = line.split("=", 1)[1].strip()
    if not app_id:
        raise RuntimeError("APP_ID mancante in enablebanking/app.env")

    def b64(data: bytes) -> str:
        import base64
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    now = int(time.time())
    header = b64(json.dumps({"typ": "JWT", "alg": "RS256", "kid": app_id}).encode())
    payload = b64(json.dumps({"iss": "enablebanking.com", "aud": "api.enablebanking.com",
                              "iat": now, "exp": now + 3600}).encode())
    signing_input = f"{header}.{payload}".encode()
    proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign",
                           str(EB_HOME / "private.key")],
                          input=signing_input, capture_output=True, check=True)
    return f"{header}.{payload}.{b64(proc.stdout)}"


def _eb_get(path: str) -> dict:
    import urllib.request
    req = urllib.request.Request(EB_BASE + path,
                                 headers={"Authorization": f"Bearer {_eb_token()}"})
    return json.loads(urllib.request.urlopen(req, timeout=40).read())


def _eb_post(path: str, body: dict) -> dict:
    import urllib.request
    req = urllib.request.Request(
        EB_BASE + path, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_eb_token()}",
                 "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=40).read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read() or b"{}")


def bank_auth() -> dict:
    """Ask for a new consent: prints the URL the user opens once."""
    redirect = "https://example.invalid/callback"
    for line in (EB_HOME / "app.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("REDIRECT_URL="):
            redirect = line.split("=", 1)[1].strip()
    import datetime
    valid = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(days=170)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _eb_post("/auth", {
        "access": {"valid_until": valid},
        "aspsp": {"name": "Revolut", "country": "IT"},
        "state": "hermes",
        "redirect_url": redirect,
        "psu_type": "personal",
    })


def bank_session(code: str) -> dict:
    """Exchange the one-time code for a session and store it, 0600."""
    data = _eb_post("/sessions", {"code": code})
    if data.get("session_id"):
        EB_HOME.mkdir(parents=True, exist_ok=True)
        path = EB_HOME / "session.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
    return data


def _eb_session() -> dict:
    return json.loads((EB_HOME / "session.json").read_text(encoding="utf-8"))


def session_days_left() -> int | None:
    """Days until the bank consent expires, or None when there is no session."""
    import datetime
    try:
        valid = (_eb_session().get("access") or {}).get("valid_until")
    except (OSError, ValueError):
        return None
    if not valid:
        return None
    when = datetime.datetime.fromisoformat(valid.replace("Z", "+00:00"))
    return (when - datetime.datetime.now(datetime.timezone.utc)).days


def reconcile(conn: sqlite3.Connection, days: int = 2) -> list[dict]:
    """Compare the statement (a snapshot) with the bank feed (the flow).

    The statement cannot know about rows pulled later, so totals will differ and that is
    not an error. What matters: (a) each API-only row is either new or a duplicate of a
    statement row under a different name/date, and (b) the balance chain still closes.
    This returns the evidence; the pairing decisions are the agent's job.
    """
    out = []
    for st in conn.execute(
            "SELECT id, period_start, period_end, summary FROM statement"
            " WHERE summary IS NOT NULL ORDER BY period_end"):
        try:
            summary = json.loads(st["summary"])
        except (TypeError, ValueError):
            continue
        extra = [dict(r) for r in conn.execute(
            "SELECT id, product, date, amount, merchant_raw, bank_status FROM txn"
            " WHERE statement_id='bank' AND date BETWEEN ? AND ?",
            (st["period_start"], st["period_end"]))]
        statement_rows = [dict(r) for r in conn.execute(
            "SELECT id, product, date, amount, merchant_raw FROM txn"
            " WHERE statement_id=? AND date BETWEEN ? AND ?",
            (st["id"], st["period_start"], st["period_end"]))]

        candidates = []
        for row in extra:
            d = date.fromisoformat(row["date"])
            for other in statement_rows:
                if other["product"] != row["product"]:
                    continue
                if abs(round(other["amount"], 2) - round(row["amount"], 2)) > 0.01:
                    continue
                if abs((date.fromisoformat(other["date"]) - d).days) > days:
                    continue
                candidates.append({"api": row["id"], "statement": other["id"],
                                   "amount": row["amount"]})
                break
        paired = {c["api"] for c in candidates}
        unmatched = [r for r in extra if r["id"] not in paired]

        # the balance chain is the real referee: statement closing + later movements
        balance_ok = {}
        for product, exp in summary.items():
            if product == "totale":
                continue
            closing = exp.get("closing")
            if closing is None:
                continue
            moved = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM txn WHERE product=? AND date > ? AND bucket<>'duplicate'",
                (product, st["period_end"])).fetchone()[0]
            balance_ok[product] = round(float(closing) + moved, 2)
        out.append({"period": f"{st['period_start']}..{st['period_end']}",
                    "extra_from_bank": len(extra),
                    "likely_duplicates": candidates,
                    "unmatched": unmatched,
                    "balance_if_chain_holds": balance_ok})
    return out


def derived_balance(conn: sqlite3.Connection, product: str) -> tuple[float, str] | None:
    """Balance from the last statement closing plus movements after it.

    Revolut reports 0.00 for the savings account, and the statement is the only place
    where its real balance appears (the "deposito" product).
    """
    best = None
    for row in conn.execute("SELECT period_end, summary FROM statement"
                            " WHERE summary IS NOT NULL ORDER BY period_end"):
        try:
            summary = json.loads(row["summary"])
        except (TypeError, ValueError):
            continue
        closing = (summary.get(product) or {}).get("closing")
        if closing is None:
            continue
        if best is None or row["period_end"] > best[0]:
            best = (row["period_end"], float(closing))
    if best is None:
        return None
    end, closing = best
    moved = conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM txn WHERE product=? AND date > ?"
        " AND bucket<>'duplicate'", (product, end)).fetchone()[0]
    return round(closing + moved, 2), f"derivato: chiusura {end} + movimenti successivi"


def bank_sync(conn: sqlite3.Connection, days_back: int = 30) -> dict:
    """Pull both Revolut accounts into the same txn table the statements fill.

    Rows are keyed by the bank's entry_reference plus the product, which is the same
    UUID the PDF prints: a statement imported later lands on the same rows instead of
    duplicating them.
    """
    import datetime

    session = _eb_session()
    since = (date.today() - timedelta(days=days_back)).isoformat()
    added = updated = 0
    accounts = []

    for acct in session.get("accounts", []):
        uid = acct["uid"]
        product = EB_PRODUCT.get(acct.get("cash_account_type", ""), "conto")
        iban = (acct.get("account_id") or {}).get("iban") or ""

        try:
            bal = _eb_get(f"/accounts/{uid}/balances").get("balances", [])
        except Exception:
            bal = []
        amount = None
        for b in bal:
            if b.get("balance_type") in ("ITAV", "CLAV", "OPAV"):
                amount = float((b.get("balance_amount") or {}).get("amount") or 0)
                break
        source = "api"
        if not amount and product == "deposito":
            derived = derived_balance(conn, product)
            if derived:
                amount, source = derived
        conn.execute(
            "INSERT INTO account (uid, name, kind, iban_last4, balance, balance_source,"
            " currency, updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET"
            " balance=excluded.balance, balance_source=excluded.balance_source,"
            " currency=excluded.currency, updated_at=excluded.updated_at, name=excluded.name",
            (uid, acct.get("name", ""), acct.get("cash_account_type", ""), iban[-4:],
             amount, source, acct.get("currency", "EUR"),
             datetime.datetime.now().astimezone().isoformat(timespec="seconds")))
        accounts.append({"name": acct.get("name"), "type": acct.get("cash_account_type"),
                          "iban_last4": iban[-4:], "balance": amount,
                          "balance_source": source, "product": product})

        key = None
        for _page in range(10):
            path = (f"/accounts/{uid}/transactions?date_from={since}"
                    + (f"&continuation_key={key}" if key else ""))
            data = _eb_get(path)
            for t in data.get("transactions", []):
                ref = t.get("entry_reference") or t.get("transaction_id")
                if not ref:
                    continue
                amt = float((t.get("transaction_amount") or {}).get("amount") or 0)
                if amt == 0:
                    continue          # card checks and authorisations, not movements
                if (t.get("credit_debit_indicator") or "DBIT") == "DBIT":
                    amt = -abs(amt)
                raw = ((t.get("creditor") or {}).get("name")
                       or (t.get("debtor") or {}).get("name") or "").strip()
                if not raw:
                    raw = " ".join(t.get("remittance_information") or [])
                key_name = merchant_key(raw)
                known = conn.execute("SELECT category FROM merchant WHERE key=?",
                                     (key_name,)).fetchone()
                bucket = bucket_for(amt, raw)
                category = known["category"] if known else contract_category(bucket)
                txid = f"{ref}:{product}"
                existing = conn.execute("SELECT id FROM txn WHERE id=?", (txid,)).fetchone()
                if existing:
                    conn.execute("UPDATE txn SET bank_status=? WHERE id=?",
                                 (t.get("status"), txid))
                    updated += 1
                    continue
                conn.execute(
                    "INSERT INTO txn (id, statement_id, product, date, amount, merchant_raw,"
                    " merchant_key, category, bucket, bank_status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (txid, "bank", product, t.get("booking_date") or since, amt, raw,
                     key_name, category, bucket, t.get("status")))
                conn.execute("INSERT OR IGNORE INTO merchant (key, name) VALUES (?,?)",
                             (key_name, raw))
                added += 1
            key = data.get("continuation_key")
            if not key:
                break

    conn.commit()
    return {"accounts": accounts, "added": added, "updated": updated,
            "since": since, "days_left": session_days_left()}


def known(conn: sqlite3.Connection, out: Path | None = None) -> list[dict]:
    """The growing map of merchants already decided, as a file you can read and correct.

    This is a cache, not a skill: the database is what stops a second search, and the
    export exists so a human can audit or fix it. Loading hundreds of names into the
    prompt as a skill would cost tokens on every finance turn and duplicate this.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT name, category, ateco, confidence, resolved_by, source FROM merchant"
        " WHERE category <> 'Da classificare' ORDER BY category, name")]
    if out:
        lines = ["# Esercenti noti", "",
                 f"{len(rows)} esercenti classificati. Generato da finance-ledger.", "",
                 "| Esercente | Categoria | ATECO | Fonte |", "|---|---|---|---|"]
        for r in rows:
            lines.append(f"| {r['name'][:60]} | {r['category']} | {r['ateco'] or ''} | "
                         f"{r['resolved_by']}{(' / ' + r['source'][:40]) if r['source'] else ''} |")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            out.chmod(0o600)
        except OSError:
            pass
    return rows


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


def summary(conn: sqlite3.Connection, month: str = "") -> dict:
    mw = " AND substr(date,1,7)=?" if month else ""
    ma = (month,) if month else ()
    one = lambda sql, *a: conn.execute(sql, a + ma).fetchone()[0]
    sp = lambda b: round(one(
        "SELECT COALESCE(SUM(-amount),0) FROM txn WHERE bucket=? AND amount<0" + mw, b), 2)
    stw = " WHERE substr(period_start,1,7)=?" if month else ""
    stmt_rows = [dict(r) for r in conn.execute(
        "SELECT id, filename, period_start, period_end, partial, reconciled, rows, seen,"
        " problems, summary FROM statement" + stw + " ORDER BY period_start", ma)]
    for row in stmt_rows:
        row["archive"] = archive_check(conn, row)
    return {
        "purchases": sp("purchase"),
        "transfers": sp("transfer"),
        "internal": sp("internal"),
        "income": round(one("SELECT COALESCE(SUM(amount),0) FROM txn WHERE bucket='income'" + mw), 2),
        "refunds": round(one("SELECT COALESCE(SUM(amount),0) FROM txn WHERE bucket='refund'" + mw), 2),
        "unclassified": round(one(
            "SELECT COALESCE(SUM(-amount),0) FROM txn"
            " WHERE bucket='purchase' AND category='Da classificare'" + mw), 2),
        "bad_rows": one("SELECT COUNT(*) FROM txn WHERE check_ok=0" + mw),
        "statements": stmt_rows,
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
.months-nav{display:flex;flex-wrap:wrap;gap:10px;margin:-8px 0 24px}
.months-nav a{font-size:12px;color:var(--muted);text-decoration:none;border:1px solid var(--rule);
  border-radius:3px;padding:3px 8px}
.months-nav a:hover{color:var(--ink);border-color:var(--muted)}
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


MONTH_NAMES = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
               "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def report(conn: sqlite3.Connection, out: Path, month: str = "") -> dict:
    s = summary(conn, month)
    mw = " AND substr(date,1,7)=?" if month else ""
    ma = (month,) if month else ()
    months = [month] if month else [r["m"] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM txn ORDER BY m")]
    per_month = defaultdict(float)
    for r in conn.execute(
            "SELECT substr(date,1,7) m, SUM(-amount) v FROM txn WHERE bucket='purchase'"
            + mw + " GROUP BY m", ma):
        per_month[r["m"]] = r["v"]

    cats = conn.execute(
        "SELECT category, SUM(-amount) v, COUNT(*) n FROM txn WHERE bucket='purchase'"
        + mw + " GROUP BY category ORDER BY v DESC", ma).fetchall()
    # one list drives donut, legend and table: same rows, same colours, same numbers
    segments = [(c["category"], c["v"], f"hsl({(205 + i * 53) % 360} 32% 45%)")
                for i, c in enumerate(cats)]

    merchants = conn.execute(
        "SELECT m.name, COUNT(*) n, SUM(-t.amount) v FROM txn t"
        " JOIN merchant m ON m.key=t.merchant_key WHERE t.bucket='purchase'"
        + mw.replace("date", "t.date") + " GROUP BY m.key ORDER BY v DESC LIMIT 8", ma).fetchall()
    statements = s["statements"]
    covered: set[date] = set()
    for st in statements:
        if st["period_start"] and st["period_end"]:
            a = date.fromisoformat(st["period_start"])
            b = date.fromisoformat(st["period_end"])
            covered.update(a + timedelta(days=i) for i in range(max((b - a).days + 1, 0)))
    if month:
        covered = {d for d in covered if d.strftime("%Y-%m") == month}
    days = len(covered)
    per_day = s["purchases"] / days if days else 0
    unclassified_pct = (100 * s["unclassified"] / s["purchases"]) if s["purchases"] else 0

    detail = conn.execute(
        "SELECT date, merchant_raw, category, bucket, amount, check_ok FROM txn"
        " WHERE bucket NOT IN ('internal','duplicate')" + mw + " ORDER BY date DESC LIMIT 80", ma).fetchall()

    title = "Spese" if not month else f"Spese · {MONTH_NAMES[int(month[5:7])]} {month[:4]}"
    all_months = [r["m"] for r in conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM txn ORDER BY m")]
    links = "".join(
        f"<a href='dashboard-{m}.html'>{MONTH_NAMES[int(m[5:7])][:3]} {m[2:4]}</a>"
        for m in all_months)
    nav = ("<nav class='months-nav'>" + ("<a href='dashboard.html'>tutto</a>" if month else "")
           + links + "</nav>")
    o = [f"<!doctype html><meta charset='utf-8'>"
         f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{title}</title><style>{CSS}</style>",
         f"<header><h1>{title}</h1>"
         f"<div class='stamp'>{len(months)} mesi · aggiornato {date.today().strftime('%d/%m/%Y')}</div>"
         "</header>",
         nav,
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

    if not month:
        accounts = conn.execute(
            "SELECT label, name, kind, balance, balance_source, currency, updated_at"
            " FROM account ORDER BY kind").fetchall()
        if accounts:
            o.append("<h2>Conti</h2><table><tr><th>Conto</th><th class='n'>Saldo</th>"
                     "<th>Fonte</th><th>Aggiornato</th></tr>")
            for a in accounts:
                label = a["label"] or ("Operativo" if a["kind"] == "CACC" else a["kind"])
                src = a["balance_source"] or ""
                o.append(f"<tr><td>{html.escape(label)}</td>"
                         f"<td class='n'>{money(a['balance'] or 0)}</td>"
                         f"<td class='muted'>{html.escape(src[:46])}</td>"
                         f"<td class='muted'>{(a['updated_at'] or '')[:16].replace('T', ' ')}</td></tr>")
            o.append("</table>")

    o.append("<h2>Dove vanno gli acquisti</h2><div class='cols'>")
    o.append(donut(segments, s["purchases"], money(s["purchases"]))
             if segments else "<div class='empty'>nessun acquisto importato</div>")
    o.append("<ul class='legend'>")
    for label, value, colour in segments:
        share = 100 * value / s["purchases"] if s["purchases"] else 0
        o.append(f"<li><span class='dot' style='background:{colour}'></span>"
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

    if per_month and not month:
        o.append("<h2>Acquisti per mese</h2><div class='months'>")
        top_m = max(per_month.values()) or 1
        for m in sorted(per_month):
            h = max(6, int(100 * per_month[m] / top_m))
            o.append(f"<div class='m'><div class='amt'>{money(per_month[m])}</div>"
                     f"<div class='bar' style='height:{h}%'></div>"
                     f"<div class='lbl'>{m}</div></div>")
        o.append("</div>")

    o.append("<h2>Estratti</h2><table><tr><th>Periodo</th><th class='n'>Nuove</th>"
             "<th>Estratto</th><th>In archivio</th></tr>")
    for st in statements:
        file_state = ("<span class='tag ok'>quadra</span>" if st["reconciled"]
                      else "<span class='tag warn'>non quadra</span>")
        if st["partial"]:
            file_state += " <span class='tag muted'>parziale</span>"
        arch = st.get("archive") or {}
        if not arch.get("known"):
            arch_state = "<span class='tag muted'>non verificabile</span>"
        elif arch.get("ok"):
            arch_state = "<span class='tag ok'>quadra</span>"
        else:
            arch_state = "<span class='tag warn'>differenza</span>"
        o.append(f"<tr><td>{st['period_start']} → {st['period_end']}</td>"
                 f"<td class='n'>{st['rows']}</td><td>{file_state}</td>"
                 f"<td>{arch_state}</td></tr>")
    o.append("</table>")

    o.append("<h2>Categorie (le stesse della torta)</h2><table><tr><th></th><th>Categoria</th>"
             "<th class='n'>Totale</th><th class='n'>Mov.</th><th class='n'>Quota</th></tr>")
    for (label, value, colour), c in zip(segments, cats):
        cls = " class='warn'" if label == "Da classificare" else ""
        share = 100 * value / s["purchases"] if s["purchases"] else 0
        o.append(f"<tr><td style='width:14px'><span class='dot' style='background:{colour};"
                 f"display:inline-block'></span></td>"
                 f"<td{cls}>{html.escape(label)}</td>"
                 f"<td class='n'>{money(value)}</td><td class='n'>{c['n']}</td>"
                 f"<td class='n'>{share:.0f}%</td></tr>")
    o.append("</table>")

    o.append(f"<h2>Altri flussi</h2><table><tr><th>Voce</th><th class='n'>Totale</th></tr>"
             f"<tr><td>Trasferimenti a persone</td><td class='n'>{money(s['transfers'])}</td></tr>"
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
    if not month:
        # one page per month beside the complete one, so the SSH service shows them
        for m in [r["m"] for r in conn.execute(
                "SELECT DISTINCT substr(date,1,7) m FROM txn ORDER BY m")]:
            report(conn, out.parent / f"dashboard-{m}.html", month=m)
    try:
        out.chmod(0o600)
    except OSError:
        pass
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
    rep.add_argument("--month", default="", help="YYYY-MM, solo quel mese")

    ls = sub.add_parser("list", help="statements and reconcile status")
    ls.add_argument("--db", type=Path, default=DEFAULT_DB)

    pen = sub.add_parser("pending", help="merchants still to identify")
    pen.add_argument("--db", type=Path, default=DEFAULT_DB)
    pen.add_argument("--new", action="store_true",
                     help="only merchants not asked in the last 30 days")

    srv = sub.add_parser("serve", help="serve the dashboard on 127.0.0.1 (use an SSH tunnel)")
    srv.add_argument("--port", type=int, default=8099)
    srv.add_argument("--dir", type=Path, default=DEFAULT_OUT.parent)

    cb = sub.add_parser("classify-batch", help="classify many merchants from JSON on stdin")
    cb.add_argument("--db", type=Path, default=DEFAULT_DB)

    ma = sub.add_parser("mark-asked", help="remember that these merchants were asked about")
    ma.add_argument("--db", type=Path, default=DEFAULT_DB)
    ma.add_argument("keys", nargs="+")

    rk = sub.add_parser("rekey", help="recompute merchant keys")
    rk.add_argument("--db", type=Path, default=DEFAULT_DB)

    cl = sub.add_parser("classify", help="set a merchant category")
    cl.add_argument("--db", type=Path, default=DEFAULT_DB)
    cl.add_argument("--key", required=True)
    cl.add_argument("--name", default="")
    cl.add_argument("--category", default="", help="omit when --ateco is given")
    cl.add_argument("--ateco", default="", help="ATECO code, e.g. 47.11")
    cl.add_argument("--source", default="", help="where the code came from (url)")
    cl.add_argument("--confidence", type=float, default=1.0)
    cl.add_argument("--by", default="manual")

    for name, helptext in (("insights", "recurring charges and category anomalies"),
                           ("subscriptions", "recurring charges only"),
                           ("anomalies", "category anomalies only")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--db", type=Path, default=DEFAULT_DB)

    ba = sub.add_parser("bank-auth", help="new bank consent: prints the URL")
    bss = sub.add_parser("bank-session", help="exchange the consent code for a session")
    bss.add_argument("code")

    al = sub.add_parser("account-label", help="name an account yourself")
    al.add_argument("label")
    al.add_argument("--db", type=Path, default=DEFAULT_DB)
    al.add_argument("--uid", default="")

    rl = sub.add_parser("reconcile-link",
                        help="mark an API row as the duplicate of a statement row")
    rl.add_argument("api_id")
    rl.add_argument("statement_id")
    rl.add_argument("--db", type=Path, default=DEFAULT_DB)

    rc = sub.add_parser("reconcile", help="statement vs bank feed: the evidence to judge")
    rc.add_argument("--db", type=Path, default=DEFAULT_DB)

    bs = sub.add_parser("bank-sync", help="pull the bank feed into the ledger")
    bs.add_argument("--db", type=Path, default=DEFAULT_DB)
    bs.add_argument("--days", type=int, default=30)

    rv = sub.add_parser("review", help="classified merchants never confirmed")
    rv.add_argument("--db", type=Path, default=DEFAULT_DB)

    au = sub.add_parser("audit", help="random classified purchases to check by hand")
    au.add_argument("--db", type=Path, default=DEFAULT_DB)
    au.add_argument("--sample", type=int, default=20)
    au.add_argument("--seed", type=int, default=0)
    au.add_argument("--json", action="store_true")

    rp = sub.add_parser("repair", help="fix rows whose bucket contradicts the category")
    rp.add_argument("--db", type=Path, default=DEFAULT_DB)

    bf = sub.add_parser("backfill", help="store the summary of an old statement file")
    bf.add_argument("pdf", type=Path)
    bf.add_argument("--db", type=Path, default=DEFAULT_DB)

    ck = sub.add_parser("check", help="per statement: does the file add up, and does the "
                                      "archive match its period?")
    ck.add_argument("--db", type=Path, default=DEFAULT_DB)

    dt = sub.add_parser("detect", help="is this PDF a statement, or a generic document?")
    dt.add_argument("path", type=Path)

    kn = sub.add_parser("known", help="export the merchants already classified")
    kn.add_argument("--db", type=Path, default=DEFAULT_DB)
    kn.add_argument("--out", type=Path, default=DEFAULT_DB.parent / "known-merchants.md")

    en = sub.add_parser("enrich", help="pending merchants with search evidence, one payload")
    en.add_argument("--db", type=Path, default=DEFAULT_DB)
    en.add_argument("--limit", type=int, default=15)

    sr = sub.add_parser("search", help="web search (Tavily/Brave), for the agent to reason on")
    sr.add_argument("query")
    sr.add_argument("--limit", type=int, default=5)

    lit = sub.add_parser("literals", help="classify merchants whose trade is in the name")
    lit.add_argument("--db", type=Path, default=DEFAULT_DB)

    at = sub.add_parser("ateco", help="look up a company's ATECO code on the web")
    at.add_argument("--name", required=True)

    uc = sub.add_parser("unclassify", help="undo a classification so it gets researched again")
    uc.add_argument("--db", type=Path, default=DEFAULT_DB)
    uc.add_argument("--key", default="")
    uc.add_argument("--by", default="")

    a = p.parse_args()
    if a.cmd in ("bank-auth", "bank-session"):   # no database needed
        payload = bank_auth() if a.cmd == "bank-auth" else bank_session(a.code)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if a.cmd == "detect":              # no database needed
        print(json.dumps(detect(a.path), indent=2, ensure_ascii=False))
        return
    if a.cmd in ("ateco", "search"):   # no database needed
        payload = (ateco_lookup(a.name) if a.cmd == "ateco"
                   else search_web(a.query, a.limit))
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    conn = connect(a.db)

    if a.cmd == "ingest":
        results = [ingest(conn, pdf) for pdf in a.pdfs]
        results.append(report(conn, a.out))
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif a.cmd == "report":
        print(json.dumps(report(conn, a.out, a.month), indent=2, ensure_ascii=False))
    elif a.cmd == "list":
        print(json.dumps(summary(conn)["statements"], indent=2, ensure_ascii=False))
    elif a.cmd == "pending":
        rows = [dict(r) for r in pending_merchants(conn, include_asked=not a.new)]
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif a.cmd == "classify-batch":
        import sys as _sys
        payload = json.load(_sys.stdin)
        print(f"classificati: {classify_batch(conn, payload)}")
    elif a.cmd == "mark-asked":
        print(f"segnati come chiesti: {mark_asked(conn, a.keys)}")
    elif a.cmd == "classify":
        classify(conn, a.key, a.name or a.key, a.category, a.confidence, a.by, a.ateco, a.source)
        print("ok")
    elif a.cmd == "insights":
        text = insights(conn)
        print(text if text else "Nessun abbonamento ricorrente e nessuna anomalia di categoria.")
    elif a.cmd == "subscriptions":
        print(json.dumps(subscriptions(conn), indent=2, ensure_ascii=False))
    elif a.cmd == "anomalies":
        print(json.dumps(anomalies(conn), indent=2, ensure_ascii=False))
    elif a.cmd == "reconcile-link":
        if not conn.execute("SELECT 1 FROM txn WHERE id=?", (a.api_id,)).fetchone():
            raise SystemExit(f"id non trovato: {a.api_id}")
        conn.execute("UPDATE txn SET bucket='duplicate', note=? WHERE id=?",
                     (f"duplicato di {a.statement_id}", a.api_id))
        conn.commit()
        print(f"{a.api_id} marcato duplicato di {a.statement_id}")
    elif a.cmd == "reconcile":
        print(json.dumps(reconcile(conn), indent=2, ensure_ascii=False))
    elif a.cmd == "account-label":
        row = (conn.execute("SELECT uid FROM account WHERE uid=?", (a.uid,)).fetchone()
               if a.uid else
               conn.execute("SELECT uid FROM account WHERE kind<>'CACC' LIMIT 1").fetchone())
        if not row:
            raise SystemExit("nessun conto da etichettare: esegui prima bank-sync")
        conn.execute("UPDATE account SET label=? WHERE uid=?", (a.label, row["uid"]))
        conn.commit()
        print(f"etichettato {row['uid']} come '{a.label}'")
    elif a.cmd == "bank-sync":
        print(json.dumps(bank_sync(conn, a.days), indent=2, ensure_ascii=False))
        left = session_days_left()
        if left is not None and left <= 21:
            import subprocess as _sp
            msg = (f"Consenso bancario in scadenza tra {left} giorni (Enable Banking).\n\n"
                   "Per rinnovarlo:\n"
                   "1. sudo -u hermes env HOME=/home/hermes finance-ledger bank-auth\n"
                   "2. apri il link, autorizza Revolut, incolla il codice a:\n"
                   "   finance-ledger bank-session <codice>")
            _sp.run(["/usr/local/sbin/hermes-notify"], input=msg, text=True, check=False)
    elif a.cmd == "review":
        rows = review(conn)
        if not rows:
            print("Nessun esercente da confermare: tutto a fiducia alta.")
        else:
            total = sum(r["v"] for r in rows)
            print(f"{len(rows)} esercenti da confermare, {money(total)} di acquisti")
            for r in rows[:30]:
                print(f"- {r['name'][:44]:<44} {r['category']:<20} "
                      f"{r['confidence']:.2f} {r['resolved_by']:<7} "
                      f"{r['n']} mov  {money(r['v'])}")
    elif a.cmd == "audit":
        rows = audit(conn, a.sample, a.seed)
        if a.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            print(f"campione di {len(rows)} acquisti (seed {a.seed}): giusti o sbagliati?")
            for r in rows:
                print(f"- {r['date']}  {r['merchant_raw'][:40]:<40} {r['category']:<20} "
                      f"{money(r['amount'])}")
    elif a.cmd == "repair":
        fixed = repair(conn)
        print("\n".join(fixed) if fixed else "niente da riparare")
    elif a.cmd == "backfill":
        print(json.dumps(backfill_summary(conn, a.pdf), indent=2, ensure_ascii=False))
    elif a.cmd == "check":
        for st in summary(conn)["statements"]:
            print(f"{st['period_start']} -> {st['period_end']}")
            print(f"  estratto: {'quadra' if st['reconciled'] else 'NON quadra'}"
                  + (" (parziale)" if st["partial"] else ""))
            for p in (st["problems"] or "").splitlines():
                if p:
                    print(f"    - {p}")
            arch = st.get("archive") or {}
            if not arch.get("known"):
                print("  archivio: non verificabile (riepilogo non salvato)")
            elif arch.get("ok"):
                print("  archivio: quadra")
            else:
                print("  archivio: differenza")
            for p in arch.get("problems", []):
                print(f"    - {p}")
            for n in arch.get("notes", []):
                print(f"    nota: {n}")
    elif a.cmd == "known":
        rows = known(conn, a.out)
        print(f"{len(rows)} esercenti noti -> {a.out}")
    elif a.cmd == "enrich":
        print(json.dumps(enrich(conn, a.limit), indent=2, ensure_ascii=False))
    elif a.cmd == "search":
        print(json.dumps(search_web(a.query, a.limit), indent=2, ensure_ascii=False))
    elif a.cmd == "literals":
        done = []
        for row in pending_merchants(conn, include_asked=True):
            cat = literal_category(row["name"])
            if cat:
                classify(conn, row["key"], row["name"], cat, 0.9, "literal")
                done.append(f"{row['name'][:40]} -> {cat}")
        print("\n".join(done) or "nessun nome auto-evidente")
    elif a.cmd == "ateco":
        print(json.dumps(ateco_lookup(a.name), indent=2, ensure_ascii=False))
    elif a.cmd == "unclassify":
        print(f"riportati da classificare: {unclassify(conn, a.by, a.key)}")
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
