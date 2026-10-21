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

migrate:
	python -m db.migrate

resample:
	python -m etl.resample_to_daily archive data/prices_daily.parquet

load: migrate
	python -m etl.load_db data/prices_daily.parquet

verify-data:
	python -m etl.verify_load

data: resample load verify-data

.PHONY: install lint test db-up db-down migrate resample load verify-data data
