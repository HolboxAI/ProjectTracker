#!/bin/bash
# Deploy ProjectTracker to EC2 using settings from .env
#
#   sh deploy/deploy_to_ec2.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env if present
if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # Parse key=value lines ignoring comments and blank lines
  while IFS='=' read -r key val || [ -n "$key" ]; do
    key=$(echo "$key" | tr -d '[:space:]')
    [[ "$key" =~ ^#.* ]] && continue
    [ -z "$key" ] && continue
    val=$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
    export "$key"="$val"
  done < <(grep -v '^[[:space:]]*#' "$ROOT_DIR/.env" | grep '=')
  set +a
fi

if [ -z "${EC2_HOST:-}" ]; then
  echo "Error: EC2_HOST is empty in .env" >&2
  echo "Please open .env and set EC2_HOST to your EC2 IP or hostname." >&2
  exit 1
fi

if [ -z "${EC2_KEY_PATH:-}" ]; then
  echo "Error: EC2_KEY_PATH is empty in .env" >&2
  echo "Please open .env and set EC2_KEY_PATH to the path of your .pem file." >&2
  exit 1
fi

# Expand tilde if present
KEY_PATH="${EC2_KEY_PATH/#\~/$HOME}"

if [ ! -f "$KEY_PATH" ]; then
  echo "Error: SSH private key not found at: $KEY_PATH" >&2
  exit 1
fi

chmod 400 "$KEY_PATH" 2>/dev/null || true

USER="${EC2_USER:-ubuntu}"
TARGET="${USER}@${EC2_HOST}"
DOMAIN="${DOMAIN:-tracker.holbox.ai}"

echo "==> Deploying ProjectTracker to $TARGET ($DOMAIN) using key: $KEY_PATH"
IDENTITY="$KEY_PATH" DOMAIN="$DOMAIN" sh "$SCRIPT_DIR/redeploy.sh" "$TARGET"
