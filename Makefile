SWIFTC ?= xcrun swiftc
TARGET ?= arm64-apple-macosx15.0
MODULE_CACHE ?= .build/module-cache
BINARY ?= blink-detector

.PHONY: build clean

build: $(BINARY)

$(BINARY): BlinkDetector.swift
	mkdir -p $(MODULE_CACHE)
	$(SWIFTC) -target $(TARGET) -module-cache-path $(MODULE_CACHE) -O BlinkDetector.swift -o $(BINARY)

clean:
	rm -rf .build $(BINARY)
