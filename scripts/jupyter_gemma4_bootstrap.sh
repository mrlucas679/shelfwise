#!/usr/bin/env bash
set -euo pipefail

# Refuse to run on the wrong interpreter: a 3.10 shell (venv not active) sends pip
# into a cp310 backtracking spiral that wastes 20+ minutes before failing anyway.
PYV="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo none)"
if [ "${PYV}" != "3.11" ]; then
  echo "ERROR: python is ${PYV}, not 3.11 - the project venv is not active in this terminal."
  echo "Fix:   source .venv/bin/activate"
  echo "       (after a pod restart, run: bash scripts/pod_start.sh first)"
  exit 1
fi

CONFIG_PATH="${1:-configs/train_gemma4_multimodal.yaml}"
RUN_NAME="${RUN_NAME:-shelfwise-mm-full-8h-001}"

echo "[1/5] repo"
git rev-parse --show-toplevel
git status --short --branch

echo "[2/5] python"
python --version
echo "Configured boundary: W7900 Jupyter training; MI300X endpoint serving is separate."

echo "[3/5] install package + training extras"
python -m pip install --upgrade pip
python -m pip install -e ".[training]"

echo "[4/5] dataset/config unit checks"
python -m pytest \
  tests/test_training_profiles.py \
  tests/test_training_evaluation_gate.py \
  tests/test_shakedown_settings.py \
  tests/test_serving_gate.py \
  tests/test_gemma4_training_harness.py \
  -q

echo "[5/5] full ShelfWise AI shakedown"
python -m shelfwise.training.shakedown \
  --config "${CONFIG_PATH}" \
  --run_name "${RUN_NAME}"

echo "Jupyter training shakedown complete. MI300X inference is proven only by generated_inference."
