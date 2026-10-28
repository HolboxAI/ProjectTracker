#!/usr/bin/env python3
"""Email: settings, the message that gets built, and the queue draining.

Starts a throwaway SMTP server on a loopback port, points the mailer at it and
sends for real -- so `smtplib`, the TLS/no-TLS branch and the SMTP conversation
are all exercised without needing a mail account or a network.

    python3 tests/mail_test.py

Nothing here touches tracker.sqlite3 or the real SMTP settings: the environment
is set inside this process and restored by exiting.
"""

from __future__ import annotations

import json
import os
import socketserver
import sys
import tempfile
import threading
import traceback
from email import message_from_bytes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import api, auth, db, mailer, notify  # noqa: E402

PASSED = []
FAILED = []
RECEIVED = []


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
    else:
        FAILED.append("%s %s" % (label, detail and "-> %s" % detail))


class FakeSMTP(socketserver.StreamRequestHandler):
    """The smallest SMTP conversation smtplib will accept, and no more."""

    def handle(self):
        self.wfile.write(b"220 fake.local ESMTP\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.strip().upper()
            if command.startswith(b"EHLO") or command.startswith(b"HELO"):
                self.wfile.write(b"250-fake.local\r\n250 8BITMIME\r\n")
            elif command.startswith(b"MAIL FROM") or command.startswith(b"RCPT TO"):
                self.wfile.write(b"250 OK\r\n")
            elif command.startswith(b"DATA"):
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                chunks = []
                while True:
                    raw = self.rfile.readline()
                    if not raw or raw == b".\r\n":
                        break
                    chunks.append(raw)
                RECEIVED.append(b"".join(chunks))
                self.wfile.write(b"250 OK\r\n")
            elif command.startswith(b"QUIT"):
                self.wfile.write(b"221 Bye\r\n")
                return
            elif command.startswith(b"RSET") or command.startswith(b"NOOP"):
                self.wfile.write(b"250 OK\r\n")
            else:
                self.wfile.write(b"250 OK\r\n")


def call(db_path, method, path, body=None, query=None):
    conn = db.connect(db_path)
    try:
        result = api.handle(method, path, query or {}, body, conn)
        if result is None:
            return 404, {"error": "no route"}
        return result
    except api.ApiError as exc:
        return exc.status, {"error": exc.message}
    finally:
        conn.close()


def main():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSMTP)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # --- not configured -------------------------------------------------
    for key in ("SMTP_HOST", "SMTP_FROM", "SMTP_USER", "SMTP_PASSWORD"):
        os.environ.pop(key, None)
    check("mail is off with no settings", mailer.configured() is False)
    check("and only a host is not enough",
          (os.environ.update(SMTP_HOST="127.0.0.1"), mailer.configured() is False)[1])
    os.environ.pop("SMTP_HOST")

    # --- configured -----------------------------------------------------
    os.environ.update(
        SMTP_HOST="127.0.0.1",
        SMTP_PORT=str(port),
        SMTP_FROM="Covex-Board <tracker@holbox.ai>",
        SMTP_SECURITY="none",
    )
    os.environ.pop("SMTP_USER", None)
    config = mailer.settings()
    check("mail turns on when host and from are set", mailer.configured() is True)
    check("the port is read as a number", config["port"] == port, config)

    # --- a header cannot be injected ------------------------------------
    check("a newline in a subject is flattened",
          "\n" not in mailer.clean_header("hello\r\nBcc: evil@example.com"),
          mailer.clean_header("hello\r\nBcc: evil@example.com"))

    # --- a real send over a real socket ---------------------------------
    mailer.send("someone@holbox.ai", "[PHX-1] Test: subject here",
                "Body line one.\nBody line two.")
    check("the message arrived", len(RECEIVED) == 1, len(RECEIVED))
    if RECEIVED:
        sent = message_from_bytes(RECEIVED[0])
        check("it has the From we configured",
              "tracker@holbox.ai" in (sent["From"] or ""), sent["From"])
        check("it is addressed to the person", sent["To"] == "someone@holbox.ai", sent["To"])
        check("the subject survives", sent["Subject"] == "[PHX-1] Test: subject here",
              sent["Subject"])
        check("the body survives", "Body line two." in sent.get_payload())
        check("it is marked auto-generated",
              (sent["Auto-Submitted"] or "") == "auto-generated", sent["Auto-Submitted"])

    # --- the whole path: change a ticket, drain the queue ----------------
    tmpdir = tempfile.mkdtemp(prefix="tracker-mail-")
    db_path = os.path.join(tmpdir, "mail.sqlite3")
    db.init_db(db_path)

    conn = db.connect(db_path)
    try:
        auth.create_user(conn, name="Priya Raman", username="priya",
                         password="test-pass-123", email="priya@holbox.ai")
        auth.create_user(conn, name="Mei Tan", username="mei",
                         password="test-pass-123", email="mei@holbox.ai")
        conn.commit()
    finally:
        conn.close()

    RECEIVED.clear()
    status, created = call(db_path, "POST", "/api/issues",
                           body={"project": "PHX", "title": "Mail me", "assignee": "Priya Raman"})
    check("creating an assigned ticket succeeds", status == 201, status)

    conn = db.connect(db_path)
    try:
        queued = conn.execute("SELECT recipient, reason FROM notifications").fetchall()
        check("it queued one mail", len(queued) == 1, [dict(r) for r in queued])
        check("to the assignee's address",
              queued and queued[0]["recipient"] == "priya@holbox.ai",
              [dict(r) for r in queued])
        sent, failed = notify.drain(conn)
    finally:
        conn.close()

    check("the queue drained over SMTP", (sent, failed) == (1, 0), (sent, failed))
    check("one message actually arrived", len(RECEIVED) == 1, len(RECEIVED))
    if RECEIVED:
        sent_message = message_from_bytes(RECEIVED[0])
        check("the subject names the ticket",
              "PHX-" in (sent_message["Subject"] or ""), sent_message["Subject"])
        check("the body links back to the ticket",
              "/#/board?project=PHX&issue=" in sent_message.get_payload(),
              sent_message.get_payload()[:200])

    conn = db.connect(db_path)
    try:
        check("nothing is left pending", notify.pending_count(conn) == 0,
              notify.pending_count(conn))
        row = conn.execute("SELECT sent_at, attempts FROM notifications").fetchone()
        check("the row records when it went", bool(row["sent_at"]) and row["attempts"] == 1,
              dict(row))
    finally:
        conn.close()

    # --- a mention, end to end ------------------------------------------
    RECEIVED.clear()
    issue_id = created["issue"]["id"]
    call(db_path, "PATCH", "/api/issues/%d" % issue_id, body={"assignee": "Priya Raman"})
    status, _ = call(db_path, "POST", "/api/issues/%d/comments" % issue_id,
                     body={"body": "@Mei Tan please confirm"})
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT recipient, reason FROM notifications ORDER BY id").fetchall()
        check("the mention queued mail for the mentioned person",
              any(r["recipient"] == "mei@holbox.ai" and r["reason"] == notify.MENTIONED
                  for r in rows), [dict(r) for r in rows])
        notify.drain(conn)
    finally:
        conn.close()
    check("the mention was delivered", len(RECEIVED) >= 1, len(RECEIVED))

    # --- self-notification is skipped -----------------------------------
    status, own = call(db_path, "POST", "/api/issues",
                       body={"project": "PHX", "title": "Mine", "assignee": "You"})
    conn = db.connect(db_path)
    try:
        mine = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE issue_id = ?",
            (own["issue"]["id"],)).fetchone()[0]
    finally:
        conn.close()
    check("assigning a ticket to yourself sends nothing", mine == 0, mine)

    server.shutdown()
    server.server_close()

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\nFailures:")
        for failure in FAILED:
            print("  x %s" % failure)
        return 1
    print("Email verified end to end, against a local SMTP server.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
