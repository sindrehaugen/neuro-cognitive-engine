# TriMCP v1.0 — Infrastructure Orchestration
#
# Standard workflows for developers and operators.

.PHONY: help up down restart status logs clean build verify typecheck lint fmt fmt-check ruff

help:
	@echo "TriMCP v1.0 — Infrastructure Orchestration"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@echo "Targets:"
	@echo "  up       Start the complete v1.0 stack (Caddy, Admin, Workers, Tri-Stack)"
	@echo "  down     Stop all services and keep data"
	@echo "  restart  Restart the application services (worker, admin, a2a, webhooks)"
	@echo "  status   Show container health and status"
	@echo "  logs     Tail application logs"
	@echo "  build    Force rebuild of application images"
	@echo "  clean    Stop services and REMOVE all data volumes (CAUTION)"
	@echo "  verify   Run the v1.0 launch verification script"
	@echo "  typecheck  Run mypy static type checking on trimcp/ (strict mode)"
	@echo "  lint     Run ruff check + ruff format --check (same gate as CI)"
	@echo "  fmt      Apply ruff formatter to the repo"
	@echo "  fmt-check  Ruff format in check-only mode"
	@echo "  ruff      Alias for lint"
	@echo ""
	@echo "Local Mode:"
	@echo "  local-up     Start only the Tri-Stack databases (for host-run development)"
	@echo "  local-down   Stop local databases"

up:
	python scripts/bootstrap-compose-secrets.py
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart worker admin a2a webhook-receiver cron

status:
	docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}\t{{.Ports}}"

logs:
	docker compose logs -f worker admin a2a webhook-receiver cron

build:
	docker compose build --no-cache

clean:
	docker compose down -v

verify:
	python verify_v1_launch.py

typecheck:
	mypy nce/

lint:
	ruff check .
	ruff format --check .

fmt:
	ruff format .

fmt-check:
	ruff format --check .

ruff: lint

lockfile:
	python scripts/compile_requirements.py

# Local development targets
local-up:
	docker compose -f docker-compose.local.yml up -d

local-down:
	docker compose -f docker-compose.local.yml down
