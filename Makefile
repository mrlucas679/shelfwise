.PHONY: test eval smoke run up down logs

test:
	PYTHONPATH=src python -m pytest -q

eval:
	PYTHONPATH=src python -m shelfwise_eval

smoke: test
	python scripts/smoke.py

run:
	PYTHONPATH=src python -m shelfwise_backend

up:
	docker compose up --build

down:
	docker compose down --remove-orphans

logs:
	docker compose logs -f backend
