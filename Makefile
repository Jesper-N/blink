PYTHON_BIN ?= python3.12
VENV ?= .venv
BINARY ?= blink-detector
MODEL ?= models/face_landmarker.task
MODEL_URL ?= https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task

.DELETE_ON_ERROR:
.PHONY: build setup run clean

build: $(BINARY)

setup: $(VENV)/.requirements-installed $(MODEL) $(BINARY)

run: setup
	./$(BINARY)

$(BINARY): blink_detector.py
	printf '%s\n' \
		'#!/bin/sh' \
		'if [ "$${BLINK_NETWORK_SANDBOX:-}" != "1" ]; then' \
		'  if ! command -v sandbox-exec >/dev/null 2>&1; then' \
		'    echo "error: sandbox-exec is required to deny outbound networking" >&2' \
		'    exit 2' \
		'  fi' \
		'  export BLINK_NETWORK_SANDBOX=1' \
		'  export ABSL_MIN_LOG_LEVEL=3' \
		'  export GLOG_minloglevel=3' \
		'  export TF_CPP_MIN_LOG_LEVEL=3' \
		'  exec sandbox-exec -p '\''(version 1)(allow default)(deny network*)'\'' "$$0" "$$@"' \
		'fi' \
		'exec "$(CURDIR)/$(VENV)/bin/python" "$(CURDIR)/blink_detector.py" "$$@"' > $(BINARY)
	chmod +x $(BINARY)

$(VENV)/bin/python:
	$(PYTHON_BIN) -m venv $(VENV)

$(VENV)/.requirements-installed: requirements.txt $(VENV)/bin/python
	$(VENV)/bin/python -m pip install --upgrade pip
	$(VENV)/bin/python -m pip install -r requirements.txt
	touch $(VENV)/.requirements-installed

$(MODEL):
	mkdir -p "$(@D)"
	curl --fail --location --show-error "$(MODEL_URL)" -o "$(MODEL).tmp"
	mv "$(MODEL).tmp" "$(MODEL)"

clean:
	rm -rf .build __pycache__ $(BINARY)
