.PHONY: up down test lint format

up:
	docker compose up --build

down:
	docker compose down -v

test:
	docker compose up -d --wait postgres rabbitmq
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy app tests

format:
	uv run ruff check --fix .
	uv run ruff format .
