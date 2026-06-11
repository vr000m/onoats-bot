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
#   make cert     create the self-signed "Code Signing" identity (once; scripted)
#   make build    compile build/onoats-capturer (unsigned; compile smoke only)
#   make app      assemble Onoats.app with the capturer at Contents/MacOS/
#   make sign     codesign Onoats.app + print the designated requirement
#   make dr       print ONLY the DR (byte-compare across rebuilds)
#   make cdhash   print the cdhash (expected to change every rebuild)
#   make rebuild  build + app + sign + dr in one go
#   make print-bin  print the signed capturer path (for ONOATS_CAPTURER_BIN)
#   make install-cli  uv tool install --editable the repo → ~/.local/bin/onoats
#                     (idempotent; re-run after dependency changes = the update)
#   make install  sign + install-cli + copy Onoats.app → ~/Applications
#                 (update = `git pull && make install`. TCC grants survive:
#                  they key on the DR — bundle id + cert — not path or cdhash.)
#   make setup    the one-command fresh-clone story: cert → install →
#                 `onoats init` (init only when no config.toml exists yet;
#                 cert is a no-op when the identity exists — make_cert.sh
#                 refuses to regenerate, which would invalidate TCC grants)
#   make clean

ARCH      := $(shell uname -m)
DEPLOY    := 14.4
IDENTITY  ?= Code Signing
BUNDLE_ID := net.varunsingh.onoats

SRC_DIR := onoats-capturer
SOURCES := $(wildcard $(SRC_DIR)/Sources/*.swift)

MENUBAR_SRC_DIR := onoats-menubar
MENUBAR_SOURCES := $(wildcard $(MENUBAR_SRC_DIR)/Sources/*.swift)
PLIST           := $(MENUBAR_SRC_DIR)/Info.plist

BUILD := build
APP   := Onoats.app
# Phase 5b bundle shape: the menu-bar app is the main executable
# (CFBundleExecutable=Onoats); the capturer is embedded alongside it. One
# bundle id => one TCC identity; the DR is unchanged by the restructure
# (it keys on identifier + cert, not the executable).
MAIN_BIN := $(APP)/Contents/MacOS/Onoats
BIN      := $(APP)/Contents/MacOS/onoats-capturer

SWIFTC_FLAGS := -O -target $(ARCH)-apple-macos$(DEPLOY) \
	-framework AVFoundation -framework CoreAudio -framework AudioToolbox

MENUBAR_SWIFTC_FLAGS := -O -target $(ARCH)-apple-macos$(DEPLOY) \
	-parse-as-library -framework SwiftUI -framework AppKit -framework CoreAudio

REPO        := $(abspath ..)
INSTALL_DIR := $(HOME)/Applications

.PHONY: build app sign dr cdhash rebuild print-bin clean check-identity \
	cert install-cli install setup

build: $(BUILD)/onoats-capturer $(BUILD)/Onoats

$(BUILD)/onoats-capturer: $(SOURCES)
	mkdir -p $(BUILD)
	swiftc $(SWIFTC_FLAGS) $(SOURCES) -o $@
	@echo "✓ compiled $@ ($(ARCH), macOS $(DEPLOY) floor)"

$(BUILD)/Onoats: $(MENUBAR_SOURCES)
	mkdir -p $(BUILD)
	swiftc $(MENUBAR_SWIFTC_FLAGS) $(MENUBAR_SOURCES) -o $@
	@echo "✓ compiled $@ (menu-bar app)"

app: build
	mkdir -p $(APP)/Contents/MacOS
	cp $(BUILD)/Onoats $(MAIN_BIN)
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

# Inner-first signing: the embedded capturer is signed as its own code object
# (unsigned, every AudioHardwareCreateProcessTap call blocks ~4 s in security
# verification — the signature on the capturer BINARY is what avoids that),
# then the bundle. Same identity + identifier for both => one DR.
# NO --deep, deliberately: the capturer is the only nested code today and is
# signed explicitly above. If a resource/helper is ever ADDED to the bundle,
# it must get its own explicit codesign line here — an unsigned addition
# silently invalidates the outer signature.
sign: app check-identity
	codesign --force --sign "$(IDENTITY)" --identifier $(BUNDLE_ID) --timestamp=none $(BIN)
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

cert:
	sh make_cert.sh

install-cli:
	uv tool install --editable '$(REPO)[macos]'
	@echo "✓ CLI shim: $$HOME/.local/bin/onoats (editable — Python edits apply"
	@echo "  on next run; re-run this target after dependency changes)"

# rm before ditto: ditto MERGES into an existing tree, which would leave stale
# files behind across bundle-layout changes. TCC grants key on the DR, not the
# path, so replacing the installed app preserves them.
install: sign install-cli
	mkdir -p $(INSTALL_DIR)
	rm -rf $(INSTALL_DIR)/$(APP)
	ditto $(APP) $(INSTALL_DIR)/$(APP)
	@echo "✓ installed $(INSTALL_DIR)/$(APP)"

# Fresh-clone bootstrap. cert is idempotent (make_cert.sh exits 0 — and never
# regenerates — when the identity exists). init runs only on a missing
# config.toml: re-running setup must never clobber an existing config. The
# CLI is invoked via its stable shim path — a fresh shell may not have
# ~/.local/bin on PATH yet.
setup: cert install
	@CONFIG="$${XDG_CONFIG_HOME:-$$HOME/.config}/onoats/config.toml"; \
	if [ -f "$$CONFIG" ]; then \
	  echo "✓ $$CONFIG exists — skipping 'onoats init' (delete it to re-run)"; \
	else \
	  "$$HOME/.local/bin/onoats" init; \
	fi
	@echo "✓ setup complete — launch Onoats.app from $(INSTALL_DIR)"

clean:
	rm -rf $(BUILD) $(APP)
