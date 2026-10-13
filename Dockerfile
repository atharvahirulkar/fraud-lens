# Dockerfile for FraudLens inference service (GCP Cloud Run)
FROM python:3.12-slim

WORKDIR /app

# libgomp1: OpenMP runtime required by LightGBM/XGBoost wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install CPU-only torch first so sentence-transformers doesn't pull the
# multi-GB CUDA build (the main reason the v1 image was ~3.7 GB).
COPY requirements-api.txt constraints.txt ./
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -c constraints.txt -r requirements-api.txt

# Copy application code
COPY src/ /app/src/
COPY data/fraud_patterns/ /app/data/fraud_patterns/

# Bundle model artifacts so the container is self-contained.
# In local docker-compose, the volume mount overrides this with the live mlruns.
# In CI, the deploy workflow syncs from GCS before docker build.
COPY mlruns/ /app/mlruns/

# Pre-download embedding model so cold starts don't hit Hugging Face
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('all-MiniLM-L6-v2')" 2>&1 | tail -1 || echo "Model cached"

# Default to the bundled file-based MLflow store.
# docker-compose overrides this to http://mlflow:5000 for local dev.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MLFLOW_TRACKING_URI=file:///app/mlruns \
    QDRANT_URL= \
    LLM_BACKEND=openai \
    PORT=8000

EXPOSE 8000

# Cloud Run injects $PORT; default to 8000 for local runs
CMD ["sh", "-c", "uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT}"]
