# Everything you need day to day. `make help` lists it.
.DEFAULT_GOAL := help
.PHONY: help up down logs ps restart build migrate migration seed reset \
        test test-unit test-integration test-live lint format typecheck check \
        eval eval-judge shell psql redis worker mcp demo clean frontend-build \
        tf-validate

COMPOSE := docker compose
API     := $(COMPOSE) exec -T api
# Quality tooling runs in a one-off `dev` container with the source mounted:
# the api image is a production artefact and ships no tests.
DEV     := $(COMPOSE) run --rm --no-deps dev

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# --- stack -------------------------------------------------------------------
up: ## Start everything (migrates and seeds on first run)
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  API        http://localhost:8000      (docs: /docs)"
	@echo "  Frontend   http://localhost:5173"
	@echo "  Mock API   http://localhost:8001/docs"
	@echo "  MCP        http://localhost:8020/mcp"
	@echo ""
	@echo "  Sign in as researcher@example.com / Research123!"

down: ## Stop everything (keeps volumes)
	$(COMPOSE) down

reset: ## Stop everything and DELETE all data
	$(COMPOSE) down -v
	rm -rf storage/documents data/pdfs

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Tail logs (make logs S=api)
	$(COMPOSE) logs -f $(or $(S),)

restart: ## Rebuild and restart the app services
	$(COMPOSE) up -d --build api celery-worker mcp

build: ## Build the application image
	$(COMPOSE) build

# --- database ----------------------------------------------------------------
migrate: ## Apply database migrations
	$(API) alembic upgrade head

migration: ## Autogenerate a migration (make migration M="add x")
	$(API) alembic revision --autogenerate -m "$(M)"

seed: ## Load sample data and ingest the sample PDFs
	$(API) python -m scripts.seed_data --with-pdfs

sample-data: ## Regenerate the synthetic dataset and PDFs
	$(API) python -m scripts.generate_sample_data
	$(API) python -m scripts.generate_pdfs

psql: ## Open a psql shell
	$(COMPOSE) exec postgres psql -U research -d research

redis: ## Open a redis-cli shell
	$(COMPOSE) exec redis redis-cli

# --- quality -----------------------------------------------------------------
test: ## Run every test (unit + integration)
	$(DEV) pytest tests -p no:cacheprovider -q

test-unit: ## Unit tests only (no database needed)
	$(DEV) pytest tests/unit -p no:cacheprovider -q

test-integration: ## Integration tests (needs postgres and redis)
	$(DEV) pytest tests/integration -p no:cacheprovider -q

test-live: ## Live tests against the real OpenAI API (needs a key)
	$(DEV) env RUN_LIVE_AI_TESTS=1 pytest tests/live -p no:cacheprovider -v

lint: ## Ruff lint + format check
	$(DEV) ruff check .
	$(DEV) ruff format --check .

format: ## Auto-fix and format
	$(DEV) ruff check . --fix
	$(DEV) ruff format .

typecheck: ## mypy
	$(DEV) mypy app mock_services

check: lint typecheck test ## Everything CI runs

# --- evaluation --------------------------------------------------------------
eval: ## Run every evaluation suite (deterministic checks only)
	$(DEV) python -m scripts.run_evaluation --all --no-judge \
		--report /app/storage/evaluation-report.json

eval-judge: ## Run every suite including LLM-as-a-judge
	$(DEV) python -m scripts.run_evaluation --all \
		--report /app/storage/evaluation-report.json

# --- misc --------------------------------------------------------------------
shell: ## Python shell inside the API container
	$(COMPOSE) exec api python

worker: ## Tail the Celery worker
	$(COMPOSE) logs -f celery-worker

mcp: ## List the MCP server's tools over the real protocol
	$(API) python -c "import asyncio; from app.mcp.client import MCPToolClient; \
	print([t['name'] for t in asyncio.run(MCPToolClient().list_tools())])"

demo: ## Run the five demo scenarios end to end
	./scripts/demo.sh

frontend-build: ## Type-check and build the frontend
	docker run --rm -v "$(PWD)/frontend":/app -w /app node:22-alpine \
		sh -c "npm install --silent && npm run typecheck && npm run build"

tf-validate: ## Validate the Terraform configuration
	docker run --rm -v "$(PWD)/terraform":/tf -w /tf --entrypoint sh hashicorp/terraform:1.9 \
		-c "terraform init -backend=false >/dev/null && terraform validate && terraform fmt -check -recursive"

clean: ## Remove caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
