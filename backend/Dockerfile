# Shared image for the DropNote backend Python services
# (FastAPI token server + LiveKit Agents worker).
#
# docker-compose runs this image twice with a per-service `command:` (split
# services). The same image also runs BOTH services under supervisord via the
# default CMD — the optional "single combined image" alternative:
#   docker build -t dropnote-backend backend/
#   docker run --env-file backend/.env -p 8080:8080 dropnote-backend
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Notes + auth use psycopg[binary] (prebuilt wheels — no C toolchain needed), so the
# image only needs CA certs (TLS to Postgres/AI APIs) and supervisor for the optional
# combined-image alternative.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates supervisor \
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
