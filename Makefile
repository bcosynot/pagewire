# Convenience targets. The spike kit is stdlib-only, so nothing has to be installed
# to run it; the relay's own targets arrive with the relay's code.

PORT ?= 8799
BOOK ?=
DEST ?=
# The base URL the card's `config.json` should carry — the WHOLE url, port included.
# Empty means "this machine", from `scripts/local-ip.sh`, which is right when the
# listener is the local `make spike`. Set it to aim a card at a DEPLOYED relay instead,
# so staging cannot silently write a laptop's address onto a card that then never
# reaches the relay:
#
#     make sd DEST=/path/to/card SERVER=http://192.168.0.112:8799
#
SERVER ?=
TOKEN_FILE := spike/plugin/token.json
VENV := .venv

.DEFAULT_GOAL := help
.PHONY: help spike ip sd token test venv clean

help: ## Show this list
	@echo "targets:"
	@grep -hE '^[a-z]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | sed 's/^/  /'
	@echo
	@echo "variables: PORT=$(PORT) (listener port), DEST=<card root>, SERVER=<base url> (blank = this machine), BOOK=<path>"

# The LAN address the device has to reach. Delegated to a script because the detection
# differs by OS (macOS has no `ip route`, Linux has no working `ipconfig`), and getting
# it wrong means the device cannot reach the listener at all.
LAN_IP = $(shell ./scripts/local-ip.sh 2>/dev/null)

ip: ## Print the LAN address the listener should advertise
	@if [ -n "$(LAN_IP)" ]; then echo "$(LAN_IP)"; else \
		echo "no LAN address found — is Wi-Fi/Ethernet up?" >&2; exit 1; \
	fi

token: $(TOKEN_FILE) ## Ensure a local listener token exists
	@echo "token file: $(TOKEN_FILE)"

$(TOKEN_FILE):
	@mkdir -p $(dir $@)
	@python3 -c "import json,secrets; json.dump({'_comment': 'Shared secret between this listener and the token.json on the SD card. Gitignored.', 'token': secrets.token_urlsafe(24)}, open('$(TOKEN_FILE)','w'), indent=2)"
	@echo ""
	@echo "!! created $(TOKEN_FILE) with a FRESH token"
	@echo "!! This token is the one the relay must hold. Any card or relay still"
	@echo "!! carrying the previous value will now answer 401 and drop every event."
	@echo "!! Set PAGEWIRE_TOKEN in the relay's environment file to this value, or"
	@echo "!! restore the old $(TOKEN_FILE) if that pairing was the intended one."
	@echo "!! (token.json is gitignored, so it does NOT travel with a clone or a worktree.)"
	@echo ""
	@python3 -c "import hashlib,json,sys; t=json.load(open('$(TOKEN_FILE)'))['token']; print('   sha256', hashlib.sha256(t.encode()).hexdigest())"


spike: $(TOKEN_FILE) ## Run the capture listener and print the address it is reachable on
	@ip="$(LAN_IP)"; \
	token=$$(python3 -c "import json; print(json.load(open('$(TOKEN_FILE)'))['token'])"); \
	echo ""; \
	echo "  listening on   http://0.0.0.0:$(PORT)  (all interfaces)"; \
	echo "  device reaches http://$$ip:$(PORT)/events/session"; \
	echo "  config.json    {\"server\": \"http://$$ip:$(PORT)\"}"; \
	echo "  token.json     {\"token\": \"$$token\"}"; \
	echo ""; \
	echo "  stage these on the card:  make sd DEST=/path/to/card"; \
	echo "  capture goes to spike/events.jsonl"; \
	if [ -n "$(SERVER)" ]; then \
		echo ""; \
		echo "  note: SERVER=$(SERVER) is set, so 'make sd' writes that into the card's"; \
		echo "        config.json instead of this machine's address."; \
	fi; \
	if [ -z "$$ip" ]; then \
		echo ""; \
		echo "  warning: no LAN address found, so the device cannot reach this host."; \
		echo "  bring the network up, then re-run 'make sd' so the card carries a real address."; \
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
	@base="$(SERVER)"; \
	if [ -z "$$base" ]; then base="$(LAN_IP)"; [ -n "$$base" ] && base="http://$$base:$(PORT)"; fi; \
	if [ -z "$$base" ]; then \
		echo "warning: no LAN address found and SERVER is unset; pass SERVER=<base url>" >&2; \
		base=CHANGE_ME; \
	fi; \
	base="$${base%/}"; \
	mkdir -p "$(DEST)/plugins/pagewire"; \
	cp spike/plugin/manifest.json spike/plugin/device.json spike/plugin/README.md "$(DEST)/plugins/pagewire/"; \
	cp $(TOKEN_FILE) "$(DEST)/plugins/pagewire/token.json"; \
	printf '{"server": "%s"}\n' "$$base" > "$(DEST)/plugins/pagewire/config.json"; \
	find "$(DEST)/plugins/pagewire" -name '._*' -delete; \
	echo "staged:"; ls -1 "$(DEST)/plugins/pagewire" | sed 's/^/  /'; \
	echo; echo "server set to $$base"; \
	echo "the token is $(TOKEN_FILE)'s — the relay must agree with it"; \
	python3 -c "import hashlib,json; t=json.load(open('$(TOKEN_FILE)'))['token']; print('token sha256', hashlib.sha256(t.encode()).hexdigest())"; \
	echo "  check it against the relay: grep '^PAGEWIRE_TOKEN=' <env file> | cut -d= -f2- | tr -d '\\n' | sha256sum"

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
