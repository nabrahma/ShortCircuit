.DEFAULT_GOAL := help
PY  ?= .venv/bin/python
PIP ?= .venv/bin/pip

COV_PKGS = --cov=strategy --cov=capital_manager --cov=rest_limiter \
           --cov=symbols --cov=market_utils

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── setup ────────────────────────────────────────────────────────────────
.PHONY: install
install:  ## Install runtime + dev dependencies
	$(PIP) install -r requirements.txt -r requirements-dev.txt

# ── quality ──────────────────────────────────────────────────────────────
.PHONY: lint
lint:  ## Run ruff
	$(PY) -m ruff check .

.PHONY: fmt
fmt:  ## Auto-fix what ruff can fix safely
	$(PY) -m ruff check --fix .

.PHONY: typecheck
typecheck:  ## Run mypy (advisory — never blocks)
	-$(PY) -m mypy . --ignore-missing-imports

# ── tests ────────────────────────────────────────────────────────────────
.PHONY: test
test:  ## Unit + property tests (no network, no credentials)
	$(PY) -m pytest tests/unit tests/property -v

.PHONY: test-integration
test-integration:  ## Integration tests (needs PostgreSQL)
	$(PY) -m pytest tests/integration -v -m integration

.PHONY: test-all
test-all: test test-integration  ## Every test level

.PHONY: coverage
coverage:  ## Tests with per-package coverage
	$(PY) -m pytest tests/unit tests/property $(COV_PKGS) --cov-report=term

# ── security ─────────────────────────────────────────────────────────────
.PHONY: security
security:  ## gitleaks (full history) + pip-audit + bandit
	@command -v gitleaks >/dev/null 2>&1 \
	  && gitleaks detect --source . --redact --log-opts="--all" \
	  || echo "gitleaks not installed — see docs/SECURITY.md"
	-$(PY) -m pip_audit -r requirements.txt --desc
	-$(PY) -m bandit -r . -ll --exclude ./.venv,./tests,./scratch,./migrations

# ── evidence ─────────────────────────────────────────────────────────────
.PHONY: audit-purity
audit-purity:  ## Regenerate the module purity audit
	@printf "%-30s %-6s %s\n" "MODULE" "LOC" "SIDE-EFFECT MARKERS"; \
	for f in *.py strategy/*.py; do \
	  [ -f "$$f" ] || continue; loc=$$(wc -l < "$$f"); m=""; \
	  grep -qE "^import requests|^from requests|fyers|aiohttp|httpx" "$$f" && m="$$m net"; \
	  grep -qE "asyncpg|psycopg2|DatabaseManager" "$$f" && m="$$m db"; \
	  grep -qE "telegram" "$$f" && m="$$m telegram"; \
	  grep -qE "datetime\.now|time\.time|time\.monotonic|date\.today" "$$f" && m="$$m clock"; \
	  grep -qE "^import os|open\(|Path\(" "$$f" && m="$$m io"; \
	  grep -qE "threading|asyncio" "$$f" && m="$$m concurrency"; \
	  printf "%-30s %-6s %s\n" "$$f" "$$loc" "$${m:- PURE}"; \
	done

.PHONY: evidence
evidence:  ## Regenerate every file in docs/evidence/
	@mkdir -p docs/evidence
	$(PY) -m pytest tests/unit tests/property -v > docs/evidence/test-suite-full.txt 2>&1 || true
	$(PY) -m pytest tests/unit/test_reconciliation.py -v > docs/evidence/reconciliation-divergence-table.txt 2>&1 || true
	$(PY) -m pytest tests/property -v > docs/evidence/property-tests-hypothesis.txt 2>&1 || true
	$(PY) -m pytest tests/unit/test_brain_isolation.py -v > docs/evidence/brain-isolation-test.txt 2>&1 || true
	$(PY) -m pytest tests/unit tests/property $(COV_PKGS) --cov-report=term > docs/evidence/coverage-by-package.txt 2>&1 || true
	$(MAKE) --no-print-directory audit-purity > docs/evidence/module-purity-audit.txt
	@echo "evidence regenerated"

# ── containers ───────────────────────────────────────────────────────────
.PHONY: docker-build
docker-build:  ## Build the runtime image
	docker build -f deploy/Dockerfile -t shortcircuit:local .

.PHONY: docker-verify
docker-verify: docker-build  ## Prove .env is not inside the image
	@if docker run --rm --entrypoint sh shortcircuit:local \
	    -c 'find / -name ".env" 2>/dev/null' | grep -q .; then \
	  echo "FAIL: .env found inside the image"; exit 1; \
	else echo "PASS: no .env in image"; fi

.PHONY: demo
demo:  ## Run the full test suite in a container — no credentials needed
	@echo "ShortCircuit needs live broker credentials to trade, so there is no"
	@echo "offline demo of order placement. What IS reproducible on any machine"
	@echo "with Docker is the build and the full test suite:"
	@echo
	docker compose -f deploy/docker-compose.test.yml up --exit-code-from tests

# ── aggregate ────────────────────────────────────────────────────────────
.PHONY: verify
verify: lint test coverage  ## Everything CI runs, locally

.PHONY: clean
clean:  ## Remove caches and coverage artefacts
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	@echo "cleaned"
