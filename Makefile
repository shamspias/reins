# Reins — the stable command surface (CLAUDE.md §6). Keep these target names forever.
#
# The Python package lives in packages/python with its own pyproject.toml
# (ruff / mypy / pytest config). `make setup` builds a local venv there; every
# other target uses that venv's tools, falling back to whatever is on PATH so a
# single target still runs if `make setup` was skipped.

PKG  := packages/python
VBIN := $(PKG)/.venv/bin

# Prefer the project venv's tools. abspath keeps the path valid after `cd $(PKG)`.
PY     := $(if $(wildcard $(VBIN)/python),$(abspath $(VBIN)/python),python3)
RUFF   := $(if $(wildcard $(VBIN)/ruff),$(abspath $(VBIN)/ruff),ruff)
MYPY   := $(if $(wildcard $(VBIN)/mypy),$(abspath $(VBIN)/mypy),mypy)
PYTEST := $(if $(wildcard $(VBIN)/pytest),$(abspath $(VBIN)/pytest),pytest)

.DEFAULT_GOAL := help
.PHONY: help setup lint types test conformance check demo clean

help:
	@echo "Reins — make targets:"
	@echo "  setup        install deps + pre-commit (prefers uv, falls back to pip)"
	@echo "  lint         ruff check + format check"
	@echo "  types        mypy --strict on src/"
	@echo "  test         pytest (happy + unhappy paths)"
	@echo "  conformance  run spec/conformance (linter + golden cases)"
	@echo "  check        lint + types + test + conformance   <- the gate"
	@echo "  demo         run the flagship example end-to-end"
	@echo "  clean        remove the local venv and caches"

setup:
	@if command -v uv >/dev/null 2>&1; then \
		echo ">> setup: using uv"; \
		uv venv $(PKG)/.venv >/dev/null; \
		uv pip install --python $(PKG)/.venv/bin/python -e "$(PKG)[dev]"; \
	else \
		echo ">> setup: using python venv + pip"; \
		test -d $(PKG)/.venv || python3 -m venv $(PKG)/.venv; \
		$(PKG)/.venv/bin/python -m pip install --quiet --upgrade pip; \
		$(PKG)/.venv/bin/python -m pip install -e "$(PKG)[dev]"; \
	fi
	@$(PKG)/.venv/bin/pre-commit install >/dev/null 2>&1 \
		&& echo ">> pre-commit hook installed" \
		|| echo ">> note: pre-commit hook not installed (needs a git repo + pre-commit)"

lint:
	cd $(PKG) && $(RUFF) check src tests
	cd $(PKG) && $(RUFF) format --check src tests

types:
	cd $(PKG) && $(MYPY) src

test:
	cd $(PKG) && $(PYTEST)

# The full conformance harness (runner + golden + linter cases) is delivered in
# M0.2; until then there are zero cases, so this target is an honest no-op.
conformance:
	@echo ">> conformance: harness lands in M0.2; 0 cases registered — nothing to run"

check: lint types test conformance
	@echo ">> check: all green"

# The flagship example (examples/fastapi_sqlalchemy) is delivered in M4.5.
demo:
	@echo ">> demo: the flagship example arrives in M4.5 (examples/fastapi_sqlalchemy)"

clean:
	rm -rf $(PKG)/.venv
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '.*_cache' -prune -exec rm -rf {} +
