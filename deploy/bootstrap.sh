#!/bin/bash
# Provision a fresh Ubuntu 24.04 EC2 instance to serve Covex-Board.
#
#   sudo bash deploy/bootstrap.sh tracker.holbox.ai you@holbox.com
#
# Run it from a checkout that is already on the instance (see deploy/DEPLOY.md
# for the scp in front of this). It installs the runtime, puts the code in
# /opt/covex-tracker, creates the first administrator, and starts the service
# behind nginx with a Let's Encrypt certificate.
#
# Idempotent: run it again after a code change and it will replace the code,
# leave the database alone, and restart the service. It never touches
# /var/lib/covex-tracker/tracker.sqlite3.
#
# The design constraint that shapes all of this: the app is *one* Python
# process over *one* SQLite file. There is deliberately no autoscaling group,
# no second worker and no read replica -- two processes on this database
# corrupt it.

set -euo pipefail

DOMAIN="${1:?usage: bootstrap.sh <domain> <email-for-certbot>}"
EMAIL="${2:?usage: bootstrap.sh <domain> <email-for-certbot>}"
ENABLE_TLS="${ENABLE_TLS:-yes}"

APP_USER=covex
APP_DIR=/opt/covex-tracker
STATE_DIR=/var/lib/covex-tracker
DB="$STATE_DIR/tracker.sqlite3"
PORT=8077
UNIT=/etc/systemd/system/covex-tracker.service
NGINX_SITE=/etc/nginx/sites-available/covex-tracker

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> Installing the runtime"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 nginx certbot python3-certbot-nginx

echo "==> Creating the service account"
# No shell, no home, and it owns exactly one directory.
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$STATE_DIR"
install -d -o root -g "$APP_USER" -m 0755 "$APP_DIR"

echo "==> Installing the application into $APP_DIR"
# README.md goes along because the systemd unit links to it as the service's
# documentation; without it that link is dead. deploy/ goes along so the
# merge and redeploy tools are available on the box itself, not only from a
# checkout that may not be to hand when you need them.
for item in server.py tracker static README.md deploy; do
  rm -rf "${APP_DIR:?}/$item"
  cp -r "$SOURCE_DIR/$item" "$APP_DIR/$item"
done
# Code is read-only to the service account; only the state directory is
# writable, so a bug in the app cannot rewrite the thing serving it.
chown -R root:"$APP_USER" "$APP_DIR"
find "$APP_DIR" -type d -exec chmod 0755 {} +
find "$APP_DIR" -type f -exec chmod 0644 {} +
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

echo "==> Creating the first administrator"
# The password is generated here, handed to the CLI through the environment
# (not argv, which `ps` shows), and left in /root/ for the operator to read
# once. The app only uses it while there are no accounts at all.
ADMIN_FILE=/root/covex-admin-password
if [ ! -f "$ADMIN_FILE" ]; then
  ADMIN_PASSWORD="$(python3 - <<'PY'
import secrets
import string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(24)))
PY
)"
  printf '%s\n' "$ADMIN_PASSWORD" > "$ADMIN_FILE"
  chmod 600 "$ADMIN_FILE"
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  ADMIN_NAME="${ADMIN_NAME:-Administrator}"
  # The workspace domain, not the site's: staff sign in as <username>@holbox.com
  # and the SSO step matches on this address.
  ADMIN_EMAIL="${ADMIN_EMAIL:-$ADMIN_USERNAME@holbox.com}"
  runuser -u "$APP_USER" -- env COVEX_NEW_PASSWORD="$ADMIN_PASSWORD" \
    python3 "$APP_DIR/server.py" --db "$DB" --no-seed --add-user \
      --name "$ADMIN_NAME" --username "$ADMIN_USERNAME" \
      --email "$ADMIN_EMAIL" --admin
  echo "    Username: $ADMIN_USERNAME"
  echo "    Password: (stored in $ADMIN_FILE, mode 600)"
else
  echo "    Already done -- $ADMIN_FILE exists, so the accounts were left alone."
fi

echo "==> Installing the systemd unit"
install -m 0644 "$SCRIPT_DIR/covex-tracker.service" "$UNIT"
systemctl daemon-reload
systemctl enable covex-tracker

echo "==> Installing the nginx site"
if [ -f "$NGINX_SITE" ]; then
  # Leave it alone on a redeploy. After the first run this file belongs to
  # certbot -- it is what adds the TLS listener and the HTTP redirect -- so
  # re-installing the plain-HTTP template over it would quietly turn HTTPS
  # off while leaving the certificate in place. A change to the template
  # therefore needs one manual `install` on an existing box.
  echo "    already configured; leaving it alone (certbot owns it after the first run)"
else
  install -m 0644 "$SCRIPT_DIR/nginx-covex-tracker.conf" "$NGINX_SITE"
  sed -i "s/__DOMAIN__/$DOMAIN/g" "$NGINX_SITE"
  ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/covex-tracker
  rm -f /etc/nginx/sites-enabled/default
fi
nginx -t
systemctl reload nginx

echo "==> Starting the service"
systemctl restart covex-tracker
sleep 2
systemctl --no-pager --lines=20 status covex-tracker || true

echo "==> Requesting a certificate for $DOMAIN"
if [ "$ENABLE_TLS" = "yes" ]; then
  # --nginx rewrites the vhost to add the TLS listener and the HTTP redirect.
  certbot --nginx --non-interactive --agree-tos --redirect \
    --domains "$DOMAIN" --email "$EMAIL" || {
      echo "!! certbot failed. The site is up over plain HTTP; fix DNS and rerun:"
      echo "   certbot --nginx -d $DOMAIN -m $EMAIL --agree-tos --redirect"
    }
else
  echo "    Skipped (ENABLE_TLS=$ENABLE_TLS)."
fi

echo
echo "==> Done."
echo "    Local health check:  curl -sI http://127.0.0.1:$PORT/login | head -1"
echo "    Sign in at:          https://$DOMAIN/login"
echo "    First password:      sudo cat $ADMIN_FILE"
echo "    Service logs:        journalctl -u covex-tracker -f"
echo "    Backups:             see deploy/DEPLOY.md -- the database is $DB"
