# Tracker

A small, self-contained issue tracker in the shape of Jira: projects, a
workflow board, sprints and a backlog, estimates, comments, and an activity
trail on every issue.

No dependencies, no build step, no `pip install`. Python 3.9+ and the
standard library.

```sh
python3 server.py                 # http://127.0.0.1:8077
```

The first run creates `tracker.sqlite3` and seeds a demo workspace — two
projects (`PHX`, `MOB`), two sprints, twenty-three issues with comments — so
the app is not an empty room. Your data lives in that one file; delete it to
start over, or pass `--db` to point somewhere else.

```sh
python3 server.py --port 9000
python3 server.py --db ~/work/tracker.sqlite3
python3 server.py --fresh         # wipe and reseed the demo data (accounts too)
```

Sign-in is required, so the first thing to do on a fresh database is create an
account — see **Accounts** below. Without one, nobody can get in.

## What it does

| View | What is there |
| --- | --- |
| Board | One column per workflow status; drag a card to move it, drop it where you want it in the column |
| Backlog | The active sprint, planned sprints, completed sprints and unscheduled work; drag rows between them, start/complete sprints |
| All issues | Flat table with a free-text search across titles, descriptions, labels and issue keys |
| Reports | Counts and points by status, priority, type and assignee, plus active-sprint progress |

The issue drawer (click any card or row) is where the full record lives:
editable title, description, status, assignee, priority, type, sprint,
estimate, due date and labels, plus comments and a history of every field
change.

Keyboard: `c` new issue, `/` search, `b` board, `k` backlog, `Esc` to close
whatever is open. `Cmd/Ctrl+Enter` posts a comment.

The URL is the state — `#/board?project=PHX&q=token&issue=7` — so any view,
filter or open issue is a link you can share, and the back button behaves.

## Layout

```
server.py            HTTP server: static files + JSON routing
tracker/db.py        SQLite schema, queries, demo seed
tracker/auth.py      Accounts, password hashing, sessions
tracker/api.py       The API: handlers, validation, activity logging
static/index.html    The shell
static/login.html    The sign-in page
static/design-tokens.css   Color, type and spacing tokens (see below)
static/app.css       Layout and components
static/js/api.js     Fetch client
static/js/app.js     State, hash routing, rail and toolbar
static/js/board.js   Board view
static/js/backlog.js Backlog view
static/js/issues.js  Table view
static/js/reports.js Reports view
static/js/drawer.js  Issue detail drawer
static/js/modal.js   New issue / new project forms
static/js/ui.js      DOM helpers, icon set, drag-and-drop, formatting
static/js/login.js   Sign-in form (imports nothing -- it is served publicly)
tests/smoke_test.py  API tests, run against a throwaway database
tests/auth_test.py   Accounts, sessions and attribution, no server needed
tests/http_check.py  HTTP tests, need a live server and an account
tests/run_http_check.sh  Starts a server, makes an account, runs the HTTP suite
deploy/              EC2 bootstrap, systemd unit, nginx site, DEPLOY.md
```

## The API

Everything the UI does goes through these. All responses are JSON; errors are
`{"error": "..."}` with a real status code (400 missing field, 404 no such
thing, 409 conflict, 422 bad value).

```
GET    /api/bootstrap
GET    /api/projects/{key|id}
POST   /api/projects
GET    /api/projects/{key}/sprints
POST   /api/projects/{key}/sprints
GET    /api/projects/{key}/stats

GET    /api/issues?project=&status=&sprint=&assignee=&priority=&type=&q=
POST   /api/issues
GET    /api/issues/{id}
PATCH  /api/issues/{id}          # also takes `index` to place a dragged card
POST   /api/issues/{id}/comments   # body + `attachment_ids`, either may be alone
POST   /api/issues/{id}/attachments  # one file, base64 in JSON
GET    /api/attachments/{id}         # metadata
GET    /api/attachments/{id}/content # the bytes, with their own Content-Type

# There is deliberately no DELETE for an issue: a ticket is the record of
# something reported, and its comments, attachments and activity trail are the
# value. Status is how work is retired.

PATCH  /api/sprints/{id}
POST   /api/sprints/{id}/start
POST   /api/sprints/{id}/complete   # body: {"move_to": <sprint id>|"backlog"}
POST   /api/sprints/{id}/reopen
DELETE /api/sprints/{id}
```

## Accounts

The app is behind a sign-in screen: nothing is served to a visitor without a
session, and writes are attributed to the person who made them. Accounts are
local for now, and an administrator creates them:

```sh
python3 server.py --add-user --name "Jane Doe" --username jane --admin
python3 server.py --list-users
python3 server.py --reset-password jane
```

Omit `--password` to be prompted for one, or to have a strong one generated and
printed once. In the running app, **Manage accounts** at the foot of the rail
(administrators only) does the same and shows a generated first password
exactly once — it is not readable again from the database.

There is **no self-service password change** in the UI yet: a person who wants a
different password asks an administrator, who either resets it with
`--reset-password <username>` on the server or clicks **Reset password** in the
app. That reset ends the account's other sessions. Changing your own password
over the API works (`POST /api/auth/password`) and keeps your current session;
it just has no screen yet.

What that buys you: a password is stored as a PBKDF2-SHA256 digest with a
per-user salt and compared in constant time, never as text. A session is a
random token in an `HttpOnly; SameSite=Lax` cookie — `Secure` as well behind
TLS — of which only the SHA-256 is stored, so a leaked database backup does not
hand over live sessions. Eight failed attempts lock an account for fifteen
minutes, a password change or reset ends every other session, and a missing
account costs the same time as a wrong password so the form cannot be used to
find out who works here.

Google Workspace sign-in for `@holbox.com` is the next step, not a built one.
The users table already carries `provider` and `google_sub` for it, so it needs
no migration and nobody will need a second password. `deploy/DEPLOY.md` has the
outline.

## Hosting

This runs on EC2 at **https://tracker.holbox.ai** — one instance behind an
Elastic IP and nginx, with a Let's Encrypt certificate. `deploy/` holds what it
takes: a bootstrap script, a systemd unit, an nginx site, and `DEPLOY.md` with
the reasoning. The app is one process over one SQLite file in WAL mode, so it is
one instance on a real disk — never an autoscaling group, a second worker, or an
ephemeral filesystem.

```sh
sh deploy/redeploy.sh ubuntu@<elastic-ip>          # push code and restart
COVEX_ADMIN_PASSWORD=... python3 tests/live_check.py https://tracker.holbox.ai
```

`tests/live_check.py` verifies a deployment over its real URL — certificate,
session gate, a real sign-in and account management — and cleans up after
itself.

## Tests

```sh
python3 tests/smoke_test.py       # 69 checks, no server needed
python3 tests/auth_test.py        # 77 checks, no server needed
sh tests/run_http_check.sh        # 92 checks, starts its own server
```

`smoke_test.py` runs against a temporary database and covers filtering,
validation, sprint transitions, board reordering and the activity trail.
`auth_test.py` covers hashing, the password policy, lockout, sessions and who
writes are attributed to. `run_http_check.sh` starts a server on a spare port
against a throwaway database, creates the account the suite signs in as, runs
`http_check.py`, and stops the server.

`http_check.py` asserts the real HTTP layer: status codes, content types, that
every module the page imports is actually served, and the session gate —
signed out, signed in, a forged cookie, and a cross-site write. It needs
credentials and has no default, because a suite that quietly skipped its
authenticated half would report success for a server nobody can use:

```sh
python3 tests/http_check.py http://127.0.0.1:8077 --user jane --password ...
```

## Notes on the design

`static/design-tokens.css` is the only place colour and type are decided;
everything else reads from those variables. The accent is a deep stamp-teal,
and the monospace face carries issue keys, counts, points and column headers —
the point is that a board overrun with small identifiers reads as a ledger,
not as prose.

Every foreground/background pair in the palette was measured for WCAG contrast
and clears AA for normal text (4.5:1) in both light and dark. If you change an
accent, re-check it against `--surface` and `--surface-raised` in both schemes
rather than eyeballing it.

## Deliberately not here

Single-team and simple: anyone with an account can read and change every
project — there are no per-project permissions. There is no email, no
attachments, no cross-project search, and no undo. It is meant to be read, run
and changed in one sitting.
