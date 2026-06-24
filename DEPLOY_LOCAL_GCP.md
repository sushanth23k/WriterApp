# Deploying the backend to a GCP VM **with on-device STT/TTS** (hybrid engine)

This deploys the whole backend — **LiveKit + token server + agent** — to one Linux
Compute Engine VM, with **STT and TTS running on the VM itself** (no Deepgram). The LLM
still runs on Groq. This is the "Hybrid" engine in the app.

> **Why not MLX?** MLX is Apple-Silicon-only. GCP VMs are Linux/x86, so the agent uses
> **faster-whisper** (CTranslate2) for STT instead of `mlx_whisper`. `stt/__init__.py`
> auto-selects the backend by platform, so the same code runs on your Mac (MLX) and the
> VM (faster-whisper). TTS (Kokoro) is PyTorch and runs on both unchanged.

It builds on the cloud guide — read **[DEPLOY.md](DEPLOY.md)** first for VM provisioning,
`.deployenv`, Artifact Registry, and firewall. This file only covers the **deltas** for
on-device audio.

---

## What's different vs. the cloud deploy

| | Cloud deploy (DEPLOY.md) | This (local audio) |
|---|---|---|
| Image | `backend/Dockerfile` | **`backend/Dockerfile.local`** (faster-whisper + Kokoro + espeak-ng) |
| VM compose | `deploy/docker-compose.vm.yml` | **`deploy/docker-compose.vm-local.yml`** (mounts `/models`) |
| Model weights | none | **downloaded onto the VM disk**, mounted read-only into the agent |
| VM size | `e2-small` (2 GB) ok | **`e2-medium` (4 GB) minimum**, `e2-standard-2` (8 GB) comfortable |

Model weights are **never** committed to git or baked into the image (see `.gitignore`
→ `models/`). They live only on the VM disk.

---

## 0. Prerequisites

- `.deployenv` filled in (same as DEPLOY.md): `GCP_PROJECT`, `GCP_REGION`, `GCP_ZONE`,
  `VM_NAME`, `AR_REPO`, external IP.
- A VM created per **DEPLOY.md Part A**, but sized **`e2-medium` or larger**, with a
  **Debian** boot disk (not Container-Optimized OS — we need a writable disk path for the
  weights at `/var/dropnote/models`). 15–20 GB standard disk is plenty.
- Firewall: TCP `7880,7881,8080` + UDP `7882` open to your phone (DEPLOY.md covers this).

---

## 1. Build & push the **local** image

Same as the cloud build, but point `-f` at `Dockerfile.local`:

```bash
# load your deploy vars
set -a; . .deployenv; set +a
AR_HOST="${GCP_REGION}-docker.pkg.dev"
IMAGE="${AR_HOST}/${GCP_PROJECT}/${AR_REPO}/dropnote-backend-local:$(date +%Y%m%d-%H%M%S)"

gcloud auth configure-docker "$AR_HOST" -q
docker buildx build --platform linux/amd64 -f backend/Dockerfile.local -t "$IMAGE" --push backend/
echo "built $IMAGE"
```

(The `linux/amd64` platform is required — you're building a Linux image from your Mac.)

---

## 2. Download the model weights **onto the VM** (one-time)

The weights aren't in the image, so fetch them onto the VM's disk into
`/var/dropnote/models`. The simplest way uses the image you just pushed — it already has
`hf` + the download script — so no extra tooling on the VM:

```bash
# authenticate the VM's docker to Artifact Registry, then run the downloader once.
gcloud compute ssh "$VM_NAME" --zone "$GCP_ZONE" --command "
  set -e
  sudo mkdir -p /var/dropnote/models
  TOKEN=\$(curl -fsS -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token \
    | sed -n 's/.*\"access_token\":\"\([^\"]*\)\".*/\1/p')
  echo \"\$TOKEN\" | sudo docker login -u oauth2accesstoken --password-stdin https://${AR_HOST}
  sudo docker run --rm -e MODELS_DIR=/models -v /var/dropnote/models:/models \
    ${IMAGE} bash scripts/download_models.sh
"
```

On Linux the script auto-downloads the **faster-whisper** STT model + Kokoro (≈0.4 GB).
You only need to do this again if you change models. `du -sh /var/dropnote/models/*` to
confirm.

> Prefer no-SSH? You can instead append this `docker run` to `deploy/startup-script.sh`
> (guarded by `[ -e /var/dropnote/models/kokoro-82m ] || …`) so a fresh boot pulls the
> weights automatically. SSH-once is simpler for a first deploy.

---

## 3. Ship config + the **local** compose, then (re)start

Same metadata mechanism as DEPLOY.md, but point `compose-file` at the local variant and
pass the image:

```bash
# materialize .env + livekit.yaml the way deploy.sh does (or reuse its TMP files), then:
gcloud compute instances add-metadata "$VM_NAME" --zone "$GCP_ZONE" \
  --metadata "dropnote-image=${IMAGE}" \
  --metadata-from-file \
    "startup-script=deploy/startup-script.sh,dropnote-env=backend/.env,livekit-yaml=backend/livekit.yaml,compose-file=deploy/docker-compose.vm-local.yml"

gcloud compute instances reset "$VM_NAME" --zone "$GCP_ZONE"
```

The startup script materializes the config in a RAM dir and runs
`docker compose -f docker-compose.vm-local.yml up -d --pull always`. That compose mounts
`/var/dropnote/models → /models:ro`, and the image sets `MODELS_DIR=/models`,
`STT_BACKEND=faster`, `HF_HUB_OFFLINE=1` — so the agent loads weights locally and makes
**no network calls for STT/TTS**.

> Using `deploy.sh` instead of manual commands? Either edit lines 73 & 141 to use
> `Dockerfile.local` + `docker-compose.vm-local.yml`, or copy it to `deploy/deploy-local.sh`
> with those two swaps. The rest (Artifact Registry, IAM, metadata, reset) is identical.

---

## 4. Verify

```bash
# watch the boot/deploy log (no SSH needed)
gcloud compute instances get-serial-port-output "$VM_NAME" --zone "$GCP_ZONE" \
  | grep -i dropnote

# health check
curl -s http://<VM_EXTERNAL_IP>:8080/health     # -> {"status":"ok"}
```

Then in the app set [config.ts](app/src/config.ts) `TOKEN_SERVER_URL` to
`http://<VM_EXTERNAL_IP>:8080`, reload, and pick **Hybrid**. First utterance is slow
(models load lazily); subsequent turns are warm.

To check the agent built the right pipeline, look for this in the logs:

```
building HYBRID session (on-device audio, Groq LLM)
faster-whisper small.en loaded from /models/faster-whisper-small-en (cpu/int8) …
Kokoro-82M loaded from /models/kokoro-82m …
```

---

## Notes / gotchas

- **RAM**: faster-whisper int8 small (~0.6 GB) + Kokoro (~0.7 GB) + torch + the stack.
  `e2-medium` (4 GB) is the floor; `e2-standard-2` (8 GB) is comfortable.
- **CPU latency**: on 2 vCPUs, expect a few seconds per turn (STT + TTS). Fine for a
  personal app; bump vCPUs or move to a GPU VM (CUDA `faster-whisper` + a CUDA torch
  wheel) if you need it snappier.
- **Cloud still works on this image** — pick **Cloud** in the app and it uses
  Deepgram/Groq; the local models simply aren't loaded.
- Weights persist on the VM disk across reboots; only re-download if you change models.
