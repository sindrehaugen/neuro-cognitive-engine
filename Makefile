# NCE v3.5.4 — Infrastructure Orchestration
#
# Standard workflows for developers and operators.

.PHONY: help up down up-all down-all mtls-certs up-mtls restart health status logs clean build verify lockfile backup restore-drill verify-day2 typecheck lint fmt fmt-check ruff linux-shim

help:
	@echo "NCE v3.5.4 — Infrastructure Orchestration"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@echo "Targets:"
	@echo "  up            Start the complete NCE stack (Caddy, Admin, Workers, Quad-DB)"
	@echo "  up-mtls       Start the NCE stack with Caddy <-> Admin mutual TLS overlay"
	@echo "  mtls-certs    Generate or renew internal Caddy <-> Admin mTLS certificates"
	@echo "  down          Stop all services and keep data"
	@echo "  restart       Restart the application services (worker, admin, a2a, webhook-receiver)"
	@echo "  status        Show container health and status"
	@echo "  logs          Tail application logs"
	@echo "  build         Force rebuild of application images"
	@echo "  linux-shim    Build reproducible nce-launch Linux binaries (amd64 + arm64)"
	@echo "  clean         Stop services and REMOVE all data volumes (CAUTION)"
	@echo "  verify        Run the launch verification script"
	@echo "  backup        Run a T_cut watermark backup across all 4 stores (Compose or local)"
	@echo "  restore-drill Run an isolated ephemeral restore drill and verification"
	@echo "  verify-day2   Run the 6-scenario Day-2 chaos & red-team verification suite"
	@echo "  typecheck     Run mypy static type checking on nce/ (strict mode)"
	@echo "  lint          Run ruff check + ruff format --check + docs integrity"
	@echo "  fmt           Apply ruff formatter to the repo"
	@echo "  fmt-check     Ruff format in check-only mode"
	@echo "  ruff          Alias for lint"
	@echo ""
	@echo "Local Mode:"
	@echo "  local-up     Start only the Quad-DB databases (for host-run development)"
	@echo "  local-down   Stop local databases"

up:
	python scripts/bootstrap-compose-secrets.py
	python scripts/stage-vertical-wheels.py --optional
	docker compose up -d --build
	@echo "[health] gating the deploy on the container verdict (Wave I-13)"
	python -m nce.deploy_health --timeout 300

down:
	docker compose down

NCE_EXTRA_COMPOSE_ARGS ?=

up-all:
	python scripts/bootstrap-compose-secrets.py
	python scripts/stage-vertical-wheels.py --optional
	docker compose -f docker-compose.yml -f ../nce-vertical-engines/docker-compose.verticals.yml $(NCE_EXTRA_COMPOSE_ARGS) up -d --build
	@echo "[health] gating the unified deploy on the container verdict (Wave I-13)"
	python -m nce.deploy_health --timeout 300

down-all:
	docker compose -f docker-compose.yml -f ../nce-vertical-engines/docker-compose.verticals.yml $(NCE_EXTRA_COMPOSE_ARGS) down

mtls-certs:
	python scripts/generate_internal_mtls.py --check-expiry-days 14 || python scripts/generate_internal_mtls.py

up-mtls: mtls-certs
	python scripts/bootstrap-compose-secrets.py
	python scripts/stage-vertical-wheels.py --optional
	docker compose -f docker-compose.yml -f docker-compose.mtls.yml $(NCE_EXTRA_COMPOSE_ARGS) up -d --build
	@echo "[health] gating the mTLS deploy on the container verdict (Wave I-13)"
	python -m nce.deploy_health --timeout 300

restart:
	docker compose restart worker admin a2a mcp-gateway webhook-receiver cron

health:
	python -m nce.deploy_health --once

status:
	docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}\t{{.Ports}}"

logs:
	docker compose logs -f worker admin a2a mcp-gateway webhook-receiver cron

build:
	python scripts/stage-vertical-wheels.py --optional
	docker compose build --no-cache

clean:
	docker compose down -v

verify:
	python verify_v1_launch.py

NCE_OPS_SERVICE ?= cron
NCE_VERIFY_DAY2_SERVICE ?= admin
NCE_OPS_LOCAL ?= 0
NCE_BACKUP_ID ?=
NCE_BACKUP_ARGS ?=
NCE_DRILL_ARGS ?=
NCE_VERIFY_DAY2_ARGS ?=

backup:
	@python -c "import shutil, subprocess, sys; local, svc, args = sys.argv[1] == '1', sys.argv[2], sys.argv[3:]; running = (not local) and bool(shutil.which('docker')) and bool(subprocess.run(['docker', 'compose', 'ps', '--status', 'running', '-q', svc], capture_output=True, text=True).stdout.strip()); cmd = ['docker', 'compose', 'exec', '-T', svc, 'nce-ops', 'backup', *args] if running else ([shutil.which('nce-ops')] if shutil.which('nce-ops') else [sys.executable, '-m', 'nce.ops.cli']) + ['backup', *args]; raise SystemExit(subprocess.call(cmd))" "$(NCE_OPS_LOCAL)" "$(NCE_OPS_SERVICE)" $(NCE_BACKUP_ARGS)

restore-drill:
	@python -c "import shutil, subprocess, sys; local, svc, args = sys.argv[1] == '1', sys.argv[2], sys.argv[3:]; running = (not local) and bool(shutil.which('docker')) and bool(subprocess.run(['docker', 'compose', 'ps', '--status', 'running', '-q', svc], capture_output=True, text=True).stdout.strip()); cmd = ['docker', 'compose', 'exec', '-T', svc, 'nce-ops', 'restore-drill', *args] if running else ([shutil.which('nce-ops')] if shutil.which('nce-ops') else [sys.executable, '-m', 'nce.ops.cli']) + ['restore-drill', *args]; raise SystemExit(subprocess.call(cmd))" "$(NCE_OPS_LOCAL)" "$(NCE_OPS_SERVICE)" $(if $(strip $(NCE_BACKUP_ID)),--backup-id $(NCE_BACKUP_ID)) $(NCE_DRILL_ARGS)

verify-day2:
	@python -c "import pathlib, shutil, subprocess, sys; local, svc, args = sys.argv[1] == '1', sys.argv[2], sys.argv[3:]; running = (not local) and bool(shutil.which('docker')) and bool(subprocess.run(['docker', 'compose', 'ps', '--status', 'running', '-q', svc], capture_output=True, text=True).stdout.strip()); raise SystemExit(subprocess.run(['docker', 'compose', 'exec', '-T', svc, 'python', '-', *args], input=pathlib.Path('scripts/verify_day2_chaos_hardening.py').read_bytes()).returncode if running else subprocess.call([sys.executable, 'scripts/verify_day2_chaos_hardening.py', *args]))" "$(NCE_OPS_LOCAL)" "$(NCE_VERIFY_DAY2_SERVICE)" $(NCE_VERIFY_DAY2_ARGS)

typecheck:
	mypy nce/

lint:
	ruff check .
	ruff format --check .
	python scripts/check_docs_integrity.py

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

linux-shim:
	bash build/linux/build.sh

