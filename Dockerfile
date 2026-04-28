# Multi-stage build: keep node + gcc out of the runtime image, and order
# layers so a source-only edit doesn't bust the npm ci / pip install caches.
#
# We deliberately track the floating python:3.12-slim and node:22-alpine tags
# instead of pinning to a digest — pin discipline tends to drift and block
# security updates. Refresh with `docker pull` before a maintenance deploy.

# ---- Stage 1: frontend builder ----
FROM node:22-alpine AS frontend
WORKDIR /build
# Copy lockfiles first so npm ci is cached unless dependencies change.
COPY frontend/package*.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: python deps ----
# Build wheels in an isolated layer so source changes don't reinstall pip pkgs.
FROM python:3.12-slim AS pydeps
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Final stage ----
FROM python:3.12-slim
WORKDIR /app

# Pull in the pip-installed packages from the deps stage. No gcc/node in
# the runtime image — significantly smaller than the previous single-stage
# build that left the build toolchain behind.
COPY --from=pydeps /install /usr/local

# App source. Listed explicitly so a stray directory (docs/, repo/, .git/,
# uploaded images, dev DBs) can't end up baked into an immutable image.
COPY app/ ./app/
COPY static/ ./static/
COPY README.md ./
# Frontend bundle from the node builder stage.
COPY --from=frontend /build/dist ./frontend/dist

RUN groupadd --system --gid 10001 vigilant \
 && useradd  --system --uid 10001 --gid vigilant --no-create-home vigilant \
 && mkdir -p /data \
 && chown -R vigilant:vigilant /app /data

ENV DATABASE_URL=sqlite+aiosqlite:////data/vigilant.db

USER vigilant

# Liveness probe. Uses urllib so we don't have to apt-install curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3); sys.exit(0 if r.status==200 else 1)" || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
