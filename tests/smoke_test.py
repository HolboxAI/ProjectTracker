#!/usr/bin/env python3
"""End-to-end smoke test for the tracker API.

Runs against a throwaway SQLite file, drives `api.handle` the same way the
server does (one connection per request), and asserts on the real responses.

    python3 tests/smoke_test.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import api, db  # noqa: E402

PASSED = []
FAILED = []


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
    else:
        FAILED.append("%s %s" % (label, detail and "-> %s" % detail))


def call(db_path, method, path, body=None, query=None):
    """One request, one connection -- exactly what the HTTP layer does."""
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
    tmpdir = tempfile.mkdtemp(prefix="tracker-test-")
    db_path = os.path.join(tmpdir, "test.sqlite3")
    db.init_db(db_path)

    # --- bootstrap ------------------------------------------------------
    status, boot = call(db_path, "GET", "/api/bootstrap")
    check("bootstrap returns 200", status == 200, status)
    check("bootstrap seeds two projects", len(boot["projects"]) == 2, boot["projects"])
    check("bootstrap exposes workflow meta",
          boot["meta"]["statuses"] == db.STATUSES and boot["meta"]["types"] == db.TYPES)
    check("project rail carries open-issue counts",
          all("open_issues" in p for p in boot["projects"]))

    # --- project payload ------------------------------------------------
    status, project = call(db_path, "GET", "/api/projects/PHX")
    check("project lookup by key", status == 200 and project["key"] == "PHX", status)
    check("project has a member roster", len(project["members"]) == 5, project.get("members"))
    check("project has two sprints", len(project["sprints"]) == 2)
    check("sprint ordering puts the active one first", project["sprints"][0]["status"] == "active")
    check("project reports issue totals", project["total_issues"] == 18, project["total_issues"])

    # --- listing and filtering -----------------------------------------
    status, listing = call(db_path, "GET", "/api/issues", query={"project": ["PHX"]})
    check("lists all PHX issues", status == 200 and listing["count"] == 18, listing.get("count"))
    check("issues come back with readable keys",
          listing["issues"][0]["key"].startswith("PHX-"), listing["issues"][0]["key"])
    check("labels deserialize to a list",
          isinstance(listing["issues"][0]["labels"], list))
    check("board order is by rank", [i["rank"] for i in listing["issues"]] ==
          sorted(i["rank"] for i in listing["issues"]))

    status, in_progress = call(db_path, "GET", "/api/issues",
                               query={"project": ["PHX"], "status": ["in_progress"]})
    check("filters by status", in_progress["count"] == 4, in_progress.get("count"))

    status, backlog = call(db_path, "GET", "/api/issues",
                           query={"project": ["PHX"], "sprint": ["backlog"]})
    check("filters to the backlog", backlog["count"] == 3, backlog.get("count"))

    status, active = call(db_path, "GET", "/api/issues",
                          query={"project": ["PHX"], "sprint": ["active"]})
    check("filters to the active sprint", active["count"] == 10, active.get("count"))

    status, bugs = call(db_path, "GET", "/api/issues",
                        query={"project": ["PHX"], "type": ["bug"], "priority": ["highest"]})
    check("combines type and priority filters", bugs["count"] == 3, bugs.get("count"))

    status, searched = call(db_path, "GET", "/api/issues",
                            query={"project": ["PHX"], "q": ["token"]})
    check("free-text search matches titles", searched["count"] >= 2, searched.get("count"))

    status, by_key = call(db_path, "GET", "/api/issues",
                          query={"project": ["PHX"], "q": ["PHX-16"]})
    check("free-text search matches an issue key", by_key["count"] == 1, by_key.get("count"))

    # --- create ---------------------------------------------------------
    status, created = call(db_path, "POST", "/api/issues", body={
        "project": "PHX",
        "title": "Regression: reconciliation export drops the final row",
        "type": "bug",
        "priority": "high",
        "assignee": "Priya Raman",
        "story_points": 3,
        "labels": "reporting, bug-bash",
        "sprint_id": project["sprints"][0]["id"],
        "description": "Off-by-one in the row slice.",
    })
    check("creates an issue", status == 201, (status, created))
    issue = created.get("issue", {})
    new_id = issue.get("id")
    check("assigns the next key in sequence", issue.get("key") == "PHX-19", issue.get("key"))
    check("keeps labels as a list", issue.get("labels") == ["reporting", "bug-bash"])
    check("records the sprint name", bool(issue.get("sprint_name")))
    check("new issues land at the end of their column", issue.get("rank", 0) > 0)

    # --- validation -----------------------------------------------------
    status, _ = call(db_path, "POST", "/api/issues", body={"project": "PHX"})
    check("rejects a missing title", status == 400, status)

    status, _ = call(db_path, "POST", "/api/issues",
                     body={"project": "PHX", "title": "x", "status": "shipped"})
    check("rejects an unknown status", status == 422, status)

    status, _ = call(db_path, "POST", "/api/issues", body={"project": "NOPE", "title": "x"})
    check("404s on an unknown project", status == 404, status)

    status, _ = call(db_path, "POST", "/api/issues",
                     body={"project": "PHX", "title": "x", "story_points": "many"})
    check("rejects non-numeric story points", status == 422, status)

    status, _ = call(db_path, "POST", "/api/projects", body={"key": "PHX", "name": "Dup"})
    check("rejects a duplicate project key", status == 409, status)

    status, _ = call(db_path, "POST", "/api/projects", body={"key": "bad key", "name": "Nope"})
    check("rejects an invalid project key", status == 422, status)

    # --- update + activity trail ---------------------------------------
    status, updated = call(db_path, "PATCH", "/api/issues/%d" % new_id,
                           body={"status": "done", "priority": "highest"})
    check("patches an issue", status == 200 and updated["issue"]["status"] == "done", status)
    check("sets resolved_at when done", bool(updated["issue"]["resolved_at"]))

    status, detail = call(db_path, "GET", "/api/issues/%d" % new_id)
    fields = [(a["field"], a["from"], a["to"]) for a in detail["activity"]]
    check("logs the status change", ("status", "todo", "done") in fields, fields)
    check("logs the priority change", ("priority", "high", "highest") in fields, fields)
    check("logs creation", any(f[0] == "created" for f in fields))
    check("activity resolves enum values for humans", "in progress" not in str(fields))

    status, reopened = call(db_path, "PATCH", "/api/issues/%d" % new_id, body={"status": "todo"})
    check("clears resolved_at when moved back out of done",
          reopened["issue"]["resolved_at"] is None)

    status, _ = call(db_path, "PATCH", "/api/issues/%d" % new_id, body={"status": None})
    check("rejects a null status on patch", status == 422, status)

    status, _ = call(db_path, "PATCH", "/api/issues/999999", body={"status": "todo"})
    check("404s patching a missing issue", status == 404, status)

    # --- board drag: status + explicit position ------------------------
    status, col = call(db_path, "GET", "/api/issues",
                       query={"project": ["PHX"], "status": ["todo"]})
    first_todo_id = col["issues"][0]["id"]
    status, moved = call(db_path, "PATCH", "/api/issues/%d" % new_id,
                         body={"status": "todo", "index": 0})
    check("drag to position 0 succeeds", status == 200, status)
    status, col_after = call(db_path, "GET", "/api/issues",
                             query={"project": ["PHX"], "status": ["todo"]})
    check("dragged card is first in the column",
          col_after["issues"][0]["id"] == new_id,
          [i["id"] for i in col_after["issues"]][:3])
    check("the displaced card is still present",
          first_todo_id in [i["id"] for i in col_after["issues"]])
    check("no duplicate ranks after a move",
          len({i["rank"] for i in col_after["issues"]}) == len(col_after["issues"]))

    # --- comments -------------------------------------------------------
    status, comment = call(db_path, "POST", "/api/issues/%d/comments" % new_id,
                           body={"body": "Confirmed on staging."})
    check("adds a comment", status == 201 and comment["comment"]["body"] == "Confirmed on staging.")
    status, _ = call(db_path, "POST", "/api/issues/%d/comments" % new_id, body={"body": "  "})
    check("rejects an empty comment", status == 400, status)
    status, detail = call(db_path, "GET", "/api/issues/%d" % new_id)
    check("comments show on the issue", len(detail["comments"]) == 1)

    # --- sprints --------------------------------------------------------
    sprints = project["sprints"]
    active_sprint = [s for s in sprints if s["status"] == "active"][0]
    planned_sprint = [s for s in sprints if s["status"] == "planned"][0]

    status, _ = call(db_path, "POST", "/api/sprints/%d/start" % planned_sprint["id"])
    check("refuses a second active sprint", status == 409, status)

    status, done = call(db_path, "POST", "/api/sprints/%d/complete" % active_sprint["id"],
                        body={"move_to": "backlog"})
    check("completes the active sprint", status == 200 and done["sprint"]["status"] == "completed")
    check("reports how much work moved", done["moved_to_backlog"] > 0, done)
    status, backlog_now = call(db_path, "GET", "/api/issues",
                               query={"project": ["PHX"], "sprint": ["backlog"]})
    check("unfinished work moved to the backlog", backlog_now["count"] >= done["moved_to_backlog"])
    status, still_open = call(db_path, "GET", "/api/issues",
                              query={"project": ["PHX"], "sprint": [str(active_sprint["id"])]})
    check("no unfinished issue is left behind in a completed sprint",
          still_open["count"] == 2, still_open.get("count"))
    check("only finished work stayed in the sprint",
          all(i["status"] == "done" for i in still_open["issues"]),
          [i["status"] for i in still_open["issues"]])

    status, started = call(db_path, "POST", "/api/sprints/%d/start" % planned_sprint["id"])
    check("a sprint can start once the previous one is done",
          status == 200 and started["sprint"]["status"] == "active", status)

    status, new_sprint = call(db_path, "POST", "/api/projects/PHX/sprints",
                              body={"name": "Sprint 17", "goal": "Cut the release."})
    check("creates a sprint", status == 201 and new_sprint["sprint"]["status"] == "planned")
    status, _ = call(db_path, "DELETE", "/api/sprints/%d" % started["sprint"]["id"])
    check("refuses to delete a running sprint", status == 409, status)
    status, _ = call(db_path, "DELETE", "/api/sprints/%d" % new_sprint["sprint"]["id"])
    check("deletes a planned sprint", status == 200, status)

    # --- project creation ----------------------------------------------
    status, fresh = call(db_path, "POST", "/api/projects", body={
        "key": "WEB", "name": "Marketing Site", "lead": "Nadia Haddad",
        "description": "Public site and docs.",
    })
    check("creates a project", status == 201 and fresh["key"] == "WEB", status)
    status, first_of_new = call(db_path, "POST", "/api/issues",
                                body={"project": "WEB", "title": "Rebuild the pricing page"})
    check("issue numbering starts at 1 for a new project",
          first_of_new["issue"]["key"] == "WEB-1", first_of_new.get("issue"))
    check("a new project seeds its lead as a member",
          any(m["name"] == "Nadia Haddad" for m in fresh["members"]))

    # --- stats ----------------------------------------------------------
    status, stats = call(db_path, "GET", "/api/projects/PHX/stats")
    check("stats return 200", status == 200, status)
    check("stats total matches the issue count", stats["totals"]["total"] == 19,
          stats["totals"])
    check("status breakdown covers every column",
          [b["value"] for b in stats["by_status"]] == db.STATUSES)
    check("status breakdown sums to the total",
          sum(b["count"] for b in stats["by_status"]) == stats["totals"]["total"])
    check("points split into done and open",
          stats["totals"]["done_points"] <= stats["totals"]["points"])
    check("stats carry a recently-updated list", len(stats["recent"]) > 0)
    check("stats carry an assignee breakdown", len(stats["by_assignee"]) > 0)

    # --- deletion is not offered ----------------------------------------
    # A ticket is the record of something reported; there is no route that
    # removes one, so the attempt has to 404 rather than quietly succeed.
    status, _ = call(db_path, "DELETE", "/api/issues/%d" % new_id)
    check("deleting an issue is not a route", status == 404, status)
    status, _ = call(db_path, "GET", "/api/issues/%d" % new_id)
    check("the issue is still there afterwards", status == 200, status)

    # --- attachments on comments ----------------------------------------
    # A real 1x1 PNG: the server sniffs these bytes and does not trust what
    # the client claims the type is.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    status, uploaded = call(
        db_path, "POST", "/api/issues/%d/attachments" % new_id,
        body={"filename": "shot.png", "content_type": "image/png",
              "data": base64.b64encode(png).decode()},
    )
    check("uploads an attachment", status == 201, (status, uploaded))
    attachment = uploaded.get("attachment", {})
    check("recognises it as an image from its bytes", attachment.get("is_image") is True, attachment)
    check("does not expose the on-disk name", "stored_name" not in attachment, attachment)

    status, posted = call(
        db_path, "POST", "/api/issues/%d/comments" % new_id,
        body={"body": "Screenshot attached.", "attachment_ids": [attachment.get("id")]},
    )
    check("a comment can carry an attachment", status == 201, (status, posted))
    check("the attachment comes back with the comment",
          [a["filename"] for a in posted.get("comment", {}).get("attachments", [])] == ["shot.png"],
          posted.get("comment"))

    status, detail = call(db_path, "GET", "/api/issues/%d" % new_id)
    carried = [a for c in detail["comments"] for a in c.get("attachments", [])]
    check("the attachment shows on the issue detail", len(carried) == 1, carried)

    # The bytes route, which is not reached through api.handle.
    ok, payload, headers, ctype = api.read_attachment(
        db.connect(db_path), attachment.get("id"))
    check("the file can be read back", ok == 200 and payload == png, len(payload))
    check("an image is served as an image", ctype == "image/png", ctype)
    check("and inline",
          any(h[0] == "Content-Disposition" and h[1].startswith("inline") for h in headers),
          headers)
    check("with nosniff",
          any(h[0] == "X-Content-Type-Options" and h[1] == "nosniff" for h in headers), headers)

    status, _ = call(
        db_path, "POST", "/api/issues/%d/comments" % new_id,
        body={"body": "reuse", "attachment_ids": [attachment.get("id")]},
    )
    check("an attachment cannot be claimed twice", status == 409, status)

    status, _ = call(db_path, "POST", "/api/issues/%d/attachments" % new_id,
                     body={"filename": "x.txt", "data": "not base64!!"})
    check("refuses data that is not base64", status == 422, status)

    status, _ = call(db_path, "POST", "/api/issues/%d/comments" % new_id,
                     body={"body": "", "attachment_ids": [999999]})
    check("refuses an attachment id that is not on the issue", status == 404, status)

    status, _ = call(db_path, "POST", "/api/issues/%d/comments" % new_id, body={"body": "  "})
    check("still rejects a comment with neither text nor file", status == 400, status)

    # --- routing --------------------------------------------------------
    status, _ = call(db_path, "GET", "/api/nonsense")
    check("unknown API path 404s", status == 404, status)

    # --- report ---------------------------------------------------------
    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\nFailures:")
        for failure in FAILED:
            print("  x %s" % failure)
        return 1
    print("All API checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
