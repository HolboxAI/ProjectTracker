#!/bin/sh
# Send the current working tree to the instance and restart it.
#
#   sh deploy/redeploy.sh ubuntu@<elastic-ip>
#   IDENTITY=~/.ssh/other.pem sh deploy/redeploy.sh ubuntu@1.2.3.4
#
# Copies code only. The database lives in /var/lib/covex-tracker on the instance
# and is excluded from the archive, so this cannot overwrite it.
#
# TLS is deliberately not touched: bootstrap runs with ENABLE_TLS=no, and it
# leaves an existing nginx site alone, so the certificate config certbot wrote
# into that file survives a redeploy.

set -eu

TARGET="${1:?usage: redeploy.sh user@host}"
DOMAIN="${DOMAIN:-tracker.holbox.ai}"
IDENTITY="${IDENTITY:-$HOME/.ssh/covex-tracker.pem}"
STAGING=/tmp/covex-tracker-deploy
ARCHIVE=/tmp/covex-tracker.tgz
SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$IDENTITY" ]; then
  echo "No key at $IDENTITY. Pass IDENTITY=/path/to/key.pem, or create the" >&2
  echo "instance key first (see deploy/DEPLOY.md)." >&2
  exit 1
fi

# -i on every call: the instance key is not a default identity, so without it
# ssh falls back to ~/.ssh/id_* , finds nothing, and reports "Permission denied
# (publickey)" -- which reads like a broken server rather than a missing flag.
remote() {
  ssh -i "$IDENTITY" -o BatchMode=yes -o ConnectTimeout=15 \
      -o StrictHostKeyChecking=accept-new "$TARGET" "$@"
}

echo "==> Packing the working tree"
tar -czf "$ARCHIVE" -C "$SOURCE_DIR" \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='*.sqlite3' \
  --exclude='*.sqlite3-shm' \
  --exclude='*.sqlite3-wal' \
  --exclude='__pycache__' \
  .

echo "==> Copying to $TARGET"
remote "rm -rf $STAGING && mkdir -p $STAGING"
scp -q -i "$IDENTITY" -o StrictHostKeyChecking=accept-new "$ARCHIVE" "$TARGET:$STAGING.tgz"
remote "tar -xzf $STAGING.tgz -C $STAGING && rm -f $STAGING.tgz"

echo "==> Running bootstrap on the instance (idempotent, TLS left as it is)"
# No -t: this has to work from a script with no terminal. The ubuntu user has
# passwordless sudo on the cloud image, so nothing here needs to prompt.
remote "sudo ENABLE_TLS=no bash $STAGING/deploy/bootstrap.sh '$DOMAIN' admin@holbox.com"

echo "==> Restarting"
remote "sudo systemctl restart covex-tracker"
sleep 2
remote "systemctl --no-pager --lines=3 status covex-tracker | head -5"

echo "==> Checking it answers locally"
remote "curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8077/login"

echo
echo "Done. Verify from outside with:"
echo "  COVEX_ADMIN_PASSWORD=... python3 tests/live_check.py https://$DOMAIN"
