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

# Copy application code
COPY app.py auth.py database.py email_client.py \
    kanboard_client.py kms.py kms_setup.py models.py \
    odt_processor.py security.py settings_routes.py \
    inventory_routes.py gunicorn.conf.py requirements.txt ./

COPY template.odt ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY entrypoint.sh ./

# Create necessary directories
RUN mkdir -p signatures static/signatures /var/log/vms /etc/vms \
    && chmod +x entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
