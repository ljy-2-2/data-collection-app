MAKEFILE_DIR := $(patsubst %/,%,$(dir $(realpath $(lastword $(MAKEFILE_LIST)))))

ifneq (,$(wildcard $(MAKEFILE_DIR)/.env))
    include $(MAKEFILE_DIR)/.env
    export
endif

ROS_DISTRO ?= jazzy
GALAXIS_IMAGE := galaxis
GALAXIS_TAG ?= latest
GALAXIS_CORE_IMAGE := galaxis-core
GALAXIS_CORE_TAG ?= latest

GALAXIS_GIT_HASH := $(shell git -C $(MAKEFILE_DIR) rev-parse HEAD)
GALAXIS_GIT_BRANCH := $(shell git -C $(MAKEFILE_DIR) rev-parse --abbrev-ref HEAD)
GALAXIS_CORE_GIT_HASH := $(shell git -C $(MAKEFILE_DIR)/galaxis-core rev-parse HEAD)
GALAXIS_CORE_GIT_BRANCH := $(shell git -C $(MAKEFILE_DIR)/galaxis-core rev-parse --abbrev-ref HEAD)

.PHONY: help lint test test-config build-config pico-adb docker docker-galaxis docker-galaxis-core

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

# ── Lint & syntax ────────────────────────────────────────────────

lint: ## Run ruff linter and formatter check
	uvx ruff check $(MAKEFILE_DIR)/galaxis_bringup/ $(MAKEFILE_DIR)/footpedal_ros/ $(MAKEFILE_DIR)/galaxis_pico/
	uvx ruff format --check $(MAKEFILE_DIR)/galaxis_bringup/ $(MAKEFILE_DIR)/footpedal_ros/ $(MAKEFILE_DIR)/galaxis_pico/

# ── Tests ────────────────────────────────────────────────────────

test: ## Run unit tests with pytest
	cd $(MAKEFILE_DIR)/galaxis_bringup && uvx --with pyyaml pytest test/
	cd $(MAKEFILE_DIR)/galaxis_pico/galaxis_pico_bridge && uvx pytest test/

test-config: ## Run only build_runtime_configs tests (subset of test)
	cd $(MAKEFILE_DIR)/galaxis_bringup && uvx --with pyyaml pytest test/test_build_runtime_configs.py -v

# ── Config ───────────────────────────────────────────────────────

build-config: ## Generate config/_runtime/single/ from galaxis_config.yaml
	python3 $(MAKEFILE_DIR)/galaxis_bringup/tools/build_runtime_configs.py --single-config $(MAKEFILE_DIR)/galaxis_bringup/config/galaxis_config.yaml --profile-file /etc/galaxis/collection_profile.yaml

pico-adb: ## Create the USB ADB reverse tunnel for the Galaxis PICO client
	bash $(MAKEFILE_DIR)/tools/galaxis_pico_adb.sh $${PICO_ADB_SERIAL:-}

# ── Docker ───────────────────────────────────────────────────────

docker-galaxis-core: ## Build galaxis-core image
	docker build \
		--build-arg ROS_DISTRO=$(ROS_DISTRO) \
		--build-arg GIT_HASH=$(GALAXIS_CORE_GIT_HASH) \
		--build-arg GIT_BRANCH=$(GALAXIS_CORE_GIT_BRANCH) \
		-f $(MAKEFILE_DIR)/galaxis-core/docker/Dockerfile \
		-t $(GALAXIS_CORE_IMAGE):$(GALAXIS_CORE_TAG) \
		$(MAKEFILE_DIR)/galaxis-core

docker-galaxis: ## Build galaxis image
	docker build \
		--build-arg ROS_DISTRO=$(ROS_DISTRO) \
		--build-arg GALAXIS_GIT_HASH=$(GALAXIS_GIT_HASH) \
		--build-arg GALAXIS_GIT_BRANCH=$(GALAXIS_GIT_BRANCH) \
		-f $(MAKEFILE_DIR)/docker/Dockerfile \
		-t $(GALAXIS_IMAGE):$(GALAXIS_TAG) \
		$(MAKEFILE_DIR)

docker: build-config docker-galaxis-core docker-galaxis ## Build all Docker images (generates config/_runtime first)
