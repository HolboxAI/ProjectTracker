"""Who gets told what, and the queue that gets it there.

Two rules shape this whole module:

1. **Queue, do not send.** An SMTP conversation takes a second or more, and a
   ticket edit must not wait for it -- still less roll back because a mail
   server was busy. The row is written in the same transaction as the change,
   so a notification can never be lost by a crash after the ticket saved, and
   a background thread sends it afterwards. `notifications.sent_at IS NULL` is
   the queue.

2. **Nobody is told about their own action.** Being emailed that you yourself
   moved a card is noise, and it trains people to filter the address.
"""

from __future__ import annotations

import re

from . import db, mailer

# Reasons a person hears about a ticket. Kept as column values, not prose.
ASSIGNED = "assigned"
UPDATED = "updated"
COMMENTED = "commented"
MENTIONED = "mentioned"

# How many rows one pass of the sender picks up.
BATCH = 20
# Attempts before a row is left alone. A wrong password should not mean an
# infinite retry loop against the provider.
MAX_ATTEMPTS = 5


def _account_by_name(conn, name):
    """The active account with this display name, or None.

    Issues store the assignee as a name, not a user id -- that predates
    accounts, and changing it now would be a migration on every ticket. So a
    name is resolved here, and a ticket assigned to somebody who is not an
    account simply produces no mail rather than a failure.
    """
    text = str(name or "").strip()
    if not text:
        return None
    rows = conn.execute(
        "SELECT id, name, username, email FROM users "
        " WHERE is_active = 1 AND name = ? COLLATE NOCASE AND email <> '' "
        " ORDER BY id LIMIT 1",
        (text,),
    ).fetchone()
    return rows


def accounts_with_email(conn):
    return conn.execute(
        "SELECT id, name, username, email FROM users "
        " WHERE is_active = 1 AND email <> '' ORDER BY LENGTH(name) DESC, id"
    ).fetchall()


def mentioned_accounts(conn, text):
    """Accounts named with `@` in a comment.

    Names here have spaces in them ("Mei Tan"), so a plain \\w+ scan would
    match only the first word. The known names are therefore matched longest
    first, which also stops `@Ana` from winning inside `@Ana Ferreira`.
    Usernames are accepted too, because they are single tokens and easier to
    type.
    """
    body = " ".join(str(text or "").split())
    if "@" not in body:
        return []
    found = []
    seen = set()
    # Longest name first: the alternation is ordered, so the first match is
    # the most specific one.
    for account in accounts_with_email(conn):
        for handle in (account["name"], account["username"]):
            if not handle:
                continue
            pattern = r"@" + re.escape(str(handle)) + r"(?![\w.-])"
            if re.search(pattern, body, re.IGNORECASE) and account["id"] not in seen:
                seen.add(account["id"])
                found.append(account)
    return found


def _subject(reason, issue):
    key = issue["key"] if "key" in issue.keys() else "%s-%s" % (
        issue["project_key"], issue["number"])
    title = mailer.clean_header(issue["title"], 120)
    prefix = {
        ASSIGNED: "Assigned to you",
        MENTIONED: "You were mentioned",
        COMMENTED: "New comment",
        UPDATED: "Updated",
    }.get(reason, "Updated")
    return "[%s] %s: %s" % (key, prefix, title)


def _body(reason, issue, actor, detail):
    key = issue["key"] if "key" in issue.keys() else "%s-%s" % (
        issue["project_key"], issue["number"])
    lines = []
    if reason == ASSIGNED:
        lines.append("%s assigned this ticket to you." % actor)
    elif reason == MENTIONED:
        lines.append("%s mentioned you on this ticket." % actor)
    elif reason == COMMENTED:
        lines.append("%s commented on this ticket." % actor)
    else:
        lines.append("%s updated this ticket." % actor)
    lines.append("")
    lines.append("%s  %s" % (key, issue["title"]))
    if detail:
        lines.append("")
        lines.append(detail)
    lines.append("")
    lines.append("Status: %s   Priority: %s" % (
        str(issue["status"]).replace("_", " "), issue["priority"]))
    if issue["assignee"]:
        lines.append("Assignee: %s" % issue["assignee"])
    lines.append("")
    lines.append("Open it: %s" % mailer.app_url("/#/board?project=%s&issue=%s"
                                               % (issue["project_key"], issue["id"])))
    lines.append("")
    lines.append("You are getting this because you are on this ticket. "
                 "Replies are not read.")
    return "\n".join(lines)


def enqueue(conn, issue, account, reason, actor, detail="", app_url=""):
    """Queue one message. Returns False when there is nothing to queue."""
    if not account:
        return False
    recipient = str(account["email"] or "").strip()
    if not recipient:
        return False
    # Your own action is not news to you.
    if account["name"] and account["name"].lower() == str(actor or "").lower():
        return False
    conn.execute(
        "INSERT INTO notifications (issue_id, recipient, recipient_name, reason, "
        "subject, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            issue["id"],
            recipient,
            account["name"],
            reason,
            _subject(reason, issue),
            _body(reason, issue, actor, detail),
            db.utcnow(),
        ),
    )
    return True


def enqueue_name(conn, issue, name, reason, actor, detail=""):
    return enqueue(conn, issue, _account_by_name(conn, name), reason, actor, detail)


def notify_assignment(conn, issue, actor, new_assignee, previous=None):
    """Someone was put on a ticket."""
    if not new_assignee or (previous and previous == new_assignee):
        return False
    return enqueue_name(conn, issue, new_assignee, ASSIGNED, actor,
                        "It is now assigned to you.")


def notify_change(conn, issue, actor, changes):
    """Anything else that happened to a ticket, to whoever holds it.

    `changes` is the list of (field, before, after) the caller already built
    for the activity trail, so the mail says what actually moved rather than
    just that something did.
    """
    if not issue["assignee"]:
        return False
    lines = []
    for field, before, after in changes:
        if field in ("assignee", "rank", "index"):
            continue
        lines.append("  %s: %s -> %s" % (
            field.replace("_", " "),
            "none" if before in (None, "") else before,
            "none" if after in (None, "") else after,
        ))
    return enqueue_name(conn, issue, issue["assignee"], UPDATED, actor,
                        "\n".join(lines))


def notify_comment(conn, issue, actor, body):
    """A comment: the assignee hears about it, and so does anyone @mentioned.

    One message per person. Somebody who is both the assignee and mentioned
    gets the mention, which is the more specific reason.
    """
    mentioned = mentioned_accounts(conn, body)
    mentioned_names = {a["name"].lower() for a in mentioned}
    sent = 0
    excerpt = " ".join(str(body or "").split())[:500]
    for account in mentioned:
        if enqueue(conn, issue, account, MENTIONED, actor,
                   "Comment:\n%s" % excerpt if excerpt else "Comment with an attachment."):
            sent += 1
    if issue["assignee"] and issue["assignee"].lower() not in mentioned_names:
        if enqueue_name(conn, issue, issue["assignee"], COMMENTED, actor,
                        "Comment:\n%s" % excerpt if excerpt else "Comment with an attachment."):
            sent += 1
    return sent


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------

def pending(conn, limit=BATCH):
    return conn.execute(
        "SELECT * FROM notifications WHERE sent_at IS NULL AND attempts < ? "
        " ORDER BY id LIMIT ?",
        (MAX_ATTEMPTS, limit),
    ).fetchall()


def drain(conn, sender=None, now=None):
    """Send what is queued. Returns (sent, failed).

    Called by the background thread in `server.py` with its own connection, and
    directly by `--flush-notifications`. A failure is recorded on the row and
    retried on a later pass, up to MAX_ATTEMPTS -- a mail server being down for
    a minute must not lose the message, and a wrong password must not retry
    forever.
    """
    send = sender or mailer.send
    sent = failed = 0
    rows = pending(conn)
    if not rows:
        return 0, 0
    for row in rows:
        try:
            send(row["recipient"], row["subject"], row["body"])
        except Exception as exc:  # noqa: BLE001 -- one bad address must not stop the queue
            failed += 1
            conn.execute(
                "UPDATE notifications SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                ("%s: %s" % (type(exc).__name__, exc)[:400], row["id"]),
            )
            continue
        sent += 1
        conn.execute(
            "UPDATE notifications SET sent_at = ?, attempts = attempts + 1, last_error = NULL "
            " WHERE id = ?",
            (now or db.utcnow(), row["id"]),
        )
    conn.commit()
    return sent, failed


def pending_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE sent_at IS NULL"
    ).fetchone()[0]
