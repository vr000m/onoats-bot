# onoats Phase-4 Pre-req spike — build / sign / inspect.
# Throwaway: proves the TCC + Core Audio premises before any production Swift.
#
# Targets:
#   make build   compile-only smoke (catches API errors before you touch perms)
#   make app     assemble Onoats.app with the helper at Contents/MacOS/onoats-capturer
#   make sign    codesign the .app with the stable self-signed cert + print the DR
#   make dr      print ONLY the designated requirement (byte-compare across rebuilds)
#   make cdhash  print the cdhash (EXPECTED to change every rebuild — not a failure)
#   make rebuild  build + app + sign + dr in one go (the rebuild ×3 loop)
#   make clean

ARCH     := $(shell uname -m)
DEPLOY   := 14.4
IDENTITY ?= Code Signing
BUNDLE_ID := net.varunsingh.onoats

APP    := Onoats.app
BIN    := $(APP)/Contents/MacOS/onoats-capturer
PLIST  := Info.plist

SWIFTC_FLAGS := -target $(ARCH)-apple-macos$(DEPLOY) \
	-framework AVFoundation -framework CoreAudio -framework AudioToolbox

.PHONY: build app sign dr cdhash rebuild clean check-identity

build:
	swiftc $(SWIFTC_FLAGS) main.swift -o onoats-capturer
	@echo "✓ compiled onoats-capturer ($(ARCH), macOS $(DEPLOY) floor)"

app: build
	mkdir -p $(APP)/Contents/MacOS
	cp onoats-capturer $(BIN)
	cp $(PLIST) $(APP)/Contents/Info.plist
	@echo "✓ assembled $(APP)"

check-identity:
	@security find-identity -v -p codesigning | grep -q "$(IDENTITY)" || { \
	  echo "✗ No code-signing identity named '$(IDENTITY)' in the keychain."; \
	  echo "  Create the self-signed 'Code Signing' cert first — see native/README.md."; \
	  exit 1; }

sign: app check-identity
	codesign --force --sign "$(IDENTITY)" --identifier $(BUNDLE_ID) --timestamp=none $(APP)
	@echo "--- designated requirement (must be byte-stable across rebuilds) ---"
	@codesign -dr - $(APP)

dr:
	@codesign -dr - $(APP)

cdhash:
	@codesign -dvvv $(APP) 2>&1 | grep -i cdhash || true

rebuild: sign dr

clean:
	rm -rf $(APP) onoats-capturer
