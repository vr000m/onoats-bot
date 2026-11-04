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
#                     (idempotent; re-run after dependency changes = the update;
#                      EXTRAS=macos,diarization keeps the diarization extra)
#   make install  sign + install-cli + copy Onoats.app → ~/Applications
#                 (update = `git pull && make install`. TCC grants survive:
#                  they key on the DR — bundle id + cert — not path or cdhash.)
#   make setup    the one-command fresh-clone story: cert → install →
#                 `onoats init` (init only when no config.toml exists yet;
#                 cert is a no-op when the identity exists — make_cert.sh
#                 refuses to regenerate, which would invalidate TCC grants)
#   make setup-cli  CLI-only bootstrap (no app bundle): cert → build + sign
#                   the bare capturer → install-cli → `onoats init` (same
#                   cert/init idempotency as setup). Run sessions with
#                   AUDIO_SOURCE=socket + ONOATS_CAPTURER_BIN (printed at end).
#   make clean

# The staged installer accepts operator data only through ONOATS_INSTALL_*
# environment variables consumed directly by Python.  Reject command-line and
# override origins by NAME only, before any recipe can expand an operator value.
ifneq ($(filter app-tree-hash install-staged rollback,$(MAKECMDGOALS)),)
$(foreach input,ONOATS_INSTALL_APP ONOATS_INSTALL_DIR ONOATS_INSTALL_BACKUP_DIR ONOATS_INSTALL_ROLLBACK_RECORD ONOATS_INSTALL_EXPECTED_IDENTITY ONOATS_INSTALL_ROLLBACK_APPROVED ONOATS_INSTALL_BASELINE_DR_HASH ONOATS_INSTALL_BASELINE_CDHASH ONOATS_INSTALL_BUNDLE_ID,$(if $(or $(findstring command line,$(origin $(input))),$(findstring override,$(origin $(input)))),$(error staged-installer input $(input) must be supplied as an environment-prefix assignment),))
$(foreach legacy,APP APP_NAME INSTALL_DIR BACKUP_DIR ROLLBACK_RECORD STAGED_APP_TREE_HASH ROLLBACK_BASELINE_APPROVED ROLLBACK_BASELINE_DR_HASH ROLLBACK_BASELINE_CDHASH BUNDLE_ID,$(if $(or $(filter environment,$(origin $(legacy))),$(findstring command line,$(origin $(legacy))),$(findstring override,$(origin $(legacy)))),$(error legacy staged-installer variable $(legacy) is unsupported; use ONOATS_INSTALL_* environment variables),))
endif

ARCH      := $(shell uname -m)
DEPLOY    := 14.4
IDENTITY  ?= Code Signing
BUNDLE_ID := net.varunsingh.onoats

SRC_DIR := onoats-capturer
SOURCES := $(wildcard $(SRC_DIR)/Sources/*.swift)

MENUBAR_SRC_DIR := onoats-menubar
MENUBAR_SOURCES := $(wildcard $(MENUBAR_SRC_DIR)/Sources/*.swift)
PLIST           := $(MENUBAR_SRC_DIR)/Info.plist
RESOURCE_DIR    := $(MENUBAR_SRC_DIR)/Resources
RESOURCE_NAMES  := Onoats.icns \
	OnoatsHeader.png OnoatsHeader@2x.png \
	OnoatsMenuBarTemplateIdle.png OnoatsMenuBarTemplateIdle@2x.png \
	OnoatsMenuBarTemplateRecording.png OnoatsMenuBarTemplateRecording@2x.png \
	OnoatsMenuBarTemplateWarning.png OnoatsMenuBarTemplateWarning@2x.png \
	OnoatsMenuBarTemplateBusy.png OnoatsMenuBarTemplateBusy@2x.png \
	OnoatsMenuBarTemplateFailed.png OnoatsMenuBarTemplateFailed@2x.png

BUILD := build
APP   := Onoats.app
APP_NAME := $(notdir $(APP))
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
EXTRAS      ?= macos
INSTALL_DIR := $(HOME)/Applications
BACKUP_DIR := $(HOME)/Library/Application Support/Onoats/app-backups
ROLLBACK_RECORD := $(BACKUP_DIR)/approved-rollback.txt
INSTALL_STAGED_SCRIPT := $(CURDIR)/install_staged.py

.PHONY: build app verify-resources sign dr cdhash rebuild print-bin clean \
	check-identity cert install-cli install app-tree-hash install-staged rollback setup \
	sign-cli setup-cli

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
	rm -rf $(APP)
	mkdir -p $(APP)/Contents/MacOS $(APP)/Contents/Resources
	cp $(BUILD)/Onoats $(MAIN_BIN)
	cp $(BUILD)/onoats-capturer $(BIN)
	cp $(PLIST) $(APP)/Contents/Info.plist
	cp $(RESOURCE_DIR)/Onoats.icns $(APP)/Contents/Resources/Onoats.icns
	cp $(RESOURCE_DIR)/OnoatsHeader.png $(APP)/Contents/Resources/OnoatsHeader.png
	cp $(RESOURCE_DIR)/OnoatsHeader@2x.png $(APP)/Contents/Resources/OnoatsHeader@2x.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateIdle.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateIdle.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateIdle@2x.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateIdle@2x.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateRecording.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateRecording.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateRecording@2x.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateRecording@2x.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateWarning.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateWarning.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateWarning@2x.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateWarning@2x.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateBusy.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateBusy.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateBusy@2x.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateBusy@2x.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateFailed.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateFailed.png
	cp $(RESOURCE_DIR)/OnoatsMenuBarTemplateFailed@2x.png $(APP)/Contents/Resources/OnoatsMenuBarTemplateFailed@2x.png
	@echo "✓ assembled $(APP)"

# The resource directory is an equality-checked allowlist. This catches both a
# stale file surviving assembly and a required export disappearing before the
# bundle is signed.
verify-resources:
	@set -eu; \
	resources="$(APP)/Contents/Resources"; \
	[ -d "$$resources" ] && [ ! -L "$$resources" ] || { echo "✗ invalid $$resources"; exit 1; }; \
	expected=$$(mktemp); actual=$$(mktemp); \
	trap 'rm -f "$$expected" "$$actual"' EXIT; \
	printf '%s\n' $(RESOURCE_NAMES) | LC_ALL=C sort > "$$expected"; \
	find "$$resources" -mindepth 1 -maxdepth 1 -exec basename {} \; | LC_ALL=C sort > "$$actual"; \
	diff -u "$$expected" "$$actual" || { echo "✗ bundle resource allowlist mismatch"; exit 1; }; \
	for name in $(RESOURCE_NAMES); do \
	  [ -f "$$resources/$$name" ] && [ ! -L "$$resources/$$name" ] || { \
	    echo "✗ resource must be a regular non-symlink file: $$name"; exit 1; \
	  }; \
	done; \
	echo "✓ verified bundle resource allowlist"

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
	$(MAKE) verify-resources APP="$(APP)"
	codesign --force --sign "$(IDENTITY)" --identifier $(BUNDLE_ID) --timestamp=none $(BIN)
	codesign --force --sign "$(IDENTITY)" --identifier $(BUNDLE_ID) --timestamp=none $(APP)
	codesign --verify --deep --strict $(APP)
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

# EXTRAS is the comma-separated extras list for the tool env. `uv tool install`
# REPLACES the env's extras, so an install that omits one you already have
# (e.g. diarization) uninstalls it: pass the full set, e.g.
#   make -C native install EXTRAS=macos,diarization
install-cli:
	uv tool install --editable '$(REPO)[$(EXTRAS)]'
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

# Canonical producer for the identity token recorded before staged smoke. It
# covers bundle paths, entry types, modes, file bytes, and symlink targets.
app-tree-hash:
	@python3 install_staged.py env-identity

# Install exactly one immutable private snapshot of the already-signed staged
# bytes. The verified snapshot is atomically exchanged with the installed app;
# the staged path is never read again after snapshot verification.
install-staged:
	@python3 install_staged.py env-install

# Restore only a private, fully verified extraction of the recorded archive.
# Atomic exchange keeps the current app installed on every validation failure.
rollback:
	@python3 install_staged.py env-rollback

# Fresh-clone bootstrap. cert is idempotent (make_cert.sh exits 0 — and never
# regenerates — when the identity exists). init runs only on a missing
# config.toml: re-running setup must never clobber an existing config. The
# CLI is invoked via its stable shim path — a fresh shell may not have
# ~/.local/bin on PATH yet.
# Recursive $(MAKE) (not prerequisites): cert MUST complete before install's
# check-identity, and prerequisite order is not guaranteed under `make -j`.
setup:
	$(MAKE) cert
	$(MAKE) install
	@CONFIG="$${XDG_CONFIG_HOME:-$$HOME/.config}/onoats/config.toml"; \
	if [ -f "$$CONFIG" ]; then \
	  echo "✓ $$CONFIG exists — skipping 'onoats init' (delete it to re-run)"; \
	else \
	  "$$HOME/.local/bin/onoats" init; \
	fi
	@echo "✓ setup complete — launch Onoats.app from $(INSTALL_DIR)"

# CLI-only signing: signs the bare capturer at build/onoats-capturer (no app
# bundle). Same identity + identifier as the bundle signing above, so the
# designated requirement — and any existing TCC grants — match. The signature
# on the capturer binary is what avoids the ~4 s-per-tap-call verification
# stall. Note: a later `make build`/`make setup` recompiles this binary and
# drops the signature — re-run setup-cli (or sign-cli) after updates.
sign-cli: $(BUILD)/onoats-capturer check-identity
	codesign --force --sign "$(IDENTITY)" --identifier $(BUNDLE_ID) --timestamp=none $(BUILD)/onoats-capturer
	@echo "✓ signed $(BUILD)/onoats-capturer"

# CLI + native capture bootstrap (macOS 14.4+, no menu-bar app). Mirrors
# `setup`: cert is idempotent (make_cert.sh refuses to regenerate — a new cert
# would invalidate TCC grants), init runs only when config.toml is missing,
# and the CLI is invoked via its stable shim path. Recursive $(MAKE) for the
# same reason as setup: cert MUST complete before check-identity under -j.
# The CLI discovers the capturer via ONOATS_CAPTURER_BIN (env-only).
setup-cli:
	$(MAKE) cert
	$(MAKE) sign-cli
	$(MAKE) install-cli
	@CONFIG="$${XDG_CONFIG_HOME:-$$HOME/.config}/onoats/config.toml"; \
	if [ -f "$$CONFIG" ]; then \
	  echo "✓ $$CONFIG exists — skipping 'onoats init' (delete it to re-run)"; \
	else \
	  "$$HOME/.local/bin/onoats" init; \
	fi
	@echo "✓ setup-cli complete — run sessions with:"
	@echo "  AUDIO_SOURCE=socket ONOATS_CAPTURER_BIN=$(CURDIR)/$(BUILD)/onoats-capturer onoats bot"

clean:
	rm -rf $(BUILD) $(APP)
