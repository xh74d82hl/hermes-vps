#!/usr/bin/env python3
"""hermes-remind — reminders that actually fire, and that you can see in Google.

One writer for three stores, so they cannot drift apart:

  ~/.hermes/reminders.db    the truth: text, instant, mirrors, state
  Google Calendar / Tasks   your mirror (best effort, never blocks the reminder)
  hermes cron one-shot      the clock: --no-agent, so firing needs no model, no quota

The cron job runs a tiny per-reminder wrapper that calls `fire`, which reads the
database and prints the message. No LLM is involved at fire time, so a reminder
still arrives when the provider, the quota or the main bot is broken.

Run this under the interpreter that has the Google libs (the installed
`hermes-remind` wrapper handles that). `fire` deliberately imports no Google code.

Commands
  new --text T --at ISO [--voice] [--mirror calendar|task|none] [--deliver DEST]
  list [--all]
  done REF
  snooze REF DURATION
  rm REF
  reconcile
  fire ID            (internal: called by the cron job)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
DB_PATH = HERMES_HOME / "reminders.db"
SCRIPTS_DIR = HERMES_HOME / "scripts"
WRAPPER_DIR = SCRIPTS_DIR / "reminders"
JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
GOOGLE_API = HERMES_HOME / "skills" / "productivity" / "google-workspace" / "scripts" / "google_api.py"

DEFAULT_VOICE = "it-IT-ElsaNeural"
DEFAULT_DELIVER = os.environ.get("HERMES_REMIND_DELIVER", "telegram")
EVENT_MINUTES = 15
LATE_MINUTES = 15
STALE_HOURS = 24

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    at          TEXT NOT NULL,
    mirror      TEXT NOT NULL DEFAULT 'calendar',
    voice       INTEGER NOT NULL DEFAULT 0,
    event_id    TEXT,
    task_id     TEXT,
    job_id      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    fired_at    TEXT,
    note        TEXT
);
"""


# --------------------------------------------------------------------------- time


def now() -> datetime:
    return datetime.now().astimezone()


def parse_instant(value: str) -> datetime:
    """ISO timestamp (naive = local) or a relative 'in 20m'."""
    value = value.strip()
    m = re.fullmatch(r"in\s+(\d+)\s*([mhd])", value, re.I)
    if m:
        n = int(m.group(1))
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2).lower()]
        return now() + timedelta(**{unit: n})
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach the machine zone, which is the user's zone
    return dt


def parse_duration(value: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", value, re.I)
    if not m:
        raise SystemExit(f"durata non valida: {value!r} (usa 10m, 2h, 1d)")
    n = int(m.group(1))
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2).lower()]: n})


# --------------------------------------------------------------------------- store


def db() -> sqlite3.Connection:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def new_id() -> str:
    return "r-" + secrets.token_hex(2)


def resolve(conn: sqlite3.Connection, ref: str, *, pending_only: bool = False):
    ref = ref.strip()
    rows = [dict(r) for r in conn.execute("SELECT * FROM reminders").fetchall()]
    if pending_only:
        rows = [r for r in rows if r["status"] == "pending"]
    exact = [r for r in rows if r["id"] == ref]
    if exact:
        return exact[0]
    pref = [r for r in rows if r["id"].startswith(ref)]
    if len(pref) == 1:
        return pref[0]
    text = [r for r in rows if ref.lower() in r["text"].lower()]
    if len(text) == 1:
        return text[0]
    if not (pref or text):
        raise SystemExit(f"nessun promemoria che corrisponde a {ref!r}")
    names = ", ".join(f"{r['id']} ({r['text'][:40]}, {r['at'][:16]})" for r in (pref or text)[:6])
    raise SystemExit(f"{ref!r} è ambiguo: {names}")


# --------------------------------------------------------------------------- google


def google():
    """Import the Google skill's module lazily — `fire` must never need it."""
    if not GOOGLE_API.exists():
        raise RuntimeError(f"google_api.py non trovato in {GOOGLE_API}")
    spec = importlib.util.spec_from_file_location("hermes_google_api", GOOGLE_API)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mirror_calendar(rid: str, text: str, at: datetime) -> str:
    ga = google()
    svc = ga.build_service("calendar", "v3")
    body = {
        "summary": text,
        "description": f"Promemoria Hermes · {rid}\nCreato da hermes-remind; riceverai anche il messaggio su Telegram.",
        "start": {"dateTime": at.isoformat()},
        "end": {"dateTime": (at + timedelta(minutes=EVENT_MINUTES)).isoformat()},
        "transparency": "transparent",  # does not block the slot, only rings
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
    }
    created = svc.events().insert(calendarId="primary", body=body).execute()
    return created.get("id", "")


def mirror_task(rid: str, text: str, at: datetime) -> str:
    ga = google()
    svc = ga._tasks_service()
    body = {
        "title": text,
        "notes": f"Promemoria {at.strftime('%H:%M')} · Hermes {rid} (l'orario vive su Telegram: l'API Tasks salva solo la data)",
        "due": ga._rfc3339_due(at.strftime("%Y-%m-%d")),
    }
    created = svc.tasks().insert(tasklist="@default", body=body).execute()
    return created.get("id", "")


def complete_task(task_id: str) -> None:
    ga = google()
    ga._tasks_service().tasks().patch(
        tasklist="@default", task=task_id, body={"status": "completed"}
    ).execute()


def delete_event(event_id: str) -> None:
    ga = google()
    ga.build_service("calendar", "v3").events().delete(
        calendarId="primary", eventId=event_id
    ).execute()


# --------------------------------------------------------------------------- cron


def hermes_bin() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    guess = Path.home() / ".local" / "bin" / "hermes"
    if guess.exists():
        return str(guess)
    raise SystemExit("comando `hermes` non trovato nel PATH")


def cron_env() -> dict:
    env = dict(os.environ)
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def write_wrapper(rid: str) -> str:
    WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    path = WRAPPER_DIR / f"fire-{rid}.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "# generated by hermes-remind; one per reminder so the cron job needs no arguments\n"
        "import runpy, sys\n"
        "from pathlib import Path\n"
        'main = Path(__file__).resolve().parents[1] / "hermes-remind.py"\n'
        f'sys.argv = ["hermes-remind", "fire", {rid!r}]\n'
        'runpy.run_path(str(main), run_name="__main__")\n',
        encoding="utf-8",
    )
    path.chmod(0o700)
    return f"reminders/{path.name}"


def job_id_by_name(name: str) -> str:
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == name:
            return str(job.get("id") or "")
    return ""


def create_job(rid: str, text: str, at: datetime, deliver: str) -> str:
    name = f"remind:{rid}"
    script = write_wrapper(rid)
    cmd = [
        hermes_bin(), "cron", "create", at.isoformat(), text,
        "--name", name,
        "--deliver", deliver,
        "--no-agent",
        "--script", script,
        "--repeat", "1",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=cron_env())
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "cron create failed").strip()[:400])
    return job_id_by_name(name)


def remove_job(rid: str) -> None:
    subprocess.run(
        [hermes_bin(), "cron", "remove", f"remind:{rid}"],
        capture_output=True, text=True, env=cron_env(),
    )


def prune(conn: sqlite3.Connection) -> int:
    """Drop exhausted reminder jobs (repeat done, no next run) so `hermes cron list` stays readable."""
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    terminal = {"fired", "done", "cancelled", "missed", "snoozed"}
    removed = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "")
        if not name.startswith("remind:") or job.get("next_run_at"):
            continue
        rid = name.split(":", 1)[1]
        row = conn.execute("SELECT status FROM reminders WHERE id=?", (rid,)).fetchone()
        exhausted = row is None or row["status"] in terminal
        if exhausted:
            remove_job(rid)
            drop_wrapper(rid)
            removed += 1
    known = {r[0] for r in conn.execute("SELECT id FROM reminders")}
    for wrapper in WRAPPER_DIR.glob("fire-*.py"):
        if wrapper.name[len("fire-"):-len(".py")] not in known:
            wrapper.unlink(missing_ok=True)
    return removed


def drop_wrapper(rid: str) -> None:
    (WRAPPER_DIR / f"fire-{rid}.py").unlink(missing_ok=True)


# --------------------------------------------------------------------------- voice


def tts_python() -> str:
    candidates = sorted((HERMES_HOME / "tools").glob("python-*/bin/python3"), reverse=True)
    for cand in candidates:
        probe = subprocess.run(
            [str(cand), "-c", "import edge_tts"], capture_output=True
        )
        if probe.returncode == 0:
            return str(cand)
    raise RuntimeError("edge-tts non trovato (esegui scripts/install-voice.sh)")


def synth_voice(rid: str, text: str) -> str:
    py = tts_python()
    mp3 = Path("/tmp") / f"hermes-remind-{rid}.mp3"
    ogg = Path("/tmp") / f"hermes-remind-{rid}.ogg"
    subprocess.run(
        [py, "-m", "edge_tts", "--voice", DEFAULT_VOICE, "--text", text,
         "--write-media", str(mp3)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
         "-c:a", "libopus", "-b:a", "48k", str(ogg)],
        check=True, capture_output=True,
    )
    mp3.unlink(missing_ok=True)
    return str(ogg)


# --------------------------------------------------------------------------- commands


def cmd_new(args) -> None:
    at = parse_instant(args.at)
    if at <= now():
        raise SystemExit(f"l'istante è già passato: {at.isoformat()}")
    rid = args.id or new_id()
    conn = db()
    conn.execute(
        "INSERT INTO reminders (id, text, at, mirror, voice, status, created_at)"
        " VALUES (?,?,?,?,?,'pending',?)",
        (rid, args.text, at.isoformat(), args.mirror, 1 if args.voice else 0, now().isoformat()),
    )
    conn.commit()

    out = {"id": rid, "at": at.isoformat(), "text": args.text, "mirror": args.mirror, "warnings": []}

    if args.mirror == "calendar":
        try:
            event_id = mirror_calendar(rid, args.text, at)
            conn.execute("UPDATE reminders SET event_id=? WHERE id=?", (event_id, rid))
            out["calendar_event"] = event_id
        except Exception as exc:  # the reminder still fires; the mirror is a bonus
            out["warnings"].append(f"calendar: {exc}")
    elif args.mirror == "task":
        try:
            task_id = mirror_task(rid, args.text, at)
            conn.execute("UPDATE reminders SET task_id=? WHERE id=?", (task_id, rid))
            out["task"] = task_id
        except Exception as exc:
            out["warnings"].append(f"task: {exc}")

    try:
        job = create_job(rid, args.text, at, args.deliver)
        conn.execute("UPDATE reminders SET job_id=? WHERE id=?", (job, rid))
        out["job"] = job or "(non trovato in jobs.json)"
    except Exception as exc:
        conn.execute("UPDATE reminders SET status='unscheduled', note=? WHERE id=?", (str(exc)[:300], rid))
        conn.commit()
        out["error"] = f"cron: {exc}"
    conn.commit()
    prune(conn)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_fire(args) -> None:
    conn = db()
    row = conn.execute("SELECT * FROM reminders WHERE id=?", (args.id,)).fetchone()
    if row is None or row["status"] != "pending":
        return  # silent: nothing to deliver
    at = parse_instant(row["at"])
    delta = now() - at
    label = at.strftime("%H:%M")
    if delta > timedelta(hours=STALE_HOURS):
        conn.execute("UPDATE reminders SET status='missed', fired_at=? WHERE id=?",
                     (now().isoformat(), row["id"]))
        conn.commit()
        print(f"Promemoria perso ({at.strftime('%d/%m %H:%M')}): {row['text']}\n{row['id']}")
        return
    prefix = ("Promemoria (in ritardo) " + label if delta > timedelta(minutes=LATE_MINUTES)
              else "Promemoria " + label)
    conn.execute("UPDATE reminders SET status='fired', fired_at=? WHERE id=?",
                 (now().isoformat(), row["id"]))
    conn.commit()
    drop_wrapper(row["id"])
    print(f"{prefix} — {row['text']}\n{row['id']}")
    if row["voice"]:
        try:
            print(f"MEDIA:{synth_voice(row['id'], row['text'])}")
        except Exception as exc:  # keep the text; a missing voice must not eat the reminder
            print(f"(audio non disponibile: {exc})")


def cmd_list(args) -> None:
    conn = db()
    where = "" if args.all else "WHERE status IN ('pending','fired')"
    rows = conn.execute(f"SELECT * FROM reminders {where} ORDER BY at").fetchall()
    if not rows:
        print("nessun promemoria")
        return
    for r in rows:
        mark = {"pending": "·", "fired": "✓", "done": "✓✓", "missed": "!", "snoozed": "»",
                "cancelled": "x", "unscheduled": "?"}.get(r["status"], "?")
        print(f"{mark} {r['id']}  {r['at'][:16]}  {r['text'][:60]}  [{r['status']}, {r['mirror']}]")


def cmd_done(args) -> None:
    conn = db()
    row = resolve(conn, args.ref)
    notes = []
    if row["task_id"]:
        try:
            complete_task(row["task_id"])
            notes.append("task completato")
        except Exception as exc:
            notes.append(f"task: {exc}")
    if row["status"] == "pending":
        remove_job(row["id"])
        notes.append("job rimosso")
    drop_wrapper(row["id"])
    conn.execute("UPDATE reminders SET status='done' WHERE id=?", (row["id"],))
    conn.commit()
    print(f"{row['id']} → done ({', '.join(notes) or 'niente da fare'})")


def cmd_snooze(args) -> None:
    conn = db()
    row = resolve(conn, args.ref, pending_only=False)
    at = now() + parse_duration(args.duration)
    remove_job(row["id"])
    job = create_job(row["id"], row["text"], at, args.deliver or DEFAULT_DELIVER)
    conn.execute("UPDATE reminders SET at=?, job_id=?, status='pending', fired_at=NULL WHERE id=?",
                 (at.isoformat(), job, row["id"]))
    conn.commit()
    print(f"{row['id']} → snooze {at.strftime('%Y-%m-%d %H:%M')}")


def cmd_rm(args) -> None:
    conn = db()
    row = resolve(conn, args.ref)
    remove_job(row["id"])
    if row["event_id"] and not args.keep_calendar:
        try:
            delete_event(row["event_id"])
        except Exception:
            pass
    drop_wrapper(row["id"])
    conn.execute("UPDATE reminders SET status='cancelled' WHERE id=?", (row["id"],))
    conn.commit()
    print(f"{row['id']} cancellato")


def cmd_reconcile(args) -> None:
    conn = db()
    fixed = 0
    for row in conn.execute("SELECT * FROM reminders WHERE status='pending'").fetchall():
        job = job_id_by_name(f"remind:{row['id']}")
        if job:
            continue
        at = parse_instant(row["at"])
        if at <= now():
            conn.execute("UPDATE reminders SET status='missed', note='job assente, istante passato' WHERE id=?",
                         (row["id"],))
        else:
            try:
                new_job = create_job(row["id"], row["text"], at, DEFAULT_DELIVER)
                conn.execute("UPDATE reminders SET job_id=? WHERE id=?", (new_job, row["id"]))
            except Exception as exc:
                conn.execute("UPDATE reminders SET note=? WHERE id=?", (str(exc)[:300], row["id"]))
        fixed += 1
    conn.commit()
    pruned = prune(conn)
    print(f"reconcile: {fixed} promemoria sistemati, {pruned} job esauriti rimossi")


# --------------------------------------------------------------------------- cli


def main() -> None:
    p = argparse.ArgumentParser(prog="hermes-remind", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="create a reminder (db + google mirror + cron job)")
    n.add_argument("--text", required=True)
    n.add_argument("--at", required=True, help="ISO timestamp (naive = local) or 'in 20m'")
    n.add_argument("--mirror", choices=("calendar", "task", "none"), default="calendar")
    n.add_argument("--voice", action="store_true", help="also send a voice note")
    n.add_argument("--deliver", default=DEFAULT_DELIVER, help="cron delivery target")
    n.add_argument("--id", default="", help="force an id (tests)")
    n.set_defaults(func=cmd_new)

    ls = sub.add_parser("list", help="list reminders")
    ls.add_argument("--all", action="store_true")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("done", help="mark done (and complete the Google Task)")
    d.add_argument("ref")
    d.set_defaults(func=cmd_done)

    s = sub.add_parser("snooze", help="move a reminder forward")
    s.add_argument("ref")
    s.add_argument("duration", help="10m, 2h, 1d")
    s.add_argument("--deliver", default="")
    s.set_defaults(func=cmd_snooze)

    r = sub.add_parser("rm", help="cancel a reminder")
    r.add_argument("ref")
    r.add_argument("--keep-calendar", action="store_true")
    r.set_defaults(func=cmd_rm)

    f = sub.add_parser("fire", help="internal: deliver one reminder")
    f.add_argument("id")
    f.set_defaults(func=cmd_fire)

    rec = sub.add_parser("reconcile", help="recreate missing cron jobs")
    rec.set_defaults(func=cmd_reconcile)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
