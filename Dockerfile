FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml requirements.txt README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY unifi_hamina_live ./unifi_hamina_live
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8080
ENV HOST=0.0.0.0 PORT=8080

# Liveness: /api/health returns HTTP 200 as soon as the server is up (its JSON
# `ok` flips true after the first successful poll). No curl in slim, so use
# Python. Assumes the default PORT=8080; adjust if you override it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "unifi_hamina_live"]
