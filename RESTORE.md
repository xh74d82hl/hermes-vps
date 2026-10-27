# RESTORE — getting your agent back after the box dies

You need exactly two things, and neither is on the dead VPS:

1. `RESTIC_PASSWORD` (from your password manager — **without it the backup is noise**)
2. The B2 application key (`keyID` + `applicationKey`)

Everything else is reproducible from this repo.

## On a fresh VPS

```bash
# 1. base box
cp vars.example.env vars.env && $EDITOR vars.env
rsync -a --exclude .git --exclude vars.env ./ root@<IP>:/opt/hermes-vps/
ssh root@<IP> 'cd /opt/hermes-vps && ./bootstrap.sh'
ssh <ADMIN_USER>@<IP> 'sudo /opt/hermes-vps/scripts/harden-ssh.sh'

# 2. restic is installed by bootstrap.sh; give it the credentials
sudo tee /etc/hermes-backup.env >/dev/null <<'EOF'
RESTIC_PASSWORD=<from your password manager>
AWS_ACCESS_KEY_ID=<B2 keyID>
AWS_SECRET_ACCESS_KEY=<B2 applicationKey>
RESTIC_REPOSITORY=s3:https://s3.<REGION>.backblazeb2.com/<BUCKET>/hermes
EOF
sudo chmod 600 /etc/hermes-backup.env

# 3. what is in there?
sudo bash -c 'set -a; . /etc/hermes-backup.env; set +a; restic snapshots'
```

`restic snapshots` lists one line per night. Pick the last good one (or a specific id).

## Restore everything

```bash
sudo bash -c 'set -a; . /etc/hermes-backup.env; set +a; restic restore latest --target /tmp/restore'
```

The tree comes back as `/tmp/restore/home/<AGENT_USER>/.hermes/...`, which is deliberate: you inspect it
before anything overwrites a live directory.

```bash
# Hermes must not be running while you swap its home
sudo -iu <AGENT_USER> systemctl --user stop hermes-gateway
sudo rsync -a /tmp/restore/home/<AGENT_USER>/.hermes/ /home/<AGENT_USER>/.hermes/
sudo chown -R <AGENT_USER>:<AGENT_USER> /home/<AGENT_USER>/.hermes
sudo -iu <AGENT_USER> systemctl --user start hermes-gateway
```

Then `scripts/install-hermes.sh` if the Hermes code itself is gone (it lives in
`~/.hermes/hermes-agent/`, which is **not** backed up on purpose — it is downloaded and updated).

## Restore one file

```bash
sudo bash -c 'set -a; . /etc/hermes-backup.env; set +a;
  restic restore latest --target /tmp/r --include /home/<AGENT_USER>/.hermes/config.yaml'
```

## Restore a database

The live SQLite files are excluded (a copy of a running DB is a corrupt DB). The backup stores
WAL-safe dumps instead, one per database, named `<name>.db.dump`:

```bash
sudo bash -c 'set -a; . /etc/hermes-backup.env; set +a;
  restic dump latest /var/backups/hermes-dbs/state.db.dump' > /tmp/state.db
sqlite3 /tmp/state.db 'pragma integrity_check;'
```

Dump it back into place only with the gateway stopped, then delete `-wal`/`-shm` files.

## What is in a snapshot

| Path | Contents |
|---|---|
| `/home/<AGENT_USER>/.hermes/` | config, `.env`, skills, memory, sessions, cron, tokens |
| `/var/backups/hermes-dbs/` | consistent `.db.dump` copies of every SQLite database |
| `BACKUP_EXTRA_PATHS` from `vars.env` | whatever else you listed (e.g. `~/cv`) |

Not included: `hermes-agent/` (source, re-downloaded), `tools/` and `installs/` (runtimes),
`logs/`, `cache/`. That is why a snapshot is ~14 MB instead of ~1 GB.

## Taking a snapshot before risky work

```bash
sudo systemctl start hermes-backup.service     # 3 seconds
sudo bash -c 'set -a; . /etc/hermes-backup.env; set +a; restic snapshots'
```

## Sanity check once a month

```bash
sudo bash -c 'set -a; . /etc/hermes-backup.env; set +a; restic check --read-data-subset=50M'
sudo systemctl list-timers hermes-backup.timer
```

`restic check` verifies the repository against bit rot; `--read-data-subset` keeps it from
downloading the whole archive.
