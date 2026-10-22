#!/usr/bin/env python3
"""Copy the work from one tracker database into another, safely.

    python3 deploy/merge_projects.py --source ~/laptop.sqlite3 \
        --target /var/lib/covex-tracker/tracker.sqlite3
    python3 deploy/merge_projects.py --source laptop.sqlite3 --target live.sqlite3 --dry-run

The situation this exists for: the database on a laptop holds the projects and
predates the accounts table, while the server holds the administrator account
and no projects. Copying either file over the other loses half of what you
want, and replacing the server's file with the laptop's would leave a
deployment holding projects and nobody able to sign in.

So this inserts the *projects* into the target and touches nothing else.
`users`, `sessions` and `login_attempts` in the target are never read and never
written, so whoever can sign in today still can afterwards.

Ids are remapped as rows are copied: sprints, comments and activity trails all
follow their project. A project whose key already exists in the target is
refused rather than merged, so running this twice cannot duplicate anything.

The source is opened read-only, so it cannot be changed by accident.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

# (table, columns that are copied, columns that are foreign keys to remap)
# Order matters: parents before children.
COPY_ORDER = [
    ("projects", ["key", "name", "description", "lead", "next_number", "created_at"], {}),
    ("members", ["project_id", "name", "role"], {"project_id": "projects"}),
    ("sprints", ["project_id", "name", "goal", "status", "starts_on", "ends_on",
                 "created_at"], {"project_id": "projects"}),
    ("issues", ["project_id", "number", "type", "title", "description", "status",
                "priority", "assignee", "reporter", "story_points", "sprint_id",
                "labels", "due_date", "rank", "created_at", "updated_at",
                "resolved_at"],
     {"project_id": "projects", "sprint_id": "sprints"}),
    ("comments", ["issue_id", "author", "body", "created_at"], {"issue_id": "issues"}),
    ("activity", ["issue_id", "actor", "field", "from_value", "to_value",
                  "created_at"], {"issue_id": "issues"}),
]


def open_read_only(path) -> sqlite3.Connection:
    """Open the source so that a mistake here cannot alter it.

    `mode=ro` on the URI, and deliberately no `journal_mode` pragma -- asking a
    read-only connection to change the journal mode is an error, and this file
    may well be a WAL database with no sidecar files next to it.
    """
    if not os.path.exists(path):
        sys.exit("No such file: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("?", "%3f"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def open_target(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def tables_of(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def check_source(conn) -> None:
    needed = {"projects", "issues"}
    present = tables_of(conn)
    missing = needed - present
    if missing:
        sys.exit("The source is not a tracker database: no %s table."
                 % ", ".join(sorted(missing)))
    projects = conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
    if projects == 0:
        sys.exit("The source has no projects in it; there is nothing to copy.")


def check_target(conn) -> None:
    needed = {"projects", "issues", "users"}
    missing = {t for t in needed if t not in tables_of(conn)}
    if missing:
        sys.exit(
            "The target is missing %s. If it has never been started, run the "
            "server against it once (or `python3 server.py --db %s --no-seed "
            "--list-users`) so the schema and the accounts table are created."
            % (", ".join(sorted(missing)), "TARGET")
        )


def collisions(source, target) -> list:
    """Project keys that already exist in the target."""
    wanted = {r["key"] for r in source.execute("SELECT key FROM projects")}
    present = {r["key"] for r in target.execute("SELECT key FROM projects")}
    return sorted(wanted & present)


def merge(source, target, dry_run=False) -> dict:
    """Copy every project across. Returns a per-table count of rows written."""
    counts = {}
    id_map = {}          # table name -> {old id: new id}

    for table, columns, fks in COPY_ORDER:
        if table not in tables_of(source):
            continue                      # older databases may lack a table
        rows = source.execute(
            "SELECT id, %s FROM %s" % (", ".join(columns), table)
        ).fetchall()
        written = 0
        for row in rows:
            values = []
            for column in columns:
                value = row[column]
                if column in fks:
                    parent = fks[column]
                    if value is None:
                        values.append(None)
                    else:
                        mapped = id_map.get(parent, {}).get(value)
                        if mapped is None:
                            raise SystemExit(
                                "%s row %s points at %s id %s, which was not "
                                "copied. The source is inconsistent; nothing "
                                "was written." % (table, row["id"], parent, value)
                            )
                        values.append(mapped)
                else:
                    values.append(value)

            if dry_run:
                # Record the mapping anyway -- identity is fine, since nothing
                # is written. Without this, the first table with a foreign key
                # to an earlier one cannot resolve it and a rehearsal fails on
                # data it would have handled perfectly well.
                id_map.setdefault(table, {})[row["id"]] = row["id"]
                written += 1
                continue

            placeholders = ", ".join("?" for _ in columns)
            cursor = target.execute(
                "INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(columns),
                                                     placeholders),
                values,
            )
            id_map.setdefault(table, {})[row["id"]] = cursor.lastrowid
            written += 1
        counts[table] = written

    if not dry_run:
        target.commit()
    return counts


def snapshot(path) -> str:
    """A consistent copy of a SQLite database, even mid-write.

    `cp` on a file in WAL mode can catch it between the database and its log;
    the backup API cannot. This is the same reason `deploy/DEPLOY.md` says to
    back up with `.backup` rather than `cp`.
    """
    handle, backup_path = tempfile.mkstemp(suffix=".sqlite3",
                                           prefix="covex-premerge-")
    os.close(handle)
    source = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
    finally:
        source.close()
    return backup_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True,
                        help="the database to copy projects out of (opened read-only)")
    parser.add_argument("--target", required=True,
                        help="the database to copy them into (modified)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be copied and write nothing")
    parser.add_argument("--allow-existing", action="store_true",
                        help="copy anyway when a project key is already present "
                             "(produces a duplicate key, so think first)")
    args = parser.parse_args(argv)

    source = open_read_only(args.source)
    check_source(source)

    # Back up the target before touching it, unless this is a rehearsal.
    if not args.dry_run:
        backup = snapshot(args.target)
        print("Backup of the target written to: %s" % backup)
        print("  (keep it until you have checked the result)")

    target = open_target(args.target)
    try:
        check_target(target)

        clash = collisions(source, target)
        if clash and not args.allow_existing:
            sys.exit(
                "These project keys already exist in the target: %s\n"
                "Nothing was written. Rename the project first, delete the "
                "existing one, or pass --allow-existing if you really mean to "
                "have two projects with the same key." % ", ".join(clash)
            )

        before = target.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
        accounts = target.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]

        counts = merge(source, target, dry_run=args.dry_run)

        if args.dry_run:
            print("\nDry run -- nothing was written. Would copy:")
        else:
            print("\nCopied:")
        for table, count in counts.items():
            print("  %-10s %d rows" % (table, count))

        if not args.dry_run:
            after = target.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]
            still = target.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            print("\nProjects in the target: %d -> %d" % (before, after))
            print("Accounts in the target: %d -> %d" % (accounts, still))
            if still != accounts:
                print("!! The account count changed, which it never should. "
                      "Restore the backup printed above and report this.")
                return 1
            print("\nSign in as before -- the accounts table was not touched.")
    except Exception:
        if not args.dry_run:
            target.rollback()
            print("\nFailed; the target was rolled back. Nothing was written.",
                  file=sys.stderr)
        raise
    finally:
        target.close()
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
