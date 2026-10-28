"""Attachment bytes on disk.

Files are the one thing here that is not a row. A workspace's worth of
screenshots inside the SQLite file would turn the thing we back up and copy to
a new host into a blob store, so the database keeps the metadata and the bytes
live in an `attachments/` directory beside it -- the two move together, which
is the property that actually matters.

Nothing in this module knows about HTTP. What it does own is the policy: how
large a file may be, and whether a given one is safe to hand back as an inline
image.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid

# 8 MiB. A retina screenshot is comfortably under this; a video is not, and
# video in a comment was never the ask.
MAX_BYTES = 8 * 1024 * 1024

# Only these are ever served inline, and only after the bytes have been checked
# against the magic numbers below.
IMAGE_TYPES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}

_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# Anything outside this is replaced, so a display name cannot smuggle a quote
# or a newline into a Content-Disposition header.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def sniff_image(data: bytes):
    """The image type these bytes really are, or None.

    What the client says a file is counts for nothing: `file.type` in a browser
    is a string that client chose. A stored file that claims to be image/png
    but is really HTML would be a stored XSS the moment it was served, so the
    decision to serve anything inline rests on these bytes instead.
    """
    if not data:
        return None
    for content_type, magics in IMAGE_TYPES.items():
        for magic in magics:
            if not data.startswith(magic):
                continue
            # A RIFF container is not necessarily an image: WAV and AVI share
            # the header, so WEBP has to be checked at its own offset.
            if content_type == "image/webp" and data[8:12] != b"WEBP":
                continue
            return content_type
    return None


def clean_filename(name) -> str:
    """A display name that cannot break a header and cannot escape a path."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _SAFE_NAME.sub("_", base)
    base = re.sub(r"\s+", " ", base).strip(". ")
    return (base or "attachment")[:120]


def root(conn) -> str:
    """Where this database keeps its files.

    Beside the database file, so a backup is one `cp` of two paths. Read out of
    SQLite rather than threaded through every call: the connection already
    knows which file it is, and `api.handle` keeps its signature for the tests
    and the CLI.
    """
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return os.path.join(os.path.dirname(os.path.abspath(row[2])), "attachments")
    except Exception:  # noqa: BLE001 -- a connection that cannot answer is not fatal here
        pass
    return os.path.join(tempfile.gettempdir(), "tracker-attachments")


def store(conn, data: bytes) -> str:
    """Write the bytes and return the name they are stored under.

    The name on disk is a random id plus the extension the bytes earned, never
    the name the client sent: two people uploading `screenshot.png` must not
    collide, and nothing the client controls should become a path.
    """
    directory = root(conn)
    os.makedirs(directory, exist_ok=True)
    name = uuid.uuid4().hex + _EXTENSIONS.get(sniff_image(data), ".bin")
    path = os.path.join(directory, name)
    # 0600: this is somebody's screenshot, not public content.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
    return name


def read(conn, stored_name) -> bytes:
    path = os.path.join(root(conn), os.path.basename(str(stored_name)))
    with open(path, "rb") as stream:
        return stream.read()


def remove(conn, stored_name) -> None:
    """Best-effort delete. Used when a row is rolled back after a write."""
    try:
        os.remove(os.path.join(root(conn), os.path.basename(str(stored_name))))
    except OSError:
        pass


def serving(content_type: str):
    """(type to send, may it be shown inline) for one stored file.

    Everything that is not a verified image is sent as an opaque download, so
    an uploaded `.html` or `.svg` can never render as a page on this origin.
    """
    if content_type in IMAGE_TYPES:
        return content_type, True
    return "application/octet-stream", False
