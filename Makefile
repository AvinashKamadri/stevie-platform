.PHONY: install db migrate harvest fetch parse reparse status progress watch sampler tuning test rebuild

# System Python is often externally-managed (PEP 668), so everything runs in a
# local venv. PY points every target at it.
VENV := .venv
PY := $(VENV)/bin/python

install:
	python -m venv $(VENV)
	$(PY) -m pip install -q -e ".[dev]"
	$(VENV)/bin/playwright install chromium

# Spin up Postgres locally via Docker (skip if you have your own).
db:
	docker run -d --name stevie-pg -p 5432:5432 --restart unless-stopped \
	  -e POSTGRES_USER=stevie -e POSTGRES_PASSWORD=stevie -e POSTGRES_DB=stevie_platform \
	  postgres:16

migrate:
	$(PY) -m stevie_platform.cli migrate

harvest:
	$(PY) -m stevie_platform.cli harvest

fetch:
	$(PY) -m stevie_platform.cli fetch

parse:
	$(PY) -m stevie_platform.cli parse

reparse:
	$(PY) -m stevie_platform.cli reparse

canonicalize:
	$(PY) -m stevie_platform.cli canonicalize

report:
	$(PY) -m stevie_platform.cli report

metrics:
	$(PY) -m stevie_platform.cli metrics

gates:
	$(PY) -m stevie_platform.cli gates

status:
	$(PY) -m stevie_platform.cli status

progress:
	bash scripts/progress.sh

watch:
	bash scripts/watch.sh

# Non-invasive fetch telemetry — run alongside a fetch (observer only).
sampler:
	bash scripts/sampler.sh

# Per-interval effective rate vs 403s vs backoff gap — the tuning guide.
tuning:
	docker exec stevie-pg psql -U stevie -d stevie_platform -c \
	  "select * from fetch_rate where effective_rps is not null order by ts desc limit 30;"

test:
	$(PY) -m pytest -q

# The success criterion: regenerate everything from the raw archive, NO network.
rebuild: reparse canonicalize report
