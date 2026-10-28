"""HTTP-agnostic JSON API.

`handle()` returns `(status, payload)` for any `/api/...` request, or `None`
when nothing matches. It owns the transaction: handlers do their writes and
this module commits on success, rolls back on failure.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
import urllib.parse

from . import auth, db, notify, storage

# Fallback attribution for callers with no signed-in user -- the CLI and the
# API tests. A real request always carries one, and `Ctx.actor` then returns
# that person's name, so the activity trail, the reporter and the comment
# author all say who actually did the work.
ACTOR = "You"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


ROUTES = []


def route(method, pattern):
    compiled = re.compile(pattern)

    def register(fn):
        ROUTES.append((method, compiled, fn))
        return fn

    return register


class Ctx:
    def __init__(self, conn, query, body, user=None, token=None):
        self.conn = conn
        self.query = query
        self.body = body or {}
        # Always the serialized account shape, whether the caller arrived with
        # a users row or an already-serialized dict.
        self.user = auth.user_fields(user)
        # The caller's own session token, so an account change can end every
        # other session without ending the one making the change.
        self.token = token or ""

    @property
    def actor(self):
        return self.user.get("name") or ACTOR

    @property
    def is_admin(self):
        return bool(self.user.get("is_admin"))

    def require_admin(self):
        if not self.is_admin:
            raise ApiError(403, "Only an administrator can manage accounts")

    def q(self, name, default=None):
        values = self.query.get(name)
        if not values:
            return default
        return values[0]

    def param(self, name, default=None):
        value = self.body.get(name, default)
        return value

    def require(self, name):
        value = self.body.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ApiError(400, "`%s` is required" % name)
        return value


def handle(method, path, query, body, conn, user=None, token=None):
    for route_method, pattern, fn in ROUTES:
        if route_method != method:
            continue
        match = pattern.fullmatch(path)
        if not match:
            continue
        ctx = Ctx(conn, query, body, user, token)
        try:
            result = fn(ctx, **match.groupdict())
            conn.commit()
            return result
        except (ApiError, auth.AuthError):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
    return None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clean_choice(value, allowed, field):
    if value is None:
        raise ApiError(422, "`%s` cannot be null" % field)
    value = str(value)
    if value not in allowed:
        raise ApiError(422, "`%s` must be one of: %s" % (field, ", ".join(allowed)))
    return value


def _clean_points(value):
    if value in (None, ""):
        return None
    try:
        points = int(value)
    except (TypeError, ValueError):
        raise ApiError(422, "`story_points` must be a whole number")
    if points < 0 or points > 100:
        raise ApiError(422, "`story_points` must be between 0 and 100")
    return points


def _clean_labels(value):
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, list):
        parts = [str(p).strip() for p in value]
    else:
        raise ApiError(422, "`labels` must be a list or a comma-separated string")
    return json.dumps([p for p in parts if p][:12])


def _clean_date(value, field):
    if value in (None, ""):
        return None
    text = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ApiError(422, "`%s` must look like YYYY-MM-DD" % field)
    return text


def _clean_text(value):
    if value is None:
        return None
    return str(value).strip()


def _project_or_404(ctx, ref):
    row = db.get_project(ctx.conn, ref)
    if row is None:
        raise ApiError(404, "No project matching `%s`" % ref)
    return row


def _issue_or_404(ctx, issue_id):
    row = ctx.conn.execute(db.ISSUE_SELECT + " WHERE i.id = ?", (issue_id,)).fetchone()
    if row is None:
        raise ApiError(404, "No issue with id %s" % issue_id)
    return row


def _sprint_or_404(ctx, sprint_id):
    row = ctx.conn.execute("SELECT * FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
    if row is None:
        raise ApiError(404, "No sprint with id %s" % sprint_id)
    return row


def _attachment_or_404(ctx, attachment_id):
    row = ctx.conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "No attachment with id %s" % attachment_id)
    return row


def _comment_payload(conn, row):
    """A comment with the files hanging off it.

    Attachments hang on the comment rather than on the issue: a screenshot is
    part of the sentence somebody was making, and a bare list of files on the
    issue would lose which remark each one belongs to.
    """
    payload = db.serialize_comment(row)
    payload["attachments"] = [
        db.serialize_attachment(a) for a in conn.execute(
            "SELECT * FROM attachments WHERE comment_id = ? ORDER BY id", (row["id"],))
    ]
    return payload


def _attachment_ids(ctx, issue_id):
    """Validate the ids a comment claims, and insist they are unclaimed.

    An id from a different issue is a 404 rather than a 403: it should not
    confirm that some other issue's attachment exists.
    """
    raw = ctx.body.get("attachment_ids")
    if raw in (None, [], ""):
        return []
    if not isinstance(raw, list):
        raise ApiError(422, "`attachment_ids` must be a list")
    ids = []
    for value in raw:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            raise ApiError(422, "`attachment_ids` must be whole numbers")
    ids = list(dict.fromkeys(ids))
    if len(ids) > 10:
        raise ApiError(422, "A comment can carry at most 10 files")
    for attachment_id in ids:
        found = ctx.conn.execute(
            "SELECT issue_id, comment_id FROM attachments WHERE id = ?", (attachment_id,)
        ).fetchone()
        if found is None or found["issue_id"] != issue_id:
            raise ApiError(404, "No attachment %s on this issue" % attachment_id)
        if found["comment_id"] is not None:
            raise ApiError(409, "Attachment %s is already on a comment" % attachment_id)
    return ids


def _label_for(value, vocabulary):
    """Turn a stored enum value into something readable for the activity feed."""
    if value in (None, ""):
        return "none"
    if value in vocabulary:
        return value.replace("_", " ")
    return str(value)


# --------------------------------------------------------------------------
# bootstrap + projects
# --------------------------------------------------------------------------

@route("GET", r"/api/bootstrap")
def bootstrap(ctx):
    projects = ctx.conn.execute(
        "SELECT p.id, p.key, p.name, p.lead, "
        "       (SELECT COUNT(*) FROM issues i WHERE i.project_id = p.id) AS total_issues, "
        "       (SELECT COUNT(*) FROM issues i WHERE i.project_id = p.id AND i.status <> 'done') AS open_issues, "
        "       (SELECT s.name FROM sprints s WHERE s.project_id = p.id AND s.status = 'active' "
        "         ORDER BY s.id LIMIT 1) AS active_sprint "
        "  FROM projects p ORDER BY p.id"
    ).fetchall()
    people = ctx.conn.execute(
        "SELECT name, username FROM users WHERE is_active = 1 "
        "ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return 200, {
        "projects": [
            {
                "id": p["id"],
                "key": p["key"],
                "name": p["name"],
                "lead": p["lead"],
                "total_issues": p["total_issues"],
                "open_issues": p["open_issues"],
                "active_sprint": p["active_sprint"],
            }
            for p in projects
        ],
        "meta": {
            "statuses": db.STATUSES,
            "priorities": db.PRIORITIES,
            "types": db.TYPES,
            "sprint_statuses": db.SPRINT_STATUSES,
        },
        # Who a ticket can be assigned to. This is the workspace's people, not
        # a project's members: a new project starts with only its lead as a
        # member, so offering just members left the assignee picker with one
        # name in it. Names and usernames only -- nothing here is private from
        # a colleague who can already see who reported and closed an issue.
        "people": [
            {"name": p["name"], "username": p["username"]} for p in people
        ],
        # `me` stays a plain display name: the client uses it as the default
        # assignee and comment author. `user` is the account behind it.
        "me": ctx.actor,
        "user": ctx.user or None,
    }


@route("POST", r"/api/projects")
def create_project(ctx):
    key = str(ctx.require("key")).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", key):
        raise ApiError(422, "Project key must be 2-10 characters, letters and digits, "
                            "starting with a letter")
    if db.get_project(ctx.conn, key) is not None:
        raise ApiError(409, "Project key `%s` is already taken" % key)
    cur = ctx.conn.execute(
        "INSERT INTO projects (key, name, description, lead, next_number, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (
            key,
            str(ctx.require("name")).strip(),
            _clean_text(ctx.body.get("description")) or "",
            _clean_text(ctx.body.get("lead")) or ctx.actor,
            db.utcnow(),
        ),
    )
    project_id = cur.lastrowid
    for name in ctx.body.get("members") or [ctx.body.get("lead") or ctx.actor]:
        name = _clean_text(name)
        if name:
            ctx.conn.execute(
                "INSERT OR IGNORE INTO members (project_id, name, role) VALUES (?, ?, ?)",
                (project_id, name, "Member"),
            )
    return 201, db.project_payload(ctx.conn, _project_or_404(ctx, project_id))


@route("GET", r"/api/projects/(?P<ref>[A-Za-z0-9\-]+)")
def get_project(ctx, ref):
    project = _project_or_404(ctx, ref)
    return 200, db.project_payload(ctx.conn, project)


# --------------------------------------------------------------------------
# issues
# --------------------------------------------------------------------------

@route("GET", r"/api/issues")
def list_issues(ctx):
    sql = db.ISSUE_SELECT
    where = []
    params = []

    project = ctx.q("project")
    if project:
        if project.isdigit():
            where.append("i.project_id = ?")
            params.append(int(project))
        else:
            where.append("p.key = ? COLLATE NOCASE")
            params.append(project)

    status = ctx.q("status")
    if status and status != "all":
        values = [s for s in status.split(",") if s in db.STATUSES]
        if values:
            where.append("i.status IN (%s)" % ",".join("?" * len(values)))
            params.extend(values)

    sprint = ctx.q("sprint")
    if sprint == "backlog":
        where.append("i.sprint_id IS NULL")
    elif sprint == "active":
        where.append("s.status = 'active'")
    elif sprint and sprint.isdigit():
        where.append("i.sprint_id = ?")
        params.append(int(sprint))

    assignee = ctx.q("assignee")
    if assignee == "unassigned":
        where.append("i.assignee = ''")
    elif assignee and assignee != "all":
        where.append("i.assignee = ?")
        params.append(assignee)

    priority = ctx.q("priority")
    if priority and priority != "all":
        where.append("i.priority = ?")
        params.append(priority)

    itype = ctx.q("type")
    if itype and itype != "all":
        where.append("i.type = ?")
        params.append(itype)

    search = ctx.q("q")
    if search:
        needle = "%%%s%%" % search.lower()
        where.append("(LOWER(i.title) LIKE ? OR LOWER(i.description) LIKE ? "
                     "OR LOWER(i.labels) LIKE ? OR (p.key || '-' || i.number) LIKE ?)")
        params.extend([needle, needle, needle, "%%%s%%" % search.lower()])

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.rank, i.id"

    rows = ctx.conn.execute(sql, params).fetchall()
    return 200, {"issues": [db.serialize_issue(r) for r in rows], "count": len(rows)}


@route("POST", r"/api/issues")
def create_issue(ctx):
    project = _project_or_404(ctx, ctx.require("project"))
    title = str(ctx.require("title")).strip()
    if len(title) > 240:
        raise ApiError(422, "Title must be 240 characters or fewer")

    itype = _clean_choice(ctx.body.get("type") or "task", db.TYPES, "type")
    status = _clean_choice(ctx.body.get("status") or "todo", db.STATUSES, "status")
    priority = _clean_choice(ctx.body.get("priority") or "medium", db.PRIORITIES, "priority")
    points = _clean_points(ctx.body.get("story_points"))
    labels = _clean_labels(ctx.body.get("labels")) or "[]"
    due = _clean_date(ctx.body.get("due_date"), "due_date")

    sprint_id = ctx.body.get("sprint_id")
    if sprint_id in ("", None):
        sprint_id = None
    else:
        sprint = _sprint_or_404(ctx, sprint_id)
        if sprint["project_id"] != project["id"]:
            raise ApiError(422, "That sprint belongs to a different project")

    assignee = _clean_text(ctx.body.get("assignee")) or ""
    if assignee:
        members = {m["name"] for m in ctx.conn.execute(
            "SELECT name FROM members WHERE project_id = ?", (project["id"],))}
        if assignee not in members:
            ctx.conn.execute(
                "INSERT OR IGNORE INTO members (project_id, name, role) VALUES (?, ?, ?)",
                (project["id"], assignee, "Member"),
            )

    now = db.utcnow()
    number = db.next_issue_number(ctx.conn, project["id"])
    cur = ctx.conn.execute(
        "INSERT INTO issues (project_id, number, type, title, description, status, priority, "
        "assignee, reporter, story_points, sprint_id, labels, due_date, rank, created_at, "
        "updated_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project["id"],
            number,
            itype,
            title,
            _clean_text(ctx.body.get("description")) or "",
            status,
            priority,
            assignee,
            _clean_text(ctx.body.get("reporter")) or ctx.actor,
            points,
            sprint_id,
            labels,
            due,
            db.rank_for_new_issue(ctx.conn, project["id"], status),
            now,
            now,
            now if status == "done" else None,
        ),
    )
    issue_id = cur.lastrowid
    db.log_activity(ctx.conn, issue_id, ctx.actor, "created", None, None)
    if assignee:
        db.log_activity(ctx.conn, issue_id, ctx.actor, "assignee", None, assignee)
    if sprint_id:
        sprint = _sprint_or_404(ctx, sprint_id)
        db.log_activity(ctx.conn, issue_id, ctx.actor, "sprint", None, sprint["name"])
    # Queued in this same transaction: if the issue does not save, no mail is
    # owed, and if it does, the row is already here for the sender thread.
    created_row = _issue_or_404(ctx, issue_id)
    if assignee:
        notify.notify_assignment(ctx.conn, created_row, ctx.actor, assignee)
    return 201, {"issue": db.serialize_issue(created_row)}


@route("GET", r"/api/issues/(?P<issue_id>\d+)")
def get_issue(ctx, issue_id):
    row = _issue_or_404(ctx, int(issue_id))
    comments = ctx.conn.execute(
        "SELECT * FROM comments WHERE issue_id = ? ORDER BY created_at, id", (row["id"],)
    ).fetchall()
    activity = ctx.conn.execute(
        "SELECT * FROM activity WHERE issue_id = ? ORDER BY created_at DESC, id DESC LIMIT 60",
        (row["id"],),
    ).fetchall()
    return 200, {
        "issue": db.serialize_issue(row),
        "comments": [_comment_payload(ctx.conn, c) for c in comments],
        "activity": [db.serialize_activity(a) for a in activity],
    }


EDITABLE = {
    "title", "description", "status", "priority", "type", "assignee",
    "reporter", "story_points", "sprint_id", "labels", "due_date",
}


@route("PATCH", r"/api/issues/(?P<issue_id>\d+)")
def update_issue(ctx, issue_id):
    row = _issue_or_404(ctx, int(issue_id))
    project = ctx.conn.execute(
        "SELECT * FROM projects WHERE id = ?", (row["project_id"],)
    ).fetchone()

    updates = {}
    changes = []

    for field in EDITABLE:
        if field not in ctx.body:
            continue
        raw = ctx.body[field]
        if field == "title":
            title = _clean_text(raw)
            if not title:
                raise ApiError(422, "Title cannot be empty")
            if len(title) > 240:
                raise ApiError(422, "Title must be 240 characters or fewer")
            updates["title"] = title
        elif field == "description":
            updates["description"] = _clean_text(raw) or ""
        elif field == "status":
            updates["status"] = _clean_choice(raw, db.STATUSES, "status")
        elif field == "priority":
            updates["priority"] = _clean_choice(raw, db.PRIORITIES, "priority")
        elif field == "type":
            updates["type"] = _clean_choice(raw, db.TYPES, "type")
        elif field == "assignee":
            updates["assignee"] = _clean_text(raw) or ""
        elif field == "reporter":
            updates["reporter"] = _clean_text(raw) or ctx.actor
        elif field == "story_points":
            updates["story_points"] = _clean_points(raw)
        elif field == "due_date":
            updates["due_date"] = _clean_date(raw, "due_date")
        elif field == "labels":
            updates["labels"] = _clean_labels(raw) or "[]"
        elif field == "sprint_id":
            if raw in ("", None, "backlog"):
                updates["sprint_id"] = None
            else:
                sprint = _sprint_or_404(ctx, raw)
                if sprint["project_id"] != project["id"]:
                    raise ApiError(422, "That sprint belongs to a different project")
                updates["sprint_id"] = sprint["id"]

    for field, value in updates.items():
        old = row[field]
        if field == "labels":
            if db._labels(old) == db._labels(value):
                continue
            changes.append((field, ", ".join(db._labels(old)) or "none",
                            ", ".join(db._labels(value)) or "none"))
        elif old == value:
            continue
        elif field == "sprint_id":
            old_name = "backlog"
            if old:
                found = ctx.conn.execute("SELECT name FROM sprints WHERE id = ?", (old,)).fetchone()
                old_name = found["name"] if found else "backlog"
            new_name = "backlog"
            if value:
                found = ctx.conn.execute("SELECT name FROM sprints WHERE id = ?", (value,)).fetchone()
                new_name = found["name"] if found else "backlog"
            changes.append((field, old_name, new_name))
        elif field == "story_points":
            changes.append((field, old, value))
        else:
            vocabulary = {"status": db.STATUSES, "priority": db.PRIORITIES, "type": db.TYPES}.get(field, [])
            changes.append((field, _label_for(old, vocabulary) if field in
                            ("status", "priority", "type") else old,
                            _label_for(value, vocabulary) if field in
                            ("status", "priority", "type") else value))

    if not updates:
        return 200, {"issue": db.serialize_issue(_issue_or_404(ctx, int(issue_id)))}

    now = db.utcnow()
    assignments = ", ".join("%s = ?" % f for f in updates)
    values = list(updates.values())
    resolved_at = row["resolved_at"]
    if "status" in updates:
        resolved_at = now if updates["status"] == "done" else None
    ctx.conn.execute(
        "UPDATE issues SET %s, updated_at = ?, resolved_at = ? WHERE id = ?" % assignments,
        values + [now, resolved_at, row["id"]],
    )

    for field, before, after in changes:
        db.log_activity(ctx.conn, row["id"], ctx.actor, field, before, after)

    # `index` lets the board place a dragged card precisely within its column.
    if "index" in ctx.body:
        db.reorder_issue(ctx.conn, row["id"], updates.get("status", row["status"]), ctx.body["index"])
    elif "status" in updates:
        db.reorder_issue(ctx.conn, row["id"], updates["status"], None)

    saved = _issue_or_404(ctx, int(issue_id))
    # A reassignment is its own message, to the person arriving rather than the
    # one leaving; anything else goes to whoever is now on the ticket. The
    # sender thread does the sending -- see notify.py for why this is a queue.
    if "assignee" in updates:
        notify.notify_assignment(ctx.conn, saved, ctx.actor, saved["assignee"], row["assignee"])
    if changes and "assignee" not in updates:
        notify.notify_change(ctx.conn, saved, ctx.actor, changes)

    return 200, {"issue": db.serialize_issue(saved)}


@route("POST", r"/api/issues/(?P<issue_id>\d+)/comments")
def add_comment(ctx, issue_id):
    row = _issue_or_404(ctx, int(issue_id))
    body = _clean_text(ctx.body.get("body")) or ""
    attachment_ids = _attachment_ids(ctx, row["id"])
    # A comment may be nothing but a screenshot: "here is the error" is often
    # the whole message. Text is required only when there is no file with it.
    if not body and not attachment_ids:
        raise ApiError(400, "`body` is required")
    if len(body) > 20000:
        raise ApiError(422, "A comment must be 20,000 characters or fewer")

    cur = ctx.conn.execute(
        "INSERT INTO comments (issue_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (row["id"], _clean_text(ctx.body.get("author")) or ctx.actor, body, db.utcnow()),
    )
    comment_id = cur.lastrowid
    for attachment_id in attachment_ids:
        ctx.conn.execute(
            "UPDATE attachments SET comment_id = ? WHERE id = ?", (comment_id, attachment_id)
        )
    ctx.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (db.utcnow(), row["id"]))
    db.log_activity(ctx.conn, row["id"], ctx.actor, "comment", None, None)
    # Whoever is @mentioned, plus the assignee if they were not one of them.
    notify.notify_comment(ctx.conn, _issue_or_404(ctx, row["id"]), ctx.actor, body)
    comment = ctx.conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    return 201, {"comment": _comment_payload(ctx.conn, comment)}


@route("POST", r"/api/issues/(?P<issue_id>\d+)/attachments")
def upload_attachment(ctx, issue_id):
    """Take one file, as base64 inside JSON.

    Not multipart/form-data: this server is the standard library only, and
    hand-writing a multipart parser to save a third of the bytes on a
    screenshot is the worse trade. `server.py` caps the request size before
    any of this runs.
    """
    row = _issue_or_404(ctx, int(issue_id))
    filename = storage.clean_filename(ctx.require("filename"))
    encoded = re.sub(r"\s+", "", str(ctx.body.get("data") or ""))
    if not encoded:
        raise ApiError(400, "`data` is required")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, "`data` must be base64")
    if not payload:
        raise ApiError(422, "That file is empty")
    if len(payload) > storage.MAX_BYTES:
        raise ApiError(413, "Files are limited to %d MB" % (storage.MAX_BYTES // (1024 * 1024)))

    # The bytes decide whether this is an image, not the client's claim -- see
    # storage.sniff_image. The declared type is kept for files that are not
    # images, only if it looks like a real media type.
    image_type = storage.sniff_image(payload)
    declared = str(ctx.body.get("content_type") or "").strip()
    if image_type:
        content_type = image_type
    elif re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", declared):
        content_type = declared.lower()
    else:
        content_type = "application/octet-stream"

    stored_name = storage.store(ctx.conn, payload)
    try:
        cur = ctx.conn.execute(
            "INSERT INTO attachments (issue_id, comment_id, uploader, filename, stored_name, "
            "content_type, byte_size, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?, ?)",
            (row["id"], ctx.actor, filename, stored_name, content_type, len(payload),
             db.utcnow()),
        )
        ctx.conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?",
                         (db.utcnow(), row["id"]))
    except Exception:
        # The row is rolled back by handle(); the file it pointed at would
        # otherwise be left behind with nothing referencing it.
        storage.remove(ctx.conn, stored_name)
        raise
    saved = _attachment_or_404(ctx, cur.lastrowid)
    return 201, {"attachment": db.serialize_attachment(saved)}


@route("GET", r"/api/attachments/(?P<attachment_id>\d+)")
def get_attachment(ctx, attachment_id):
    return 200, {"attachment": db.serialize_attachment(_attachment_or_404(ctx, int(attachment_id)))}


def read_attachment(conn, attachment_id):
    """The bytes of one attachment -- the only route here that is not JSON.

    Called straight from `server.py` rather than reached through `handle`,
    because a file has to leave as bytes with its own Content-Type. The return
    is the same shape the HTTP layer already understands, with a fourth slot
    carrying the type to send instead of application/json.
    """
    ctx = Ctx(conn, {}, {})
    row = _attachment_or_404(ctx, int(attachment_id))
    try:
        payload = storage.read(conn, row["stored_name"])
    except OSError:
        raise ApiError(404, "That file is missing from the server")

    content_type, inline = storage.serving(row["content_type"])
    # nosniff is what stops a browser from deciding an uploaded file is HTML
    # because it looks like HTML -- the header is the lock, the magic-number
    # check above is the bolt.
    headers = [("X-Content-Type-Options", "nosniff")]
    quoted = urllib.parse.quote(row["filename"])
    disposition = "inline" if inline else "attachment"
    headers.append(("Content-Disposition",
                    "%s; filename*=UTF-8''%s" % (disposition, quoted)))
    return 200, payload, headers, content_type


# --------------------------------------------------------------------------
# sprints
# --------------------------------------------------------------------------

@route("GET", r"/api/projects/(?P<ref>[A-Za-z0-9\-]+)/sprints")
def list_sprints(ctx, ref):
    project = _project_or_404(ctx, ref)
    rows = ctx.conn.execute(
        "SELECT * FROM sprints WHERE project_id = ? "
        "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'planned' THEN 1 ELSE 2 END, id",
        (project["id"],),
    ).fetchall()
    return 200, {"sprints": [db.serialize_sprint(s) for s in rows]}


@route("POST", r"/api/projects/(?P<ref>[A-Za-z0-9\-]+)/sprints")
def create_sprint(ctx, ref):
    project = _project_or_404(ctx, ref)
    name = str(ctx.require("name")).strip()
    cur = ctx.conn.execute(
        "INSERT INTO sprints (project_id, name, goal, status, starts_on, ends_on, created_at) "
        "VALUES (?, ?, ?, 'planned', ?, ?, ?)",
        (
            project["id"],
            name,
            _clean_text(ctx.body.get("goal")) or "",
            _clean_date(ctx.body.get("starts_on"), "starts_on"),
            _clean_date(ctx.body.get("ends_on"), "ends_on"),
            db.utcnow(),
        ),
    )
    row = ctx.conn.execute("SELECT * FROM sprints WHERE id = ?", (cur.lastrowid,)).fetchone()
    return 201, {"sprint": db.serialize_sprint(row)}


@route("POST", r"/api/sprints/(?P<sprint_id>\d+)/(?P<action>start|complete|reopen)")
def sprint_transition(ctx, sprint_id, action):
    sprint = _sprint_or_404(ctx, int(sprint_id))

    if action == "start":
        if sprint["status"] == "active":
            raise ApiError(409, "That sprint is already active")
        active = ctx.conn.execute(
            "SELECT name FROM sprints WHERE project_id = ? AND status = 'active' AND id <> ?",
            (sprint["project_id"], sprint["id"]),
        ).fetchone()
        if active is not None:
            raise ApiError(409, "`%s` is still active — complete it first" % active["name"])
        ctx.conn.execute(
            "UPDATE sprints SET status = 'active', starts_on = COALESCE(starts_on, ?) WHERE id = ?",
            (db.today(), sprint["id"]),
        )

    elif action == "complete":
        target = ctx.body.get("move_to")  # sprint id, or "backlog" (default)
        unfinished = ctx.conn.execute(
            "SELECT id FROM issues WHERE sprint_id = ? AND status <> 'done'", (sprint["id"],)
        ).fetchall()
        destination = None
        if target not in (None, "", "backlog"):
            other = _sprint_or_404(ctx, target)
            if other["project_id"] != sprint["project_id"] or other["id"] == sprint["id"]:
                raise ApiError(422, "Move unfinished work to a different sprint in this project")
            destination = other["id"]
        for issue in unfinished:
            ctx.conn.execute("UPDATE issues SET sprint_id = ? WHERE id = ?", (destination, issue["id"]))
        ctx.conn.execute(
            "UPDATE sprints SET status = 'completed', ends_on = COALESCE(ends_on, ?) WHERE id = ?",
            (db.today(), sprint["id"]),
        )

    else:  # reopen
        ctx.conn.execute("UPDATE sprints SET status = 'planned' WHERE id = ?", (sprint["id"],))

    row = ctx.conn.execute("SELECT * FROM sprints WHERE id = ?", (sprint["id"],)).fetchone()
    return 200, {
        "sprint": db.serialize_sprint(row),
        "moved_to_backlog": len(unfinished) if action == "complete" else 0,
    }


@route("PATCH", r"/api/sprints/(?P<sprint_id>\d+)")
def update_sprint(ctx, sprint_id):
    sprint = _sprint_or_404(ctx, int(sprint_id))
    updates = {}
    for field in ("name", "goal"):
        if field in ctx.body:
            value = _clean_text(ctx.body[field])
            if field == "name" and not value:
                raise ApiError(422, "Sprint name cannot be empty")
            updates[field] = value or ""
    for field in ("starts_on", "ends_on"):
        if field in ctx.body:
            updates[field] = _clean_date(ctx.body[field], field)
    if not updates:
        return 200, {"sprint": db.serialize_sprint(sprint)}
    assignments = ", ".join("%s = ?" % f for f in updates)
    ctx.conn.execute(
        "UPDATE sprints SET %s WHERE id = ?" % assignments, list(updates.values()) + [sprint["id"]]
    )
    row = ctx.conn.execute("SELECT * FROM sprints WHERE id = ?", (sprint["id"],)).fetchone()
    return 200, {"sprint": db.serialize_sprint(row)}


@route("DELETE", r"/api/sprints/(?P<sprint_id>\d+)")
def delete_sprint(ctx, sprint_id):
    sprint = _sprint_or_404(ctx, int(sprint_id))
    if sprint["status"] == "active":
        raise ApiError(409, "Complete the sprint before deleting it")
    ctx.conn.execute("UPDATE issues SET sprint_id = NULL WHERE sprint_id = ?", (sprint["id"],))
    ctx.conn.execute("DELETE FROM sprints WHERE id = ?", (sprint["id"],))
    return 200, {"deleted": sprint["id"]}


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

@route("GET", r"/api/projects/(?P<ref>[A-Za-z0-9\-]+)/stats")
def project_stats(ctx, ref):
    project = _project_or_404(ctx, ref)
    pid = project["id"]

    def group(column, vocabulary):
        rows = ctx.conn.execute(
            "SELECT %s AS value, COUNT(*) AS count, "
            "       COALESCE(SUM(story_points), 0) AS points "
            "  FROM issues WHERE project_id = ? GROUP BY %s" % (column, column),
            (pid,),
        ).fetchall()
        found = {r["value"]: {"count": r["count"], "points": r["points"]} for r in rows}
        return [
            {"value": key, "count": found.get(key, {}).get("count", 0),
             "points": found.get(key, {}).get("points", 0)}
            for key in vocabulary
        ]

    totals = ctx.conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(status = 'done') AS done, "
        "       COALESCE(SUM(story_points), 0) AS points, "
        "       COALESCE(SUM(CASE WHEN status = 'done' THEN story_points END), 0) AS done_points, "
        "       SUM(assignee = '') AS unassigned, "
        "       SUM(sprint_id IS NULL) AS backlog "
        "  FROM issues WHERE project_id = ?",
        (pid,),
    ).fetchone()

    active = ctx.conn.execute(
        "SELECT * FROM sprints WHERE project_id = ? AND status = 'active' ORDER BY id LIMIT 1",
        (pid,),
    ).fetchone()
    active_payload = None
    if active is not None:
        sprint_issues = ctx.conn.execute(
            "SELECT status, story_points FROM issues WHERE sprint_id = ?", (active["id"],)
        ).fetchall()
        active_payload = {
            "sprint": db.serialize_sprint(active),
            "total": len(sprint_issues),
            "done": sum(1 for i in sprint_issues if i["status"] == "done"),
            "points": sum(i["story_points"] or 0 for i in sprint_issues),
            "done_points": sum(i["story_points"] or 0 for i in sprint_issues if i["status"] == "done"),
        }

    by_assignee = ctx.conn.execute(
        "SELECT assignee AS name, COUNT(*) AS count, "
        "       COALESCE(SUM(CASE WHEN status <> 'done' THEN story_points END), 0) AS open_points "
        "  FROM issues WHERE project_id = ? GROUP BY assignee ORDER BY count DESC, name",
        (pid,),
    ).fetchall()

    recent = ctx.conn.execute(
        db.ISSUE_SELECT + " WHERE i.project_id = ? ORDER BY i.updated_at DESC, i.id DESC LIMIT 8",
        (pid,),
    ).fetchall()

    return 200, {
        "project": project["key"],
        "totals": {
            "total": totals["total"] or 0,
            "done": totals["done"] or 0,
            "points": totals["points"] or 0,
            "done_points": totals["done_points"] or 0,
            "unassigned": totals["unassigned"] or 0,
            "backlog": totals["backlog"] or 0,
        },
        "by_status": group("status", db.STATUSES),
        "by_priority": group("priority", db.PRIORITIES),
        "by_type": group("type", db.TYPES),
        "by_assignee": [
            {"name": r["name"] or "Unassigned", "count": r["count"], "open_points": r["open_points"]}
            for r in by_assignee
        ],
        "active_sprint": active_payload,
        "recent": [db.serialize_issue(i) for i in recent],
    }


# --------------------------------------------------------------------------
# accounts -- administrators only
# --------------------------------------------------------------------------

@route("GET", r"/api/users")
def list_accounts(ctx):
    ctx.require_admin()
    rows = auth.list_users(ctx.conn)
    return 200, {"users": [auth.serialize_user(r) for r in rows]}


@route("POST", r"/api/users")
def create_account(ctx):
    ctx.require_admin()
    # An administrator hands a first password to a person. If none is supplied
    # one is generated here and returned exactly once, in this response, so it
    # can be passed on out of band -- it is never readable again afterwards.
    password = ctx.body.get("password") or auth.generate_password()
    row = auth.create_user(
        ctx.conn,
        name=ctx.require("name"),
        username=ctx.require("username"),
        password=password,
        email=ctx.body.get("email"),
        is_admin=bool(ctx.body.get("is_admin")),
    )
    return 201, {"user": auth.serialize_user(row), "password": password}


@route("POST", r"/api/users/(?P<user_id>\d+)/password")
def reset_account_password(ctx, user_id):
    """Set a new password -- supplied, or generated and returned once."""
    ctx.require_admin()
    row = auth.get_user(ctx.conn, int(user_id))
    if row is None:
        raise ApiError(404, "No account with id %s" % user_id)
    password = ctx.body.get("password") or auth.generate_password()
    auth.set_password(ctx.conn, row["id"], password)
    # Resetting a password ends every session for that account: the usual
    # reason to reset one is that somebody else had it. An administrator
    # changing their own keeps the browser they are working in.
    keep = ctx.token if ctx.user.get("id") == row["id"] else None
    signed_out = auth.end_all_sessions(ctx.conn, row["id"], keep_token=keep)
    return 200, {
        "user": auth.serialize_user(auth.get_user(ctx.conn, row["id"])),
        "password": password,
        "sessions_ended": signed_out,
    }


@route("PATCH", r"/api/users/(?P<user_id>\d+)")
def update_account(ctx, user_id):
    ctx.require_admin()
    row = auth.get_user(ctx.conn, int(user_id))
    if row is None:
        raise ApiError(404, "No account with id %s" % user_id)
    target_id = row["id"]
    is_self = ctx.user.get("id") == target_id

    if "name" in ctx.body:
        auth.rename_user(ctx.conn, target_id, ctx.body["name"])

    if ctx.body.get("password"):
        auth.set_password(ctx.conn, target_id, ctx.body["password"])
        auth.end_all_sessions(ctx.conn, target_id,
                              keep_token=ctx.token if is_self else None)

    if "is_active" in ctx.body:
        active = bool(ctx.body["is_active"])
        if is_self and not active:
            raise ApiError(422, "You cannot deactivate your own account")
        if not active:
            _protect_last_admin(ctx, row, "deactivate")
        auth.set_active(ctx.conn, target_id, active)

    if "is_admin" in ctx.body:
        is_admin = bool(ctx.body["is_admin"])
        if is_self and not is_admin:
            raise ApiError(422, "You cannot remove your own administrator access")
        if not is_admin:
            _protect_last_admin(ctx, row, "demote")
        auth.set_admin(ctx.conn, target_id, is_admin)

    return 200, {"user": auth.serialize_user(auth.get_user(ctx.conn, target_id))}


def _protect_last_admin(ctx, row, verb):
    """Refuse a change that would leave nobody able to manage accounts.

    A backstop, not the first line of defence. Through today's routes the
    self-checks in `update_account` fire first: only an active account holds a
    session, so an administrator acting on a *different* administrator always
    leaves one behind, and the self-checks catch the rest. It stays because the
    rule it enforces is the one that matters, and a future route that forgot it
    should trip over this rather than quietly empty the administrator seat.
    """
    if row["is_admin"] and row["is_active"] and auth.count_active_admins(ctx.conn) <= 1:
        raise ApiError(
            409,
            "That is the only active administrator -- to %s it would lock "
            "everyone out of account management" % verb,
        )


@route("DELETE", r"/api/users/(?P<user_id>\d+)")
def delete_account(ctx, user_id):
    ctx.require_admin()
    row = auth.get_user(ctx.conn, int(user_id))
    if row is None:
        raise ApiError(404, "No account with id %s" % user_id)
    if ctx.user.get("id") == row["id"]:
        raise ApiError(422, "You cannot delete your own account")
    _protect_last_admin(ctx, row, "delete")
    auth.delete_user(ctx.conn, row["id"])
    return 200, {"deleted": row["id"], "username": row["username"]}
