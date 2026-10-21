install:
	uv venv && uv pip install -e ".[dev]"

lint:
	ruff check . && ruff format --check .

test:
	pytest -q

db-up:
	docker compose up -d

db-down:
	docker compose down

.PHONY: install lint test db-up db-down
