PKG_DIR := galaxis-core
SRC_DIR := $(PKG_DIR)/galaxis_core
TEST_DIR := $(PKG_DIR)/test

ROS_DISTRO ?= jazzy
GIT_BRANCH_NAME ?= $(shell git rev-parse --abbrev-ref HEAD)
DOCKER_IMAGE := galaxis-core
DOCKER_TAG := latest

GIT_HASH ?= $(shell git rev-parse --short HEAD)
GIT_BRANCH ?= $(GIT_BRANCH_NAME)

DOCKER_BUILD_ARGS := \
	--build-arg ROS_DISTRO=$(ROS_DISTRO) \
	--build-arg GIT_HASH=$(GIT_HASH) \
	--build-arg GIT_BRANCH=$(GIT_BRANCH)

# ── Help ────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

# ── Lint & syntax ────────────────────────────────────────────────

.PHONY: lint
DS_SRC := data-backend/src/data_backend
DS_TEST := data-backend/tests
# Newer ROS nodes covered by lint/fmt (legacy nodes excluded)
LINT_NODES := $(SRC_DIR)/storage_node.py \
	$(SRC_DIR)/ros_log_bridge.py $(SRC_DIR)/recording_gate.py \
	$(SRC_DIR)/recording_gate_node.py $(SRC_DIR)/sentry_setup.py

lint: ## Run ruff linter and formatter check
	uvx ruff check $(LINT_NODES) $(TEST_DIR) $(DS_SRC) $(DS_TEST) lock_server/
	uvx ruff format --check $(LINT_NODES) $(TEST_DIR) $(DS_SRC) $(DS_TEST) lock_server/

fmt: ## Auto-fix ruff formatting
	uvx ruff format $(LINT_NODES) $(TEST_DIR) $(DS_SRC) $(DS_TEST) lock_server/

# ── Unit tests ──────────────────────────────────────────────────

.PHONY: test test-gc
test: ## Run unit tests with pytest
	cd $(PKG_DIR) && uvx --with pyyaml --with "airoa_metadata @ git+https://github.com/airoa-org/airoa-metadata.git@development" --with "data-backend @ file://$(CURDIR)/data-backend" pytest test/ --ignore=test/test_flake8.py --ignore=test/test_pep257.py --ignore=test/test_copyright.py

test-gc: ## Run data-backend unit + scenario tests
	uvx --with pytest --with minio --with sentry-sdk --with "data-backend @ file://$(CURDIR)/data-backend" pytest data-backend/tests/ -v -m "not integration"

# ── Docker ───────────────────────────────────────────────────────

.PHONY: docker
docker: ## Build the Docker image (galaxis-core:latest)
	docker build $(DOCKER_BUILD_ARGS) \
		-f docker/Dockerfile \
		-t $(DOCKER_IMAGE):$(DOCKER_TAG) \
		.

# ── Integration tests ───────────────────────────────────────────

.PHONY: test-storage test-gate test-integration

test-storage: ## Run S3 storage integration tests (starts MinIO, runs, cleans up)
	@docker compose -f docker-compose.test.yml down -v 2>/dev/null || true
	@docker compose -f docker-compose.test.yml up -d
	@docker compose -f docker-compose.test.yml wait createbucket || true
	@rc=0; \
	uvx --with pytest --with minio --with sentry-sdk --with "data-backend @ file://$(CURDIR)/data-backend" \
		pytest data-backend/tests/test_integration_gc.py data-backend/tests/test_integration_storage.py -v -m integration || rc=$$?; \
	docker compose -f docker-compose.test.yml down -v; \
	exit $$rc

test-gate: ## Run ROS2 gate integration tests in Docker
	docker compose -f docker-compose.test-gate.yml run --build --rm gate-test
	@docker compose -f docker-compose.test-gate.yml down -v 2>/dev/null || true

test-integration: ## Run all integration tests (storage + gate)
	@rc=0; \
	$(MAKE) test-storage || rc=$$?; \
	$(MAKE) test-gate    || rc=$$?; \
	exit $$rc
