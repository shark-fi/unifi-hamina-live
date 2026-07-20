FROM python:3.12-slim

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml requirements.txt README.md ./
RUN pip install --no-cache-dir -r requirements.txt

COPY unifi_hamina_live ./unifi_hamina_live
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8080
ENV HOST=0.0.0.0 PORT=8080

CMD ["python", "-m", "unifi_hamina_live"]
