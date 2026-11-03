SHELL := /bin/bash
.DEFAULT_GOAL := help

# Pinned binaries from `make tools` (.bin/) first; PATH only as a fallback.
tool = $(shell if [ -x .bin/$(1) ]; then echo .bin/$(1); else command -v $(1) || echo .bin/$(1); fi)
ACTIONLINT := $(call tool,actionlint)
SHELLCHECK := $(call tool,shellcheck)
D2 := $(call tool,d2)
HELM := $(call tool,helm)
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else command -v python3; fi)
export D2

CHAOS_OUT ?= evidence/cluster
UP_DOWN_MSG := not used; see docs/runbook.md for start, approve, recover and teardown

.PHONY: all help tools lint test eval demo diagram chart chaos up down

all: lint test eval diagram ## Run lint, tests, golden eval and diagram

help: ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z]+:.*## / {printf "  %-8s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

tools: ## Install pinned tool binaries into .bin/ (sha256-verified)
	scripts/tools.sh d2 actionlint gitleaks trivy

lint: ## actionlint, shellcheck, d2 validate, hygiene checks
	$(ACTIONLINT)
	$(SHELLCHECK) scripts/*.sh
	$(D2) validate docs/architecture.d2
	scripts/check-hygiene.sh

test: ## Unit and regression tests + hygiene checks
	$(PYTHON) -m pytest -q tests
	scripts/check-hygiene.sh

eval: ## Run all golden fixtures; fail on a regression or missing case
	$(PYTHON) -m agent_hardening_kit eval --out .cache/eval

demo: ## Run a free, local tenant-scoped fixture query
	$(PYTHON) -m agent_hardening_kit demo

diagram: ## Render docs/architecture.d2 to docs/img/architecture.{svg,png}
	scripts/render-diagram.sh

chart: ## Helm lint/template plus rendered-manifest policy check (no cluster)
	@mkdir -p .cache
	$(HELM) lint deploy/chart --strict
	$(HELM) template ahk deploy/chart --namespace ahk > .cache/rendered.yaml
	$(PYTHON) scripts/check_manifests.py .cache/rendered.yaml --local-image ahk-worker:local

chaos: ## Owned kind cluster: deploy, kill the worker pod, verify recovery, always tear down
	$(PYTHON) scripts/check_cluster.py --out $(CHAOS_OUT)

up: ## Bring up the local environment
	@echo "$(UP_DOWN_MSG)"

down: ## Tear down the local environment
	@echo "$(UP_DOWN_MSG)"
