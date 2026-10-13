# FraudLens

> Production ML system for real-time fraud detection with explainable AI.  
> IEEE-CIS · XGBoost · LightGBM · MLflow · FastAPI · GCP Cloud Run · BigQuery · Qdrant · Airflow
>
> **v2:** GCP port of [FraudLens v1 (AWS)](https://github.com/atharvahirulkar/fraudlens).

![Status](https://img.shields.io/badge/status-live-brightgreen)
![Python](https://img.shields.io/badge/python-3.11-blue)
![License](https://img.shields.io/badge/license-MIT-green)

**Live API:** `https://fraudlens-api-<hash>-uc.a.run.app` (printed by `infra/setup-gcp.sh`)  
`/health` · `/predict` · `/explain` · `/model/info` · `/docs`

---

## What this is

FraudLens is a production-grade ML system that trains, tracks, and serves a fraud detection model - and explains every prediction using a RAG-powered explanation engine. XGBoost and LightGBM are compared across 20+ MLflow experiment runs on the IEEE-CIS Fraud Detection dataset. The best model by AUC-PR is registered in the MLflow model registry, containerized, and deployed to Google Cloud Run. A Qdrant-backed RAG layer retrieves fraud pattern context and generates natural language explanations for each flagged transaction using GPT-4o-mini. A daily Airflow DAG scores new batches and writes results to Cloud Storage + BigQuery.

The focus is the full production loop: reproducible training, experiment tracking, cloud deployment, explainability, and orchestrated batch inference.

---

## Architecture

```
IEEE-CIS Dataset (Kaggle)
        │
        ▼
Feature Engineering               ← Pandas, Scikit-learn
· Transaction aggregates
· Temporal + behavioral features
· Target encoding + imputation
        │
        ▼
MLflow Experiment Tracking
· XGBoost runs  (N hyperparameter configs)
· LightGBM runs (N hyperparameter configs)
· Logged: AUC-ROC, AUC-PR, F1, SHAP values
        │
        ▼
MLflow Model Registry             ← best AUC-PR run → "production"
        │
        ▼
FastAPI Inference Service
· POST /predict                   ← fraud score + label + explanation
· POST /explain                   ← SHAP + RAG explanation only
· GET  /health
· GET  /model/info
        │
        ├── GCP Cloud Run         ← containerized, HTTPS, scales to zero
        │
        ▼
Qdrant Vector Store               ← fraud pattern knowledge base
· 384-dim embeddings (all-MiniLM-L6-v2)
· Retrieves top-K relevant fraud patterns
· LLM (Claude Haiku / Ollama Mistral) generates explanation
        │
        ▼
Airflow DAG (daily batch)
· Pulls new transactions from GCS
· Scores via inference API
· Writes results → GCS + BigQuery
        │
        ▼
BigQuery over GCS parquet         ← fraud trend analysis, reporting
```

---

## Stack

| Layer | Technology |
|---|---|
| Dataset | IEEE-CIS Fraud Detection (Kaggle) |
| Models | XGBoost, LightGBM |
| Experiment tracking | MLflow |
| API framework | FastAPI + Uvicorn |
| Data validation | Pydantic v2 |
| Containerization | Docker |
| Cloud deployment | GCP Cloud Run + Artifact Registry |
| Batch storage | Cloud Storage + BigQuery |
| Secrets | Secret Manager |
| Vector store | Qdrant |
| Embeddings | sentence-transformers (all-MiniLM-L6-v2) |
| LLM (explanation) | OpenAI GPT-4o-mini (prod) / Ollama Mistral (local dev) |
| Orchestration | Apache Airflow (Compute Engine VM) |
| CI/CD | GitHub Actions + Workload Identity Federation |
| Language | Python 3.11 |

---

## Project Structure

```
fraudlens/
├── data/
│   ├── raw/                     # Kaggle CSVs (gitignored)
│   ├── processed/               # Feature-engineered parquet files
│   └── fraud_patterns/          # RAG knowledge base docs (JSON)
├── notebooks/
│   ├── 01_eda.ipynb             # Exploratory data analysis (see GitHub)
│   └── 02_feature_eng.ipynb     # Feature development
├── src/
│   ├── train/
│   │   ├── train.py             # MLflow experiment runs - XGB + LGBM
│   │   ├── features.py          # Feature engineering pipeline
│   │   └── evaluate.py          # AUC-PR, threshold tuning, SHAP
│   ├── api/
│   │   ├── main.py              # FastAPI application
│   │   ├── schemas.py           # Pydantic request/response models
│   │   ├── predictor.py         # Loads best MLflow model at startup
│   │   └── explainer.py         # SHAP + RAG explanation engine
│   ├── rag/
│   │   ├── ingest.py            # Embed + index fraud patterns → Qdrant
│   │   ├── retriever.py         # Top-K pattern retrieval
│   │   └── generator.py         # LLM explanation generation
│   └── tests/
│       ├── test_api.py          # Endpoint integration tests
│       └── test_predict.py      # Prediction unit tests
├── dags/
│   └── daily_scoring.py         # Airflow DAG: GCS → Cloud Run → GCS/BigQuery
├── mlruns/                      # MLflow local tracking store
├── infra/
│   ├── setup-gcp.sh             # One-time GCP bootstrap (Cloud Run, WIF, IAM, BigQuery)
│   └── airflow-vm/              # Compute Engine Airflow: create-vm.sh, startup.sh, compose
├── scripts/
│   └── make_sample_batch.py     # Synthetic batch → gs://…/incoming/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .github/
│   └── workflows/
│       └── deploy.yml           # Build → Artifact Registry → Cloud Run deploy
└── README.md
```

---

## API Reference

### `POST /predict`

```json
// Request
{
  "transaction_amt": 142.50,
  "product_cd": "W",
  "card_type": "credit",
  "addr1": 315,
  "dist1": 19.0,
  "p_emaildomain": "gmail.com",
  "r_emaildomain": "anonymous.com"
}

// Response
{
  "fraud_score": 0.83,
  "is_fraud": true,
  "model_version": "lightgbm-v2",
  "latency_ms": 14.2,
  "explanation": "This transaction was flagged primarily due to a mismatched recipient email domain (anonymous.com) combined with an above-average transaction amount. These features match patterns associated with card-not-present fraud where the billing and shipping email domains differ. SHAP top drivers: r_emaildomain (+0.31), transaction_amt (+0.18), dist1 (+0.09)."
}
```

### `POST /explain`

```json
// Request - same schema as /predict
// Response
{
  "shap_values": {
    "r_emaildomain": 0.31,
    "transaction_amt": 0.18,
    "dist1": 0.09
  },
  "retrieved_patterns": [
    "card-not-present email mismatch",
    "high-value CNP transaction"
  ],
  "explanation": "..."
}
```

### `GET /model/info`

```json
{
  "model_name": "fraudlens-detector",
  "version": "2",
  "algorithm": "LightGBM",
  "auc_pr": 0.91,
  "auc_roc": 0.93,
  "registered_at": "2026-05-15T10:00:00Z"
}
```

---

## RAG Explanation System

The explanation layer retrieves relevant fraud pattern context before generating a natural language explanation:

```
Incoming transaction features + SHAP values
        │
        ▼
Embed top SHAP features → query vector (384-dim)
        │
        ▼
Qdrant similarity search → top-3 fraud pattern docs
(e.g. "card-not-present email mismatch", "velocity abuse")
        │
        ▼
LLM prompt:
  · Transaction summary
  · SHAP top drivers
  · Retrieved pattern context
        │
        ▼
Natural language explanation (2–3 sentences)
```

**Fraud pattern knowledge base** (`data/fraud_patterns/`): ~50 curated JSON documents describing common IEEE-CIS fraud patterns, each with feature signatures, typical SHAP driver profiles, and plain-English descriptions. Embedded at startup and indexed in Qdrant.

**LLM config:**
- Dev: Ollama (Mistral 7B, fully local, zero cost)
- Prod: OpenAI GPT-4o-mini (low latency, low cost per call)

---

## Airflow Batch Scoring DAG

`dags/daily_scoring.py` runs nightly at 2 AM UTC:

```
GCS: gs://<bucket>/incoming/YYYY-MM-DD/
        │ download new transactions
        ▼
Score via inference API (POST /predict)
        │
        ▼
GCS: gs://<bucket>/scored/dt=YYYY-MM-DD/
        │ BigQuery external table: fraudlens.scored
        ▼
BigQuery: fraudlens.daily_summary (fraud rate · p95 score · flagged amount)
```

BigQuery reads the Parquet files in GCS directly (hive partitioning on `dt`), so there's no load step for raw results. The DAG upserts one row per day into a native `daily_summary` table.

---

## Experiment Tracking

Both models are evaluated across multiple hyperparameter configurations in MLflow:

| Logged artifact | Description |
|---|---|
| `auc_roc` | Standard ranking metric |
| `auc_pr` | Primary metric (imbalanced data) |
| `f1_score` | At optimal threshold |
| `feature_importance` | SHAP summary plot |
| `confusion_matrix` | At 0.5 + optimal threshold |
| `model` | Serialized artifact |

Best run by AUC-PR is promoted to `production` in the MLflow model registry. `predictor.py` loads this at API startup - no hardcoded paths.

---

## Cloud Deployment (GCP)

| Component | Service |
|---|---|
| Inference API | **Cloud Run** (HTTPS URL built in, scales to zero, max 2 instances) |
| Container images | Artifact Registry (cleanup policy keeps last 5) |
| Model artifacts, batch in/out, DAGs | Cloud Storage `gs://fraudlens-artifacts-<project>` |
| Analytics | BigQuery: hive-partitioned external table `fraudlens.scored` + `fraudlens.daily_summary` |
| Orchestration | Self-hosted Airflow 2.9 on a Compute Engine e2-medium (Docker, UI via IAP tunnel only) |
| Secrets | Secret Manager (`openai-api-key`), mounted into Cloud Run as `OPENAI_API_KEY` |
| CI/CD | GitHub Actions → Cloud Run via **Workload Identity Federation** (no JSON keys) |
| Identity | 3 least-privilege service accounts: `fraudlens-api`, `fraudlens-deployer`, `fraudlens-airflow` |

### Bootstrap (one time)

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project <PROJECT_ID>

# APIs, Artifact Registry, bucket, service accounts, secret, IAM, BigQuery,
# Workload Identity Federation, first image build, Cloud Run deploy
GITHUB_ORG=atharvahirulkar GITHUB_REPO=fraudlens-v2 bash infra/setup-gcp.sh

# Airflow VM (pulls DAGs from GCS every 2 min)
bash infra/airflow-vm/create-vm.sh
```

The setup script prints the four GitHub secrets to add: `GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_DEPLOYER_SA`, `GCS_BUCKET`. After that, every push to `main`:
1. Authenticates to GCP through Workload Identity Federation (only this repo can impersonate the deployer SA)
2. Syncs model artifacts from GCS (`mlruns/`)
3. Builds the image and pushes it to Artifact Registry
4. Deploys a new Cloud Run revision (traffic shifts once it's healthy)
5. Smoke-tests `/health` and syncs `dags/` to GCS

### Batch scoring end to end

```bash
# Drop a synthetic batch into incoming/
python scripts/make_sample_batch.py --bucket fraudlens-artifacts-<PROJECT_ID> --date 2026-10-14

# Trigger the DAG for that date (or wait for 02:00 UTC)
gcloud compute ssh fraudlens-airflow --zone us-central1-a --tunnel-through-iap \
  --command "sudo docker exec airflow airflow dags trigger fraudlens_daily_scoring -e 2026-10-14"

# Query results
bq query --nouse_legacy_sql \
  'SELECT * FROM fraudlens.daily_summary ORDER BY dt DESC'
```

### Cost on the $300 trial

| Resource | Approx. monthly |
|---|---|
| Cloud Run (scale to zero) | ~$0 to $2 (free tier: 2M requests, 180k vCPU-s) |
| Airflow VM e2-medium + 20 GB disk | ~$27 running; ~$2 stopped |
| GCS / Artifact Registry / BigQuery | < $1 at this scale |

Stop the VM when you're not using it: `gcloud compute instances stop fraudlens-airflow --zone us-central1-a`.

---

## Quickstart

> **Prerequisites:** Docker Desktop · Python 3.11+ · Kaggle account (free) · GCP project (the $300 free trial covers this) · `gcloud` CLI

```bash
# 1. Clone and install
git clone https://github.com/atharvahirulkar/fraudlens-v2.git
cd fraudlens-v2
pip install -r requirements.txt

# 2. Download dataset (place in data/raw/)
#    train_transaction.csv + train_identity.csv
#    https://www.kaggle.com/c/ieee-fraud-detection/data

# 3. Run training experiments
python src/train/train.py
mlflow ui --port 5000
# Open http://localhost:5000 - compare XGBoost vs LightGBM runs

# 4. Start Qdrant and ingest fraud patterns
docker run -d -p 6333:6333 qdrant/qdrant
python src/rag/ingest.py

# 5. Start inference API locally
uvicorn src.api.main:app --reload --port 8000

# 6. Run full stack via Docker Compose
docker compose up
# API:     http://localhost:8000
# MLflow:  http://localhost:5000
# Qdrant:  http://localhost:6333/dashboard

# 7. Test prediction + explanation
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_amt": 142.50,
    "product_cd": "W",
    "card_type": "credit",
    "addr1": 315,
    "dist1": 19.0,
    "p_emaildomain": "gmail.com",
    "r_emaildomain": "anonymous.com"
  }'

# 8. Health check
curl http://localhost:8000/health
```

---

## Performance Targets

| Metric | Target |
|---|---|
| AUC-PR (imbalanced test set) | > 0.88 |
| AUC-ROC | > 0.92 |
| p50 inference latency (local) | < 20ms |
| p99 inference latency (Cloud Run, warm) | < 80ms |
| Explanation generation latency | < 2s |
| Airflow DAG daily run time | < 5 min |

*Targets for Cloud Run (us-central1, 1 vCPU / 2 GiB). Cold starts after scale-to-zero add ~10-20 s to the first request.*

---

## Status

- [x] EDA notebook (`01_eda.ipynb`)
- [x] Feature engineering pipeline (`src/train/features.py`)
- [x] MLflow experiment runs - XGBoost + LightGBM (20+ configs)
- [x] Threshold tuning + SHAP (`src/train/evaluate.py`)
- [x] FastAPI service + Pydantic schemas
- [x] MLflow model registry integration (`src/api/predictor.py`)
- [x] Full feature pipeline applied at inference time (not just 7 raw fields)
- [x] Real SHAP values via `shap.TreeExplainer` on native model
- [x] Fraud pattern knowledge base (~51 JSON docs in `data/fraud_patterns/`)
- [x] Qdrant ingestion pipeline (`src/rag/ingest.py`)
- [x] RAG retrieval + in-memory fallback (`src/rag/retriever.py`)
- [x] LLM explanation generator - GPT-4o-mini / Ollama (`src/rag/generator.py`)
- [x] SHAP + RAG explainer endpoint (`src/api/explainer.py`)
- [x] Docker image + docker-compose (Qdrant + MLflow + API)
- [x] Artifact Registry + Cloud Run deploy (`infra/setup-gcp.sh`)
- [x] GCS model artifact store + versioning
- [x] Airflow DAG on Compute Engine - daily batch scoring → GCS + BigQuery (`dags/daily_scoring.py`)
- [x] GitHub Actions CI/CD via Workload Identity Federation - push to main auto-deploys to Cloud Run (`deploy.yml`)
- [x] CPU-only torch build (image much smaller than v1's ~3.7 GB)

---

## Dataset

[IEEE-CIS Fraud Detection](https://www.kaggle.com/c/ieee-fraud-detection) - 590K transactions, ~3.5% fraud rate. A heavily imbalanced binary classification problem, making AUC-PR the primary evaluation metric over AUC-ROC.

---

## Author

**Atharva Hirulkar** - MS Data Science, UC San Diego  
[GitHub](https://github.com/atharvahirulkar) · [LinkedIn](https://linkedin.com/in/atharva-hirulkar)
