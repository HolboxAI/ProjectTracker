#!/usr/bin/env python3
"""Checks the running server over real HTTP: static files, content types,
JSON API round trips, error statuses, and the session gate.

    python3 tests/http_check.py [base_url] [--user NAME] [--password SECRET]

The app is behind a login now, so this signs in first and keeps the session
cookie for everything after. Credentials come from --user/--password or from
COVEX_TEST_USERNAME / COVEX_TEST_PASSWORD. There is no default: a suite that
quietly skipped its authenticated half would report success for a server
nobody can use. `tests/run_http_check.sh` starts a server, creates an account
and runs this in one step.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ARGV = sys.argv[1:]
BASE = ARGV[0] if ARGV and not ARGV[0].startswith("--") else "http://127.0.0.1:8077"


def arg_value(name):
    if name in ARGV:
        index = ARGV.index(name)
        if index + 1 < len(ARGV):
            return ARGV[index + 1]
    return None


USERNAME = arg_value("--user") or os.environ.get("COVEX_TEST_USERNAME")
PASSWORD = arg_value("--password") or os.environ.get("COVEX_TEST_PASSWORD")

PASSED = []
FAILED = []
COOKIE = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back to the caller instead of following it."""

    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
    else:
        FAILED.append("%s %s" % (label, "-> %s" % (detail,) if detail else ""))


def call(method, path, payload=None, headers=None, sign_in=False, follow=True):
    """One request. Everything but the sign-in call carries the cookie.

    `follow=False` reports a redirect as its own response instead of chasing
    it, so a test can look at the Location header.
    """
    url = BASE + path
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if COOKIE and not sign_in:
        request_headers["Cookie"] = COOKIE
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    opener = urllib.request.build_opener() if follow else _NO_REDIRECT_OPENER
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read()
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def json_body(body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def main():
    global COOKIE

    if not USERNAME or not PASSWORD:
        print("Needs an account to test against, and there is no default.\n")
        print("  python3 tests/http_check.py %s --user NAME --password SECRET\n" % BASE)
        print("or set COVEX_TEST_USERNAME / COVEX_TEST_PASSWORD. To do all of it in")
        print("one step, including starting the server:  sh tests/run_http_check.sh")
        return 2

    # --- what a stranger can reach --------------------------------------
    status, _, body = call("GET", "/")
    check("a signed-out visitor gets the sign-in page",
          status == 200 and b'id="login-form"' in body, status)
    check("the sign-in page does not leak the app shell", b'<div class="app"' not in body)

    status, _, body = call("GET", "/login")
    check("GET /login serves the sign-in page",
          status == 200 and b'id="login-form"' in body, status)

    for path in ["/design-tokens.css", "/app.css", "/js/login.js"]:
        status, _, _ = call("GET", path)
        check("the sign-in page can load %s" % path, status == 200, status)

    status, _, _ = call("GET", "/js/app.js")
    check("the app bundle is refused without a session", status == 401, status)
    status, _, _ = call("GET", "/api/bootstrap")
    check("the API is refused without a session", status == 401, status)
    status, _, body = call("GET", "/js/nope.js")
    check("a missing asset is refused before it is 404", status == 401, status)
    status, _, body = call("GET", "/api/auth/me")
    check("asking who you are works signed out", status == 200, status)
    check("and the answer is nobody", (json_body(body) or {}).get("user") is None)

    status, headers, _ = call("GET", "/some/deep/link", follow=False)
    check("a deep link sends a stranger to sign in", status == 302, status)
    check("and remembers where they were going",
          "/login?next=" in headers.get("Location", ""), headers.get("Location"))

    # --- signing in -------------------------------------------------------
    status, _, body = call("POST", "/api/auth/login",
                           {"username": USERNAME, "password": PASSWORD + "-wrong"}, sign_in=True)
    check("a wrong password is refused", status == 401, status)
    check("and the message does not say whether the account exists",
          (json_body(body) or {}).get("error") == "Wrong username or password",
          json_body(body))

    status, headers, body = call("POST", "/api/auth/login",
                                 {"username": USERNAME, "password": PASSWORD}, sign_in=True)
    check("the right password signs in", status == 200, status)
    session = json_body(body) or {}
    check("sign-in returns the account", session.get("user", {}).get("username") == USERNAME,
          session.get("user"))
    check("sign-in reports the display name", bool(session.get("me")), session.get("me"))

    cookie = headers.get("Set-Cookie", "")
    check("sign-in sets a session cookie", cookie.startswith("covex_session="), cookie[:40])
    check("the cookie is HttpOnly", "HttpOnly" in cookie, cookie)
    check("the cookie is SameSite", "SameSite=Lax" in cookie, cookie)
    if not cookie:
        print("\nCannot continue without a session cookie.")
        return 1
    COOKIE = cookie.split(";")[0]

    # --- static assets, signed in ----------------------------------------
    for path, expected_type, marker in [
        ("/", "text/html", b"<div class=\"app\""),
        ("/design-tokens.css", "text/css", b"--accent"),
        ("/app.css", "text/css", b".ticket"),
        ("/app.css", "text/css", b".auth-card"),
        ("/js/login.js", "javascript", b"nextUrl"),
        ("/js/app.js", "javascript", b"handleRoute"),
        ("/js/board.js", "javascript", b"attachDrop"),
        ("/js/backlog.js", "javascript", b"sprintBlock"),
        ("/js/issues.js", "javascript", b"COLUMNS"),
        ("/js/reports.js", "javascript", b"barPanel"),
        ("/js/drawer.js", "javascript", b"activitySection"),
        ("/js/modal.js", "javascript", b"openUsers"),
        ("/js/ui.js", "javascript", b"showDropLine"),
        ("/js/api.js", "javascript", b"sprintAction"),
    ]:
        status, headers, body = call("GET", path)
        check("GET %s is 200" % path, status == 200, status)
        check("GET %s has the right content type" % path,
              expected_type in headers.get("Content-Type", ""),
              headers.get("Content-Type"))
        check("GET %s serves the real file" % path, marker in body, body[:60])

    # --- the page links its stylesheets with a leading slash ------------
    status, _, body = call("GET", "/")
    html = body.decode("utf-8")
    check("index links /design-tokens.css", 'href="/design-tokens.css"' in html)
    check("index links /app.css", 'href="/app.css"' in html)
    check("index loads the module entry point", 'src="/js/app.js"' in html)

    # --- missing asset and traversal ------------------------------------
    status, _, _ = call("GET", "/js/nope.js")
    check("missing asset is 404", status == 404, status)

    # --- the gate does not trust a made-up cookie -----------------------
    real_cookie, COOKIE = COOKIE, "covex_session=not-a-real-token"
    status, _, _ = call("GET", "/api/bootstrap")
    check("a forged session cookie is refused", status == 401, status)
    COOKIE = real_cookie

    # --- api ------------------------------------------------------------
    status, _, body = call("GET", "/api/bootstrap")
    boot = json_body(body) or {}
    check("bootstrap is 200", status == 200, status)
    check("bootstrap lists projects", len(boot.get("projects", [])) >= 2)
    check("bootstrap carries workflow meta", boot.get("meta", {}).get("statuses") == [
        "todo", "in_progress", "in_review", "done"])
    check("bootstrap names the signed-in person", bool(boot.get("me")), boot.get("me"))

    status, _, body = call("GET", "/api/issues?project=PHX&status=in_progress")
    listing = json_body(body) or {}
    check("filtered issue list works over HTTP", listing.get("count") == 4, listing.get("count"))

    status, _, body = call("GET", "/api/issues?project=PHX&q=a")
    check("search query over HTTP", status == 200 and (json_body(body) or {}).get("count", 0) > 0)

    status, _, body = call("POST", "/api/issues", {
        "project": "PHX",
        "title": "HTTP round-trip check",
        "type": "bug",
        "priority": "high",
        "labels": "http, check",
    })
    created = json_body(body) or {}
    issue = created.get("issue", {})
    check("POST creates an issue", status == 201 and issue.get("key", "").startswith("PHX-"), status)
    check("labels survive the round trip", issue.get("labels") == ["http", "check"], issue.get("labels"))
    check("the issue is signed with the signed-in person",
          issue.get("reporter") == boot.get("me"), issue.get("reporter"))

    issue_id = issue.get("id")
    status, _, body = call("PATCH", "/api/issues/%s" % issue_id, {"status": "done", "index": 0})
    check("PATCH moves the issue", status == 200 and (json_body(body) or {})["issue"]["status"] == "done", status)

    status, _, body = call("POST", "/api/issues/%s/comments" % issue_id, {"body": "Checked over HTTP."})
    check("POST adds a comment", status == 201, status)

    status, _, body = call("GET", "/api/issues/%s" % issue_id)
    detail = json_body(body) or {}
    check("GET issue returns comments", len(detail.get("comments", [])) == 1)
    check("GET issue returns activity",
          any(a["field"] == "status" for a in detail.get("activity", [])))

    status, _, _ = call("DELETE", "/api/issues/%s" % issue_id)
    check("DELETE removes the issue", status == 200, status)
    status, _, _ = call("GET", "/api/issues/%s" % issue_id)
    check("deleted issue 404s", status == 404, status)

    # --- error handling -------------------------------------------------
    status, _, body = call("POST", "/api/issues", {"project": "PHX"})
    check("missing title is 400", status == 400, status)
    check("error payload carries a message", "title" in (json_body(body) or {}).get("error", ""),
          json_body(body))

    status, _, body = call("POST", "/api/issues", {"project": "PHX", "title": "x", "status": "shipped"})
    check("bad enum is 422", status == 422, status)

    status, _, body = call("GET", "/api/does-not-exist")
    check("unknown API route is 404", status == 404, status)

    status, _, body = call("GET", "/api/issues?project=NOPE")
    listing = json_body(body) or {}
    check("unknown project yields an empty list, not an error",
          status == 200 and listing.get("count") == 0, status)

    # --- a cross-site write is refused ----------------------------------
    status, _, _ = call("POST", "/api/issues", {"project": "PHX", "title": "cross-site"},
                        headers={"Origin": "https://not-this-host.example"})
    check("a write from another origin is refused", status == 403, status)

    # --- signing out -----------------------------------------------------
    status, headers, _ = call("POST", "/api/auth/logout")
    check("signing out is 200", status == 200, status)
    check("signing out clears the cookie", "Max-Age=0" in headers.get("Set-Cookie", ""),
          headers.get("Set-Cookie"))
    status, _, _ = call("GET", "/api/bootstrap")
    check("the old cookie stops working after sign-out", status == 401, status)

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\nFailures:")
        for failure in FAILED:
            print("  x %s" % failure)
        return 1
    print("HTTP layer verified, including the session gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
