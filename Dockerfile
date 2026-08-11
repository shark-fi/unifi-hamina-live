FROM python:3.12-slim

# Stamped by CI so a running container can say which commit it is. Every
# "the fix didn't work" report here has started with a container that predated
# the fix, and answering that took an exec and a guess. Now it takes a curl.
ARG GIT_SHA=unknown
ARG BUILT_AT=unknown
ENV BUILD_SHA=$GIT_SHA BUILD_TIME=$BUILT_AT

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml requirements.txt README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY unifi_hamina_live ./unifi_hamina_live
RUN pip install --no-cache-dir --no-deps -e .

# The companion OpenIntent exporter, baked in rather than mounted.
#
# It used to be a read-only mount of a sibling checkout, which had two costs. A
# fresh `docker compose up` left the refresh silently inert — it reported
# "exporter not found" on a status endpoint nobody thinks to read — and the two
# repos could drift, which they did: a change here that stopped passing
# --password needed an exporter new enough to read UNIFI_PASSWORD, and a stale
# checkout would have prompted into the void.
#
# Pinned to a commit, not a branch, so an image is reproducible and the exporter
# version is a deliberate bump rather than whatever main happened to be at build
# time. The exporter is a single stdlib-only file, so this costs one layer and
# no dependencies. Mount over /opt/exporter to develop against a local copy.
ARG EXPORTER_REF=511471a597f8839c4cfeb77a89e8da144d71361e
ADD https://raw.githubusercontent.com/shark-fi/unifi-hamina-export/${EXPORTER_REF}/unifi_export.py \
    /opt/exporter/unifi_export.py
RUN chmod 0444 /opt/exporter/unifi_export.py

EXPOSE 8080
ENV HOST=0.0.0.0 PORT=8080

# Liveness: /api/health returns HTTP 200 as soon as the server is up (its JSON
# `ok` flips true after the first successful poll). No curl in slim, so use
# Python. Assumes the default PORT=8080; adjust if you override it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "unifi_hamina_live"]
