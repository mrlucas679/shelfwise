#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-configs/train_gemma4_multimodal.yaml}"
ADAPTER_PATH="${ADAPTER_PATH:-}"

echo "[1/4] install package + training extras"
python -m pip install -e ".[training]"

echo "[2/4] harness tests"
python -m pytest tests/test_gemma4_training_harness.py -q

echo "[3/4] dry-run eval"
python -m shelfwise.training.evaluate --config "${CONFIG_PATH}" --dry-run

echo "[4/4] adapter metadata check"
if [[ -n "${ADAPTER_PATH}" ]]; then
  python -m shelfwise.training.serving_check \
    --config "${CONFIG_PATH}" \
    --adapter-path "${ADAPTER_PATH}" \
    --skip-model-load
else
  echo "ADAPTER_PATH not set; skipping adapter metadata check."
fi
