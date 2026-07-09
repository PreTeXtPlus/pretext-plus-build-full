# HOST_DATA_DIR must be the *absolute host path* of ./data so the worker can
# mount per-job dirs into sibling build containers. Compute it here and export
# it so `docker compose` picks it up via ${HOST_DATA_DIR} in compose.yaml.
HOST_DATA_DIR := $(CURDIR)/data
export HOST_DATA_DIR

.PHONY: up down logs test pull-real warm-image update-warm-image

up:            ## Build images and start the stack (detached)
	docker compose up --build -d

down:          ## Stop the stack
	docker compose down

logs:          ## Tail logs from all services
	docker compose logs -f

test:          ## Submit the sample project and poll until it finishes
	bash ./scripts/smoke_test.sh

pull-real:     ## Pre-pull the real PreTeXt image (~5GB) for real builds
	docker pull pretextbook/pretext-full

warm-image:    ## Build the "warm" image (bakes in PreTeXt's first-run setup)
	docker build -t pretext-plus-build:warm ./build-image

update-warm-image: ## Pull latest pretext-full, rebuild+smoke-test, promote on pass
	bash ./scripts/update_warm_image.sh
