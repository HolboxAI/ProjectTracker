#!/usr/bin/env python3
"""Checks for accounts, sessions and the gate in front of the API.

    python3 tests/auth_test.py

Runs against a throwaway database -- no server needed. Everything here is
either `tracker/auth.py` on its own or `api.handle` driven exactly the way the
HTTP layer drives it, so a rule that only holds in one of those places fails
here.

`tests/http_check.py` covers the parts that need a socket: the cookie, the
sign-in page, and which paths are reachable without a session.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import api, auth, db  # noqa: E402

PASSED = []
FAILED = []

# Hashing is deliberately expensive in production -- that cost is the point of
# PBKDF2. These tests call the pieces directly, so they pass a low round count
# and keep the run fast; nothing here is checking the cost of hashing, and the
# accounts created through the API below still pay the real price.
FAST = 2000


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
    else:
        FAILED.append("%s %s" % (label, detail and "-> %s" % detail))


def call(db_path, method, path, body=None, query=None, user=None, token=None):
    conn = db.connect(db_path)
    try:
        result = api.handle(method, path, query or {}, body, conn, user, token)
        if result is None:
            return 404, {"error": "no route"}
        return result[0], result[1]
    except (api.ApiError, auth.AuthError) as exc:
        return exc.status, {"error": exc.message}
    finally:
        conn.close()


def account(db_path, **kwargs):
    """Create an account and return its serialized form plus its row."""
    conn = db.connect(db_path)
    try:
        row = auth.create_user(
            conn,
            name=kwargs.get("name", "Dana Whitfield"),
            username=kwargs.get("username", "dana"),
            password=kwargs.get("password", "correct-horse-9"),
            is_admin=kwargs.get("is_admin", False),
            rounds=FAST,
        )
        conn.commit()
        return auth.serialize_user(row)
    finally:
        conn.close()


def main():
    tmpdir = tempfile.mkdtemp(prefix="tracker-auth-test-")
    db_path = os.path.join(tmpdir, "test.sqlite3")
    db.init_db(db_path)

    # --- password hashing -----------------------------------------------
    digest = auth.hash_password("correct-horse-9", rounds=FAST)
    check("a password verifies against its digest", auth.verify_password(digest, "correct-horse-9"))
    check("a wrong password fails", not auth.verify_password(digest, "correct-horse-8"))
    check("an empty password fails", not auth.verify_password(digest, ""))
    check("a corrupt digest fails closed",
          not auth.verify_password("not-a-hash", "correct-horse-9"))
    check("an empty digest fails closed", not auth.verify_password("", "correct-horse-9"))
    check("the digest is not the password", "correct-horse-9" not in digest)
    check("the digest names its algorithm", digest.startswith("pbkdf2_sha256$"))
    second = auth.hash_password("correct-horse-9", rounds=FAST)
    check("the same password hashes differently every time", digest != second,
          "a fixed salt would make two identical passwords obvious in the table")

    # --- password policy -------------------------------------------------
    for label, value in [
        ("too short", "Abc1"),
        ("no digit", "abcdefghij"),
        ("no letter", "1234567890"),
        ("a common password", "password123"),
    ]:
        try:
            auth.check_password(value)
            check("policy rejects %s" % label, False)
        except auth.AuthError:
            check("policy rejects %s" % label, True)

    generated = auth.generate_password()
    check("a generated password satisfies the policy",
          auth.check_password(generated) == generated and len(generated) >= 16)
    check("two generated passwords differ", generated != auth.generate_password())

    # --- accounts ---------------------------------------------------------
    dana = account(db_path)
    check("an account is created", dana["username"] == "dana" and dana["name"] == "Dana Whitfield")
    check("usernames are normalized to lower case",
          account(db_path, username="MixedCase", name="Mixed")["username"] == "mixedcase")
    check("an account gets a default work address", dana["email"] == "dana@holbox.com", dana["email"])
    check("a new account is not an administrator", dana["is_admin"] is False)
    check("a new account is active", dana["is_active"] is True)
    check("a new account has never signed in", dana["last_login_at"] is None)
    check("serialization never carries the digest", "password_hash" not in dana)

    conn = db.connect(db_path)
    try:
        row = auth.get_user(conn, "dana")
        check("the digest is stored on the row, not in the API shape",
              row["password_hash"].startswith("pbkdf2_sha256$"))

        for label, kwargs in [
            ("a duplicate username", {"username": "DANA"}),
            ("a username with spaces", {"username": "not valid"}),
            ("a one-letter username", {"username": "x"}),
            ("an empty name", {"name": "  "}),
            ("a malformed email", {"email": "nope"}),
        ]:
            try:
                auth.create_user(conn, name=kwargs.get("name", "Someone"),
                                 username=kwargs.get("username", "someone"),
                                 password="a-good-password-1",
                                 email=kwargs.get("email"), rounds=FAST)
                check("refuses %s" % label, False)
            except auth.AuthError as exc:
                check("refuses %s" % label, exc.status in (400, 409, 422), exc.status)
    finally:
        conn.close()

    # --- sign-in ----------------------------------------------------------
    conn = db.connect(db_path)
    try:
        check("the right password returns the account",
              auth.authenticate(conn, "dana", "correct-horse-9") is not None)
        check("a wrong password returns nothing",
              auth.authenticate(conn, "dana", "wrong-password-1") is None)
        check("an unknown username returns nothing",
              auth.authenticate(conn, "nobody", "correct-horse-9") is None)
        check("signing in is case-insensitive on the username",
              auth.authenticate(conn, "DANA", "correct-horse-9") is not None)
        fresh = auth.get_user(conn, "dana")
        check("a successful sign-in is stamped", fresh["last_login_at"] is not None)

        # A correct password on a switched-off account is a distinct message,
        # not a failed login: the person did nothing wrong.
        asleep = account(db_path, name="Sleepy Person", username="sleepy")
        auth.set_active(conn, asleep["id"], False)
        conn.commit()
        try:
            auth.authenticate(conn, "sleepy", "correct-horse-9")
            check("a deactivated account is refused", False)
        except auth.AuthError as exc:
            check("a deactivated account is refused with 403", exc.status == 403, exc.status)
        auth.set_active(conn, asleep["id"], True)
        conn.commit()
    finally:
        conn.close()

    # --- lockout ----------------------------------------------------------
    locked = account(db_path, name="Brute Force", username="bruteforce")
    conn = db.connect(db_path)
    try:
        for _ in range(auth.MAX_FAILED_ATTEMPTS):
            auth.authenticate(conn, "bruteforce", "wrong-password-1")
        check("failures are counted", auth.locked_out(conn, "bruteforce"))
        try:
            auth.authenticate(conn, "bruteforce", "correct-horse-9")
            check("a locked account is refused even with the right password", False)
        except auth.AuthError as exc:
            check("a locked account is refused with 429", exc.status == 429, exc.status)
        check("lockout is per account, not global", not auth.locked_out(conn, "dana"))
        check("the locked account is still locked, not signed in",
              auth.count_sessions(conn, locked["id"]) == 0)
    finally:
        conn.close()

    # --- sessions ---------------------------------------------------------
    conn = db.connect(db_path)
    try:
        row = auth.get_user(conn, "dana")
        token = auth.start_session(conn, row["id"], user_agent="test", ip="127.0.0.1")
        check("a session token is returned", bool(token) and len(token) > 20)
        check("the session resolves to its account",
              (auth.resolve_session(conn, token) or {})["username"] == "dana")
        check("a junk token resolves to nothing", auth.resolve_session(conn, "junk") is None)
        check("an empty token resolves to nothing", auth.resolve_session(conn, "") is None)
        check("the raw token is not what is stored",
              conn.execute("SELECT COUNT(*) AS c FROM sessions WHERE token_hash = ?",
                           (token,)).fetchone()["c"] == 0,
              "storing the token itself would make the table worth stealing")
        check("the session is counted", auth.count_sessions(conn, row["id"]) == 1)

        # An expired session is dropped rather than trusted.
        conn.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                     ("2000-01-01T00:00:00+00:00", auth._token_hash(token)))
        conn.commit()
        check("an expired session resolves to nothing",
              auth.resolve_session(conn, token) is None)
        check("the expired session is cleaned up",
              auth.count_sessions(conn, row["id"]) == 0)

        # Signing out ends exactly one session.
        keep = auth.start_session(conn, row["id"])
        other = auth.start_session(conn, row["id"])
        auth.end_session(conn, other)
        check("signing out ends that session", auth.resolve_session(conn, other) is None)
        check("signing out leaves other sessions alone",
              auth.resolve_session(conn, keep) is not None)

        # A password change ends everything except the tab making the change.
        spare = auth.start_session(conn, row["id"])
        auth.end_all_sessions(conn, row["id"], keep_token=keep)
        check("changing a password keeps the current session",
              auth.resolve_session(conn, keep) is not None)
        check("changing a password ends the other sessions",
              auth.resolve_session(conn, spare) is None)

        # Deactivating an account closes it immediately.
        auth.set_active(conn, row["id"], False)
        conn.commit()
        check("deactivating an account ends its sessions",
              auth.resolve_session(conn, keep) is None)
        auth.set_active(conn, row["id"], True)
        conn.commit()
    finally:
        conn.close()

    # --- the gate ---------------------------------------------------------
    dana = account(db_path, name="Dana Whitfield", username="dana-user")
    admin = account(db_path, name="Ada Okonkwo", username="ada", is_admin=True)

    status, _ = call(db_path, "GET", "/api/users")
    # 403, not 401: `api.handle` is reached only through the HTTP layer, which
    # refuses an anonymous request before it gets here. That 401 is asserted
    # over a real socket in tests/http_check.py; from this side an absent user
    # is simply not an administrator.
    check("account management needs a user at all", status == 403, status)
    status, _ = call(db_path, "GET", "/api/users", user=dana)
    check("account management refuses a non-administrator", status == 403, status)
    status, body = call(db_path, "GET", "/api/users", user=admin)
    check("an administrator can list accounts", status == 200 and len(body["users"]) >= 2, status)

    # An administrator creates an account, and the generated password works.
    status, body = call(db_path, "POST", "/api/users", {
        "name": "Tomas Berg", "username": "tomas", "is_admin": False,
    }, user=admin)
    check("an administrator can create an account", status == 201, body)
    handed_over = body.get("password")
    check("the generated password comes back once", bool(handed_over))
    conn = db.connect(db_path)
    try:
        check("the handed-over password really signs in",
              auth.authenticate(conn, "tomas", handed_over) is not None)
        check("the password is not in the account payload", "password" not in body["user"])
    finally:
        conn.close()

    status, body = call(db_path, "POST", "/api/users", {
        "name": "Another Dana", "username": "dana-user",
    }, user=admin)
    check("a duplicate username is refused over the API", status == 409, status)
    status, body = call(db_path, "POST", "/api/users", {
        "name": "No Password", "username": "nopass", "password": "short1",
    }, user=admin)
    check("a weak password is refused over the API", status == 422, status)

    # The ways an administrator can lock everyone out -- each refused.
    status, _ = call(db_path, "PATCH", "/api/users/%s" % admin["id"],
                     {"is_admin": False}, user=admin)
    check("an administrator cannot remove their own administrator access",
          status == 422, status)
    status, _ = call(db_path, "PATCH", "/api/users/%s" % admin["id"],
                     {"is_active": False}, user=admin)
    check("an administrator cannot deactivate their own account", status == 422, status)
    status, _ = call(db_path, "DELETE", "/api/users/%s" % admin["id"], user=admin)
    check("an administrator cannot delete their own account", status == 422, status)

    # And a member cannot manage accounts at all, whatever they ask for.
    for label, method, path, payload in [
        ("deactivate themselves", "PATCH", "/api/users/%s" % dana["id"], {"is_active": False}),
        ("delete themselves", "DELETE", "/api/users/%s" % dana["id"], None),
        ("promote themselves", "PATCH", "/api/users/%s" % dana["id"], {"is_admin": True}),
    ]:
        status, _ = call(db_path, method, path, payload, user=dana)
        check("a member cannot %s" % label, status == 403, status)

    # With a second administrator, the first is no longer the last one.
    status, body = call(db_path, "POST", "/api/users", {
        "name": "Second Admin", "username": "admin2", "is_admin": True,
    }, user=admin)
    second_admin = body["user"]
    status, _ = call(db_path, "PATCH", "/api/users/%s" % admin["id"],
                     {"is_admin": False}, user=second_admin)
    check("a demotion is allowed once another administrator exists", status == 200, status)
    call(db_path, "PATCH", "/api/users/%s" % second_admin["id"], {"is_admin": True},
         user=second_admin)

    # A reset ends the target's sessions and returns the new password once.
    conn = db.connect(db_path)
    try:
        tomas = auth.get_user(conn, "tomas")
        stale = auth.start_session(conn, tomas["id"])
    finally:
        conn.close()
    status, body = call(db_path, "POST", "/api/users/%s/password" % tomas["id"], {},
                        user=admin)
    check("an administrator can reset a password", status == 200 and body.get("password"), status)
    conn = db.connect(db_path)
    try:
        check("a reset ends the account's sessions",
              auth.resolve_session(conn, stale) is None)
        check("a reset leaves the administrator signed in",
              auth.authenticate(conn, "ada", "correct-horse-9") is not None)
        check("the reset password signs in",
              auth.authenticate(conn, "tomas", body["password"]) is not None)
    finally:
        conn.close()

    status, _ = call(db_path, "GET", "/api/users/9999", user=admin)
    check("an unknown account 404s", status == 404, status)

    # --- attribution ------------------------------------------------------
    # With an account attached, the work is signed with that person's name
    # instead of the old single-user `You`.
    status, body = call(db_path, "POST", "/api/issues", {
        "project": "PHX", "title": "Attribute this to Dana",
    }, user=dana)
    issue_id = body["issue"]["id"]
    check("the creator is the reporter", body["issue"]["reporter"] == "Dana Whitfield",
          body["issue"]["reporter"])

    call(db_path, "POST", "/api/issues/%s/comments" % issue_id, {"body": "Mine."}, user=dana)
    status, body = call(db_path, "GET", "/api/issues/%s" % issue_id, user=dana)
    check("a comment is signed by its author", body["comments"][0]["author"] == "Dana Whitfield",
          body["comments"][0]["author"])
    check("the activity trail names the actor",
          all(a["actor"] == "Dana Whitfield" for a in body["activity"]),
          [a["actor"] for a in body["activity"]])

    status, body = call(db_path, "GET", "/api/bootstrap", user=admin)
    check("bootstrap reports who you are", body["me"] == "Ada Okonkwo", body["me"])
    check("bootstrap carries the account for the UI",
          body["user"]["is_admin"] is True and body["user"]["username"] == "ada", body["user"])
    check("bootstrap says nobody is signed in without a user",
          call(db_path, "GET", "/api/bootstrap")[1]["user"] is None)

    # --- Firebase Google SSO ---------------------------------------------
    # Domain allowlist
    auth.check_google_domain("alice@holbox.ai", ("holbox.ai", "holbox.com"))
    auth.check_google_domain("bob@holbox.com", ("holbox.ai", "holbox.com"))
    domain_rejected = False
    try:
        auth.check_google_domain("charlie@gmail.com", ("holbox.ai", "holbox.com"))
    except auth.AuthError as exc:
        domain_rejected = exc.status == 403
    check("an untrusted Google domain is rejected with 403", domain_rejected)

    # Empty DB: First Google user becomes admin
    sso_db_path = os.path.join(tmpdir, "sso_empty.sqlite3")
    db.init_db(sso_db_path)
    sso_conn = db.connect(sso_db_path)
    try:
        def mock_first_user(tok, proj):
            return {
                "sub": "google-sub-first",
                "email": "krish@holbox.ai",
                "email_verified": True,
                "name": "Krish Sharma",
            }

        first_user = auth.authenticate_google(
            sso_conn, "mock-token", project_id="test-proj",
            allowed_domains=("holbox.ai", "holbox.com"), _verifier=mock_first_user,
        )
        check("first user on empty DB is promoted to admin",
              bool(first_user["is_admin"]) is True and first_user["email"] == "krish@holbox.ai",
              first_user)
        check("first user provider is google", first_user["provider"] == "google")

        # Second Google user is standard member (not admin)
        def mock_second_user(tok, proj):
            return {
                "sub": "google-sub-second",
                "email": "dev@holbox.com",
                "email_verified": True,
                "name": "Dev Person",
            }

        second_user = auth.authenticate_google(
            sso_conn, "mock-token", project_id="test-proj",
            allowed_domains=("holbox.ai", "holbox.com"), _verifier=mock_second_user,
        )
        check("subsequent Google user is not an admin",
              bool(second_user["is_admin"]) is False and second_user["email"] == "dev@holbox.com")
        check("second user gets unique username",
              second_user["username"] != first_user["username"])

        # Signing in again with the same google_sub returns the same user
        same_user = auth.authenticate_google(
            sso_conn, "mock-token", project_id="test-proj",
            allowed_domains=("holbox.ai", "holbox.com"), _verifier=mock_second_user,
        )
        check("repeat Google sign-in resolves to the same account",
              same_user["id"] == second_user["id"])

        # Linking an existing account created by username
        auth.create_user(sso_conn, "Local User", "localdev", "strong-password-1",
                         email="localdev@holbox.ai", rounds=FAST)
        sso_conn.commit()

        def mock_localdev(tok, proj):
            return {
                "sub": "google-sub-localdev",
                "email": "localdev@holbox.ai",
                "email_verified": True,
                "name": "Local User",
            }

        linked = auth.authenticate_google(
            sso_conn, "mock-token", project_id="test-proj",
            allowed_domains=("holbox.ai", "holbox.com"), _verifier=mock_localdev,
        )
        check("existing account links by email",
              linked["username"] == "localdev" and linked["google_sub"] == "google-sub-localdev")
        check("linked user can still sign in with local password",
              auth.authenticate(sso_conn, "localdev", "strong-password-1") is not None)

        # Deactivated user cannot sign in with Google
        auth.set_active(sso_conn, second_user["id"], False)
        sso_conn.commit()
        deactivated_blocked = False
        try:
            auth.authenticate_google(
                sso_conn, "mock-token", project_id="test-proj",
                allowed_domains=("holbox.ai", "holbox.com"), _verifier=mock_second_user,
            )
        except auth.AuthError as exc:
            deactivated_blocked = exc.status == 403
        check("a deactivated Google account is rejected with 403", deactivated_blocked)
    finally:
        sso_conn.close()

    print("\n%d passed, %d failed" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\nFailures:")
        for failure in FAILED:
            print("  x %s" % failure)
        return 1
    print("Accounts, sessions and attribution verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
