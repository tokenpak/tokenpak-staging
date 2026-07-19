# TokenPak Makefile
# Requires: Python 3.10+, pip
# Usage: make dev && make test

.DEFAULT_GOAL := help

# ── Configuration ─────────────────────────────────────────────────────────────
PYTHON      := python3
VENV        := .venv
VENV_BIN    := $(VENV)/bin
PIP         := $(VENV_BIN)/pip
PYTEST      := $(VENV_BIN)/pytest
RUFF        := $(VENV_BIN)/ruff
BUILD       := $(VENV_BIN)/python3 -m build
MKDOCS      := $(VENV_BIN)/mkdocs

# Detect OS for cross-platform compatibility
UNAME := $(shell uname -s)

# ── Phony targets ──────────────────────────────────────────────────────────────
.PHONY: help dev test lint format check build docs clean install hooks benchmark-headline

# ── Help ──────────────────────────────────────────────────────────────────────
help:  ## Show this help message
	@echo "TokenPak — available make targets:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── Development setup ─────────────────────────────────────────────────────────
dev: $(VENV)/bin/activate  ## Create venv and install tokenpak[dev] in editable mode

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -e ".[dev]"
	@echo ""
	@echo "✅  Dev environment ready. Activate with: source $(VENV)/bin/activate"
	@echo "    Then run: make test"

install:  ## Install tokenpak (non-editable, no dev extras)
	$(PYTHON) -m pip install .

# ── Testing ────────────────────────────────────────────────────────────────────
test:  ## Run full test suite
	$(PYTEST) tests/ -q --tb=short

test-quick:  ## Run quick audit subset (<30s, no live proxy needed)
	$(PYTEST) -m quick -q --tb=short

test-fast:  ## Run tests, stop on first failure
	$(PYTEST) tests/ -q --tb=short -x

test-cov:  ## Run tests with coverage report
	$(PYTEST) tests/ -q --tb=short \
		--cov=tokenpak \
		--cov-report=term-missing \
		--cov-report=html:htmlcov
	@echo "Coverage report: htmlcov/index.html"

test-chaos:  ## Run chaos & resilience tests (fault injection / failure-recovery)
	$(PYTEST) tests/chaos/ -m chaos -q --tb=short

benchmark-headline:  ## Run headline 30-50% claim benchmark (standard 21 §9.8 blocking)
	$(PYTEST) tests/benchmarks/test_headline_claim.py -v -s

# ── Linting & formatting ───────────────────────────────────────────────────────
lint:  ## Run ruff linter
	$(RUFF) check tokenpak/ tests/

format:  ## Run ruff formatter (auto-fix)
	$(RUFF) format tokenpak/ tests/

format-check:  ## Check formatting without making changes
	$(RUFF) format --check tokenpak/ tests/

check: lint format-check test  ## Run lint + format check + tests (CI gate)

# ── Build ──────────────────────────────────────────────────────────────────────
build:  ## Build source distribution and wheel
	$(VENV_BIN)/python3 -m pip install --quiet build
	$(VENV_BIN)/python3 -m build
	@echo ""
	@ls -lh dist/*.whl dist/*.tar.gz 2>/dev/null || true
	@echo "✅  Build complete. Artifacts in dist/"

# ── Docs ───────────────────────────────────────────────────────────────────────
docs:  ## Build MkDocs documentation site
	@if [ ! -f mkdocs.yml ]; then \
		echo "⚠️  mkdocs.yml not found — run: pip install mkdocs mkdocs-material"; \
		exit 1; \
	fi
	$(PIP) install --quiet "mkdocs>=1.5.0" "mkdocs-material>=9.5.0"
	$(VENV_BIN)/mkdocs build
	@echo "✅  Docs built in site/"

docs-serve:  ## Serve MkDocs documentation locally
	$(VENV_BIN)/mkdocs serve

# ── Hooks ─────────────────────────────────────────────────────────────────────
hooks:  ## Install pre-commit hooks
	$(PIP) install --quiet pre-commit
	$(VENV_BIN)/pre-commit install
	@echo "✅  Pre-commit hooks installed"

# ── Clean ─────────────────────────────────────────────────────────────────────
clean:  ## Remove build artifacts, caches, and dist/
	rm -rf dist/ build/ *.egg-info .eggs/
	rm -rf .pytest_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -not -path './$(VENV)/*' -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -not -path './$(VENV)/*' -delete 2>/dev/null || true
	find . -type f -name '*.pyo' -not -path './$(VENV)/*' -delete 2>/dev/null || true
	@echo "✅  Clean complete"

clean-all: clean  ## Remove everything including venv
	rm -rf $(VENV)
	@echo "✅  Full clean (including venv)"

# ── Release-Gate Trust Contract (Std 30, ratified 2026-05-09) ────────────────
.PHONY: api-snapshot api-snapshot-check api-snapshot-diff workflow-steps-snapshot \
        workflow-steps-check telemetry-snapshot telemetry-check taxonomy-check \
        deps-audit migration-multihop release-gate-snapshots release-gate-check \
        release-leak-check

api-snapshot:  ## Std 30 §7 (R7) — regenerate tokenpak/_snapshots/public-api.json
	$(PYTHON) scripts/release_gate/gen_api_snapshot.py

api-snapshot-check:  ## Std 30 §7 — fail if public-api.json drifts from current source
	$(PYTHON) scripts/release_gate/gen_api_snapshot.py --check

api-snapshot-diff:  ## Std 30 §6 (R6) + Std 21 §11 — diff snapshots between BASE and HEAD
	$(PYTHON) scripts/release_gate/api_snapshot_diff.py $(BASE) $(HEAD)

workflow-steps-snapshot:  ## Std 30 §13.3 (R11) + Std 21 §12 — regenerate workflow-steps.json
	$(PYTHON) scripts/release_gate/gen_workflow_steps.py

workflow-steps-check:  ## Std 30 §13.3 — fail if workflow-steps.json drifts
	$(PYTHON) scripts/release_gate/gen_workflow_steps.py --check

telemetry-snapshot:  ## Std 30 §7 — regenerate tokenpak/_snapshots/telemetry-schema.json
	$(PYTHON) scripts/release_gate/gen_telemetry_schema.py

telemetry-check:  ## Std 30 §7 — fail if telemetry-schema.json drifts (HOME-isolated; never reads live ~/.tokenpak DBs)
	HOME="$$(mktemp -d)" $(PYTHON) scripts/release_gate/gen_telemetry_schema.py --check

taxonomy-check:  ## Std 02 §13 + Std 30 §5 (R5) — every test has exactly one taxonomy marker
	$(PYTHON) scripts/release_gate/taxonomy_check.py

deps-audit:  ## Std 02 §14 + Std 30 §13.2 (R17) — uv lock --check + pip-audit + yanked-package scan
	@if command -v uv >/dev/null 2>&1; then uv lock --check; else echo "uv not installed; skipping uv lock --check (install uv to enable)"; fi
	$(PYTHON) -m pip install --quiet pip-audit
	$(PYTHON) -m pip_audit --strict --skip-editable

migration-multihop:  ## Std 30 §14.1 (R16) + Std 10 §E9 — run migrations from each of last 6 minor versions
	$(PYTHON) scripts/release_gate/migration_multihop.py

release-gate-snapshots: api-snapshot workflow-steps-snapshot telemetry-snapshot  ## Regenerate ALL release-gate snapshots
	@echo "✅  All release-gate snapshots regenerated"

release-gate-check: api-snapshot-check workflow-steps-check telemetry-check taxonomy-check  ## Validate ALL release-gate snapshots
	@echo "✅  All release-gate checks passed"

release-leak-check:  ## Full-tree public-leak scan of the built sdist + wheel (release gate)
	$(PYTHON) -m pip install --quiet build
	$(PYTHON) -m build
	$(PYTHON) scripts/release_gate/check_release_leaks.py --dist dist/

# ── Std 10 §5 Release Check (A1-A7 / B1 / B3 / C5 / C6) ──────────────────────
# `make release-check` automates the Std 10 §5 step-2 gate set. Mapping:
#   A1-A7 → ci-lint + test + api-snapshot-check + workflow-steps-check
#           + telemetry-check + taxonomy-check + release-gate-check
#           + migration-multihop
#           (per Std 10 §4; A1 is the EXACT CI ruff selection from
#           .github/workflows/ci.yml — deliberately NOT `make check`, whose
#           format-check step is CI-unenforced tooling drift handled in a
#           separate packet; gates with no standalone local target — A3 mypy,
#           A5 fresh-machine install, A6 bench — remain manual §5 checklist
#           items and are NOT silently claimed by this target)
#   B1    → audit                       (Std 10 B1)
#   B3    → marketing-filler-check      (Std 10 B3 / Constitution §8)
#   C5    → docs-check                  (Std 10 C5)
#   C6    → release-docs-pattern-check  (Std 10 C6)
# Ceiling: C9 / C10 / C16 and coverage are deliberately excluded — coverage is
# advisory per Std 66 §11.2; C9/C10/C16 run via the release master checklist.
.PHONY: ci-lint audit docs-check marketing-filler-check release-docs-pattern-check release-check

ci-lint:  ## Std 10 A1 — the EXACT CI ruff selection (.github/workflows/ci.yml), not `make check`
	$(RUFF) check tokenpak/ tests/ --select=E,F,W,I --ignore=E501,E701,E702,E402,E741,F841

audit:  ## Std 10 B1 — doc-drift audit (advisory) + strict dependency audit (blocking)
	@bash scripts/audit-docs.sh \
		|| echo "⚠️  audit-docs.sh findings above are ADVISORY (soft warning gate per its own header + CI continue-on-error); Std 10 B1 blocks only on Critical/High not accepted in Std 09 §6"
	$(VENV_BIN)/python -m pip install --quiet pip-audit
	$(VENV_BIN)/python -m pip_audit --strict .
	@echo "✅  B1 audit clean (no known vulnerabilities in declared dependency tree)"
# NOTE: `make deps-audit` cannot pass inside the editable dev venv: with
# --strict, pip-audit escalates the --skip-editable skip of the local tokenpak
# dist to an error. B1 therefore audits the declared dependency tree in
# project mode with the same tool and the same --strict severity floor.

docs-check:  ## Std 10 C5 — strict MkDocs build (links resolve) + CLI reference freshness
	$(PIP) install --quiet "mkdocs>=1.5.0" "mkdocs-material>=9.5.0"
	$(MKDOCS) build --strict
	PATH="$(abspath $(VENV_BIN)):$$PATH" bash scripts/check-cli-docs.sh

marketing-filler-check:  ## Std 10 B3 — no marketing filler in README/docs/site/dashboard
	@! git grep -n -i -E '\b(revolutionary|game-changing|cutting-edge|industry-leading|next-gen|best-in-class|simply|easily)\b' -- README.md docs site tokenpak/dashboard \
		|| { echo "❌  B3 FAIL: marketing filler found (Std 10 B3 / Constitution §8)"; exit 1; }
	@git grep -n -i -E '\bjust\b' -- README.md docs site tokenpak/dashboard \
		| sed 's/^/  B3 ADVISORY (\"just\"-as-qualifier requires human judgment): /' || true
	@echo "✅  B3 clean (all mechanically-checkable filler terms absent)"

# Public-baseline ref that defines "docs touched by this release" (Std 10 C6).
# The release workbench names the public remote `github`; ad-hoc clones may
# carry it as `public`. Override: make release-check RELEASE_BASE_REF=<ref>
RELEASE_BASE_REF ?= $(shell git rev-parse -q --verify github/main 2>/dev/null || git rev-parse -q --verify public/main 2>/dev/null)

release-docs-pattern-check:  ## Std 10 C6 — no TODO / "coming soon" / stale updated dates in release-touched docs
	@base='$(RELEASE_BASE_REF)'; \
	if [ -z "$$base" ]; then \
		echo "❌  C6: cannot resolve public baseline ref; run with RELEASE_BASE_REF=<public-main-ref>"; exit 1; fi; \
	echo "C6 baseline: $$base"; \
	files=$$(git diff --name-only "$$base"...HEAD -- docs README.md); \
	if [ -z "$$files" ]; then echo "✅  C6: no release-touched docs vs baseline"; exit 0; fi; \
	base_date=$$(git log -1 --format=%cs "$$base"); fail=0; \
	for f in $$files; do \
		[ -f "$$f" ] || continue; \
		if grep -n -E '\bTODO\b' "$$f" || grep -n -i 'coming soon' "$$f"; then \
			echo "❌  C6 FAIL: forbidden pattern in $$f"; fail=1; fi; \
		stale=$$(grep -o -i -E '(last +)?updated[:* ]+20[0-9]{2}-[0-9]{2}-[0-9]{2}' "$$f" | grep -o -E '20[0-9]{2}-[0-9]{2}-[0-9]{2}' | awk -v d="$$base_date" '$$0 < d' || true); \
		if [ -n "$$stale" ]; then \
			echo "❌  C6 FAIL: stale updated date(s) in $$f (predate baseline $$base_date): $$stale"; fail=1; fi; \
	done; \
	if [ $$fail -ne 0 ]; then exit 1; fi; \
	echo "✅  C6 clean over release-touched docs"

release-check: ci-lint test api-snapshot-check workflow-steps-check telemetry-check taxonomy-check release-gate-check migration-multihop audit marketing-filler-check docs-check release-docs-pattern-check  ## Std 10 §5 step 2 — automate gates A1-A7 / B1 / B3 / C5 / C6
	@echo "✅  release-check: A1-A7 / B1 / B3 / C5 / C6 gate targets all executed"
