#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-configs/train_gemma4_multimodal.yaml}"
RUN_NAME="${RUN_NAME:-shelfwise-mm-full-8h-001}"

echo "[1/5] repo"
git rev-parse --show-toplevel
git status --short --branch

echo "[2/5] python"
python --version

echo "[3/5] install package + training extras"
python -m pip install --upgrade pip
python -m pip install -e ".[training]"

echo "[4/5] dataset/config unit checks"
python -m pytest tests/test_gemma4_training_harness.py -q

echo "[5/5] full ShelfWise AI shakedown"
python -m shelfwise.training.shakedown \
  --config "${CONFIG_PATH}" \
  --run_name "${RUN_NAME}"

echo "Jupyter GPU bootstrap complete. Check runs/gemma4-multimodal/ for outputs."
