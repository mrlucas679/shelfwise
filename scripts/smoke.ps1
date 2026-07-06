$ErrorActionPreference = "Stop"
$env:PYTHONPATH = "src"
python -m pytest -q
python -c "from shelfwise_backend import run_golden_cascade; r=run_golden_cascade(); print(r['decision']['status'], r['decision']['action']['type'], len(r['evidence']))"
