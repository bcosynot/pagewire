# Convenience targets. The spike kit is stdlib-only, so nothing has to be installed
# to run it; the relay's own targets arrive with the relay's code.

PORT ?= 8799
BOOK ?=
DEST ?=
TOKEN_FILE := spike/plugin/token.json
VENV := .venv

.DEFAULT_GOAL := help
.PHONY: help spike ip sd token test venv clean

help: ## Show this list
	@echo "targets:"
	@grep -hE '^[a-z]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | sed 's/^/  /'
	@echo
	@echo "variables: PORT=$(PORT) (listener port), BOOK=<path> (also verify the hash)"

# The LAN address the device has to reach. The device joins your home network for the
# sleep-entry drain, so a 0.0.0.0 bind is not something it can use — it needs a real
# address on the same network, which is what this prints.
ip: ## Print the LAN address the listener should advertise
	@$(MAKE) --no-print-directory -s _ip

_ip:
	@ip=$$(ip route get 1.1.1.1 2>/dev/null | awk '{print $$7; exit}'); \
	if [ -z "$$ip" ]; then ip=$$(hostname -I 2>/dev/null | awk '{print $$1}'); fi; \
	if [ -z "$$ip" ]; then ip=0.0.0.0; fi; \
	echo "$$ip"

token: $(TOKEN_FILE) ## Ensure a local listener token exists
	@echo "token file: $(TOKEN_FILE)"

$(TOKEN_FILE):
	@mkdir -p $(dir $@)
	@python3 -c "import json,secrets; json.dump({'_comment': 'Shared secret between this listener and the token.json on the SD card. Gitignored.', 'token': secrets.token_urlsafe(24)}, open('$(TOKEN_FILE)','w'), indent=2)"
	@echo "created $(TOKEN_FILE) with a fresh token"

spike: $(TOKEN_FILE) ## Run the capture listener and print the address it is reachable on
	@ip=$$(ip route get 1.1.1.1 2>/dev/null | awk '{print $$7; exit}'); \
	if [ -z "$$ip" ]; then ip=$$(hostname -I 2>/dev/null | awk '{print $$1}'); fi; \
	if [ -z "$$ip" ]; then ip=0.0.0.0; fi; \
	token=$$(python3 -c "import json; print(json.load(open('$(TOKEN_FILE)'))['token'])"); \
	echo ""; \
	echo "  listening on   http://0.0.0.0:$(PORT)  (all interfaces)"; \
	echo "  device reaches http://$$ip:$(PORT)/events/session"; \
	echo "  config.json    {\"server\": \"http://$$ip:$(PORT)\"}"; \
	echo "  token.json     {\"token\": \"$$token\"}"; \
	echo ""; \
	echo "  put both files in /.crosspoint/plugins/pagewire/ on the SD card."; \
	echo "  capture goes to spike/events.jsonl"; \
	if [ "$$ip" = "0.0.0.0" ]; then \
		echo ""; \
		echo "  warning: no LAN address found, so the device cannot reach this host."; \
	fi; \
	echo ""; \
	exec python3 spike/listener.py --host 0.0.0.0 --port $(PORT) --token "$$token" $(if $(BOOK),--book $(BOOK))

sd: $(TOKEN_FILE) ## Copy the plugin folder onto a mounted SD card (DEST=/path/to/card)
	@if [ -z "$(DEST)" ]; then \
		echo "usage: make sd DEST=/path/to/card" >&2; \
		echo "  the mounted card root, e.g. /Volumes/CROSSPOINT or /run/media/\$$USER/CROSSPOINT" >&2; \
		exit 2; \
	fi
	@if [ ! -d "$(DEST)" ]; then echo "no such directory: $(DEST)" >&2; exit 2; fi
	@ip=$$(ip route get 1.1.1.1 2>/dev/null | awk '{print $$7; exit}'); \
	if [ -z "$$ip" ]; then ip=$$(hostname -I 2>/dev/null | awk '{print $$1}'); fi; \
	if [ -z "$$ip" ]; then echo "warning: no LAN address found; fix config.json on the card by hand" >&2; ip=CHANGE_ME; fi; \
	mkdir -p "$(DEST)/plugins/pagewire"; \
	cp spike/plugin/manifest.json spike/plugin/device.json spike/plugin/README.md "$(DEST)/plugins/pagewire/"; \
	cp $(TOKEN_FILE) "$(DEST)/plugins/pagewire/token.json"; \
	printf '{"server": "http://%s:%s"}\n' "$$ip" "$(PORT)" > "$(DEST)/plugins/pagewire/config.json"; \
	echo "staged:"; ls -1 "$(DEST)/plugins/pagewire" | sed 's/^/  /'; \
	echo; echo "server set to http://$$ip:$(PORT)"; \
	echo "the token on the card is the same one 'make spike' will use"

venv: ## Create .venv and install the package with its dev extras
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -e '.[dev]'

test: ## Run the tests (uses .venv when it exists)
	@if [ -x "$(VENV)/bin/pytest" ]; then $(VENV)/bin/pytest; else python3 -m pytest; fi

clean: ## Remove caches and the local capture file
	@rm -rf .pytest_cache src/*.egg-info
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@rm -f spike/events.jsonl
	@echo "cleaned (kept $(TOKEN_FILE) and $(VENV))"
