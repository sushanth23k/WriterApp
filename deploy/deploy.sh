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
: "${AR_REPO:?set AR_REPO in .deployenv (Artifact Registry repo name)}"

# Resolve the VM's CURRENT external IP straight from GCP. Ephemeral IPs change across
# stop/start, and a stale VM_EXTERNAL_IP in .deployenv silently breaks BOTH the app-facing
# LIVEKIT_URL (so voice can't connect) and the health poll below (so deploy hangs). We
# always trust the live IP; the .deployenv value is only a last-resort fallback.
echo "→ Resolving ${VM_NAME}'s current external IP from GCP…"
RESOLVED_IP="$(gcloud compute instances describe "$VM_NAME" \
  --zone "$GCP_ZONE" --project "$GCP_PROJECT" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true)"
VM_IP="${RESOLVED_IP:-${VM_EXTERNAL_IP:-}}"
: "${VM_IP:?could not determine the VM external IP (gcloud describe failed AND VM_EXTERNAL_IP is unset)}"
if [ -n "$RESOLVED_IP" ] && [ "$RESOLVED_IP" != "${VM_EXTERNAL_IP:-}" ]; then
  echo "  ⚠ live IP ${RESOLVED_IP} differs from .deployenv VM_EXTERNAL_IP=${VM_EXTERNAL_IP:-<unset>} — using the live IP."
  echo "    (update VM_EXTERNAL_IP in .deployenv, and TOKEN_SERVER_URL in app/src/config.ts, to http://${RESOLVED_IP}:8080)"
fi
echo "  using external IP: ${VM_IP}"

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
TMP_ENV="$(mktemp)"; TMP_NORM="$(mktemp)"; TMP_LK="$(mktemp)"
trap 'rm -f "$TMP_ENV" "$TMP_NORM" "$TMP_LK"' EXIT

# Normalize backend/.env into strict `KEY=value` lines so Docker Compose's env_file
# parser (which, unlike python-dotenv, does NOT tolerate `KEY = value` or surrounding
# quotes) reads every value correctly on the VM. This was the deploy-breaker: a line
# like `DATABASE_URL = "postgres://…"` arrived at the container as an unset/garbled
# value. We trim whitespace around `=` and strip one layer of matching quotes, drop
# comments/blanks, and drop now-dead vars (LIVEKIT_URL is re-added below; SQLCipher and
# NOTES_DB_PATH are gone since notes moved to Postgres).
awk '
  /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
  {
    eq = index($0, "=")
    if (eq == 0) next
    key = substr($0, 1, eq-1)
    val = substr($0, eq+1)
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
    gsub(/\r$/, "", val)
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
    # strip one layer of matching surrounding quotes
    if (length(val) >= 2) {
      f = substr(val, 1, 1); l = substr(val, length(val), 1)
      if (f == l && (f == "\"" || f == "\x27")) val = substr(val, 2, length(val)-2)
    }
    if (key == "LIVEKIT_URL" || key == "NOTES_DB_PATH" || key == "SQLCIPHER_KEY") next
    print key "=" val
  }
' backend/.env > "$TMP_NORM"

# Fail early (on the laptop) with a clear message if any must-have secret is missing,
# rather than shipping a broken config and debugging a dead VM.
get_env() { sed -nE "s/^$1=//p" "$TMP_NORM" | tail -n1; }
missing=""
for v in DATABASE_URL AUTH_JWT_SECRET LIVEKIT_API_KEY LIVEKIT_API_SECRET DEEPGRAM_API_KEY GROQ_API_KEY; do
  [ -n "$(get_env "$v")" ] || missing="${missing} ${v}"
done
[ -z "$missing" ] || { echo "ERROR: backend/.env is missing required var(s):${missing}"; exit 1; }

# Final env shipped to the VM: the normalized file, with LIVEKIT_URL forced to the VM's
# PUBLIC IP (the address the phone/app connects to).
cp "$TMP_NORM" "$TMP_ENV"
echo "LIVEKIT_URL=ws://${VM_IP}:7880" >> "$TMP_ENV"

# LiveKit server config (key/secret must match backend/.env; the cloud VM discovers its
# public IP). Read the two values from the NORMALIZED env so quotes/whitespace are
# already handled.
LIVEKIT_API_KEY="$(get_env LIVEKIT_API_KEY)"
LIVEKIT_API_SECRET="$(get_env LIVEKIT_API_SECRET)"
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
HEALTH="http://${VM_IP}:8080/health"
ok=""
for _ in $(seq 1 60); do                  # up to ~5 min
  if curl -fsS "$HEALTH" >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done

if [ -n "$ok" ]; then
  echo "✅ Deployed. ${HEALTH} is healthy."
  cat <<EOF

Point the app at this VM — set in app/src/config.ts:
  export const TOKEN_SERVER_URL = 'http://${VM_IP}:8080';
(The token server hands the app LIVEKIT_URL=ws://${VM_IP}:7880, so voice uses this same
 IP. If you see notes load but no speaking/listening, this IP is the thing to check.)

Create your first login (no UI):
  curl -X POST http://${VM_IP}:8080/users \\
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
