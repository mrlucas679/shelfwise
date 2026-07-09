#!/usr/bin/env bash
# Run this ONCE after every pod start/restart: bash scripts/pod_start.sh
# then: source .venv/bin/activate
#
# Why this exists: only /workspace survives a pod restart. Python 3.11 (apt) and
# the Jupyter kernelspec live on the ephemeral root filesystem and vanish every
# time; recreating .venv with the leftover system Python 3.10 silently builds a
# 3.10 venv that cannot install this project (requires-python >= 3.11) and sends
# pip into version-backtracking hell. This script makes the rebuild deterministic
# and idempotent - safe to run even when everything is already correct.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Caches under /workspace so the 6GB ROCm torch wheel and the 24GB model survive restarts.
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-/workspace/.pip-cache}"
export HF_HOME="${HF_HOME:-/workspace/hf_cache}"
mkdir -p "$PIP_CACHE_DIR" "$HF_HOME"

echo "[1/6] python3.11 present?"
if ! command -v python3.11 >/dev/null 2>&1; then
  echo "  installing python3.11 (deadsnakes) - ephemeral, needed after every restart"
  apt-get update -qq
  apt-get install -y -qq software-properties-common >/dev/null
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
  apt-get update -qq
  apt-get install -y -qq python3.11 python3.11-venv python3.11-dev >/dev/null
fi
python3.11 --version

echo "[2/6] venv built on 3.11?"
VENV_OK=false
if [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
  VENV_OK=true
fi
if [ "$VENV_OK" != true ]; then
  echo "  rebuilding .venv with python3.11 (was missing, broken, or built on the wrong Python)"
  rm -rf .venv
  python3.11 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python --version

echo "[3/6] ROCm torch"
ROCM_VERSION="$(cut -d. -f1-2 /opt/rocm/.info/version 2>/dev/null || echo 7.2)"
if ! python -c 'import torch' 2>/dev/null; then
  pip install --quiet --index-url "https://download.pytorch.org/whl/rocm${ROCM_VERSION}" torch
fi
python - <<'PY'
import torch
assert torch.cuda.is_available(), (
    "torch installed but does not see the GPU - wrong wheel index? "
    "Check `cat /opt/rocm/.info/version` and reinstall with the matching rocm index."
)
print(f"torch {torch.__version__} sees: {torch.cuda.get_device_name(0)}")
PY

echo "[4/6] project + training + dev extras"
pip install --quiet -e ".[dev]"
pip install --quiet -e ".[training]"

echo "[5/6] jupyter kernel (kernelspec lives on the ephemeral filesystem)"
pip install --quiet ipykernel
python -m ipykernel install --user --name shelfwise-py311 --display-name "ShelfWise Python 3.11" >/dev/null

echo "[6/6] quick gate"
python -m pytest -q tests/test_golden_cascade.py 2>&1 | tail -1

echo
echo "Pod ready. In THIS terminal you are already in the venv."
echo "Every NEW terminal tab still needs: source .venv/bin/activate"
