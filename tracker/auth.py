"""Accounts, password hashing and server-side sessions.

Two rules hold this together:

* A password is never stored -- only a PBKDF2-SHA256 digest with a per-user
  salt, compared with `hmac.compare_digest`. A failed lookup still pays the
  hashing cost, so the login form cannot be used to find out who has an
  account.
* A session is a random token carried in an HttpOnly cookie. The database
  stores only the SHA-256 of that token, so a leaked database file (or a
  backup of one) does not hand over live sessions.

The endpoints that set or read the cookie live in `server.py`, because
cookies are an HTTP concern. Every rule about accounts and sessions lives
here, so it can be tested without opening a socket.

Sign-in is local-only for now. `provider` and `google_sub` are already on the
users table so Google Workspace SSO can be added later by matching the
Workspace address against `email` and backfilling `google_sub`: no migration
of existing accounts, and no second password for anyone to keep.

The one write path here commits its own work (`resolve_session` housekeeping,
`start_session`, `authenticate`). That is deliberate and safe because the
server hands this module a fresh connection per request, outside the
`api.handle()` transaction. Rows touched are single and independent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

COOKIE_NAME = "covex_session"

PBKDF2_ROUNDS = 210_000
SALT_BYTES = 16
SESSION_DAYS = 14
MIN_PASSWORD_LENGTH = 10
MAX_FAILED_ATTEMPTS = 8
LOCKOUT_MINUTES = 15

# Accounts are local by default; `google` is reserved for the SSO step.
PROVIDERS = ("local", "google")
DEFAULT_EMAIL_DOMAIN = "holbox.com"

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

PASSWORD_ALPHABET = (
    "abcdefghijkmnopqrstuvwxyz"      # no l
    "ABCDEFGHJKLMNPQRSTUVWXYZ"       # no I O
    "23456789"                       # no 0 1
    "-_!@#%+="
)

# Rejected outright: these are the ones a hand-typed first password lands on.
WEAK_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "letmein123",
    "qwertyuiop", "1234567890", "changeme123", "holbox1234", "administrator",
    "welcome123", "tracker123", "covexboard", "covex-board",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  name          TEXT    NOT NULL,
  email         TEXT    NOT NULL DEFAULT '',
  password_hash TEXT    NOT NULL DEFAULT '',
  provider      TEXT    NOT NULL DEFAULT 'local',
  google_sub    TEXT,
  is_admin      INTEGER NOT NULL DEFAULT 0,
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT    NOT NULL,
  updated_at    TEXT    NOT NULL,
  last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT    PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL,
  expires_at TEXT    NOT NULL,
  last_seen  TEXT    NOT NULL,
  user_agent TEXT    NOT NULL DEFAULT '',
  ip         TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL,
  ip         TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts ON login_attempts (username, created_at);
"""


class AuthError(Exception):
    """A refusal the HTTP layer can turn into a status code and a message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _shift(minutes=0, days=0) -> str:
    moment = datetime.now(timezone.utc) + timedelta(minutes=minutes, days=days)
    return moment.replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, rounds: int = PBKDF2_ROUNDS) -> str:
    """`pbkdf2_sha256$<rounds>$<salt>$<digest>` -- self-describing, so the
    cost can be raised later without invalidating existing passwords."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2_sha256$%d$%s$%s" % (rounds, _b64(salt), _b64(digest))


def verify_password(stored: str, password: str) -> bool:
    if not stored or not password:
        return False
    try:
        algorithm, rounds, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(salt), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_b64(digest), expected)


# Spending the same work on a missing account keeps the response time flat.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(18), rounds=PBKDF2_ROUNDS)


def check_password(password) -> str:
    """Raise AuthError unless the password satisfies the policy."""
    if not isinstance(password, str) or password == "":
        raise AuthError(400, "A password is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(422, "Password must be at least %d characters" % MIN_PASSWORD_LENGTH)
    if len(password) > 256:
        raise AuthError(422, "Password must be 256 characters or fewer")
    if password.lower() in WEAK_PASSWORDS:
        raise AuthError(422, "That password is too common to use here")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        raise AuthError(422, "Password must include at least one letter and one number")
    return password


def generate_password(length: int = 16) -> str:
    """A password strong enough to be handed to someone by email."""
    while True:
        candidate = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        try:
            return check_password(candidate)
        except AuthError:
            continue


# --------------------------------------------------------------------------
# identity helpers
# --------------------------------------------------------------------------

def normalize_username(value) -> str:
    return str(value or "").strip().lower()


def clean_username(value) -> str:
    username = normalize_username(value)
    if not USERNAME_RE.match(username):
        raise AuthError(
            422,
            "Username must be 2-32 characters: lowercase letters, digits, "
            "dot, dash or underscore, starting with a letter or digit",
        )
    return username


def clean_name(value) -> str:
    name = str(value or "").strip()
    if not name:
        raise AuthError(400, "A name is required")
    if len(name) > 80:
        raise AuthError(422, "Name must be 80 characters or fewer")
    return name


def clean_email(value, username=None, strict=False) -> str:
    email = str(value or "").strip().lower()
    if not email and username:
        email = "%s@%s" % (username, DEFAULT_EMAIL_DOMAIN)
    if not email:
        return ""
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        if strict:
            raise AuthError(422, "That does not look like an email address")
        return ""
    return email


def serialize_user(row) -> dict:
    """The only shape a user is ever handed to the API or the browser in --
    the password digest is not in it."""
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "email": row["email"],
        "provider": row["provider"],
        "is_admin": bool(row["is_admin"]),
        "is_active": bool(row["is_active"]),
        "has_password": bool(row["password_hash"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def user_fields(user) -> dict:
    """Normalize a signed-in account to the serialized shape.

    `api.handle` is called with a database row by the HTTP layer (straight out
    of `resolve_session`) and with the serialized dict by the tests, so the
    conversion happens once here rather than being assumed at every use.
    """
    if not user:
        return {}
    if isinstance(user, sqlite3.Row):
        return serialize_user(user)
    return dict(user)


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

def get_user(conn: sqlite3.Connection, ref):
    """Look a user up by numeric id or by username (case-insensitive)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return conn.execute("SELECT * FROM users WHERE id = ?", (int(ref),)).fetchone()
    return conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (normalize_username(ref),)
    ).fetchone()


def list_users(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM users ORDER BY is_active DESC, name COLLATE NOCASE"
    ).fetchall()


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def count_active_admins(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_admin = 1 AND is_active = 1"
    ).fetchone()["c"]


def create_user(conn, name, username, password, email=None, is_admin=False,
                provider="local", rounds=PBKDF2_ROUNDS):
    name = clean_name(name)
    username = clean_username(username)
    if provider not in PROVIDERS:
        raise AuthError(422, "Unknown sign-in provider `%s`" % provider)
    if get_user(conn, username) is not None:
        raise AuthError(409, "Username `%s` is already taken" % username)

    password_hash = ""
    if password:
        password_hash = hash_password(check_password(password), rounds=rounds)
    elif provider == "local":
        raise AuthError(400, "A password is required for a local account")

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO users (username, name, email, password_hash, provider, "
        "is_admin, is_active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            username,
            name,
            clean_email(email, username, strict=True),
            password_hash,
            provider,
            1 if is_admin else 0,
            now,
            now,
        ),
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()


def set_password(conn, user_id, password, rounds=PBKDF2_ROUNDS) -> None:
    digest = hash_password(check_password(password), rounds=rounds)
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (digest, utcnow(), user_id),
    )


def set_active(conn, user_id, active: bool) -> None:
    conn.execute(
        "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
        (1 if active else 0, utcnow(), user_id),
    )
    if not active:
        end_all_sessions(conn, user_id)


def set_admin(conn, user_id, is_admin: bool) -> None:
    conn.execute(
        "UPDATE users SET is_admin = ?, updated_at = ? WHERE id = ?",
        (1 if is_admin else 0, utcnow(), user_id),
    )


def rename_user(conn, user_id, name) -> None:
    conn.execute(
        "UPDATE users SET name = ?, updated_at = ? WHERE id = ?",
        (clean_name(name), utcnow(), user_id),
    )


def delete_user(conn, user_id) -> None:
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def set_last_login(conn, user_id) -> None:
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), user_id))


# --------------------------------------------------------------------------
# throttling
# --------------------------------------------------------------------------

def record_failure(conn, username, ip="") -> None:
    now = utcnow()
    conn.execute(
        "INSERT INTO login_attempts (username, ip, created_at) VALUES (?, ?, ?)",
        (normalize_username(username), ip or "", now),
    )
    conn.execute(
        "DELETE FROM login_attempts WHERE created_at < ?", (_shift(minutes=-LOCKOUT_MINUTES),)
    )
    conn.commit()


def clear_failures(conn, username) -> None:
    conn.execute("DELETE FROM login_attempts WHERE username = ?", (normalize_username(username),))
    conn.commit()


def recent_failures(conn, username) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM login_attempts WHERE username = ? AND created_at >= ?",
        (normalize_username(username), _shift(minutes=-LOCKOUT_MINUTES)),
    ).fetchone()
    return row["c"]


def locked_out(conn, username) -> bool:
    return recent_failures(conn, username) >= MAX_FAILED_ATTEMPTS


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(conn, user_id, user_agent="", ip="") -> str:
    """Mint a session and return the token. Only its digest is stored."""
    purge_expired(conn)
    token = secrets.token_urlsafe(32)
    now = utcnow()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, "
        "last_seen, user_agent, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _token_hash(token),
            user_id,
            now,
            _shift(days=SESSION_DAYS),
            now,
            str(user_agent or "")[:200],
            str(ip or "")[:64],
        ),
    )
    conn.commit()
    return token


def resolve_session(conn, token):
    """Return the user row behind a cookie token, or None if it is not good.

    Housekeeping (dropping the expired row, stamping `last_seen`) is written
    and committed here; see the note at the top of the module.
    """
    if not token:
        return None
    digest = _token_hash(token)
    session = conn.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (digest,)
    ).fetchone()
    if session is None:
        return None

    # ISO-8601 UTC strings of equal format sort correctly as text.
    if session["expires_at"] <= utcnow():
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
        conn.commit()
        return None

    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if user is None or not user["is_active"]:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (digest,))
        conn.commit()
        return None

    conn.execute(
        "UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (utcnow(), digest)
    )
    conn.commit()
    return user


def end_session(conn, token) -> None:
    if not token:
        return
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()


def end_all_sessions(conn, user_id, keep_token=None) -> int:
    """Sign a user out everywhere -- used on password change and deactivation.

    `keep_token` spares the session making the change, so changing your own
    password does not immediately log you out of the tab you are sitting in.
    """
    if keep_token:
        cur = conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash <> ?",
            (user_id, _token_hash(keep_token)),
        )
    else:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount


def purge_expired(conn) -> int:
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),))
    conn.commit()
    return cur.rowcount


def count_sessions(conn, user_id) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM sessions WHERE user_id = ? AND expires_at > ?",
        (user_id, utcnow()),
    ).fetchone()["c"]


# --------------------------------------------------------------------------
# sign-in
# --------------------------------------------------------------------------

def authenticate(conn, username, password, ip=""):
    """Check credentials and return the user row, or None.

    Every failure looks the same from the outside: the caller sends one
    generic message, so a wrong password and a missing account are
    indistinguishable.
    """
    username = normalize_username(username)
    if locked_out(conn, username):
        raise AuthError(
            429,
            "Too many failed attempts. Try again in %d minutes." % LOCKOUT_MINUTES,
        )

    user = get_user(conn, username)
    if user is None or user["provider"] != "local" or not user["password_hash"]:
        # Pay the hashing cost anyway, then fail.
        verify_password(_DUMMY_HASH, password or "")
        record_failure(conn, username, ip)
        return None

    if not verify_password(user["password_hash"], password or ""):
        record_failure(conn, username, ip)
        return None

    if not user["is_active"]:
        # Correct password, switched-off account: say so plainly rather than
        # letting them guess why nothing works.
        clear_failures(conn, username)
        raise AuthError(403, "That account has been deactivated")

    clear_failures(conn, username)
    set_last_login(conn, user["id"])
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
