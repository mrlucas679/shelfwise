.PHONY: test smoke run

test:
	PYTHONPATH=src python -m pytest -q

smoke: test
	PYTHONPATH=src python -c "from shelfwise_backend import run_golden_cascade; r=run_golden_cascade(); print(r['decision']['status'], r['decision']['action']['type'], len(r['evidence']))"

run:
	PYTHONPATH=src python -m shelfwise_backend
