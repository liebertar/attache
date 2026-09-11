.PHONY: up down logs dev-up dev test lint demo reset scoreboard

# 로컬 스택 = docker-compose.local.yml + .env.local, dev 서버 = docker-compose.dev.yml + .env.dev.
# env 파일이 없으면 예시에서 복사합니다(키는 비어 있어도 규칙만으로 뜸).
COMPOSE_LOCAL = docker compose -f docker-compose.local.yml --env-file .env.local
COMPOSE_DEV = docker compose -f docker-compose.dev.yml --env-file .env.dev

.env.local:
	cp .env.local.example .env.local

.env.dev:
	cp .env.dev.example .env.dev

up: .env.local
	$(COMPOSE_LOCAL) up --build

down: .env.local
	$(COMPOSE_LOCAL) down -v

logs: .env.local
	$(COMPOSE_LOCAL) logs -f runtime sim

dev-up: .env.dev
	$(COMPOSE_DEV) up -d --build

dev:
	./scripts/dev.sh

demo: up

test:
	PYTHONPATH=. python3 -m unittest discover -s tests -v

scoreboard:
	PYTHONPATH=. python3 tests/test_two_worlds.py

lint:
	ruff check backend drone shared sim tests scripts

reset:
	rm -f ledger.jsonl
