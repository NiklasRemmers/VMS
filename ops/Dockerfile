# ─── Build Stage ───
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─── Runtime Stage ───
FROM python:3.12-slim

# System deps (libreoffice-writer needed for ODT→PDF conversion)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# App directory
WORKDIR /app

# Copy application code (build context is the repo root: `docker build -f ops/Dockerfile .`)
COPY vms/ ./vms/
COPY migrations/ ./migrations/
COPY assets/ ./assets/
COPY templates/ ./templates/
COPY static/ ./static/
COPY ops/gunicorn.conf.py ops/entrypoint.sh requirements.txt ./

# Create necessary directories
RUN mkdir -p signatures static/signatures /var/log/vms /etc/vms \
    && chmod +x entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
