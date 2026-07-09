$ErrorActionPreference = "Stop"
$env:PYTHONPATH = "src"

python -m pytest -q
python scripts/smoke.py
