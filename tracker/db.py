"""SQLite storage layer for the tracker.

Everything the app knows lives in one SQLite file. No ORM, no migrations:
`init_db` creates the schema if it is missing and seeds a demo workspace the
first time it runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import auth, storage

# The workflow, in order. The board renders one column per entry.
STATUSES = ["todo", "in_progress", "in_review", "done"]
PRIORITIES = ["highest", "high", "medium", "low"]
TYPES = ["story", "bug", "task", "epic"]
SPRINT_STATUSES = ["planned", "active", "completed"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  key         TEXT    NOT NULL UNIQUE,
  name        TEXT    NOT NULL,
  description TEXT    NOT NULL DEFAULT '',
  lead        TEXT    NOT NULL DEFAULT '',
  next_number INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name       TEXT    NOT NULL,
  role       TEXT    NOT NULL DEFAULT 'Developer',
  UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS sprints (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name       TEXT    NOT NULL,
  goal       TEXT    NOT NULL DEFAULT '',
  status     TEXT    NOT NULL DEFAULT 'planned',
  starts_on  TEXT,
  ends_on    TEXT,
  created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  number       INTEGER NOT NULL,
  type         TEXT    NOT NULL DEFAULT 'task',
  title        TEXT    NOT NULL,
  description  TEXT    NOT NULL DEFAULT '',
  status       TEXT    NOT NULL DEFAULT 'todo',
  priority     TEXT    NOT NULL DEFAULT 'medium',
  assignee     TEXT    NOT NULL DEFAULT '',
  reporter     TEXT    NOT NULL DEFAULT '',
  story_points INTEGER,
  sprint_id    INTEGER REFERENCES sprints(id) ON DELETE SET NULL,
  labels       TEXT    NOT NULL DEFAULT '[]',
  due_date     TEXT,
  rank         REAL    NOT NULL DEFAULT 1000,
  created_at   TEXT    NOT NULL,
  updated_at   TEXT    NOT NULL,
  resolved_at  TEXT,
  UNIQUE (project_id, number)
);

CREATE TABLE IF NOT EXISTS comments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id   INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  author     TEXT    NOT NULL,
  body       TEXT    NOT NULL,
  created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id   INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  actor      TEXT    NOT NULL,
  field      TEXT    NOT NULL,
  from_value TEXT,
  to_value   TEXT,
  created_at TEXT    NOT NULL
);

-- Attachments belong to a comment. `comment_id` is null between the upload and
-- the comment that carries it: the client sends the files first, then posts the
-- comment with their ids, so a comment and its screenshots arrive together in
-- one write. The bytes themselves are on disk -- `stored_name` is a random name
-- in the attachments/ directory beside this database, never the client's.
CREATE TABLE IF NOT EXISTS attachments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id     INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  comment_id   INTEGER REFERENCES comments(id) ON DELETE CASCADE,
  uploader     TEXT    NOT NULL,
  filename     TEXT    NOT NULL,
  stored_name  TEXT    NOT NULL UNIQUE,
  content_type TEXT    NOT NULL DEFAULT 'application/octet-stream',
  byte_size    INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_id, status, rank);
CREATE INDEX IF NOT EXISTS idx_issues_sprint  ON issues(sprint_id);
CREATE INDEX IF NOT EXISTS idx_comments_issue ON comments(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_activity_issue ON activity(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_issue   ON attachments(issue_id);
CREATE INDEX IF NOT EXISTS idx_attachments_comment ON attachments(comment_id);

-- Outgoing mail, queued rather than sent. The row is written in the same
-- transaction as the change that caused it, so a crash after the ticket saved
-- cannot lose the notification; `sent_at IS NULL` is the queue and the sender
-- thread in server.py works it down. Nothing here is sent inline: an SMTP
-- round trip must not hold the request or the write lock.
CREATE TABLE IF NOT EXISTS notifications (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  issue_id       INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
  recipient      TEXT    NOT NULL,
  recipient_name TEXT    NOT NULL DEFAULT '',
  reason         TEXT    NOT NULL,
  subject        TEXT    NOT NULL,
  body           TEXT    NOT NULL,
  created_at     TEXT    NOT NULL,
  sent_at        TEXT,
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_pending ON notifications(sent_at, id);
"""

ISSUE_SELECT = """
SELECT i.*,
       p.key  AS project_key,
       p.name AS project_name,
       s.name AS sprint_name,
       s.status AS sprint_status
  FROM issues i
  JOIN projects p ON p.id = i.project_id
  LEFT JOIN sprints s ON s.id = i.sprint_id
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today() -> str:
    return date.today().isoformat()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str, with_demo: bool = True) -> None:
    """Create the database and its schema.

    `with_demo` fills an empty database with the demo workspace, which is what
    you want locally and what the tests assume. A real deployment passes
    False: the demo projects would otherwise be permanent, because there is no
    route that deletes a project.

    The flag is not called `seed` because it would shadow the `seed()`
    function below, and the call underneath would then be a call to a bool.
    """
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        # Accounts and sessions live next to the work rather than in a second
        # file: one database is the thing you back up and the thing you move
        # to a new host. auth.py owns their shape.
        conn.executescript(auth.SCHEMA)
        if with_demo and conn.execute(
                "SELECT COUNT(*) AS c FROM projects").fetchone()["c"] == 0:
            seed(conn)
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------

def _labels(raw) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def serialize_issue(row: sqlite3.Row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "key": "%s-%d" % (row["project_key"], row["number"]),
        "number": row["number"],
        "project_id": row["project_id"],
        "project_key": row["project_key"],
        "project_name": row["project_name"] if "project_name" in keys else None,
        "type": row["type"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "assignee": row["assignee"],
        "reporter": row["reporter"],
        "story_points": row["story_points"],
        "sprint_id": row["sprint_id"],
        "sprint_name": row["sprint_name"] if "sprint_name" in keys else None,
        "sprint_status": row["sprint_status"] if "sprint_status" in keys else None,
        "labels": _labels(row["labels"]),
        "due_date": row["due_date"],
        "rank": row["rank"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "resolved_at": row["resolved_at"],
    }


def serialize_sprint(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "goal": row["goal"],
        "status": row["status"],
        "starts_on": row["starts_on"],
        "ends_on": row["ends_on"],
        "created_at": row["created_at"],
    }


def serialize_comment(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "issue_id": row["issue_id"],
        "author": row["author"],
        "body": row["body"],
        "created_at": row["created_at"],
    }


def serialize_attachment(row: sqlite3.Row) -> dict:
    content_type = row["content_type"]
    return {
        "id": row["id"],
        "issue_id": row["issue_id"],
        "comment_id": row["comment_id"],
        "filename": row["filename"],
        "content_type": content_type,
        "byte_size": row["byte_size"],
        "uploader": row["uploader"],
        "created_at": row["created_at"],
        # Only a verified image is safe to show inline; the client renders an
        # <img> for these and a download chip for everything else.
        "is_image": content_type in storage.IMAGE_TYPES,
        # The bytes have their own route: JSON carries the metadata, not 8 MB
        # of base64 the browser would have to decode before it could draw.
        # The stored name on disk is deliberately not exposed.
        "url": "/api/attachments/%d/content" % row["id"],
    }


def serialize_activity(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "issue_id": row["issue_id"],
        "actor": row["actor"],
        "field": row["field"],
        "from": row["from_value"],
        "to": row["to_value"],
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------
# lookups / writes
# --------------------------------------------------------------------------

def get_project(conn: sqlite3.Connection, ref):
    """Look a project up by numeric id or by key (case-insensitive)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(ref),)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM projects WHERE key = ? COLLATE NOCASE", (str(ref),)
        ).fetchone()
    return row


def project_payload(conn: sqlite3.Connection, project: sqlite3.Row) -> dict:
    members = conn.execute(
        "SELECT name, role FROM members WHERE project_id = ? ORDER BY name",
        (project["id"],),
    ).fetchall()
    sprints = conn.execute(
        "SELECT * FROM sprints WHERE project_id = ? "
        "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'planned' THEN 1 ELSE 2 END, id",
        (project["id"],),
    ).fetchall()
    counts = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(status = 'done') AS done, "
        "       COALESCE(SUM(CASE WHEN status <> 'done' THEN story_points END), 0) AS open_points "
        "  FROM issues WHERE project_id = ?",
        (project["id"],),
    ).fetchone()
    return {
        "id": project["id"],
        "key": project["key"],
        "name": project["name"],
        "description": project["description"],
        "lead": project["lead"],
        "created_at": project["created_at"],
        "members": [{"name": m["name"], "role": m["role"]} for m in members],
        "sprints": [serialize_sprint(s) for s in sprints],
        "total_issues": counts["total"] or 0,
        "done_issues": counts["done"] or 0,
        "open_points": counts["open_points"] or 0,
    }


def next_issue_number(conn: sqlite3.Connection, project_id: int) -> int:
    row = conn.execute(
        "SELECT next_number FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    number = int(row["next_number"])
    conn.execute("UPDATE projects SET next_number = ? WHERE id = ?", (number + 1, project_id))
    return number


def log_activity(conn, issue_id, actor, field, from_value, to_value):
    conn.execute(
        "INSERT INTO activity (issue_id, actor, field, from_value, to_value, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            issue_id,
            actor or "Someone",
            field,
            None if from_value is None else str(from_value),
            None if to_value is None else str(to_value),
            utcnow(),
        ),
    )


def reorder_issue(conn, issue_id: int, status: str, index) -> None:
    """Move an issue to `status`, at position `index` within that column."""
    issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if issue is None:
        return
    siblings = conn.execute(
        "SELECT id FROM issues WHERE project_id = ? AND status = ? AND id <> ? "
        "ORDER BY rank, id",
        (issue["project_id"], status, issue_id),
    ).fetchall()
    ids = [r["id"] for r in siblings]
    try:
        pos = int(index)
    except (TypeError, ValueError):
        pos = len(ids)
    pos = max(0, min(pos, len(ids)))
    ids.insert(pos, issue_id)
    for offset, iid in enumerate(ids):
        conn.execute(
            "UPDATE issues SET rank = ?, status = ? WHERE id = ?",
            (float(offset + 1) * 1000.0, status, iid),
        )


def rank_for_new_issue(conn, project_id: int, status: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(MAX(rank), 0) AS top FROM issues WHERE project_id = ? AND status = ?",
        (project_id, status),
    ).fetchone()
    return float(row["top"]) + 1000.0


# --------------------------------------------------------------------------
# demo seed -- only runs against an empty database
# --------------------------------------------------------------------------

SEED_SPRINTS = [
    # (name, goal, status, start offset days, end offset days)
    ("Sprint 15", "Ship card-on-file tokenization behind the launch flag.", "active", -6, 8),
    ("Sprint 16", "Retry strategy and the reconciliation dashboard.", "planned", 9, 23),
]

SEED_ISSUES = [
    # (type, title, status, priority, assignee, points, sprint idx, labels, description)
    ("story", "Tokenize stored cards without breaking existing vault references",
     "in_progress", "highest", "Mei Tan", 8, 0, ["payments", "security"],
     "Legacy vault rows use a 12-digit reference. The new flow writes a token plus a "
     "back-reference so both can resolve during the transition."),
    ("bug", "Webhook retries create duplicate ledger entries on 5xx from acquirer",
     "in_progress", "highest", "Dan Okafor", 5, 0, ["payments", "bug-bash"],
     "Acquirer returns 504 under load; our retry handler re-posts the same event id and "
     "the ledger inserts a second row. Needs the idempotency key respected on retry."),
    ("story", "Add idempotency keys to the charge endpoint",
     "in_review", "high", "Mei Tan", 5, 0, ["api"],
     "Clients should be able to retry a charge safely with an Idempotency-Key header. "
     "Replays return the original response with a 200."),
    ("task", "Set up the feature flag for the tokenization rollout",
     "done", "medium", "Jonas Weber", 2, 0, ["infra"],
     "Flag name: `payments.tokenize_cards`. Default off, enabled per-merchant first."),
    ("story", "Reconciliation report export (CSV) for finance",
     "todo", "high", "Priya Raman", 3, 0, ["reporting"],
     "Finance currently pulls numbers by hand. Export the daily settlement view with "
     "the same filters as the on-screen table."),
    ("bug", "Currency switch in checkout resets after page refresh",
     "todo", "high", "Jonas Weber", 3, 0, ["frontend"],
     "Selecting EUR then reloading drops back to the account default. The choice is "
     "held in memory instead of being persisted."),
    ("task", "Write the runbook for acquirer failover",
     "in_progress", "medium", "Dan Okafor", 2, 0, ["docs"],
     "Steps for cutting traffic to the secondary acquirer, including who to page and "
     "how to confirm settlement has caught up."),
    ("epic", "Identity: migrate legacy sessions to short-lived tokens",
     "todo", "high", "Ana Ferreira", None, None, ["identity"],
     "Umbrella for the session migration: token issuance, refresh rotation, and the "
     "SCIM role mapping that unblocks enterprise onboarding."),
    ("bug", "Session expiry logs users out mid-checkout on Safari",
     "in_review", "highest", "Jonas Weber", 3, 0, ["frontend", "bug-bash"],
     "Reproduced on Safari 17: the refresh call is blocked as cross-site, so the "
     "session dies before the payment sheet opens."),
    ("task", "Audit third-party SDK versions for known CVEs",
     "done", "low", "Priya Raman", 2, 0, ["security"],
     "Ran the dependency scan across the payments and identity services; two patch "
     "upgrades outstanding, tickets filed separately."),
    ("task", "Add a p95 latency panel to the payments dashboard",
     "in_progress", "medium", "Priya Raman", 2, 0, ["observability"],
     "The dashboard shows p50 only, which hides the tail that actually pages us."),
    ("story", "SSO: map SCIM groups to workspace roles",
     "todo", "medium", "Ana Ferreira", 8, None, ["identity"],
     "Admins want to drive roles from their directory instead of inviting people "
     "one at a time."),
    ("story", "Auto-close stale refund requests after 14 days",
     "todo", "low", "Mei Tan", 3, None, ["payments"],
     "Requests with no merchant response for two weeks should close with a clear "
     "reason rather than sitting open forever."),
    ("story", "Bulk import legacy ledger rows into the new schema",
     "todo", "highest", "Dan Okafor", 13, 1, ["data"],
     "Roughly 4M rows. Import in batches with a checkpoint table so a failed run can "
     "resume instead of restarting."),
    ("task", "Design review: tokenization empty and error states",
     "done", "low", "Jonas Weber", 1, 1, ["design"],
     "Reviewed the six empty states and the card-declined copy."),
    ("bug", "Rounding drift of 0.01 on multi-currency refunds",
     "todo", "highest", "Mei Tan", 5, 1, ["payments", "bug-bash"],
     "Refunding a partial amount in a non-settlement currency rounds twice: once at "
     "capture, once at refund. Reconcile to a single rounding point."),
    ("story", "Expose a signed-payload verification SDK for webhooks",
     "todo", "medium", "Mei Tan", 5, 1, ["api"],
     "Merchants are hand-rolling HMAC verification and getting it wrong. Ship a small "
     "library in Python, Node, and Go."),
    ("task", "Spike: Kafka versus SQS for ledger events",
     "todo", "low", "Ana Ferreira", 3, 1, ["infra"],
     "Timeboxed to two days. Decision matters for the ordering guarantees the ledger "
     "needs."),
]

SEED_COMMENTS = {
    # issue number -> [(author, body)]
    1: [
        ("Dan Okafor", "Please keep the back-reference column nullable so the rollback "
                       "path stays open for one release."),
        ("Mei Tan", "Agreed. Migration adds the column and backfills in batches; nothing "
                    "reads it until the flag flips."),
    ],
    2: [
        ("Priya Raman", "Reproduced with 40 concurrent retries. Event id is regenerated "
                        "on the retry instead of being reused."),
        ("Dan Okafor", "That is the bug. Reuse the original event id and let the ledger "
                       "unique index do the deduplication."),
        ("Mei Tan", "Fix is up, adding a regression test that posts the same event id "
                    "twenty times."),
    ],
    6: [
        ("Jonas Weber", "Storing the selection in a cookie fixes the reload, but I want "
                        "the account default to win on a fresh device."),
    ],
    9: [
        ("Ana Ferreira", "Only Safari. Chrome and Firefox keep the session because the "
                         "refresh call is same-site there."),
        ("Jonas Weber", "Moving the refresh to the same-site endpoint and proxying the "
                        "rest. Slightly more work on our side, no user-visible change."),
    ],
    16: [
        ("Priya Raman", "Finance flagged a 0.01 discrepancy on 3 of 400 refunds last "
                        "week; it lines up with this."),
        ("Mei Tan", "Rounding once at settlement and carrying the remainder should close "
                    "it. Verifying against the last month of refunds."),
    ],
}

SEED_ACTORS = ["Ana Ferreira", "Dan Okafor", "Mei Tan", "Jonas Weber", "Priya Raman"]


def seed(conn: sqlite3.Connection) -> None:
    """Create a demo workspace so the app is not an empty room on first run."""
    now = utcnow()

    projects = [
        {
            "key": "PHX",
            "name": "Phoenix Platform",
            "description": "Payments and identity for the merchant platform.",
            "lead": "Ana Ferreira",
            "members": [
                ("Ana Ferreira", "Product Lead"),
                ("Dan Okafor", "Engineering Lead"),
                ("Mei Tan", "Backend"),
                ("Jonas Weber", "Frontend"),
                ("Priya Raman", "QA"),
            ],
        },
        {
            "key": "MOB",
            "name": "Mobile App",
            "description": "The customer-facing iOS and Android client.",
            "lead": "Jonas Weber",
            "members": [
                ("Jonas Weber", "Mobile Lead"),
                ("Sofia Lindqvist", "iOS"),
                ("Tomas Berg", "Android"),
            ],
        },
    ]

    created = {}
    for spec in projects:
        cur = conn.execute(
            "INSERT INTO projects (key, name, description, lead, next_number, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (spec["key"], spec["name"], spec["description"], spec["lead"], now),
        )
        pid = cur.lastrowid
        created[spec["key"]] = pid
        for name, role in spec["members"]:
            conn.execute(
                "INSERT INTO members (project_id, name, role) VALUES (?, ?, ?)",
                (pid, name, role),
            )

    phx = created["PHX"]
    sprint_ids = []
    for name, goal, status, start_off, end_off in SEED_SPRINTS:
        cur = conn.execute(
            "INSERT INTO sprints (project_id, name, goal, status, starts_on, ends_on, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                phx,
                name,
                goal,
                status,
                (date.today() + timedelta(days=start_off)).isoformat(),
                (date.today() + timedelta(days=end_off)).isoformat(),
                now,
            ),
        )
        sprint_ids.append(cur.lastrowid)

    for index, spec in enumerate(SEED_ISSUES):
        itype, title, status, priority, assignee, points, sprint_idx, labels, description = spec
        number = index + 1
        sprint_id = sprint_ids[sprint_idx] if sprint_idx is not None else None
        reporter = SEED_ACTORS[index % len(SEED_ACTORS)]
        rank = float(
            sum(1 for s in SEED_ISSUES[: index + 1] if s[2] == status)
        ) * 1000.0
        cur = conn.execute(
            "INSERT INTO issues (project_id, number, type, title, description, status, "
            "priority, assignee, reporter, story_points, sprint_id, labels, due_date, "
            "rank, created_at, updated_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                phx,
                number,
                itype,
                title,
                description,
                status,
                priority,
                assignee,
                reporter,
                points,
                sprint_id,
                json.dumps(labels),
                None,
                rank,
                now,
                now,
                now if status == "done" else None,
            ),
        )
        issue_id = cur.lastrowid
        log_activity(conn, issue_id, reporter, "created", None, None)
        if assignee:
            log_activity(conn, issue_id, reporter, "assignee", None, assignee)
        if sprint_id:
            log_activity(conn, issue_id, reporter, "sprint", None, "Sprint")
        for author, body in SEED_COMMENTS.get(number, []):
            conn.execute(
                "INSERT INTO comments (issue_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (issue_id, author, body, now),
            )

    conn.execute("UPDATE projects SET next_number = ? WHERE id = ?", (len(SEED_ISSUES) + 1, phx))

    # A second, smaller project so switching workspaces is visible from the first run.
    mob = created["MOB"]
    mob_sprint = conn.execute(
        "INSERT INTO sprints (project_id, name, goal, status, starts_on, ends_on, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            mob,
            "Sprint 4",
            "Offline mode for the checkout flow.",
            "active",
            (date.today() + timedelta(days=-3)).isoformat(),
            (date.today() + timedelta(days=11)).isoformat(),
            now,
        ),
    ).lastrowid
    mob_issues = [
        ("story", "Queue failed captures and retry when the device is back online",
         "in_progress", "highest", "Sofia Lindqvist", 8, mob_sprint, ["offline"]),
        ("bug", "Deep link from a push notification lands on an empty cart",
         "todo", "high", "Tomas Berg", 3, mob_sprint, ["navigation"]),
        ("task", "Replace the deprecated barcode scanner dependency",
         "todo", "medium", "Jonas Weber", 2, None, ["tech-debt"]),
        ("story", "Face ID unlock for returning customers",
         "todo", "low", "Sofia Lindqvist", 5, None, ["auth"]),
        ("task", "Ship the crash-free-sessions dashboard",
         "done", "medium", "Tomas Berg", 2, mob_sprint, ["observability"]),
    ]
    for index, (itype, title, status, priority, assignee, points, sprint_id, labels) in enumerate(mob_issues):
        number = index + 1
        reporter = "Jonas Weber"
        cur = conn.execute(
            "INSERT INTO issues (project_id, number, type, title, description, status, "
            "priority, assignee, reporter, story_points, sprint_id, labels, rank, "
            "created_at, updated_at, resolved_at) "
            "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mob,
                number,
                itype,
                title,
                status,
                priority,
                assignee,
                reporter,
                points,
                sprint_id,
                json.dumps(labels),
                float(index + 1) * 1000.0,
                now,
                now,
                now if status == "done" else None,
            ),
        )
        log_activity(conn, cur.lastrowid, reporter, "created", None, None)
    conn.execute("UPDATE projects SET next_number = ? WHERE id = ?", (len(mob_issues) + 1, mob))
