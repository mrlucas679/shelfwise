#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-configs/train_gemma4_multimodal.yaml}"
ADAPTER_PATH="${ADAPTER_PATH:-}"
SERVING_GATE_MODE="${SERVING_GATE_MODE:-metadata_only}"

echo "[1/4] install package + training extras"
python -m pip install -e ".[training]"

echo "[2/4] harness tests"
python -m pytest \
  tests/test_training_profiles.py \
  tests/test_training_evaluation_gate.py \
  tests/test_shakedown_settings.py \
  tests/test_serving_gate.py \
  -q

echo "[3/4] fixture-only eval (cannot pass the generated-evaluation gate)"
python -m shelfwise.training.evaluate --config "${CONFIG_PATH}" --dry-run

echo "[4/4] adapter serving gate: ${SERVING_GATE_MODE}"
if [[ -n "${ADAPTER_PATH}" ]]; then
  python -m shelfwise.training.serving_check \
    --config "${CONFIG_PATH}" \
    --adapter-path "${ADAPTER_PATH}" \
    --mode "${SERVING_GATE_MODE}"
else
  echo "ADAPTER_PATH not set; skipping adapter serving gate."
fi
