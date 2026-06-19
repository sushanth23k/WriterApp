# Deploying the DropNote backend to a GCP VM (lowest cost)

This deploys the whole backend — **LiveKit SFU + token server + agent** — onto a **single
Compute Engine VM**. We use one VM (no load balancer, no managed services) because the
LiveKit SFU needs a **UDP** port for WebRTC media, which rules out Cloud Run (HTTP/one-TCP-
port only). Only the AI services (Deepgram, Groq) and your Postgres remain off-box.

> **Provisioning** is done in the Cloud Console UI (Part A). **Deployment** is fully
> gcloud-driven from your laptop (Part B): the image is built **locally**, pushed to
> Artifact Registry, and pulled by the VM — **no SSH, no GitHub, no building on the VM**.

---

## Cost: keep it cheap

| Lever | Choice | Why |
|---|---|---|
| Machine type | **`e2-small`** (2 vCPU burst, 2 GB) | Safe floor for LiveKit + agent + Silero VAD. `e2-micro` (1 GB) is *free-tier* eligible but RAM-tight — try it only if you want $0 and accept instability. |
| Provisioning model | **Spot** (optional) | ~60–80% cheaper; GCP can preempt it. Fine for personal use. |
| Region | A **free-tier** region: `us-west1`, `us-central1`, or `us-east1` | `e2-micro` free-tier discount + low egress. |
| Disk | 10–15 GB **standard** (not SSD) | Tiny image; standard pd is cheapest. |
| External IP | **Ephemeral** (don't reserve a static IP) | A *reserved* static IP is billed **while the VM is stopped**; ephemeral is free while running. |
| When idle | **Stop the VM** | You pay only for the disk while stopped. |

Rough cost: an `e2-small` on-demand is ~**$12–13/mo** if left running 24/7; Spot or
stop-when-idle drops that substantially. `e2-micro` in a free-tier region can be ~**$0**.

---

## Part A — Provision in the Cloud Console (UI)

### A1. Create the VM
1. Console → **Compute Engine → VM instances → Create instance**.
2. **Name**: `dropnote`. **Region**: pick a free-tier region (e.g. `us-central1`), any zone.
3. **Machine configuration**: series **E2**, machine type **`e2-small`**.
4. (Optional, to save money) expand **Advanced options → Management** (or the
   "Availability policies" panel) and set **VM provisioning model → Spot**.
5. **Boot disk**: Change → **Standard persistent disk**, **15 GB**, and for the image pick
   either **Debian 12 (bookworm)** *or* **Container-Optimized OS** (Public images →
   Operating system: *Container Optimized OS*). COS has Docker preinstalled, so the VM skips
   the one-time Docker install (recommended).
6. **Firewall** (the checkboxes on this page): leave **Allow HTTP/HTTPS off** — we open
   custom ports separately in A2.
7. **Networking → Network tags**: add the tag **`dropnote`** (the firewall rule in A2
   targets this tag).
8. Click **Create**. Note the VM's **External IP** once it boots.

#### A1 (alternative). Create the VM as Container-Optimized OS via gcloud
Prefer the CLI? This one command creates a zero-install (Docker preinstalled) COS VM with
the network tag and the OAuth scope the VM needs to pull from Artifact Registry. Replace the
`$…` with your `.deployenv` values (Spot lines optional, for the cheapest price):
```bash
gcloud compute instances create "$VM_NAME" \
  --project="$GCP_PROJECT" --zone="$GCP_ZONE" \
  --machine-type=e2-small \
  --image-family=cos-stable --image-project=cos-cloud \
  --boot-disk-size=15GB --boot-disk-type=pd-standard \
  --tags="$VM_NETWORK_TAG" \
  --scopes=cloud-platform \
  --provisioning-model=SPOT --instance-termination-action=STOP
```
> `--scopes=cloud-platform` lets the VM's service account authenticate Docker to Artifact
> Registry (combined with the `artifactregistry.reader` role `deploy.sh` grants). Drop the
> last line for a standard (non-preemptible) VM.

### A2. Open the firewall ports
1. Console → **VPC network → Firewall → Create firewall rule**.
2. **Name**: `dropnote-ports`. **Direction**: Ingress. **Action**: Allow.
3. **Targets**: *Specified target tags* → tag **`dropnote`** (matches A1.7).
4. **Source IPv4 ranges**: `0.0.0.0/0` (or, more securely, just your home IP `x.x.x.x/32`).
5. **Protocols and ports** → *Specified protocols and ports*:
   - **TCP**: `7880,7881,8080`  (LiveKit signaling, LiveKit TCP fallback, token server)
   - **UDP**: `7882`            (**WebRTC media** — the reason this can't be Cloud Run)
6. **Create**.

Or via gcloud:
```bash
gcloud compute firewall-rules create dropnote-ports \
  --project="$GCP_PROJECT" --direction=INGRESS --action=ALLOW \
  --target-tags="$VM_NETWORK_TAG" --source-ranges=0.0.0.0/0 \
  --rules=tcp:7880,tcp:7881,tcp:8080,udp:7882
```

### A3. (Optional) Static IP / DNS
Only if you want a stable address: **VPC network → IP addresses → Reserve external static
address**, attach it to the VM. ⚠️ Remember a reserved static IP **bills while the VM is
stopped** — skip it for pure cost-minimization.

---

## Part B — Build locally, deploy via gcloud (no SSH, no GitHub, no build on the VM)

The image is built **on your laptop** from local files and pushed to **Artifact Registry**;
the VM just **pulls and runs** it. `gcloud` ships the run-config to the VM as **instance
metadata** and `reset`s it — we never SSH in. The flow:

```
laptop:  docker buildx (linux/amd64)  ──push──►  Artifact Registry
gcloud:  startup-script + .env + livekit.yaml + compose  ──metadata──►  VM, then reset
VM:      docker login (its service account)  ──pull──►  docker compose up   (NO build)
laptop:  poll http://VM:8080/health until it's up
```

| File | Role |
|---|---|
| `.deployenv` (repo root, gitignored) | VM identity/location: project, **region**, zone, `VM_NAME`, **external IP**, **private/internal IP**, `AR_REPO`. Copy from [`.deployenv.example`](.deployenv.example). |
| `backend/.env` (gitignored) | The app **secrets** — shipped to the VM as metadata at deploy time. |
| [`deploy/docker-compose.vm.yml`](deploy/docker-compose.vm.yml) | The compose the VM runs. Uses `image: ${DROPNOTE_IMAGE}` (the prebuilt image) — **no `build:`**. |
| [`deploy/startup-script.sh`](deploy/startup-script.sh) | Runs **on the VM**: ensures Docker (one-time), writes config from metadata, `docker login` to Artifact Registry via the VM service account, `docker compose up --pull always`. |
| [`deploy/deploy.sh`](deploy/deploy.sh) | Runs **on your laptop**: builds + pushes the image, ships metadata, resets the VM, and **waits** for `/health`. |

### B0. What we expect from GCP
- A project with **billing on**. `deploy.sh` enables the **Artifact Registry API** and
  creates the `AR_REPO` repo if missing.
- The VM from Part A (network **tag** + **firewall** TCP 7880/7881/8080 + UDP 7882) with an
  attached **service account** (the default Compute SA is fine) — `deploy.sh` grants it
  `roles/artifactregistry.reader` so the VM can pull the image.
- Local **Docker Desktop (with buildx)** and **gcloud** authenticated:
  ```bash
  gcloud auth login
  gcloud config set project <YOUR_PROJECT_ID>
  ```
- Your account with **roles/artifactregistry.admin** (push + create repo) and
  **roles/compute.instanceAdmin.v1** (set metadata + reset the VM).

### B1. Fill in the config
```bash
cp .deployenv.example .deployenv      # edit: project, region, zone, VM_NAME, VM_EXTERNAL_IP,
                                      # VM_INTERNAL_IP, AR_REPO
cp backend/.env.example backend/.env  # fill in real secrets (DATABASE_URL, GROQ, DEEPGRAM,
                                      # AUTH_JWT_SECRET, ADMIN_SECRET…)
```
> **`.env` format:** write `KEY=value` with **no spaces around `=` and no surrounding
> quotes**. `deploy.sh` normalizes these before shipping (so a stray `DATABASE_URL = "…"`
> won't break the VM), but keeping the file clean avoids surprises.
Read the VM's external + internal (private) IPs from the Console, or:
```bash
gcloud compute instances describe "$VM_NAME" --zone "$GCP_ZONE" \
  --format='value(networkInterfaces[0].accessConfigs[0].natIP, networkInterfaces[0].networkIP)'
```
> `deploy.sh` sets the app-facing `LIVEKIT_URL` to `ws://VM_EXTERNAL_IP:7880` when it ships
> `.env` — you don't hardcode it. Notes live in Postgres (the `writer_app` schema), so the
> VM keeps **no** persistent state of its own.

### B2. Deploy
```bash
./deploy/deploy.sh
```
It builds the image locally (linux/amd64), pushes it to Artifact Registry, ships the config
as metadata, resets the VM, and **blocks until `/health` is green** (~1–3 min — the VM only
*pulls*, it never builds). On timeout it prints the VM's serial-console deploy log.

> **Secrets note:** they travel as instance metadata (visible to anyone with
> `compute.instances.get` on the project) and are materialized only into a **RAM-backed
> tmpfs** (`/run/dropnote`) on the VM — never written to its persistent disk, and gone on
> shutdown; the startup script re-fetches them from metadata on every boot. `livekit.yaml`
> is generated the same way (`use_external_ip: true` so LiveKit finds the VM's public IP).
> For stricter handling, switch to **Secret Manager**.

### B3. Create your login (no sign-up UI)
```bash
curl -X POST http://VM_EXTERNAL_IP:8080/users \
  -H "X-Admin-Token: <ADMIN_SECRET from backend/.env>" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"a-strong-password"}'
```

### Redeploy later
Just re-run `./deploy/deploy.sh` — it rebuilds locally, pushes a fresh image tag, and the VM
pulls it on reset. No SSH, no GitHub.

> **Zero-install alternative:** to avoid the one-time Docker install on the VM entirely, use
> a **Container-Optimized OS** boot image in Part A (Docker is preinstalled and already
> configured to authenticate to Artifact Registry). The same `deploy.sh` works.

---

## Point the app at the VM
In `app/src/config.ts`, set `TOKEN_SERVER_URL` to your VM (the IP `deploy.sh` prints at the
end of a successful run):
```ts
export const TOKEN_SERVER_URL = 'http://VM_EXTERNAL_IP:8080';
```

> **Ephemeral-IP gotcha — "notes load but no speaking/listening":** the VM's external IP is
> **ephemeral** and changes on stop/start. Two places depend on it: the app's
> `TOKEN_SERVER_URL` (above) and the server's `LIVEKIT_URL` (the LiveKit address the app
> connects to for voice). `deploy.sh` now **auto-resolves the VM's live IP from GCP** and
> bakes it into `LIVEKIT_URL` + the health poll, so a stale `.deployenv` no longer breaks
> voice or hangs the deploy — but you still must update `TOKEN_SERVER_URL` in `config.ts`
> and rebuild the app whenever the IP changes. If notes work (REST) but voice is silent,
> the IP drifted: re-run `deploy.sh` and update `config.ts` to the IP it prints. To stop
> chasing IPs entirely, **reserve a static IP** (A3) or put a domain in front.

**iOS ATS note:** iOS blocks plain `http://`/`ws://` to a bare IP by default. DropNote ships
an Expo config plugin ([`app/plugins/withAtsArbitraryLoads.js`](app/plugins/withAtsArbitraryLoads.js))
that sets ATS to exactly `{ NSAllowsArbitraryLoads: true }`. **Gotcha:** just adding
`NSAllowsArbitraryLoads` is NOT enough — iOS *ignores* it when `NSAllowsLocalNetworking`
(which Expo's prebuild adds by default) is also present, so the plugin overwrites the whole
ATS dict to drop that key. The clean alternative for non-personal use is **Caddy/Nginx with
HTTPS** (a domain + Let's Encrypt) in front of the token server and LiveKit, then `https://`
/`wss://`.

## Day-to-day
- **Stop to save money:** Console → VM instances → ⋮ → **Stop** (you pay only for the disk).
- **Update code / secrets:** edit locally (and/or `backend/.env`), then re-run
  `./deploy/deploy.sh` — it rebuilds + pushes a new image and the VM pulls it. No SSH.
- **Deploy logs (no SSH):**
  `gcloud compute instances get-serial-port-output "$VM_NAME" --zone "$GCP_ZONE" | grep -i dropnote`
