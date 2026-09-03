# Convenience wrapper over docker compose. Every target here is a short alias
# for a command spelled out in the README -- nothing hidden.

COMPOSE := docker compose

.PHONY: help up down logs ps build seed simulate failover clean rebuild

help:
	@echo "Fleet Policy Manager"
	@echo "  make up          start the whole platform"
	@echo "  make seed        create and publish a starter policy per group"
	@echo "  make simulate    run 500 virtual devices for 2 minutes"
	@echo "  make failover    run the Compliance Service failover demonstration"
	@echo "  make logs        tail logs from every service"
	@echo "  make down        stop the platform (keeps data)"
	@echo "  make clean       stop the platform and delete all data volumes"
	@echo ""
	@echo "  Dashboard:    http://localhost:8090"
	@echo "  API gateway:  http://localhost:8000/docs"

up:
	$(COMPOSE) up -d --build
	@echo "\nDashboard  -> http://localhost:8090"
	@echo "API docs   -> http://localhost:8000/docs"

build:
	$(COMPOSE) build

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down -v

rebuild: clean up

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail 40

seed:
	$(COMPOSE) run --rm --no-deps --entrypoint python simulator seed_policies.py --gateway http://gateway:8000

# Runs the simulator container against the in-network gateway. Override
# device count or duration with:  make simulate ARGS="--devices 1000 --duration 300"
ARGS ?= --devices 500 --duration 120
simulate:
	$(COMPOSE) run --rm simulator $(ARGS)

# The failover script drives docker compose itself, so it runs on the host.
# It needs a load source in parallel -- start `make simulate` in another shell.
failover:
	python3 tools/ha_failover_test.py --victim compliance-2
