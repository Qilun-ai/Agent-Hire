.DEFAULT_GOAL := help
SHELL := /bin/bash

CAREERS := services/careers-scraper
AGENT  := apps/agent
WEB    := apps/web
API    := apps/api-rs
EXT    := apps/extension

# Ports. APP_PORT is the agent (the front door): it serves the web UI (apps/web/dist) and the chat API,
# and forwards /api, /ws and /bootstrap to the Rust API on API_INTERNAL_PORT.
APP_PORT          ?= 8000
API_INTERNAL_PORT ?= 8010
COMPANY_SCRAPER_HOME ?= $(abspath ../company-careers-scraper)

.PHONY: help
help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── install ───────────────────────────────────────────────────────────────────
.PHONY: setup install update
setup: ## one-command install: toolchains, deps, builds
	./scripts/install.sh

install: ## install every workspace's deps
	cd $(CAREERS) && uv sync
	cd $(API) && cargo fetch --locked
	cd $(WEB) && npm install
	cd $(AGENT) && npm install
	cd $(EXT) && npm install

update: ## update the installed app to the latest source and restart it
	./scripts/update.sh $(ARGS)

# ── run ───────────────────────────────────────────────────────────────────────
.PHONY: up down api agent web web-dev ext-build

up: ## start everything (API, then the agent serving the web UI on :8000) and wait until each is ready
	./scripts/start.sh

down: ## stop everything `make up` started
	./scripts/start.sh --stop

api: ## run the internal API alone on API_INTERNAL_PORT
	cd $(API) && cargo run --release -p agent-hire-server -- --port $(API_INTERNAL_PORT)

web: ## build the web UI into apps/web/dist (the agent serves it)
	cd $(WEB) && npm run build

web-dev: ## web UI with hot reload on :5173, against the app running on APP_PORT
	cd $(WEB) && APP_PORT=$(APP_PORT) npm run dev

agent: ## run the agent (front door) alone; needs `make api` and the same AGENT_HIRE_AGENT_SECRET in both
	cd $(AGENT) && npm run build && APP_PORT=$(APP_PORT) API_INTERNAL_PORT=$(API_INTERNAL_PORT) node dist/main.js

ext-build: ## build the MV3 extension into apps/extension/dist
	cd $(EXT) && npm run build

# ── scraping ──────────────────────────────────────────────────────────────────
# Scraping runs in the background from the app (Jobs tab or the agent). These targets help operators.
.PHONY: vendor-careers
vendor-careers: ## refresh services/careers-scraper from a company-careers-scraper checkout
	rsync -a --delete --exclude __pycache__ $(COMPANY_SCRAPER_HOME)/src/career_scraper $(CAREERS)/src/
	cp $(COMPANY_SCRAPER_HOME)/pyproject.toml $(COMPANY_SCRAPER_HOME)/uv.lock $(CAREERS)/
	rm -rf $(CAREERS)/companies && mkdir -p $(CAREERS)/companies
	cd $(COMPANY_SCRAPER_HOME)/companies && for d in */; do [ -f "$$d/filters.json" ] && mkdir -p "$(abspath $(CAREERS))/companies/$$d" && cp "$$d/filters.json" "$(abspath $(CAREERS))/companies/$$d"; done; true
	python3 scripts/vendor_careers_catalog.py $(COMPANY_SCRAPER_HOME) $(CAREERS)/companies.json

# ── tests ─────────────────────────────────────────────────────────────────────
.PHONY: test test-api test-agent test-web test-ext lint typecheck build

# `test` runs typecheck and the production build everywhere, not just the unit suites.
#
# vitest transpiles without typechecking, so a type error in a test file passed `npm test` and only
# surfaced when `npm run build` ran tsc — after the suite had already reported green. Anything the
# release build would reject has to fail here too, or "tests pass" means less than it appears to.
test: test-api test-agent test-web test-ext build ## every suite + typecheck + build

test-api:
	cd $(API) && cargo test --workspace --exclude agent-hire-contract
	cd $(API) && cargo build -p agent-hire-server
	cd $(API) && CONTRACT_BACKEND=rust CONTRACT_MAX_BACKENDS=2 cargo test -p agent-hire-contract --test contract

test-agent: ## agent unit tests (+ the live flow test when an API is reachable on AGENT_HIRE_API_URL)
	cd $(AGENT) && npm run typecheck && npm test

test-web:
	cd $(WEB) && npm run typecheck && npm test

test-ext:
	cd $(EXT) && npx tsc --noEmit && npm test

typecheck: ## typecheck every TypeScript workspace
	cd $(WEB) && npm run typecheck
	cd $(AGENT) && npm run typecheck
	cd $(EXT) && npx tsc --noEmit

build: ## production build of every bundle — catches what unit tests cannot
	cd $(API) && cargo build --release --target-dir target -p agent-hire-server
	cd $(WEB) && npm run build
	cd $(AGENT) && npm run build
	cd $(EXT) && npm run build

lint: ## Clippy (the API)
	cd $(API) && cargo clippy --workspace --all-targets -- -D warnings

# ── reclaiming disk ───────────────────────────────────────────────────────────
#
# This project is a heavy disk user: several workspaces of dependencies and a browser engine for the
# anti-detect scrapers. On a machine whose root partition is small, the package-manager caches are
# what fill it — not the checkout.
#
# Check where the space actually is before deleting anything:
#     df -h /                       # is the root disk the one under pressure?
#     du -sh ~/.cache/* ~/.npm      # usually the answer

.PHONY: clean clean-cache clean-all
clean: ## remove build output (safe; regenerated by `make build`)
	rm -rf $(EXT)/dist $(AGENT)/dist
	cd $(API) && cargo clean --target-dir target
	find $(CAREERS) -type d -name __pycache__ -not -path './*/node_modules/*' -prune -exec rm -rf {} + 2>/dev/null || true
	find $(CAREERS) -type d -name .pytest_cache -not -path './*/node_modules/*' -prune -exec rm -rf {} + 2>/dev/null || true

clean-cache: ## reclaim shared tool caches (safe; re-downloads on next install)
	-uv cache clean
	-npm cache clean --force

# Deliberately NOT part of `clean-all`: the job index, chats, and your own app data.
clean-all: clean clean-cache ## build output + caches, but never scraper or app data
	rm -rf $(EXT)/node_modules $(AGENT)/node_modules
	rm -rf $(CAREERS)/.venv
	@echo "run 'make install' to restore"
