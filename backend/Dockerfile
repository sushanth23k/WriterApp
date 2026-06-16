# Shared image for the Voice Memory Assistant backend Python services
# (FastAPI token server + LiveKit Agents worker).
#
# docker-compose runs this image twice with a per-service `command:` (split
# services). The same image also runs BOTH services under supervisord via the
# default CMD — the optional "single combined image" alternative:
#   docker build -t vma-backend backend/
#   docker run --env-file backend/.env -p 8080:8080 vma-backend
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# sqlcipher3 compiles a C extension linking libsqlcipher (OpenSSL-backed).
# supervisor is for the combined-image alternative.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config libsqlcipher-dev libssl-dev ca-certificates \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Pre-cache plugin model files (Silero VAD, etc.) so the worker starts without a
# first-run download. Tolerate failure so the build still succeeds offline.
RUN python agent.py download-files || true

# Default = combined-image alternative: run BOTH services under supervisord.
# docker-compose overrides this with a per-service `command:`.
CMD ["supervisord", "-c", "/app/supervisord.conf"]
