#!/usr/bin/env python3
"""Build a slim dev database from the production one.

WHY THIS SHAPE
--------------
Production's vigilant.db is ~180 GiB, essentially all killmails. A full copy is
impossible (270 GiB free) and pointless. This extracts everything except
killmails whole, plus a bounded recent slice of killmails and their children.
Measured on production 2026-07-26: 493,783 killmails in 30 days, ~1.3M
attackers, ~8.8M items -- roughly 1.5-2 GiB of output.

WHY PRODUCTION STAYS UP
-----------------------
The 2026-07-10 outage came from holding ONE long transaction against the live
database on a spinning disk, not from reading it. Every query here is
index-driven (verified with EXPLAIN QUERY PLAN: both child tables have a
killmail_id index, and the parent filter uses COVERING INDEX
ix_killmails_killmail_time), so the work decomposes into many short reads. We
open and close a fresh read transaction every chunk, which lets WAL
checkpointing proceed in between, and sleep briefly to pace the I/O.

WHY NOT ?mode=ro
----------------
`file:...?mode=ro` FAILS on the live database: it is journal_mode=wal, and a WAL
reader must create the -shm file. We open a normal read-write handle and simply
issue no writes against production, exactly as every ordinary app reader does.

WHY --entrypoint python3 AND NOT -u 10001
-----------------------------------------
Two different situations, two different answers -- do not mix them up:

* `docker exec` into the RUNNING production container needs `-u 10001`. That
  container drops CAP_DAC_OVERRIDE (cap_drop: ALL, cap_add: CHOWN/SETUID/
  SETGID), so uid 0 cannot even open a file owned by vigilant:vigilant --
  SQLite fails with "attempt to write a readonly database" before reading a
  single row.

* `docker run` of the IMAGE must NOT pass `-u`. The image ENTRYPOINT
  (docker-entrypoint.sh) runs as root, chowns /data, then `exec gosu vigilant`.
  As uid 10001 the chown fails under `set -e` and gosu cannot switch users, so
  the container dies before this script starts. Bypass it with
  `--entrypoint python3` and run as root -- a default `docker run` grants root
  the full capability set, so it can read production's volume and write the dev
  one. Files land root-owned, which is fine: the dev container's own entrypoint
  chowns /data to vigilant on first boot.

WHY sqlite_master AND NOT create_all
------------------------------------
Replaying production's own DDL captures schema drift (columns added by the ALTER
TABLE block in app/main.py that a model may not describe) and keeps this script
free of any app import, so it runs in a bare container.

USAGE
-----
    docker run --rm --entrypoint python3 \
      -v vigilant_app_data:/prod \
      -v vigilant-dev_dev_data:/devdata \
      -v /opt/vigilant-dev/scripts:/scripts:ro \
      vigilant-dev:local \
      /scripts/seed-dev-db.py --prod /prod/vigilant.db --out /devdata/vigilant.db
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dev_seed_tables as cls  # noqa: E402

CHUNK = 5000          # killmail_ids per read transaction
SLEEP_BETWEEN = 0.05  # seconds; paces I/O so production is not starved


def log(msg):
    print(f"[seed] {msg}", flush=True)


def copy_schema(prod, out):
    """Replay production's CREATE TABLE statements. Indexes are created LAST.

    Building indexes after the bulk insert rather than during it is
    substantially faster and produces a tighter B-tree.
    """
    tables, indexes = [], []
    for typ, name, sql in prod.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ):
        if name in cls.SKIP:
            continue
        (tables if typ == "table" else indexes).append((name, sql))
    for name, sql in tables:
        out.execute(sql)
    out.commit()
    log(f"schema: {len(tables)} tables created, {len(indexes)} indexes deferred")
    return indexes


def copy_whole(prod, out, table):
    cols = [r[1] for r in prod.execute(f"PRAGMA table_info({table})")]
    if not cols:
        log(f"{table}: absent from production, skipped")
        return 0
    placeholders = ",".join("?" * len(cols))
    collist = ",".join(f'"{c}"' for c in cols)
    n = 0
    cur = prod.execute(f"SELECT {collist} FROM {table}")
    while True:
        rows = cur.fetchmany(CHUNK)
        if not rows:
            break
        out.executemany(
            f"INSERT INTO {table} ({collist}) VALUES ({placeholders})", rows)
        out.commit()
        n += len(rows)
    log(f"{table}: {n} rows")
    return n


def copy_slice(prod_path, out, cutoff):
    """Copy the parent slice, then its children, chunk by chunk.

    A FRESH connection per chunk is what keeps production healthy: it ends the
    read transaction so WAL checkpointing can proceed between chunks.
    """
    prod = sqlite3.connect(prod_path, timeout=30)
    ids = [r[0] for r in prod.execute(
        f"SELECT killmail_id FROM {cls.SLICE_PARENT} WHERE killmail_time >= ?",
        (cutoff,))]
    prod.close()
    log(f"{cls.SLICE_PARENT}: {len(ids)} ids in window (cutoff {cutoff})")

    for table in (cls.SLICE_PARENT,) + cls.SLICE_CHILDREN:
        total = 0
        for i in range(0, len(ids), CHUNK):
            batch = ids[i:i + CHUNK]
            prod = sqlite3.connect(prod_path, timeout=30)
            cols = [r[1] for r in prod.execute(f"PRAGMA table_info({table})")]
            collist = ",".join(f'"{c}"' for c in cols)
            q = ",".join("?" * len(batch))
            rows = prod.execute(
                f"SELECT {collist} FROM {table} WHERE killmail_id IN ({q})",
                batch).fetchall()
            prod.close()
            out.executemany(
                f"INSERT INTO {table} ({collist}) "
                f"VALUES ({','.join('?' * len(cols))})", rows)
            out.commit()
            total += len(rows)
            time.sleep(SLEEP_BETWEEN)
        log(f"{table}: {total} rows")


def scrub(out):
    """Empty every stored OAuth token.

    They are EncryptedText bound to production's SECRET_KEY and production's SSO
    client_id, so they are useless in dev -- a dev instance uses its own EVE
    application and you log in fresh there. Both columns are nullable=False,
    hence empty string rather than NULL.
    """
    out.execute("UPDATE characters SET access_token = '', refresh_token = ''")
    out.commit()
    log("scrubbed access_token/refresh_token on all characters")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=cls.SLICE_DAYS)
    args = ap.parse_args()

    if os.path.exists(args.out):
        sys.exit(f"refusing to overwrite existing {args.out}")

    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    prod = sqlite3.connect(args.prod, timeout=30)
    out = sqlite3.connect(args.out)
    # Throwaway output file: durability guarantees buy nothing and cost a lot
    # on a bulk load. If this crashes we delete the file and start over.
    out.execute("PRAGMA journal_mode=OFF")
    out.execute("PRAGMA synchronous=OFF")

    indexes = copy_schema(prod, out)
    for table in cls.COPY_ALL:
        copy_whole(prod, out, table)
    prod.close()

    copy_slice(args.prod, out, cutoff)
    scrub(out)

    log(f"creating {len(indexes)} indexes")
    for name, sql in indexes:
        try:
            out.execute(sql)
        except sqlite3.OperationalError as e:
            log(f"index {name}: {e}")
    out.commit()
    out.close()
    log(f"done: {args.out} is {os.path.getsize(args.out) / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
