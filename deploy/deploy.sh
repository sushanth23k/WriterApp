#!/usr/bin/env bash
# DropNote — build the backend Docker image LOCALLY and deploy it to a GCP VM using the
# gcloud CLI only. No interactive SSH, no GitHub on the VM, and NO build on the VM:
#
#   laptop:  docker buildx build (linux/amd64)  ->  push to Artifact Registry
#   gcloud:  ship startup-script + config as instance metadata  ->  reset the VM
#   VM:      pull the prebuilt image  ->  docker compose up
#   laptop:  wait until http://VM:8080/health is OK (or dump the serial log)
#
# Prereqs (see .deployenv.example):
#   - .deployenv filled in (project, region, zone, VM_NAME, external IP, AR_REPO)
#   - backend/.env filled in (the app secrets shipped to the VM)
#   - local Docker with buildx (Docker Desktop) + gcloud authenticated (`gcloud auth login`)
#   - the VM already created with the network tag + firewall (DEPLOY.md Part A)
#
# Usage:  ./deploy/deploy.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -f .deployenv ]    || { echo "Missing .deployenv (copy .deployenv.example)"; exit 1; }
[ -f backend/.env ]  || { echo "Missing backend/.env (the app secrets)"; exit 1; }

set -a; source .deployenv; set +a

: "${GCP_PROJECT:?set GCP_PROJECT in .deployenv}"
: "${GCP_REGION:?set GCP_REGION in .deployenv}"
: "${GCP_ZONE:?set GCP_ZONE in .deployenv}"
: "${VM_NAME:?set VM_NAME in .deployenv}"
: "${VM_EXTERNAL_IP:?set VM_EXTERNAL_IP in .deployenv (the VM public IP)}"
: "${AR_REPO:?set AR_REPO in .deployenv (Artifact Registry repo name)}"

AR_HOST="${GCP_REGION}-docker.pkg.dev"
IMAGE_NAME="${AR_HOST}/${GCP_PROJECT}/${AR_REPO}/dropnote-backend"
TAG="$(date +%Y%m%d%H%M%S)"               # immutable tag => the VM always pulls fresh
IMAGE="${IMAGE_NAME}:${TAG}"

echo "→ Ensuring Artifact Registry repo '${AR_REPO}' in ${GCP_REGION}…"
gcloud services enable artifactregistry.googleapis.com --project "$GCP_PROJECT" -q
gcloud artifacts repositories describe "$AR_REPO" \
  --location "$GCP_REGION" --project "$GCP_PROJECT" >/dev/null 2>&1 || \
gcloud artifacts repositories create "$AR_REPO" \
  --repository-format=docker --location "$GCP_REGION" --project "$GCP_PROJECT"

echo "→ Granting the VM's service account read access to Artifact Registry…"
SA="$(gcloud compute instances describe "$VM_NAME" \
  --zone "$GCP_ZONE" --project "$GCP_PROJECT" \
  --format='value(serviceAccounts[0].email)')"
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --location "$GCP_REGION" --project "$GCP_PROJECT" \
  --member "serviceAccount:${SA}" --role roles/artifactregistry.reader >/dev/null

echo "→ Building the image LOCALLY (linux/amd64) and pushing to ${IMAGE}…"
gcloud auth configure-docker "$AR_HOST" -q
find backend -name '._*' -type f -delete 2>/dev/null || true
# buildx targets amd64 explicitly — your Mac is arm64, but the GCE VM is amd64.
docker buildx build --platform linux/amd64 -t "$IMAGE" --push backend/

# --- Config shipped to the VM as metadata (small text; never in the repo/image) ---
TMP_ENV="$(mktemp)"; TMP_LK="$(mktemp)"
trap 'rm -f "$TMP_ENV" "$TMP_LK"' EXIT

# backend/.env, but with the app-facing LIVEKIT_URL forced to the VM's PUBLIC IP and the
# notes DB pointed at the persistent volume.
grep -vE '^[[:space:]]*(LIVEKIT_URL|NOTES_DB_PATH)=' backend/.env > "$TMP_ENV" || true
{
  echo "LIVEKIT_URL=ws://${VM_EXTERNAL_IP}:7880"
  echo "NOTES_DB_PATH=/app/data/notes.db"
} >> "$TMP_ENV"

# LiveKit server config (key/secret must match backend/.env; the cloud VM discovers its
# public IP). Read the two values robustly — tolerant of leading whitespace and CRLF line
# endings — instead of `source`-ing, and fail with a clear message if either is missing.
read_env() { sed -nE "s/^[[:space:]]*$1=//p" backend/.env | tail -n1 | tr -d '\r'; }
LIVEKIT_API_KEY="$(read_env LIVEKIT_API_KEY)"
LIVEKIT_API_SECRET="$(read_env LIVEKIT_API_SECRET)"
[ -n "$LIVEKIT_API_KEY" ]    || { echo "ERROR: LIVEKIT_API_KEY not found in backend/.env"; exit 1; }
[ -n "$LIVEKIT_API_SECRET" ] || { echo "ERROR: LIVEKIT_API_SECRET not found in backend/.env"; exit 1; }
cat > "$TMP_LK" <<YAML
port: 7880
rtc:
  tcp_port: 7881
  udp_port: 7882
  use_external_ip: true
logging:
  level: info
keys:
  ${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}
YAML

echo "→ Pushing startup-script + config to ${VM_NAME} (${GCP_ZONE})…"
gcloud compute instances add-metadata "$VM_NAME" \
  --project "$GCP_PROJECT" --zone "$GCP_ZONE" \
  --metadata "dropnote-image=${IMAGE}" \
  --metadata-from-file "startup-script=deploy/startup-script.sh,dropnote-env=${TMP_ENV},livekit-yaml=${TMP_LK},compose-file=deploy/docker-compose.vm.yml"

echo "→ Resetting ${VM_NAME} so it pulls + runs the new image…"
gcloud compute instances reset "$VM_NAME" --project "$GCP_PROJECT" --zone "$GCP_ZONE"

echo "→ Waiting for the VM to come up (image is already built + pushed)…"
HEALTH="http://${VM_EXTERNAL_IP}:8080/health"
ok=""
for _ in $(seq 1 60); do                  # up to ~5 min
  if curl -fsS "$HEALTH" >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done

if [ -n "$ok" ]; then
  echo "✅ Deployed. ${HEALTH} is healthy."
  cat <<EOF

Create your first login (no UI):
  curl -X POST http://${VM_EXTERNAL_IP}:8080/users \\
    -H "X-Admin-Token: <ADMIN_SECRET from backend/.env>" \\
    -H 'Content-Type: application/json' \\
    -d '{"email":"you@example.com","password":"a-strong-password"}'
EOF
else
  echo "⚠️  Not healthy after ~5 min. Recent VM deploy log (serial console, no SSH):"
  gcloud compute instances get-serial-port-output "$VM_NAME" \
    --project "$GCP_PROJECT" --zone "$GCP_ZONE" 2>/dev/null | grep -i dropnote | tail -40 || true
  exit 1
fi
