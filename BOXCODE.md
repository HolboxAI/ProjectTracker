# Project notes

## Environment

- **No Node.js or npm on this machine.** `node --version` and `npm --version`
  both fail. Available: `python3` is **3.15.0**, SQLite 3.43.2. Do not propose
  a React/Vite/Express stack or any `npm install` step here — it cannot run.
- The code targets Python 3.9 as a *floor* (`from __future__ import annotations`
  is present where newer annotations are used), but the interpreter on this
  machine is 3.15 — so a 3.9-only feature is not a compatibility argument, it
  is about what the README promises.

## Conventions

- **Assignee pickers read `assignableNames(state, project)` from `ui.js`**, not
  `project.members`. Members are only the people who have actually worked on a
  project (the creator, then anyone assigned), so a brand-new project has one
  member and a member-only list looks empty. `assignableNames` unions the
  workspace's accounts (`state.people`, from `/api/bootstrap`) with the
  project's members, which keeps historical names selectable too. The same list
  drives the create form, the issue drawer and the toolbar filter — change it in
  `ui.js` and all three follow. Assigning somebody to an issue adds them to
  `members` server-side, so the two converge over time.

- `static/design-tokens.css` is the only file that defines colours, type and
  spacing. Nothing else hardcodes a hex. Changing an accent means re-checking
  it against `--surface` and `--surface-raised` in both light and dark.
- User-supplied text goes through `document.createTextNode` (via the `el()`
  helper in `static/js/ui.js`). `innerHTML` is used only for the literal icon
  set in that same file.
- The URL hash is the app's state: view, project, filters and open issue all
  live in `#/view?project=X&q=Y&issue=N`. Any new view or panel state should
  follow that pattern rather than getting its own ad-hoc store.
- `tracker/api.py` transaction handling lives in `handle()`: handlers never
  call `commit()` themselves.
- Status colours are applied as **inline styles** in JS, not as classes
  (`dot.style.background = meta.color` in `ui.js`), so there is no selector
  for, say, "the in-progress column". Board columns carry a `data-status`
  attribute for exactly this — extend that rather than styling a class the
  renderer never emits.
- `app.js` hides the issue drawer 180ms after `.open` is removed, so the
  `.drawer` transition in `app.css` has to stay at or under 180ms (`--dur`).
  A nicer 300ms slide gets cut off mid-flight on close.

- **There is no route that deletes an issue, on purpose.** `DELETE
  /api/issues/{id}` was removed; sprints and accounts still delete. A ticket
  is the record of something reported, so it is retired by moving it to `done`.
  Do not re-add the route or a "delete" button in the drawer.

- Attachments belong to a **comment**, not to the issue (`attachments.comment_id`),
  and are null between upload and the comment that claims them: the client
  uploads, then posts the comment with `attachment_ids`. A comment made only of
  a screenshot has an empty `body`, which `add_comment` allows.

- **Attachment bytes are files on disk, not rows.** `tracker/storage.py` writes
  them into an `attachments/` directory beside the database file (path read from
  `PRAGMA database_list`, so a backup is a copy of two paths). The DB keeps only
  metadata. `stored_name` is a random id + an extension earned by sniffing magic
  numbers — never the client's filename, which is only a display label.

- **`GET /api/attachments/{id}/content` is handled in `server.py`, not through
  `api.handle`**, because it returns bytes with their own `Content-Type`. It is
  `api.read_attachment`, and `_finish` therefore accepts a 4-tuple
  `(status, payload, headers, content_type)`. Only magic-checked images are
  served `inline`; everything else is `application/octet-stream` +
  `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`, so an
  uploaded `.html`/`.svg` can never render on this origin. `file.type` from the
  browser is never trusted.

- Uploads are base64 inside JSON, not multipart — this server is standard
  library only. `MAX_REQUEST_BYTES` in `server.py` caps the request before it is
  read; keep it clear of `storage.MAX_BYTES * 4/3` or a legal file will 413.

- **Email is queued, never sent from a request handler.** `tracker/notify.py`
  writes a `notifications` row inside the same transaction as the change; the
  daemon thread `notification_worker` in `server.py` drains it every
  `COVEX_MAIL_INTERVAL` seconds (default 15). Do not call `mailer.send` from
  `api.py` — an SMTP round trip would hold the write lock and make a board drag
  feel broken whenever the mail server is slow.

- Mail is **opt-in by configuration**: `mailer.configured()` is false unless
  `SMTP_HOST` and `SMTP_FROM` are both set, and then everything runs as before
  with the queue waiting. `smtplib` is stdlib, so this needs no dependency. On
  the instance the settings come from `/etc/covex-tracker.env` via
  `EnvironmentFile=-` in the unit (the `-` keeps it optional); locally from
  `.env`.

- The tests **never send mail**. They assert on `notifications` rows and pass a
  fake `sender=` to `notify.drain`. Keep it that way — a suite that needs a
  mail server cannot run on a laptop or in CI.

- Assignees are stored on a ticket as a **display name**, not a user id, so
  `notify._account_by_name` resolves name → account and a ticket assigned to
  somebody who is not an account simply produces no mail. Mentions match
  `@Full Name` or `@username`, longest name first — a `\w+` scan would stop at
  the space in "Mei Tan".

## Authentication

- Accounts, hashing and sessions live in `tracker/auth.py`; **all cookie
  handling lives in `server.py`**, because a cookie is an HTTP concern. Nothing
  below `server.py` knows what a cookie is — keep it that way.
- `PUBLIC_STATIC` in `server.py` is the complete set of paths a stranger can
  fetch. The sign-in page must work with nothing that is not on that list:
  `js/login.js` deliberately imports nothing. If you add a stylesheet or module
  the login page needs, add it there too.
- Values in that set have a **leading slash** (`STATIC_ALIASES = {"/login":
  "/login.html"}`). The unslashed form does not match `PUBLIC_STATIC` and the
  request redirects to `/login` forever, which looks like a broken page rather
  than a wrong string.
- `api.handle(...)` receives the signed-in account as a **`users` row** from
  `server.py` and as a **serialized dict** from the tests. `Ctx` normalizes with
  `auth.user_fields()`, so read identity through `ctx.actor` / `ctx.user` /
  `ctx.is_admin` and never index the raw argument.
- A write's actor, reporter and comment author all come from `ctx.actor`. Do not
  hardcode a name in a handler — the activity trail is the point of it.
- The tests and the CLI call `api.handle` with no user, which is why `ACTOR =
  "You"` still exists as a fallback. Anything reachable without a session is
  still guarded by `server.py`, so an absent user is *not* a 401 down here —
  `require_admin()` raises 403, and the 401 is asserted over a real socket.
- Sign-in is local-only. `provider` and `google_sub` are already columns for the
  Workspace SSO step, so it can be added without a migration.

## Testing

- `python3 tests/smoke_test.py` — API-level, runs against a temp database, no
  server needed. This is the fast one.
- `python3 tests/auth_test.py` — hashing, password policy, lockout, sessions,
  the account API and write attribution. Also no server needed. It passes a low
  PBKDF2 round count for speed; that is the only difference from production.
- `sh tests/run_http_check.sh` — the whole HTTP suite: starts a server on a
  spare port against a throwaway database, creates the account it signs in as,
  runs `tests/http_check.py`, stops the server. **The app is behind a login, so
  `http_check.py` now needs `--user`/`--password`** (or `COVEX_TEST_USERNAME` /
  `COVEX_TEST_PASSWORD`) and exits with 2 without them rather than skipping its
  authenticated half. When you add a JS module, add it to the served-module list
  in `http_check.py`.
- `sh -c 'curl ... | python3'` is rejected by the tooling's safety guard. To
  inspect an API response, use `tests/http_check.py` or `python3 -c` with
  `urllib` instead of piping curl into an interpreter.
- Neither test file touches `tracker.sqlite3`; they use temp directories. Do not
  "tidy" them onto the real database.
- `tests/live_check.py` is different again: it points at a **deployed** instance
  over HTTPS and checks the certificate, the gate, a real sign-in, and account
  management. It creates one temporary account and deletes it, and deliberately
  creates no projects or issues — production has no seeded workspace and no
  route deletes a project, so anything it left there would be permanent. It
  takes the password from `COVEX_ADMIN_PASSWORD` (not argv, where `ps` shows it).

## Deployment

- **Hosting is one EC2 instance**, `tracker.holbox.ai`, account `992382417943`,
  `us-west-2`, behind an Elastic IP and nginx with a Let's Encrypt certificate.
  The app is one process over one SQLite file in WAL mode, so never a second
  worker, an autoscaling group, or an ephemeral filesystem. `deploy/DEPLOY.md`
  has the shape and the reasoning.
- **The app runs with `--no-seed` in production.** There is no route that
  deletes a project, so seeded demo projects would be permanent.
- **IAM is denied** under the `holbox-tracker` profile (`PowerUserAccess`
  excludes it), so no instance profile can be created and **SSM Session Manager
  is not available** — SSH with the key pair is the only way onto the box.
- **The operator network has multi-homed, rotating egress.** One `curl` can
  leave from `103.141.126.33`, the next from `103.141.127.53`, the next from
  `14.139.123.54`. A `/32` SSH rule therefore works intermittently and looks
  like a broken instance: the symptom is that port 22 times out from `ssh`
  while a `python3` socket probe to the same port succeeds, because the two
  connections were seen from different addresses. The rule covers
  `103.141.126.0/23` plus `14.139.123.54/32` for this reason. Expect to widen
  it again if the ISP adds a range.
- The instance key is `~/.ssh/covex-tracker.pem` (mode 600, never committed —
  `*.pem` is in `.gitignore`). `sudo cat /root/covex-admin-password` on the
  instance reads the first administrator's generated password.
