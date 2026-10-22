#!/usr/bin/env python3
"""Verify a *deployed* instance over its real HTTPS URL.

    COVEX_ADMIN_USERNAME=leeneksh COVEX_ADMIN_PASSWORD=... \
      python3 tests/live_check.py https://tracker.holbox.ai

This is not the same job as `tests/http_check.py`. That one runs against a
throwaway seeded database and asserts the API in depth. This one asks a live
deployment the questions that only a deployment raises: is the certificate
good, does the gate hold, does a real sign-in work, and can an administrator
actually manage accounts.

It creates one temporary account (to prove a generated password really signs
in), then deletes it, so it leaves no residue. It deliberately does **not**
create projects or issues: production has no seeded workspace, and nothing
deletes a project, so anything it left behind would be permanent.

The password is read from the environment rather than argv, where `ps` would
show it to anyone on the machine.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://tracker.holbox.ai"
USERNAME = os.environ.get("COVEX_ADMIN_USERNAME") or "admin"
PASSWORD = os.environ.get("COVEX_ADMIN_PASSWORD")

PASSED = []
FAILED = []
COOKIE = None


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
    else:
        FAILED.append("%s %s" % (label, "-> %s" % (detail,) if detail else ""))


def call(method, path, payload=None, cookie=None, headers=None):
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if cookie:
        request_headers["Cookie"] = cookie
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def body_of(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def main():
    global COOKIE

    if not PASSWORD:
        print("Set COVEX_ADMIN_PASSWORD (and COVEX_ADMIN_USERNAME if not 'admin').")
        print("On the instance:  sudo cat /root/covex-admin-password")
        return 2

    host = BASE.split("//", 1)[-1].split("/")[0]
    print("Target: %s\n" % BASE)

    # --- certificate ----------------------------------------------------
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as raw_socket:
            with context.wrap_socket(raw_socket, server_hostname=host) as tls:
                cert = tls.getpeercert()
        # The default context validates the chain and the hostname, so getting
        # here at all means the certificate is trusted for this name.
        subject = dict(x[0] for x in cert["subject"]).get("commonName", "?")
        issuer = dict(x[0] for x in cert["issuer"]).get("organizationName", "?")
        expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        days_left = (expires.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        check("the certificate is valid for %s" % host, subject == host or host.endswith(subject),
              subject)
        check("the certificate chains to a real authority", issuer != "?", issuer)
        check("the certificate is not about to expire", days_left > 20,
              "%d days left (expires %s)" % (days_left, expires.date()))
        print("  issuer: %s, expires in %d days\n" % (issuer, days_left))
    except Exception as exc:
        check("TLS handshake against %s" % host, False, "%s: %s" % (type(exc).__name__, exc))

    # --- the gate -------------------------------------------------------
    status, headers, raw = call("GET", "/login")
    check("the sign-in page is served", status == 200, status)
    check("it is the sign-in page", b'id="login-form"' in raw)
    check("the app shell is not served to a stranger", b'<div class="app"' not in raw)
    status, _, _ = call("GET", "/api/bootstrap")
    check("the API refuses an anonymous request", status == 401, status)
    status, _, _ = call("GET", "/js/app.js")
    check("the app bundle refuses an anonymous request", status == 401, status)

    # --- a real sign-in --------------------------------------------------
    status, headers, raw = call("POST", "/api/auth/login",
                                {"username": USERNAME, "password": PASSWORD + "-nope"})
    check("a wrong password is refused", status == 401, status)

    status, headers, raw = call("POST", "/api/auth/login",
                                {"username": USERNAME, "password": PASSWORD})
    check("the administrator can sign in", status == 200, status)
    cookie = headers.get("Set-Cookie", "")
    check("the session cookie is HttpOnly", "HttpOnly" in cookie)
    check("the session cookie is Secure over HTTPS", "Secure" in cookie,
          "without Secure the cookie would leak if anyone reached this over http")
    if not cookie:
        print("No cookie; cannot continue.")
        return 1
    COOKIE = cookie.split(";")[0]

    status, _, raw = call("GET", "/api/bootstrap", cookie=COOKIE)
    boot = body_of(raw) or {}
    check("bootstrap answers with the session", status == 200, status)
    check("the account is an administrator", (boot.get("user") or {}).get("is_admin") is True,
          boot.get("user"))
    # Deliberately *not* "is the workspace empty": people use this, so a live
    # deployment has projects in it before long, and an assertion on emptiness
    # fails the moment someone does real work. What actually matters and stays
    # true is that the demo workspace was never seeded -- nothing can delete a
    # project through the app, so a seeded one would be permanent.
    project_keys = [p.get("key") for p in boot.get("projects", [])]
    seeded = sorted({"PHX", "MOB"} & set(project_keys))
    check("the demo workspace was never seeded", not seeded, "found %s" % seeded)
    print("  workspace: %d project(s) %s\n"
          % (len(project_keys), ", ".join(project_keys) or "(none)"))

    # --- account management, end to end ---------------------------------
    temp_user = "live-check-temp"
    status, _, raw = call("POST", "/api/users",
                          {"name": "Live Check", "username": temp_user}, cookie=COOKIE)
    created = body_of(raw) or {}
    check("an administrator can create an account", status == 201, created)
    temp_password = created.get("password")
    check("a first password is generated and returned once", bool(temp_password))
    temp_id = (created.get("user") or {}).get("id")
    if temp_id:
        try:
            status, _, _ = call("POST", "/api/auth/login",
                                {"username": temp_user, "password": temp_password})
            check("the generated password actually signs in", status == 200, status)

            status, headers, _ = call("POST", "/api/auth/login",
                                      {"username": temp_user, "password": temp_password})
            temp_cookie = headers.get("Set-Cookie", "").split(";")[0]
            status, _, _ = call("GET", "/api/users", cookie=temp_cookie)
            check("a non-administrator cannot list accounts", status == 403, status)
        finally:
            # Always clean up, even if a check above raised.
            status, _, _ = call("DELETE", "/api/users/%s" % temp_id, cookie=COOKIE)
            check("the temporary account is deleted again", status == 200, status)
        status, _, _ = call("POST", "/api/auth/login",
                            {"username": temp_user, "password": temp_password})
        check("the deleted account can no longer sign in", status == 401, status)

    # --- sign-out --------------------------------------------------------
    status, headers, _ = call("POST", "/api/auth/logout", cookie=COOKIE)
    check("signing out succeeds", status == 200, status)
    check("signing out clears the cookie", "Max-Age=0" in headers.get("Set-Cookie", ""))
    status, _, _ = call("GET", "/api/bootstrap", cookie=COOKIE)
    check("the session is dead after sign-out", status == 401, status)

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\nFailures:")
        for failure in FAILED:
            print("  x %s" % failure)
        return 1
    print("Live deployment verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
