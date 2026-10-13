#!/usr/bin/env bash
# Compute Engine startup script for the FraudLens Airflow VM.
# Runs as root on every boot; idempotent.
set -euo pipefail

APP_DIR=/opt/fraudlens-airflow
MD=http://metadata.google.internal/computeMetadata/v1/instance/attributes
md() { curl -fsS -H "Metadata-Flavor: Google" "$MD/$1"; }

# Docker Engine + compose plugin (first boot only)
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

mkdir -p "$APP_DIR/dags"
md compose-file > "$APP_DIR/docker-compose.yml"
cat > "$APP_DIR/.env" <<EOF
GCS_BUCKET=$(md gcs-bucket)
GCP_PROJECT_ID=$(md gcp-project-id)
API_ENDPOINT=$(md api-endpoint)
BQ_DATASET=fraudlens
BQ_LOCATION=$(md bq-location)
EOF

cd "$APP_DIR"
docker compose pull --quiet
docker compose up -d
