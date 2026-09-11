.PHONY: up dev down logs test lint demo reset

up:
	docker compose up --build

dev:
	./scripts/dev.sh

demo: up

down:
	docker compose down -v

logs:
	docker compose logs -f runtime sim

test:
	PYTHONPATH=. python3 -m unittest discover -s tests -v

scoreboard:
	PYTHONPATH=. python3 tests/test_two_worlds.py

lint:
	ruff check holdshort sim tests

reset:
	rm -f ledger.jsonl
