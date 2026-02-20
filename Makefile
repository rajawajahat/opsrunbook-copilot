AWS_VAULT_PROFILE ?= opsrunbook-dev

# ── Development ───────────────────────────────────────────────────

api-dev:
	cd services/api && uvicorn src.app:app --reload --port 8000

# ── Testing ───────────────────────────────────────────────────────

test:
	python -m pytest tests/ -q --tb=short

test-verbose:
	python -m pytest tests/ -v

smoke:
	./scripts/smoke_it5.sh

# ── Linting ───────────────────────────────────────────────────────

lint:
	ruff check services/ packages/ tests/ --fix --show-fixes

format:
	ruff format services/ packages/ tests/

format-check:
	ruff format --check services/ packages/ tests/

# ── Terraform ─────────────────────────────────────────────────────

tf-plan-dev:
	aws-vault exec $(AWS_VAULT_PROFILE) -- sh -c 'cd infra/terraform/envs/dev && terraform init && terraform plan'

tf-dev:
	aws-vault exec $(AWS_VAULT_PROFILE) -- sh -c 'cd infra/terraform/envs/dev && terraform init && terraform apply'

tf-destroy-dev:
	aws-vault exec $(AWS_VAULT_PROFILE) -- sh -c 'cd infra/terraform/envs/dev && terraform init && terraform destroy'

tf-output-dev:
	aws-vault exec $(AWS_VAULT_PROFILE) -- sh -c 'cd infra/terraform/envs/dev && terraform output -json'

# ── CI pipeline (lint + test) ─────────────────────────────────────

ci: lint test

.PHONY: api-dev test test-verbose smoke lint format format-check tf-plan-dev tf-dev tf-destroy-dev tf-output-dev ci
