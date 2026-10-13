#!/usr/bin/env bash
# One-time GCP infrastructure bootstrap for FraudLens v2 — Cloud Run edition.
# Safe to re-run: every step skips resources that already exist.
#
# Prerequisites:
#   gcloud auth login
#   gcloud config set project <PROJECT_ID>
#   docker running (with buildx)
#
# Usage:
#   GITHUB_ORG=your-github-username GITHUB_REPO=fraudlens-v2 bash infra/setup-gcp.sh
#
# Optional:
#   OPENAI_API_KEY=sk-...   stored in Secret Manager (otherwise you're prompted)
#   REGION=us-central1      (default; cheapest region + free-tier eligible)

set -euo pipefail

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
[ -n "$PROJECT_ID" ] || { echo "Run: gcloud config set project <PROJECT_ID>"; exit 1; }
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
REGION="${REGION:-us-central1}"
AR_REPO="fraudlens"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/fraudlens-api"
BUCKET="fraudlens-artifacts-${PROJECT_ID}"
SERVICE="fraudlens-api"
SECRET="openai-api-key"
BQ_DATASET="fraudlens"
GITHUB_ORG="${GITHUB_ORG:?Set GITHUB_ORG to your GitHub username/org}"
GITHUB_REPO="${GITHUB_REPO:-fraudlens-v2}"

API_SA="fraudlens-api@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOYER_SA="fraudlens-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
AIRFLOW_SA="fraudlens-airflow@${PROJECT_ID}.iam.gserviceaccount.com"

WIF_POOL="github"
WIF_PROVIDER="github-oidc"

echo "======================================================"
echo " FraudLens GCP Bootstrap — Cloud Run"
echo " Project : $PROJECT_ID ($PROJECT_NUMBER)"
echo " Region  : $REGION"
echo " Bucket  : gs://$BUCKET"
echo "======================================================"

# ── 1. APIs ─────────────────────────────────────────────────────────────────
echo ""
echo "→ Enabling APIs (first run takes ~1 min)"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  bigquery.googleapis.com \
  compute.googleapis.com \
  storage.googleapis.com \
  iap.googleapis.com

# ── 2. Artifact Registry ────────────────────────────────────────────────────
echo ""
echo "→ Artifact Registry repo: $AR_REPO"
gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1 \
  && echo "  (already exists)" \
  || gcloud artifacts repositories create "$AR_REPO" \
       --repository-format docker \
       --location "$REGION" \
       --description "FraudLens container images"

# Keep only the 5 most recent images so storage stays inside the free 0.5 GB
gcloud artifacts repositories set-cleanup-policies "$AR_REPO" \
  --location "$REGION" \
  --policy=<(cat <<'JSON'
[
  {"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 5}},
  {"name": "delete-old",  "action": {"type": "Delete"}, "condition": {"tagState": "ANY"}}
]
JSON
) --no-dry-run >/dev/null

# ── 3. GCS bucket ───────────────────────────────────────────────────────────
echo ""
echo "→ GCS bucket: gs://$BUCKET"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 \
  && echo "  (already exists)" \
  || gcloud storage buckets create "gs://$BUCKET" \
       --location "$REGION" \
       --uniform-bucket-level-access \
       --public-access-prevention
gcloud storage buckets update "gs://$BUCKET" --versioning >/dev/null

echo "→ Uploading model artifacts + DAGs"
gcloud storage rsync -r mlruns "gs://$BUCKET/mlruns"
gcloud storage rsync -r dags "gs://$BUCKET/dags" --exclude '__pycache__/.*'

# ── 4. Service accounts ─────────────────────────────────────────────────────
create_sa() {
  local name=$1 display=$2
  gcloud iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    && echo "  $name (already exists)" \
    || gcloud iam service-accounts create "$name" --display-name "$display"
}
echo ""
echo "→ Service accounts"
create_sa fraudlens-api      "FraudLens Cloud Run runtime"
create_sa fraudlens-deployer "FraudLens GitHub Actions deployer"
create_sa fraudlens-airflow  "FraudLens Airflow VM"

# ── 5. Secret Manager ───────────────────────────────────────────────────────
echo ""
echo "→ Secret: $SECRET"
if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  if [ -z "${OPENAI_API_KEY:-}" ]; then
    read -r -s -p "  Paste your OpenAI API key (input hidden): " OPENAI_API_KEY; echo
  fi
  printf '%s' "$OPENAI_API_KEY" | gcloud secrets create "$SECRET" \
    --replication-policy automatic --data-file=-
else
  echo "  (already exists — rotate with: printf 'sk-...' | gcloud secrets versions add $SECRET --data-file=-)"
fi

# ── 6. IAM (least privilege) ────────────────────────────────────────────────
echo ""
echo "→ IAM bindings"
# Runtime SA: read only its own secret. Nothing else.
gcloud secrets add-iam-policy-binding "$SECRET" \
  --member "serviceAccount:$API_SA" --role roles/secretmanager.secretAccessor >/dev/null

# Deployer SA (GitHub Actions): push images, deploy Cloud Run, act as runtime SA,
# sync model artifacts + DAGs in the bucket.
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location "$REGION" \
  --member "serviceAccount:$DEPLOYER_SA" --role roles/artifactregistry.writer >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$DEPLOYER_SA" --role roles/run.developer --condition=None >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$API_SA" \
  --member "serviceAccount:$DEPLOYER_SA" --role roles/iam.serviceAccountUser >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$DEPLOYER_SA" --role roles/storage.objectAdmin >/dev/null

# Airflow SA: read incoming / write scored in the bucket, run BigQuery jobs.
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$AIRFLOW_SA" --role roles/storage.objectAdmin >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$AIRFLOW_SA" --role roles/bigquery.jobUser --condition=None >/dev/null
echo "  done"

# ── 7. BigQuery dataset ─────────────────────────────────────────────────────
echo ""
echo "→ BigQuery dataset: $BQ_DATASET"
bq --location "$REGION" show "$PROJECT_ID:$BQ_DATASET" >/dev/null 2>&1 \
  && echo "  (already exists)" \
  || bq --location "$REGION" mk --dataset \
       --description "FraudLens batch scoring results" "$PROJECT_ID:$BQ_DATASET"
# Dataset-scoped edit access for Airflow (it creates the external table + summary)
bq query --nouse_legacy_sql --location "$REGION" \
  "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${PROJECT_ID}.${BQ_DATASET}\` TO 'serviceAccount:${AIRFLOW_SA}'" >/dev/null

# ── 8. Workload Identity Federation for GitHub Actions ─────────────────────
echo ""
echo "→ Workload Identity Federation: pool=$WIF_POOL provider=$WIF_PROVIDER"
gcloud iam workload-identity-pools describe "$WIF_POOL" --location global >/dev/null 2>&1 \
  && echo "  pool (already exists)" \
  || gcloud iam workload-identity-pools create "$WIF_POOL" \
       --location global --display-name "GitHub Actions"

gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" \
  --location global --workload-identity-pool "$WIF_POOL" >/dev/null 2>&1 \
  && echo "  provider (already exists)" \
  || gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" \
       --location global \
       --workload-identity-pool "$WIF_POOL" \
       --display-name "GitHub OIDC" \
       --issuer-uri "https://token.actions.githubusercontent.com" \
       --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
       --attribute-condition "assertion.repository == '${GITHUB_ORG}/${GITHUB_REPO}'"

# Only workflows from this one repo can impersonate the deployer SA
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_ORG}/${GITHUB_REPO}" \
  >/dev/null

WIF_PROVIDER_NAME="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}"

# ── 9. Build + push initial image ──────────────────────────────────────────
echo ""
echo "→ Building initial image (linux/amd64 — Cloud Run does not run arm64)"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker buildx build --platform linux/amd64 -t "${IMAGE}:latest" --push .

# ── 10. Cloud Run service ───────────────────────────────────────────────────
echo ""
echo "→ Deploying Cloud Run service: $SERVICE"
gcloud run deploy "$SERVICE" \
  --image "${IMAGE}:latest" \
  --region "$REGION" \
  --service-account "$API_SA" \
  --port 8000 \
  --cpu 1 \
  --memory 2Gi \
  --cpu-boost \
  --min-instances 0 \
  --max-instances 2 \
  --concurrency 20 \
  --timeout 60 \
  --set-env-vars "MLFLOW_TRACKING_URI=file:///app/mlruns,LLM_BACKEND=openai" \
  --set-secrets "OPENAI_API_KEY=${SECRET}:latest" \
  --allow-unauthenticated

SERVICE_URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')

# ── 11. Summary ─────────────────────────────────────────────────────────────
echo ""
echo "======================================================"
echo " Setup complete!"
echo "======================================================"
echo ""
echo " API: ${SERVICE_URL}"
echo "   curl ${SERVICE_URL}/health   (first call cold-starts, ~10-20 s)"
echo ""
echo " Add these GitHub repo secrets (Settings → Secrets → Actions):"
echo "   GCP_PROJECT_ID   = ${PROJECT_ID}"
echo "   GCP_WIF_PROVIDER = ${WIF_PROVIDER_NAME}"
echo "   GCP_DEPLOYER_SA  = ${DEPLOYER_SA}"
echo "   GCS_BUCKET       = ${BUCKET}"
echo ""
echo " Next: Airflow VM →  bash infra/airflow-vm/create-vm.sh"
echo "======================================================"
