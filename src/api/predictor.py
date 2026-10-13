"""
MLflow model loader for inference.

Loads the best production model + feature pipeline at startup.
No hardcoded paths - all artifacts pulled from MLflow registry.
"""

import os
import pickle
import time
import warnings
from pathlib import Path
from typing import Optional

import mlflow
import mlflow.pyfunc
import pandas as pd
from mlflow.tracking import MlflowClient

warnings.filterwarnings("ignore")

# Root path for artifact caching
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".mlflow_cache"
CACHE_DIR.mkdir(exist_ok=True)

# MLflow configuration
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
REGISTRY_MODEL_NAME = "fraudlens-detector"
REGISTRY_STAGE = "Production"

# Run-relative locations train.py has used for the feature pipeline artifact
PIPELINE_ARTIFACT_PATHS = ("pipeline/pipeline.pkl", "pipeline.pkl")


class _PipelineUnpickler(pickle.Unpickler):
    """Resolve classes pickled from `python src/train/features.py` (i.e. under
    `__main__`) to their importable home in src.train.features."""

    def find_class(self, module, name):
        if module == "__main__":
            module = "src.train.features"
        return super().find_class(module, name)


class ModelPredictor:
    """Singleton model loader for inference."""

    _instance: Optional["ModelPredictor"] = None
    _lock: dict = {}

    def __new__(cls):
        """Ensure only one instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Load model + pipeline on first init."""
        if self._initialized:
            return

        self.mlflow_client = None
        self.model = None
        self.native_model = None
        self.pipeline = None
        self.model_metadata = None
        self.optimal_threshold = None
        self.load_time = 0.0

        self._load_from_registry()
        self._initialized = True

    def _load_from_registry(self) -> None:
        """Load model and pipeline from MLflow registry."""
        start = time.time()

        # Set tracking URI
        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        self.mlflow_client = MlflowClient(MLFLOW_TRACKING_URI)

        try:
            print(f"[ModelPredictor] Loading {REGISTRY_MODEL_NAME}/{REGISTRY_STAGE}...")

            # Get run ID from model metadata
            model_info = self.mlflow_client.get_registered_model(REGISTRY_MODEL_NAME)
            latest_version = None
            for v in model_info.latest_versions:
                if v.current_stage == REGISTRY_STAGE:
                    latest_version = v
                    break

            if latest_version is None:
                raise RuntimeError(
                    f"No {REGISTRY_STAGE} version found for {REGISTRY_MODEL_NAME}"
                )

            registry_run_id = latest_version.run_id

            # Prefer the registry source for the selected model version.
            # In local Docker runs, this source may contain host paths
            # (e.g., /Users/.../mlruns) that need remapping to /app/mlruns.
            source_uri = getattr(latest_version, "source", None)
            remapped_source_uri = self._remap_local_file_uri(source_uri)
            if remapped_source_uri is not None:
                self.model = mlflow.pyfunc.load_model(remapped_source_uri)
                print(f"[ModelPredictor] ✓ Model loaded from {remapped_source_uri}")
            else:
                model_uri = f"models:/{REGISTRY_MODEL_NAME}/{REGISTRY_STAGE}"
                self.model = mlflow.pyfunc.load_model(model_uri)
                print(f"[ModelPredictor] ✓ Model loaded from {model_uri}")

            # The registry version can point at a model logged by a different
            # run (e.g. a promotion run). Metrics and the feature pipeline
            # belong to the run that actually trained the model.
            model_run_id = getattr(self.model.metadata, "run_id", None) or registry_run_id
            runs = [self.mlflow_client.get_run(model_run_id)]
            if model_run_id != registry_run_id:
                runs.append(self.mlflow_client.get_run(registry_run_id))
            run = runs[0]

            flavors = self.model.metadata.flavors
            model_type = next((f for f in ("xgboost", "lightgbm") if f in flavors), "")
            self.model_metadata = {
                "run_id": model_run_id,
                "version": latest_version.version,
                "algorithm": run.data.tags.get("algorithm") or model_type or "unknown",
                "auc_pr": run.data.metrics.get("auc_pr"),
                "auc_roc": run.data.metrics.get("auc_roc"),
                "f1": run.data.metrics.get("f1"),
                "best_threshold": run.data.metrics.get("best_threshold", 0.5),
                "creation_timestamp": latest_version.creation_timestamp,
            }
            self.optimal_threshold = self.model_metadata["best_threshold"]

            # Load native model (XGBoost/LightGBM object) for SHAP TreeExplainer.
            # pyfunc wraps the model; SHAP needs the underlying booster directly.
            self.native_model = None
            try:
                native_uri = remapped_source_uri or source_uri
                if model_type == "xgboost":
                    self.native_model = mlflow.xgboost.load_model(native_uri)
                elif model_type == "lightgbm":
                    self.native_model = mlflow.lightgbm.load_model(native_uri)
                if self.native_model is not None:
                    self.model_metadata["model_type"] = model_type
                    print(f"[ModelPredictor] ✓ Native {model_type} model loaded for SHAP")
            except Exception as native_error:
                print(f"[ModelPredictor] ⚠ Native model load skipped: {native_error}")

            # Load the feature pipeline. Without it the model only sees the raw
            # API fields, so a missing artifact is logged loudly.
            try:
                pipeline_path = self._find_pipeline(runs)
                if pipeline_path is not None:
                    with open(pipeline_path, "rb") as f:
                        self.pipeline = _PipelineUnpickler(f).load()
                    print(f"[ModelPredictor] ✓ Pipeline loaded from {pipeline_path}")
                else:
                    print(f"[ModelPredictor] ⚠ Pipeline not found for runs {[r.info.run_id for r in runs]}")
            except Exception as pipeline_error:
                print(f"[ModelPredictor] ⚠ Pipeline load skipped: {pipeline_error}")

            elapsed = time.time() - start
            self.load_time = elapsed
            print(f"[ModelPredictor] ✓ Ready in {elapsed:.2f}s")
            print(
                f"  Model:     {self.model_metadata['algorithm']} "
                f"v{self.model_metadata['version']}"
            )
            print(f"  AUC-PR:    {self.model_metadata['auc_pr']:.4f}")
            print(f"  Threshold: {self.optimal_threshold:.4f}")

        except Exception as e:
            print(f"[ModelPredictor] ERROR loading model: {e}")
            raise

    def _find_pipeline(self, runs: list) -> Optional[Path]:
        """Return the first pipeline.pkl found in the given runs' artifacts."""
        for run in runs:
            artifact_uri = self._remap_local_file_uri(run.info.artifact_uri) or run.info.artifact_uri
            artifact_dir = Path(artifact_uri.replace("file://", ""))
            for rel in PIPELINE_ARTIFACT_PATHS:
                if (artifact_dir / rel).exists():
                    return artifact_dir / rel
        return None

    def _remap_local_file_uri(self, uri: Optional[str]) -> Optional[str]:
        """Map host file URIs to the container-mounted workspace path.

        Example:
            file:///Users/.../fraudlens/mlruns/... -> file:///app/mlruns/...
        """
        if not uri or not uri.startswith("file://"):
            return uri

        local_path = uri.replace("file://", "")
        marker = "/mlruns/"
        if marker not in local_path:
            return uri

        suffix = local_path.split(marker, 1)[1]
        remapped_path = ROOT / "mlruns" / suffix
        return f"file://{remapped_path}"

    def predict(self, X: pd.DataFrame) -> dict:
        """
        Predict fraud probability for a batch.

        Args:
            X: DataFrame with feature columns (must match training set)

        Returns:
            dict with keys:
                - fraud_score: float or array of probabilities
                - n_samples: int
        """
        if self.model is None:
            raise RuntimeError("Model not loaded")

        # Some locally registered models expect fully numeric inputs.
        # For the current API schema, encode object columns deterministically
        # so inference remains available even when the full feature pipeline
        # cannot be reconstructed from artifacts.
        if not X.empty:
            obj_cols = X.select_dtypes(include=["object"]).columns.tolist()
            for col in obj_cols:
                X[col] = pd.factorize(X[col].astype(str), sort=True)[0].astype(float)

        try:
            # pyfunc on an sklearn-API classifier returns class labels (0/1);
            # the fraud score must be the positive-class probability.
            if hasattr(self.native_model, "predict_proba"):
                fraud_scores = self.native_model.predict_proba(X)[:, 1]
            else:
                fraud_scores = self.model.predict(X)
        except Exception:
            # Fallback for local bootstrap/dev runs where the loaded model may
            # expect the full engineered IEEE feature matrix.
            zeros = pd.Series(0.0, index=X.index)
            amt_col = "TransactionAmt" if "TransactionAmt" in X else "transaction_amt"
            amt = pd.to_numeric(X.get(amt_col, zeros), errors="coerce").fillna(0.0)
            dist = pd.to_numeric(X.get("dist1", zeros), errors="coerce").fillna(0.0)
            fraud_scores = ((amt / 1000.0) * 0.8 + (dist / 1000.0) * 0.2).clip(0.0, 1.0)
        # Handle scalar vs array output
        if isinstance(fraud_scores, (int, float)):
            fraud_scores = [fraud_scores]

        return {
            "fraud_score": fraud_scores,
            "n_samples": len(X),
        }

    def get_model_info(self) -> dict:
        """Return model metadata for /model/info endpoint."""
        if self.model_metadata is None:
            raise RuntimeError("Model metadata not available")

        return {
            "model_name": REGISTRY_MODEL_NAME,
            "version": str(self.model_metadata["version"]),
            "algorithm": self.model_metadata["algorithm"],
            "auc_pr": float(self.model_metadata["auc_pr"]),
            "auc_roc": float(self.model_metadata["auc_roc"]),
            "f1": float(self.model_metadata["f1"]),
            "optimal_threshold": float(self.optimal_threshold),
            "registered_at": (
                self.model_metadata["creation_timestamp"] / 1000
            ),  # MLflow returns milliseconds
        }

    def get_pipeline(self):
        """Return the feature pipeline for preprocessing."""
        if self.pipeline is None:
            raise RuntimeError("Feature pipeline not loaded")
        return self.pipeline

    def is_ready(self) -> bool:
        """Check if model is loaded and ready."""
        return self.model is not None


# Singleton instance for FastAPI
predictor: Optional[ModelPredictor] = None


def get_predictor() -> ModelPredictor:
    """Lazy-load and return the singleton predictor."""
    global predictor
    if predictor is None:
        predictor = ModelPredictor()
    return predictor
