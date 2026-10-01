# HOST_DATA_DIR must be the *absolute host path* of ./data so the worker can
# mount per-job dirs into sibling build containers. Compute it here and export
# it so `docker compose` picks it up via ${HOST_DATA_DIR} in compose.yaml.
HOST_DATA_DIR := $(CURDIR)/data
export HOST_DATA_DIR

# Job dirs older than this (minutes) are deleted by `make prune-jobs`. Once a
# job's Redis record expires (JOB_TTL, default 24h) the API 404s on it, so its
# files are unreachable; the default leaves an hour of slack past that. The
# worker already prunes expired job dirs after each build; this is for when
# no builds have run lately or JOB_TTL was lowered.
JOB_MAX_AGE_MIN ?= 1500

.PHONY: up down logs test pull-real warm-image update-warm-image disk-usage prune-images prune-jobs clean

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

disk-usage:    ## Show what's using disk: filesystem, Docker, job dirs
	df -h /
	docker system df
	du -sh $(HOST_DATA_DIR)/jobs 2>/dev/null || true

prune-images:  ## Remove stopped containers, untagged images, and build cache (keeps :warm, :warm-previous, volumes)
	docker container prune -f
	docker image prune -f
	docker builder prune -f

prune-jobs:    ## Delete job dirs older than JOB_MAX_AGE_MIN (root-owned, so removed via a container)
	docker run --rm -v $(HOST_DATA_DIR)/jobs:/jobs alpine \
		find /jobs -mindepth 1 -maxdepth 1 -type d -mmin +$(JOB_MAX_AGE_MIN) -exec rm -rf {} +

clean: prune-images prune-jobs ## Reclaim disk: prune-images + prune-jobs
