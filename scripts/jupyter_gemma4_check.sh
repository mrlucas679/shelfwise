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
  tests/test_gemma4_training_harness.py \
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
