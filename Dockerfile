# Agent Rooms — one image, one origin, no vendor.
#
# Stage 1 compiles the room console to static files. Stage 2 runs the API and serves those
# files itself, so a deployment is a single container with a single port. That is what makes
# "anyone starts a room and invites someone over the internet" a deploy rather than a
# project: no second service, no CORS matrix, no split hostname.
#
# Nothing here is specific to a hosting provider. `fly.toml` is a convenience for one fast
# path; the same image runs on Railway, Render, a VPS with Docker, or a Raspberry Pi.

# ---------------------------------------------------------------- console
# Debian rather than Alpine deliberately. Next.js ships prebuilt native SWC binaries, and
# the musl variants are the ones that historically go wrong; this stage is discarded after
# the build, so the extra size costs nothing at runtime and removes a class of failure that
# would only show up on a host we cannot reproduce locally.
FROM node:22-bookworm-slim AS console

WORKDIR /console
COPY frontend/package.json frontend/package-lock.json ./
# `npm ci` rather than `install`: the lockfile is the build input, and a deploy that
# silently resolves a different dependency tree than the one tested is not reproducible.
RUN npm ci

COPY frontend/ ./
# Empty means "same origin as the page". The API and the console are served together, so
# the console must not bake in an absolute hostname — that would break the moment the
# instance moves, which is precisely the failure this milestone exists to end.
ENV NEXT_PUBLIC_API_BASE=""
RUN npm run build

# ---------------------------------------------------------------- server
FROM python:3.12-slim AS server

# PYTHONUNBUFFERED so container logs appear as they happen rather than on flush; a deploy
# you cannot watch is a deploy you cannot diagnose.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=console /console/out ./console

# Defaults chosen so the image is correct on any host that sets PORT and a volume:
#   * bind every interface, because a container's loopback is unreachable from outside;
#   * keep state on the mounted volume, not in the layer, so a redeploy does not discard
#     rooms — the event log is the source of truth and must outlive the process;
#   * the console lives beside the app in the image.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    DATABASE_PATH=/data/agent_rooms.db \
    CONSOLE_DIR=/srv/console

# Present so a `docker run` with no volume still starts. A real deployment mounts over it —
# see docs/DEPLOY.md, which explains why an unmounted /data is a data-loss trap.
RUN mkdir -p /data

# Not set here on purpose: PUBLIC_BASE_URL, OPERATOR_TOKEN, MCP_REQUIRE_AUTH. Each is
# deployment-specific, and the startup guard in app/config.py refuses to boot a publicly
# reachable instance that left them at their local defaults. Baking a value in would defeat
# a check whose whole job is to be un-defeatable.

EXPOSE 8080

# `sh -c` so ${PORT} expands: hosting platforms assign it, and a hardcoded port means the
# platform's health check hits nothing.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
