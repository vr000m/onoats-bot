# onoats native (macOS) — build + sign the PRODUCTION capturer into Onoats.app.
#
# The bundle is signed with the stable self-signed "Code Signing" cert and the
# net.varunsingh.onoats identifier — the SAME designated requirement the
# Pre-req spikes obtained the mic + system-audio TCC grants against, so the
# grants carry over (TCC keys on the DR: identifier + cert leaf, not cdhash).
# Never ship/benchmark the unsigned binary: unsigned, every
# AudioHardwareCreateProcessTap call blocks ~4 s in security verification.
#
# Targets:
#   make build    compile build/onoats-capturer (unsigned; compile smoke only)
#   make app      assemble Onoats.app with the capturer at Contents/MacOS/
#   make sign     codesign Onoats.app + print the designated requirement
#   make dr       print ONLY the DR (byte-compare across rebuilds)
#   make cdhash   print the cdhash (expected to change every rebuild)
#   make rebuild  build + app + sign + dr in one go
#   make print-bin  print the signed capturer path (for ONOATS_CAPTURER_BIN)
#   make clean
#
# The throwaway spike kit lives in spike/ with its own Makefile.

ARCH      := $(shell uname -m)
DEPLOY    := 14.4
IDENTITY  ?= Code Signing
BUNDLE_ID := net.varunsingh.onoats

SRC_DIR := onoats-capturer
SOURCES := $(wildcard $(SRC_DIR)/Sources/*.swift)
PLIST   := $(SRC_DIR)/Info.plist

BUILD := build
APP   := Onoats.app
BIN   := $(APP)/Contents/MacOS/onoats-capturer

SWIFTC_FLAGS := -O -target $(ARCH)-apple-macos$(DEPLOY) \
	-framework AVFoundation -framework CoreAudio -framework AudioToolbox

.PHONY: build app sign dr cdhash rebuild print-bin clean check-identity

build: $(BUILD)/onoats-capturer

$(BUILD)/onoats-capturer: $(SOURCES)
	mkdir -p $(BUILD)
	swiftc $(SWIFTC_FLAGS) $(SOURCES) -o $@
	@echo "✓ compiled $@ ($(ARCH), macOS $(DEPLOY) floor)"

app: build
	mkdir -p $(APP)/Contents/MacOS
	cp $(BUILD)/onoats-capturer $(BIN)
	cp $(PLIST) $(APP)/Contents/Info.plist
	@echo "✓ assembled $(APP)"

# Deliberately NOT find-identity -v: a self-signed root fails the keychain
# *trust* evaluation so -v hides it, but codesign still signs with it and TCC
# keys on the cert identity (the DR), not on trust. See native/README.md.
check-identity:
	@security find-identity -p codesigning | grep -q "$(IDENTITY)" || { \
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

print-bin:
	@echo "$(CURDIR)/$(BIN)"

clean:
	rm -rf $(BUILD) $(APP)
