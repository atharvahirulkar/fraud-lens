"""
Promote a training run's logged model to Production in the local MLflow registry.

Registers the run's logged model as a new 'fraudlens-detector' version and moves
it to the Production stage (previous versions are archived). Sync mlruns/ to GCS
afterwards so the next Cloud Run deploy picks it up.

Usage:
    python scripts/promote_model.py --run-name xgb-deep
"""

import argparse
from pathlib import Path

import mlflow

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "fraudlens-detector"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--experiment", default="fraudlens-detection")
    args = parser.parse_args()

    mlflow.set_tracking_uri(f"file://{ROOT / 'mlruns'}")
    client = mlflow.tracking.MlflowClient()
    exp = client.get_experiment_by_name(args.experiment)
    runs = client.search_runs([exp.experiment_id], f"tags.mlflow.runName = '{args.run_name}'",
                              order_by=["metrics.auc_pr DESC"], max_results=1)
    if not runs:
        raise SystemExit(f"No run named {args.run_name} in {args.experiment}")
    run = runs[0]

    # MLflow 3 stores logged models under <experiment>/models/<model_id>/artifacts
    model_dirs = [p.parent for p in (ROOT / "mlruns" / exp.experiment_id / "models").glob("*/meta.yaml")
                  if run.info.run_id in p.read_text()]
    if not model_dirs:
        raise SystemExit(f"No logged model found for run {run.info.run_id}")
    source = f"file://{model_dirs[0] / 'artifacts'}"

    version = client.create_model_version(
        MODEL_NAME, source=source, run_id=run.info.run_id,
        description=f"{args.run_name} (AUC-PR {run.data.metrics.get('auc_pr', float('nan')):.3f})",
    )
    client.transition_model_version_stage(MODEL_NAME, version.version, "Production",
                                          archive_existing_versions=True)
    print(f"{MODEL_NAME} v{version.version} → Production ({args.run_name}, run {run.info.run_id[:8]})")


if __name__ == "__main__":
    main()
