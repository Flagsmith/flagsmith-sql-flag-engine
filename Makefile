.PHONY: install-packages
install-packages: ## Install all required packages
	uv sync

.PHONY: install-pre-commit
install-pre-commit: ## Install pre-commit hooks
	uv run prek install

.PHONY: install
install: install-packages install-pre-commit ## Ensure the environment is set up

.PHONY: lint
lint: ## Run linters (pre-commit hooks across the tree)
	uv run prek run --all-files

.PHONY: test
test: ## Run unit tests. Override scope with opts, e.g. `make test opts='-m parity'`
	uv run pytest $(opts)

.PHONY: typecheck
typecheck: ## Run mypy
	uv run mypy

.PHONY: help
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Available targets:"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
