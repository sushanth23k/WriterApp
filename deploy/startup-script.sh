#!/usr/bin/env bash
# DropNote VM startup script — runs on the VM (as root) on each boot/reset.
#
# Delivered to the VM by deploy/deploy.sh via instance metadata (key: startup-script),
# so deployment is 100% gcloud-driven — no interactive SSH, no GitHub, no build on the VM.
# On each run it: ensures Docker is present (one-time install on the FIRST boot only),
# materializes the config from metadata, authenticates Docker to Artifact Registry using
# the VM's own service account, and `docker compose up` — PULLING the prebuilt image that
# was built on your laptop and pushed to Artifact Registry.
set -euo pipefail

LOG=/var/log/dropnote-deploy.log
# Tee to both the log file and stdout so output also reaches the VM serial console —
# readable with `gcloud compute instances get-serial-port-output` (no SSH needed).
exec > >(tee -a "$LOG") 2>&1
echo "===== dropnote startup $(date -u) ====="

META="http://metadata.google.internal/computeMetadata/v1/instance"
meta()  { curl -fsS -H "Metadata-Flavor: Google" "$META/attributes/$1" 2>/dev/null || true; }
token() { curl -fsS -H "Metadata-Flavor: Google" "$META/service-accounts/default/token"; }

APP_DIR=/opt/dropnote
DROPNOTE_IMAGE="$(meta dropnote-image)"
AR_HOST="${DROPNOTE_IMAGE%%/*}"   # e.g. us-central1-docker.pkg.dev
umask 077

# --- 1. Ensure Docker + compose plugin (ONE-TIME; the `if` makes reboots no-ops) ---
if ! command -v docker >/dev/null 2>&1; then
  echo "installing docker (first boot only)…"
  apt-get update -y
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

# --- 2. Materialize config from metadata (secrets never leave the VM) ---
mkdir -p "$APP_DIR"
meta dropnote-env  > "$APP_DIR/.env"
meta livekit-yaml  > "$APP_DIR/livekit.yaml"
meta compose-file  > "$APP_DIR/docker-compose.vm.yml"

# --- 3. Authenticate Docker to Artifact Registry via the VM service account ---
ACCESS_TOKEN="$(token | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
echo "$ACCESS_TOKEN" | docker login -u oauth2accesstoken --password-stdin "https://${AR_HOST}"

# --- 4. Pull the prebuilt image(s) and run — NO build happens on the VM ---
cd "$APP_DIR"
export DROPNOTE_IMAGE
docker compose -f docker-compose.vm.yml up -d --pull always --remove-orphans

echo "===== dropnote startup done $(date -u) ====="
