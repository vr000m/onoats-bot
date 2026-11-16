# Task: Document Moonshine and update the STT client

**Status**: Complete
**Component**: STT and website
**Branch**: chore/moonshine-stt-060
**Created**: 2026-11-16

## Scope

Use `pipecat-local-stt-server>=0.6.0,<0.7` and regenerate the lockfile without
upgrading unrelated packages. Document the released Moonshine backend on the
local-models page: CPU inference, `medium-streaming@en`, MIT models and port 9965.
Include it in the toolbar model table: the owner confirms that the toolbar PR
is merging and authorises documenting it as available. The backend returns a completed
utterance after commit; its model name does not imply live partial transcripts.

Update the changelog and the dependency range in AGENTS.md. Existing landing
page and STT lifecycle plans record different work and need no correction.
No recorder code, runtime configuration, screenshots, or deployment changes.

## Validation

- Verify the dependency and lockfile agree on 0.6.x with no unrelated upgrades.
- Run site checks, Ruff format/check, review-marker checks and the full suite.
- Run the live STT integration check if a suitable server and speech fixture
  are available; report a skip explicitly rather than claim live compatibility.

## Outcome

- Added Moonshine to the toolbar table and linked the v0.6.0 backend guide.
- Updated the dependency, lockfile, changelog and AGENTS.md. Parsed old/new
  lockfiles confirm the only package version change is STT 0.5.0 to 0.6.0.
- `uv run pytest -q`: 8514 passed, 4 skipped, 46 warnings in 203.43 seconds.
- `just check`, `uv run ruff format` (110 unchanged), `uv run ruff check`,
  `scripts/check_review_markers.py` and `git diff --check` passed.
- Browser checks at 390 and 1200 pixels show the Moonshine row, no horizontal
  overflow, and the existing six-child STT/TTS grid structure.
- Automated live STT integration skipped: neither `ONOATS_STT_FIXTURE` nor
  `ONOATS_DIARIZATION_FIXTURE` is configured.
- Manual live verification (2026-11-16): the owner changed their configuration
  to point to the Moonshine server and reported that it worked on a call.
  This is owner-reported evidence; the automated audio test remains skipped.
