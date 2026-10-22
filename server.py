#!/usr/bin/env python3
"""Tracker -- a small, dependency-free Jira-style project tracker.

    python3 server.py                          # http://127.0.0.1:8077
    python3 server.py --port 9000
    python3 server.py --db /tmp/demo.sqlite3 --fresh

Accounts:

    python3 server.py --add-user --name "Jane Doe" --username jane --admin
    python3 server.py --list-users
    python3 server.py --reset-password jane

Serves the JSON API under /api and the single-page client from static/.
Standard library only: no pip install, no build step.

The split between this file and `tracker/api.py` is deliberate: everything to
do with cookies -- setting one on sign-in, reading one on every request,
clearing one on sign-out -- is here, because that is an HTTP concern. The
rules for accounts and sessions are in `tracker/auth.py`, and the JSON routes
are in `tracker/api.py`. Nothing downstream of this file has to know what a
cookie is.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tracker import api, auth, db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_DB = os.path.join(BASE_DIR, "tracker.sqlite3")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}

# Served to a visitor with no session. This is exactly the sign-in page plus
# the files it needs to render, so anything listed here is something a
# stranger could already read by opening the login screen. js/login.js is on
# the list and imports nothing, on purpose: a dependency there would have to
# be public too.
PUBLIC_STATIC = {
    "/login",
    "/login.html",
    "/design-tokens.css",
    "/app.css",
    "/js/login.js",
}

# The sign-in page is a real file with two names. The alias value keeps its
# leading slash so it matches PUBLIC_STATIC -- without it, /login looked like a
# path nobody is allowed to see and redirected to itself, forever.
STATIC_ALIASES = {"/login": "/login.html"}

LOCAL_PEERS = ("127.0.0.1", "::1")
COOKIE_MAX_AGE = auth.SESSION_DAYS * 24 * 60 * 60


class Handler(BaseHTTPRequestHandler):
    server_version = "Tracker/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _send(self, status, payload, content_type="application/json; charset=utf-8",
              extra_headers=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode("utf-8")
        elif payload is None:
            body = b""
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status, payload, extra_headers=None):
        self._send(status, payload, extra_headers=extra_headers)

    def _redirect(self, location):
        # A redirect with no body still has to carry Content-Length for
        # keep-alive to stay in step, which _send does.
        self._send(302, None, "text/plain; charset=utf-8", [("Location", location)])

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    def _client_ip(self):
        peer = self.client_address[0]
        if peer in LOCAL_PEERS:
            forwarded = self.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return peer

    def _is_https(self):
        """True when a proxy we put there ourselves terminated TLS.

        Only believed for a loopback peer: a stranger connecting directly
        must not be able to talk us into setting Secure-less cookies, or a
        local peer into thinking plain HTTP was encrypted.
        """
        if self.client_address[0] not in LOCAL_PEERS:
            return False
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth.COOKIE_NAME:
                return urllib.parse.unquote(value)
        return ""

    def _session_cookie(self, token, max_age):
        flags = "Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % max_age
        if self._is_https():
            flags += "; Secure"
        return "%s=%s; %s" % (auth.COOKIE_NAME, token, flags)

    def _clear_cookie(self):
        return "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % auth.COOKIE_NAME

    def _same_origin(self):
        """Reject a state-changing request that a different site started.

        The session cookie is SameSite=Lax, so a cross-site POST should not
        arrive carrying it in the first place. This is the second lock on the
        same door: if an Origin header is present it has to be ours.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        parts = urllib.parse.urlsplit(origin)
        return bool(parts.netloc) and parts.netloc.lower() == host.lower()

    # -- verbs ------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("GET")

    # -- routing ----------------------------------------------------------

    def _dispatch(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api"):
            self._handle_api(method, path, query)
            return

        if method != "GET":
            self._read_body()  # drain, so keep-alive does not desync
            self._send_json(405, {"error": "Method not allowed"})
            return
        self._serve_static(path)

    def _handle_api(self, method, path, query):
        body = self._read_body()

        if method in ("POST", "PATCH", "DELETE") and not self._same_origin():
            self._send_json(403, {"error": "Cross-site request refused"})
            return

        conn = db.connect(self.server.db_path)
        try:
            token = self._cookie_token()
            user = auth.resolve_session(conn, token)

            if path.startswith("/api/auth/"):
                result = self._handle_auth(method, path, body, conn, user, token)
            else:
                if user is None:
                    self._send_json(401, {"error": "Sign in to continue"})
                    return
                result = api.handle(method, path, query, body, conn, user, token)
        except (api.ApiError, auth.AuthError) as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        except Exception as exc:  # noqa: BLE001 -- surface the fault instead of hiding it
            self._send_json(500, {"error": "Internal error: %s" % exc})
            return
        finally:
            conn.close()

        self._finish(result, method, path)

    def _finish(self, result, method, path):
        if result is None:
            self._send_json(404, {"error": "No route for %s %s" % (method, path)})
            return
        if len(result) == 3:
            status, payload, headers = result
            self._send_json(status, payload, headers)
            return
        status, payload = result
        self._send_json(status, payload)

    def _handle_auth(self, method, path, body, conn, user, token):
        """The four session endpoints. They live here because they are the
        only routes that touch a cookie."""
        body = body or {}

        if method == "POST" and path == "/api/auth/login":
            username = str(body.get("username") or "").strip()
            password = body.get("password")
            if not username or not isinstance(password, str) or not password:
                raise api.ApiError(400, "Username and password are both required")
            account = auth.authenticate(conn, username, password, ip=self._client_ip())
            if account is None:
                # One message for a bad password and for no such account, so
                # the form cannot be used to enumerate who works here.
                raise api.ApiError(401, "Wrong username or password")
            new_token = auth.start_session(
                conn, account["id"], user_agent=self.headers.get("User-Agent", ""),
                ip=self._client_ip(),
            )
            payload = auth.serialize_user(account)
            return 200, {"user": payload, "me": payload["name"]}, [
                ("Set-Cookie", self._session_cookie(new_token, COOKIE_MAX_AGE)),
            ]

        if method == "POST" and path == "/api/auth/logout":
            auth.end_session(conn, token)
            return 200, {"ok": True}, [("Set-Cookie", self._clear_cookie())]

        if method == "GET" and path == "/api/auth/me":
            if user is None:
                return 200, {"user": None}
            payload = auth.serialize_user(user)
            return 200, {"user": payload, "me": payload["name"]}

        if method == "POST" and path == "/api/auth/password":
            if user is None:
                raise api.ApiError(401, "Sign in to continue")
            current = body.get("current_password")
            new_password = body.get("new_password")
            if user["password_hash"] and not auth.verify_password(
                    user["password_hash"], current or ""):
                raise api.ApiError(403, "That is not your current password")
            auth.set_password(conn, user["id"], new_password)
            # Changing a password ends sessions everywhere else. The tab doing
            # the changing keeps working, because being thrown out of the page
            # you just used would be a puzzle, not a security win.
            auth.end_all_sessions(conn, user["id"], keep_token=token)
            return 200, {"ok": True}

        raise api.ApiError(405, "No auth route for %s %s" % (method, path))

    # -- static -----------------------------------------------------------

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        else:
            path = STATIC_ALIASES.get(path, path)

        public = path in PUBLIC_STATIC
        if not public:
            token = self._cookie_token()
            if token:
                conn = db.connect(self.server.db_path)
                try:
                    if auth.resolve_session(conn, token) is not None:
                        public = True
                finally:
                    conn.close()
            else:
                # No cookie at all: nothing to check against the database.
                public = False

        if not public:
            # A page someone asked for by name gets the sign-in screen; an
            # asset belongs to a page they have not proved they may see.
            if path == "/index.html" or self._looks_like_page(path):
                self._redirect("/login?next=%s" % urllib.parse.quote(path))
            else:
                self._send_json(401, {"error": "Sign in to continue"})
            return

        candidate = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not candidate.startswith(STATIC_DIR + os.sep):
            self._send_json(403, {"error": "Forbidden"})
            return
        if not os.path.isfile(candidate):
            # Hash routing means every real page is index.html; still, a deep
            # link with no extension should land on the app rather than a 404.
            if "." not in os.path.basename(path):
                candidate = os.path.join(STATIC_DIR, "index.html")
            else:
                self._send_json(404, {"error": "Not found: %s" % path})
                return
        ext = os.path.splitext(candidate)[1].lower()
        with open(candidate, "rb") as handle:
            payload = handle.read()
        self._send(200, payload, CONTENT_TYPES.get(ext, "application/octet-stream"))

    @staticmethod
    def _looks_like_page(path):
        basename = os.path.basename(path)
        return "." not in basename or basename.endswith(".html")


# --------------------------------------------------------------------------
# accounts from the command line
# --------------------------------------------------------------------------

def _resolve_password(explicit, label):
    """Use the given password, take one from the environment, ask, or mint one."""
    if explicit:
        return explicit
    # Provisioning passes the first password through the environment so it
    # never lands in the process list, where `ps` would show the arguments to
    # anyone who can run it on the box.
    from_env = os.environ.get("COVEX_NEW_PASSWORD")
    if from_env:
        return from_env
    if sys.stdin.isatty():
        first = getpass.getpass("%s password: " % label)
        second = getpass.getpass("Repeat it: ")
        if first != second:
            sys.exit("Those passwords did not match. Nothing was changed.")
        return first
    return auth.generate_password()


def _add_user(args):
    db.init_db(args.db, with_demo=not args.no_seed)
    conn = db.connect(args.db)
    try:
        if not args.name or not args.username:
            sys.exit("--add-user needs --name and --username.")
        password = _resolve_password(args.password, args.username)
        row = auth.create_user(
            conn,
            name=args.name,
            username=args.username,
            password=password,
            email=args.email,
            is_admin=args.admin,
        )
        conn.commit()
    except auth.AuthError as exc:
        sys.exit("%s" % exc.message)
    finally:
        conn.close()
    print("Created %s (%s)%s" % (row["username"], row["name"],
                                 " as an administrator" if row["is_admin"] else ""))
    if not args.password:
        print("Password: %s" % password)
        print("Hand that over now -- it cannot be read back out of the database.")


def _list_users(args):
    db.init_db(args.db, with_demo=not args.no_seed)
    conn = db.connect(args.db)
    try:
        rows = auth.list_users(conn)
        if not rows:
            print("No accounts yet. Create the first one with:")
            print("  python3 server.py --add-user --name \"Your Name\" "
                  "--username you --admin")
            return
        print("%-4s %-24s %-26s %-9s %-8s %-6s %s" %
              ("id", "username", "name", "role", "status", "sessions", "last login"))
        for row in rows:
            print("%-4s %-24s %-26s %-9s %-8s %-6s %s" % (
                row["id"],
                row["username"],
                row["name"][:26],
                "admin" if row["is_admin"] else "member",
                "active" if row["is_active"] else "off",
                auth.count_sessions(conn, row["id"]),
                row["last_login_at"] or "never",
            ))
    finally:
        conn.close()


def _reset_password(args):
    db.init_db(args.db, with_demo=not args.no_seed)
    conn = db.connect(args.db)
    try:
        row = auth.get_user(conn, args.reset_password)
        if row is None:
            sys.exit("No account called `%s`." % args.reset_password)
        password = _resolve_password(args.password, row["username"])
        auth.set_password(conn, row["id"], password)
        # A reset always invalidates sessions: if the password is being reset
        # because someone else knew it, they should not still be signed in.
        removed = auth.end_all_sessions(conn, row["id"])
        conn.commit()
    except auth.AuthError as exc:
        sys.exit("%s" % exc.message)
    finally:
        conn.close()
    print("Password reset for %s (%s). %d session(s) signed out."
          % (row["username"], row["name"], removed))
    if not args.password:
        print("Password: %s" % password)


def _bootstrap_admin_from_env(conn):
    """Create the first administrator from the environment, once.

    Exists so a fresh instance can come up ready to use during provisioning,
    when there is no interactive terminal to answer a password prompt. The
    credentials are passed in, never stored in this repository.
    """
    username = os.environ.get("COVEX_ADMIN_USERNAME")
    password = os.environ.get("COVEX_ADMIN_PASSWORD")
    if not username or not password or auth.count_users(conn) > 0:
        return False
    try:
        auth.create_user(
            conn,
            name=os.environ.get("COVEX_ADMIN_NAME") or username,
            username=username,
            password=password,
            email=os.environ.get("COVEX_ADMIN_EMAIL"),
            is_admin=True,
        )
        conn.commit()
    except auth.AuthError as exc:
        sys.stderr.write("Bootstrap administrator not created: %s\n" % exc.message)
        return False
    print("Created bootstrap administrator `%s`." % username)
    return True


# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the tracker server.")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite file (default: %(default)s)")
    parser.add_argument("--fresh", action="store_true",
                        help="delete the existing database and reseed demo data "
                             "(accounts too)")
    parser.add_argument("--no-seed", action="store_true",
                        help="create the schema without the demo workspace. For a real "
                             "deployment: a seeded project cannot be deleted through "
                             "the app, so it would be permanent.")
    parser.add_argument("--add-user", action="store_true", help="create an account and exit")
    parser.add_argument("--list-users", action="store_true", help="show accounts and exit")
    parser.add_argument("--reset-password", metavar="USERNAME",
                        help="set a new password for an account and exit")
    parser.add_argument("--name", help="full name, for --add-user")
    parser.add_argument("--username", help="sign-in name, for --add-user")
    parser.add_argument("--email", help="address, for --add-user (defaults to "
                                       "<username>@%s)" % auth.DEFAULT_EMAIL_DOMAIN)
    parser.add_argument("--password", help="password, for --add-user / --reset-password. "
                                          "Omit to be prompted, or to have one generated.")
    parser.add_argument("--admin", action="store_true",
                        help="make the account an administrator, for --add-user")
    args = parser.parse_args(argv)

    if args.fresh and os.path.exists(args.db):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(args.db + suffix)
            except FileNotFoundError:
                pass

    if args.add_user:
        _add_user(args)
        return
    if args.list_users:
        _list_users(args)
        return
    if args.reset_password:
        _reset_password(args)
        return

    db.init_db(args.db, with_demo=not args.no_seed)

    conn = db.connect(args.db)
    try:
        _bootstrap_admin_from_env(conn)
        if auth.count_users(conn) == 0:
            print("No accounts exist yet. Until one does, nobody can sign in:")
            print("  python3 server.py --add-user --name \"Your Name\" "
                  "--username you --admin")
    finally:
        conn.close()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.db_path = args.db
    url = "http://%s:%d" % (args.host, args.port)
    print("Tracker running at %s" % url)
    print("Database: %s" % args.db)
    print("Press Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
