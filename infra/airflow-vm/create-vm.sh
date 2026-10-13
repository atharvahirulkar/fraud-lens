#!/usr/bin/env bash
# Create the self-hosted Airflow VM on Compute Engine.
# Run after infra/setup-gcp.sh (needs the bucket, the fraudlens-airflow SA,
# and the Cloud Run service URL).
#
# Usage:
#   bash infra/airflow-vm/create-vm.sh
#
# Cost: e2-medium ≈ $25/month while running. Stop it when you're not demoing:
#   gcloud compute instances stop fraudlens-airflow --zone us-central1-a

set -euo pipefail

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-${REGION}-a}"
VM="fraudlens-airflow"
BUCKET="fraudlens-artifacts-${PROJECT_ID}"
AIRFLOW_SA="fraudlens-airflow@${PROJECT_ID}.iam.gserviceaccount.com"
HERE="$(cd "$(dirname "$0")" && pwd)"

API_ENDPOINT=$(gcloud run services describe fraudlens-api --region "$REGION" --format 'value(status.url)')
[ -n "$API_ENDPOINT" ] || { echo "Cloud Run service not found — run infra/setup-gcp.sh first"; exit 1; }

echo "→ Firewall: allow SSH only from Google IAP (35.235.240.0/20)"
gcloud compute firewall-rules describe allow-iap-ssh >/dev/null 2>&1 \
  && echo "  (already exists)" \
  || gcloud compute firewall-rules create allow-iap-ssh \
       --network default \
       --direction INGRESS \
       --action allow \
       --rules tcp:22 \
       --source-ranges 35.235.240.0/20 \
       --target-tags airflow

echo ""
echo "→ VM: $VM ($ZONE)"
if gcloud compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1; then
  echo "  (already exists) — pushing updated metadata and re-running startup script"
  gcloud compute instances add-metadata "$VM" --zone "$ZONE" \
    --metadata "gcs-bucket=${BUCKET},gcp-project-id=${PROJECT_ID},api-endpoint=${API_ENDPOINT},bq-location=${REGION}" \
    --metadata-from-file "startup-script=${HERE}/startup.sh,compose-file=${HERE}/docker-compose.yml"
  gcloud compute ssh "$VM" --zone "$ZONE" --tunnel-through-iap \
    --command "sudo google_metadata_script_runner startup"
else
  gcloud compute instances create "$VM" \
    --zone "$ZONE" \
    --machine-type e2-medium \
    --image-family debian-12 \
    --image-project debian-cloud \
    --boot-disk-size 20GB \
    --boot-disk-type pd-balanced \
    --service-account "$AIRFLOW_SA" \
    --scopes cloud-platform \
    --tags airflow \
    --shielded-secure-boot \
    --metadata "gcs-bucket=${BUCKET},gcp-project-id=${PROJECT_ID},api-endpoint=${API_ENDPOINT},bq-location=${REGION}" \
    --metadata-from-file "startup-script=${HERE}/startup.sh,compose-file=${HERE}/docker-compose.yml"
fi

cat <<EOF

======================================================
 Airflow VM ready (first boot installs Docker: ~3-4 min)
======================================================

 Open the Airflow UI through an IAP tunnel:
   gcloud compute ssh $VM --zone $ZONE --tunnel-through-iap -- -N -L 8080:localhost:8080
   → http://localhost:8080   user: admin

 Admin password:
   gcloud compute ssh $VM --zone $ZONE --tunnel-through-iap \\
     --command "sudo docker exec airflow cat /opt/airflow/standalone_admin_password.txt"

 Stop / start to save credits:
   gcloud compute instances stop  $VM --zone $ZONE
   gcloud compute instances start $VM --zone $ZONE
======================================================
EOF
