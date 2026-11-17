# Task: Point the site to the standalone toolbar

**Status**: Complete
**Component**: Website
**Branch**: docs/site-standalone-toolbar
**Created**: 2026-11-17

## Scope

Update `public/local-models/index.html` to use the standalone
`pipecat-ai/pipecat-service-toolbar` repository, its current build prerequisites
and quickstart. Link the migration guide for existing installations. Add the
toolbar repository to `public/llms.txt` and update `README.md` with its current
screenshot source while retaining the provenance of the existing 0.9.0 images.

Verified against the upstream README, justfile and migration guide on 2026-11-17.
The STT/TTS repositories, model ports and existing image bytes remain valid.
The parent Moonshine and landing-page remediation plans need no correction:
their model-support and asset-versioning decisions still hold.

## Validation

- Run `just check` and the existing site contract tests.
- Check the diff for stale toolbar clone paths and whitespace errors.
- Run Ruff format/check, review-marker checks and the full test suite before
  committing and opening the PR. No deployment or package changes are required.

## Outcome

- Updated the toolbar link, clone commands, Xcode 26 SDK prerequisites and
  migration guidance; added the repository to `llms.txt` and clarified image provenance.
- `just check` and `git diff --check` passed. No old toolbar clone path,
  `macos/` repository link or Xcode 16 prerequisite remains in the site.
- Site contract suite: 355 passed, one dependency deprecation warning. The first
  sandboxed run blocked three process/Swift checks; the native rerun passed all.
- Documentation audit: updated the site README and parent changelog; parent
  README, AGENTS.md and historical plans need no changes.
- PR validation: full suite passed (8,514 passed, 4 skipped, 46 warnings);
  Ruff format left all 110 files unchanged, Ruff check and review-marker
  checks passed. The staged diff contains only the five intended files and
  was reviewed for credentials and personal data. No deployment performed.
